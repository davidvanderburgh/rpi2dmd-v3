"""RPI2DMD v3 web UI.

Flask application serving the configuration pages and the JSON API
described in docs/contracts.md ("Web UI" section). Imports the player
package for config/clock/rda so previews render through the exact same
code path as the panel.

Fully usable when the player daemon is offline: the dashboard shows
"offline" and config editing still works (Config.save + best-effort
reload_config over the control socket).

Python 3.7 / Flask 1.0.2 / Pillow 5.4 compatible. No external web
resources (all CSS/JS served from static/).
"""

import argparse
import copy
import hmac
import io
import json
import os
import platform
import random
import re
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/opt/rpi2dmd-v3/player",
              os.path.normpath(os.path.join(_HERE, "..", "player"))):
    if os.path.isdir(os.path.join(_cand, "rpi2dmd")):
        if _cand not in sys.path:
            sys.path.insert(0, _cand)
        break

from flask import (Flask, Response, abort, jsonify, render_template,
                   request, send_file)

from rpi2dmd import clock, config, paths, rda

import ctl

VERSION = "3.0.0"

app = Flask(__name__)

CFG = config.Config()

NAV = [
    ("dashboard", "/", "Dashboard"),
    ("clock", "/clock", "Clock"),
    ("library", "/library", "Library"),
    ("playback", "/playback", "Playback"),
    ("message", "/message", "Message"),
    ("schedule", "/schedule", "Schedule"),
    ("network", "/network", "Network"),
    ("system", "/system", "System"),
]

FREQ_LABELS = [
    ("random_1_20", "Random 1-20 s (Run-DMD default)"),
    ("random_5_60", "Random 5-60 s"),
    ("1s", "Every 1 second"),
    ("2s", "Every 2 seconds"),
    ("3s", "Every 3 seconds"),
    ("5s", "Every 5 seconds"),
    ("10s", "Every 10 seconds"),
    ("15s", "Every 15 seconds"),
    ("30s", "Every 30 seconds"),
    ("1m", "Every minute"),
    ("5m", "Every 5 minutes"),
    ("10m", "Every 10 minutes"),
    ("off", "Off (clock only)"),
]

MESSAGE_FREQS = [
    ("off", "Off (manual send only)"),
    ("1s", "Every 1 second"),
    ("5s", "Every 5 seconds"),
    ("15s", "Every 15 seconds"),
    ("30s", "Every 30 seconds"),
    ("1m", "Every minute"),
    ("5m", "Every 5 minutes"),
]

TRANSITIONS = [
    ("random", "Random (Run-DMD default)"),
    ("up_up", "Scroll up / up"),
    ("down_down", "Scroll down / down"),
    ("up_down", "Scroll up / down"),
    ("down_up", "Scroll down / up"),
    ("fade", "Fade"),
    ("none", "None (cut)"),
]

_COMMON_TZ = [
    "UTC",
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage",
    "Pacific/Honolulu", "America/Toronto", "America/Vancouver",
    "America/Mexico_City", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Paris",
    "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam",
    "Europe/Brussels", "Europe/Zurich", "Europe/Vienna", "Europe/Stockholm",
    "Europe/Oslo", "Europe/Copenhagen", "Europe/Helsinki", "Europe/Warsaw",
    "Europe/Prague", "Europe/Budapest", "Europe/Athens", "Europe/Istanbul",
    "Europe/Moscow", "Africa/Cairo", "Africa/Johannesburg", "Asia/Dubai",
    "Asia/Kolkata", "Asia/Bangkok", "Asia/Singapore", "Asia/Hong_Kong",
    "Asia/Shanghai", "Asia/Taipei", "Asia/Tokyo", "Asia/Seoul",
    "Australia/Perth", "Australia/Adelaide", "Australia/Brisbane",
    "Australia/Sydney", "Australia/Melbourne", "Pacific/Auckland",
]

_IMAGE_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".bmp")

_MISSING = object()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _refresh_config():
    """Pick up SMB/manual edits of rpi2dmd.json between requests."""
    try:
        if CFG.changed_on_disk():
            CFG.load()
    except (OSError, ValueError):
        pass


def _safe_name(name):
    """True when name is a plain single path component (no traversal)."""
    if not name or name in (".", ".."):
        return False
    return "/" not in name and "\\" not in name and ".." not in name


def _local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        sock.close()


def _cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return "%.1f\xb0C" % (int(f.read().strip()) / 1000.0)
    except (OSError, ValueError):
        return "n/a"


def _fmt_uptime(secs):
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def _sys_uptime():
    try:
        with open("/proc/uptime") as f:
            return _fmt_uptime(float(f.read().split()[0]))
    except (OSError, ValueError):
        return "n/a"


_index_cache = {"mtime": None, "data": None}


def _load_index():
    """RDA library index.json, cached by mtime."""
    path = os.path.join(paths.dmd_dir(), "index.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _index_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _index_cache["data"] = json.load(f)
            _index_cache["mtime"] = mtime
        except (OSError, ValueError):
            return {}
    return _index_cache["data"] or {}


# Walking 10k+ GIFs off a slow SD card took ~9 s per request on a Pi Zero
# and was redone on every page load. Cache it; the library changes rarely
# (SMB drops), so a TTL plus a directory-mtime check is plenty.
_gif_scan_cache = {"at": 0.0, "sig": None, "cats": None}
_GIF_SCAN_TTL_S = 120


def _gif_dir_signature(root):
    """Cheap staleness check: (mtime, size) of the gif root + each category."""
    sig = []
    try:
        sig.append(os.path.getmtime(root))
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if os.path.isdir(d):
                sig.append((name, os.path.getmtime(d)))
    except OSError:
        return None
    return tuple(sig)


def _gif_scan(force=False):
    """-> sorted list of (category, [gif filenames]). Cached."""
    root = paths.gif_dir()
    now = time.time()
    cached = _gif_scan_cache["cats"]
    if not force and cached is not None:
        if now - _gif_scan_cache["at"] < _GIF_SCAN_TTL_S:
            return cached
        if _gif_dir_signature(root) == _gif_scan_cache["sig"]:
            _gif_scan_cache["at"] = now   # unchanged: extend the TTL
            return cached

    sig = _gif_dir_signature(root)
    cats = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(f for f in os.listdir(d)
                           if f.lower().endswith(".gif"))
        except OSError:
            files = []
        cats.append((name, files))
    _gif_scan_cache.update({"at": now, "sig": sig, "cats": cats})
    return cats


def _library_counts():
    idx = _load_index()
    game_flags = CFG.get("dmd.games", {}) or {}
    disabled = set(CFG.get("dmd.disabled_animations", []) or [])
    total = 0
    enabled = 0
    for game, anims in (idx.get("games") or {}).items():
        g_en = bool(game_flags.get(game, True))
        for a in anims:
            total += 1
            if g_en and a.get("name") not in disabled:
                enabled += 1
    cat_flags = CFG.get("gif.categories", {}) or {}
    gif_total = 0
    gif_enabled = 0
    for name, files in _gif_scan():
        gif_total += len(files)
        if bool(cat_flags.get(name, True)):
            gif_enabled += len(files)
    return {"dmd_animations": total, "dmd_enabled": enabled,
            "gif_files": gif_total, "gif_enabled": gif_enabled}


def _list_backgrounds():
    """Background image choices: Background* files in fonts/ plus
    everything in fonts/Background/."""
    found = []
    fd = paths.fonts_dir()
    try:
        for f in sorted(os.listdir(fd)):
            p = os.path.join(fd, f)
            if (f.startswith("Background") and os.path.isfile(p)
                    and f.lower().endswith(_IMAGE_EXTS)):
                found.append(f)
    except OSError:
        pass
    sub = os.path.join(fd, "Background")
    try:
        for f in sorted(os.listdir(sub)):
            if (os.path.isfile(os.path.join(sub, f))
                    and f.lower().endswith(_IMAGE_EXTS)):
                found.append("Background/" + f)
    except OSError:
        pass
    return found


def _load_background(name):
    """Resolve a clock background setting to a PIL RGB image (first frame)
    or None."""
    if not name or name == "none":
        return None
    from PIL import Image
    if name == "random":
        options = _list_backgrounds()
        if not options:
            return None
        name = random.choice(options)
    if ".." in name or name.startswith("/") or name.startswith("\\"):
        return None
    fd = paths.fonts_dir()
    candidates = [os.path.join(fd, name.replace("/", os.sep)),
                  os.path.join(fd, "Background", name)]
    for cand in candidates:
        if os.path.isfile(cand):
            try:
                return Image.open(cand).convert("RGB")
            except (OSError, ValueError):
                return None
    return None


def _list_fonts():
    fonts = []
    fd = paths.fonts_dir()
    try:
        for f in sorted(os.listdir(fd)):
            if (f.lower().endswith(".ttf")
                    and os.path.isfile(os.path.join(fd, f))):
                fonts.append(f)
    except OSError:
        pass
    sub = os.path.join(fd, "Polices")
    try:
        for f in sorted(os.listdir(sub)):
            if f.lower().endswith(".ttf"):
                fonts.append("Polices/" + f)
    except OSError:
        pass
    return fonts


def _timezones():
    """All zoneinfo names on Linux, else a curated list. Always includes
    the currently configured zone."""
    zones = []
    zdir = "/usr/share/zoneinfo"
    if os.path.isdir(zdir):
        skip = ("posix", "right")
        junk = ("posixrules", "localtime", "leapseconds", "tzdata.zi",
                "zone.tab", "zone1970.tab", "iso3166.tab", "leap-seconds.list",
                "SECURITY", "Factory")
        for root, dirs, files in os.walk(zdir):
            dirs[:] = sorted(d for d in dirs if d not in skip)
            for f in sorted(files):
                rel = os.path.relpath(os.path.join(root, f), zdir)
                rel = rel.replace(os.sep, "/")
                if rel in junk or rel.split("/")[0] in junk:
                    continue
                if not rel[0].isupper():
                    continue
                zones.append(rel)
        zones.sort()
    if not zones:
        zones = list(_COMMON_TZ)
    current = CFG.get("network.timezone", "")
    if current and current not in zones:
        zones.insert(0, current)
    return zones


def _rgb_to_hex(color):
    try:
        r, g, b = [max(0, min(255, int(c))) for c in list(color)[:3]]
        return "#%02x%02x%02x" % (r, g, b)
    except (TypeError, ValueError):
        return "#ff8c00"


# ---------------------------------------------------------------------------
# Config validation (POST /api/config)
# ---------------------------------------------------------------------------

def _to_bool(value, path):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    raise ValueError("%s: expected a boolean" % path)


def _to_int(value, path):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError("%s: expected a number" % path)


def _to_float(value, path):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("%s: expected a number" % path)


def _to_rgb(value, path):
    if isinstance(value, str):
        m = re.match(r"^#?([0-9a-fA-F]{6})$", value.strip())
        if not m:
            raise ValueError("%s: expected #rrggbb or [r,g,b]" % path)
        h = m.group(1)
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return [max(0, min(255, _to_int(v, path))) for v in list(value)[:3]]
    raise ValueError("%s: expected #rrggbb or [r,g,b]" % path)


_COLOR_PATHS = ("clock.color", "date.color", "weather.color")


def _coerce_like(value, default, path):
    """Coerce value to the shape of the DEFAULTS entry at the same path."""
    if isinstance(default, bool):
        return _to_bool(value, path)
    if isinstance(default, int):
        return _to_int(value, path)
    if isinstance(default, float):
        return _to_float(value, path)
    if path == "display.tint":
        if isinstance(value, (list, tuple)):
            return _to_rgb(value, path)
        if isinstance(value, str) and value.startswith("#"):
            return _to_rgb(value, path)
        return str(value)
    if isinstance(default, list):
        if path == "display.brightness_by_hour":
            if not isinstance(value, (list, tuple)) or len(value) != 24:
                raise ValueError(
                    "display.brightness_by_hour: expected 24 values")
            return [max(0, min(100, _to_int(v, path))) for v in value]
        if path in _COLOR_PATHS:
            return _to_rgb(value, path)
        if not isinstance(value, list):
            raise ValueError("%s: expected a list" % path)
        return value
    if isinstance(default, str):
        if isinstance(value, (str, int, float)):
            return str(value)
        raise ValueError("%s: expected a string" % path)
    return value


def _validate(node, defaults, prefix=""):
    """Walk a merged config dict against DEFAULTS, coercing leaf types.
    Keys unknown to DEFAULTS below the top level are preserved as-is."""
    out = {}
    for key, value in node.items():
        path = prefix + "." + key if prefix else key
        default = defaults.get(key, _MISSING)
        if default is _MISSING:
            out[key] = copy.deepcopy(value)
        elif isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError("%s: expected an object" % path)
            out[key] = _validate(value, default, path)
        else:
            out[key] = _coerce_like(value, default, path)
    return out


_CLAMPS = [
    (("clock", "shade"), 1, 15),
    (("clock", "font_size"), 4, 64),
    (("panel", "pwm_bits"), 1, 11),
    (("panel", "cols"), 8, 256),
    (("panel", "rows"), 8, 128),
    (("panel", "chain"), 1, 12),
    (("panel", "parallel"), 1, 3),
    (("web", "port"), 1, 65535),
]


def _apply_clamps(doc):
    for (section, key), lo, hi in _CLAMPS:
        try:
            doc[section][key] = max(lo, min(hi, int(doc[section][key])))
        except (KeyError, TypeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Auth + shared template context
# ---------------------------------------------------------------------------

# The dashboard polls /api/status on a timer; that is the page breathing,
# not a human doing something. Counting it would pause the panel forever
# just because a tab is open somewhere.
_NOT_ACTIVITY = ("/api/status",)


def _mark_ui_active():
    """Tell the player a human is using the UI, so it backs off and gives
    the core to this request (single-core Pi). Just an mtime bump."""
    try:
        p = paths.ui_active_path()
        with open(p, "a"):
            os.utime(p, None)
    except OSError:
        pass


@app.before_request
def _gate():
    if request.path.startswith("/static/"):
        return None
    if request.path not in _NOT_ACTIVITY:
        _mark_ui_active()
    _refresh_config()
    if not CFG.get("web.auth_enabled", False):
        return None
    auth = request.authorization
    user = CFG.get("web.username", "admin") or ""
    pw = CFG.get("web.password", "") or ""
    if auth is not None and auth.username is not None:
        got_user = (auth.username or "").encode("utf-8")
        got_pw = (auth.password or "").encode("utf-8")
        ok_user = hmac.compare_digest(got_user, user.encode("utf-8"))
        ok_pw = hmac.compare_digest(got_pw, pw.encode("utf-8"))
        if ok_user and ok_pw:
            return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="RPI2DMD"'})


def _asset_version(filename):
    """mtime-based cache buster: browsers cache static files for hours, so
    without this a deployed app.js fix can silently not reach the user."""
    try:
        return int(os.path.getmtime(
            os.path.join(_HERE, "static", filename)))
    except OSError:
        return 0


@app.context_processor
def _template_globals():
    return {"nav": NAV, "version": VERSION, "cfg": CFG.data,
            "asset_v": _asset_version}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def page_dashboard():
    counts = _library_counts()
    return render_template(
        "dashboard.html", active="dashboard",
        hostname=socket.gethostname(), ip=_local_ip(),
        sys_uptime=_sys_uptime(), cpu_temp=_cpu_temp(), counts=counts)


@app.route("/clock")
def page_clock():
    return render_template(
        "clock.html", active="clock",
        tints=sorted(rda.TINTS.keys()),
        backgrounds=_list_backgrounds(),
        transitions=TRANSITIONS,
        clock_color_hex=_rgb_to_hex(CFG.get("clock.color", [255, 140, 0])))


@app.route("/library")
def page_library():
    return render_template("library.html", active="library")


@app.route("/playback")
def page_playback():
    return render_template("playback.html", active="playback",
                           freq_labels=FREQ_LABELS)


@app.route("/message")
def page_message():
    return render_template("message.html", active="message",
                           message_freqs=MESSAGE_FREQS)


@app.route("/schedule")
def page_schedule():
    hours = CFG.get("display.brightness_by_hour", [50] * 24)
    if not isinstance(hours, list) or len(hours) != 24:
        hours = [50] * 24
    return render_template("schedule.html", active="schedule", hours=hours)


@app.route("/network")
def page_network():
    return render_template("network.html", active="network",
                           timezones=_timezones())


@app.route("/system")
def page_system():
    return render_template("system.html", active="system",
                           is_linux=platform.system() == "Linux")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    resp = ctl.send("status")
    if resp.get("ok"):
        return jsonify(resp)
    try:
        with open(paths.status_path(), "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            # the player heartbeats status.json every <=5s; anything much
            # older is a dead player, not live state
            try:
                age = time.time() - float(doc.get("updated_at", 0))
            except (TypeError, ValueError):
                age = None
            if age is None or age > 15:
                doc["state"] = "offline"
                doc["stale"] = True
            else:
                doc.setdefault("state", "offline")
            return jsonify(doc)
    except (OSError, ValueError):
        pass
    return jsonify({"state": "offline",
                    "error": resp.get("error", "player offline")})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(CFG.data)
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False,
                        "error": "body must be a JSON object"}), 400
    unknown = [k for k in body if k not in config.DEFAULTS]
    if unknown:
        return jsonify({"ok": False, "error": "unknown config section(s): %s"
                        % ", ".join(sorted(unknown))}), 400
    merged = config.deep_merge(CFG.data, body)
    try:
        merged = _validate(merged, config.DEFAULTS)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _apply_clamps(merged)
    CFG.data = merged
    CFG.save()
    player = ctl.send("reload_config")
    return jsonify({"ok": True, "player": player})


@app.route("/api/control/<cmd>", methods=["POST"])
def api_control(cmd):
    system_cmds = {
        "reboot": ["shutdown", "-r", "now"],
        "shutdown": ["shutdown", "-h", "now"],
        "restart_player": ["systemctl", "restart", "rpi2dmd-player"],
    }
    forward_cmds = ("pause", "resume", "skip", "sleep", "wake", "play",
                    "marquee", "test_pattern", "brightness",
                    "reload_config", "stop")
    if cmd in system_cmds:
        if platform.system() != "Linux":
            return jsonify({"ok": False,
                            "error": "%s unavailable on this platform" % cmd})
        try:
            subprocess.Popen(system_cmds[cmd])
            return jsonify({"ok": True})
        except OSError as exc:
            return jsonify({"ok": False, "error": str(exc)})
    if cmd not in forward_cmds:
        return jsonify({"ok": False, "error": "unknown command"}), 400
    args = request.get_json(force=True, silent=True)
    if not isinstance(args, dict):
        args = {}
    args.pop("cmd", None)
    return jsonify(ctl.send(cmd, **args))


@app.route("/api/preview/clock.png")
def api_preview_clock():
    cfg_clock = copy.deepcopy(CFG.get("clock", {}))
    tint = CFG.get("display.tint", rda.DEFAULT_TINT)
    try:
        gamma = float(CFG.get("display.gamma", rda.DEFAULT_GAMMA))
    except (TypeError, ValueError):
        gamma = rda.DEFAULT_GAMMA
    clock_defaults = config.DEFAULTS["clock"]
    for key in request.args:
        value = request.args.get(key)
        if key == "tint":
            if value.startswith("#"):
                try:
                    tint = _to_rgb(value, "tint")
                except ValueError:
                    pass
            elif value:
                tint = value
            continue
        if key not in clock_defaults:
            continue
        default = clock_defaults[key]
        try:
            cfg_clock[key] = _coerce_like(value, default, "clock." + key)
        except ValueError:
            pass
    try:
        cfg_clock["font_size"] = max(4, min(64, int(
            cfg_clock.get("font_size", 20))))
    except (TypeError, ValueError):
        cfg_clock["font_size"] = 20
    background = _load_background(cfg_clock.get("background", "none"))
    frame = clock.render_scene(cfg_clock, 128, 32, background=background,
                               tint=tint, gamma=gamma,
                               fonts_dir=paths.fonts_dir())
    frame = frame.resize((512, 128), resample=0)  # NEAREST
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    resp = Response(buf.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/preview/anim/<game>/<name>.gif")
def api_preview_anim(game, name):
    if not _safe_name(game) or not _safe_name(name):
        abort(404)
    rda_path = os.path.join(paths.dmd_dir(), game, name + ".rda")
    if not os.path.isfile(rda_path):
        abort(404)
    tint = CFG.get("display.tint", rda.DEFAULT_TINT)
    if isinstance(tint, (list, tuple)):
        tint_key = "-".join(str(int(c)) for c in tint[:3])
        tint_val = tuple(tint[:3])
    else:
        tint_key = str(tint)
        tint_val = tint
    try:
        gamma = float(CFG.get("display.gamma", rda.DEFAULT_GAMMA))
    except (TypeError, ValueError):
        gamma = rda.DEFAULT_GAMMA
    cache_dir = os.path.join(paths.run_dir(), "preview-cache")
    if not os.path.isdir(cache_dir):
        try:
            os.makedirs(cache_dir)
        except OSError:
            pass
    cache_name = "%s__%s__%s__%d.gif" % (
        game, name, tint_key, int(os.path.getmtime(rda_path)))
    cache_path = os.path.join(cache_dir, cache_name)
    if not os.path.isfile(cache_path):
        _prune_preview_cache(cache_dir)
        # Pillow picks the output format from the extension, so the temp
        # file must end in .gif; unique per request so concurrent renders
        # of the same animation cannot corrupt each other.
        tmp = "%s.%d.%d.tmp.gif" % (cache_path, os.getpid(),
                                    threading.get_ident())
        try:
            rda.rda_to_gif(rda_path, tmp, tint=tint_val, gamma=gamma,
                           scale=2)
            os.replace(tmp, cache_path)
        except Exception:
            # corrupt/truncated .rda on the SMB-writable partition
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            abort(404)
    resp = send_file(cache_path, mimetype="image/gif")
    resp.headers["Cache-Control"] = "public, max-age=31536000"
    return resp


_PREVIEW_CACHE_MAX_BYTES = 16 * 1024 * 1024  # /run is RAM-backed tmpfs


def _prune_preview_cache(cache_dir, max_bytes=_PREVIEW_CACHE_MAX_BYTES):
    """Keep the preview cache bounded: evict oldest files beyond the cap."""
    try:
        entries = []
        total = 0
        for e in os.scandir(cache_dir):
            if not e.is_file():
                continue
            st = e.stat()
            entries.append((st.st_mtime, st.st_size, e.path))
            total += st.st_size
        if total <= max_bytes:
            return
        entries.sort()
        for _mt, size, p in entries:
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
            if total <= max_bytes // 2:
                break
    except OSError:
        pass


@app.route("/api/preview/gif/<category>/<filename>")
def api_preview_gif(category, filename):
    if not _safe_name(category) or not _safe_name(filename):
        abort(404)
    path = os.path.join(paths.gif_dir(), category, filename)
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype="image/gif")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/library")
def api_library():
    idx = _load_index()
    game_flags = CFG.get("dmd.games", {}) or {}
    disabled = set(CFG.get("dmd.disabled_animations", []) or [])
    games_out = []
    dmd_total = 0
    dmd_enabled = 0
    for game in sorted((idx.get("games") or {}).keys()):
        anims = idx["games"][game] or []
        g_en = bool(game_flags.get(game, True))
        rows = []
        for a in anims:
            a_en = a.get("name") not in disabled
            dmd_total += 1
            if g_en and a_en:
                dmd_enabled += 1
            rows.append({"name": a.get("name"),
                         "frames": a.get("frames"),
                         "duration_ms": a.get("duration_ms"),
                         "clock_type": a.get("clock_type"),
                         "enabled": a_en})
        games_out.append({"game": game, "enabled": g_en,
                          "count": len(anims), "animations": rows})
    cat_flags = CFG.get("gif.categories", {}) or {}
    cats_out = []
    gif_total = 0
    gif_enabled = 0
    for name, files in _gif_scan():
        c_en = bool(cat_flags.get(name, True))
        gif_total += len(files)
        if c_en:
            gif_enabled += len(files)
        cats_out.append({"category": name, "enabled": c_en,
                         "count": len(files), "files": files})
    tint = CFG.get("display.tint", rda.DEFAULT_TINT)
    if isinstance(tint, (list, tuple)):
        tint = "-".join(str(int(c)) for c in tint[:3])
    return jsonify({
        "dmd": {"games": games_out, "total": dmd_total,
                "enabled": dmd_enabled},
        "gif": {"categories": cats_out, "total": gif_total,
                "enabled": gif_enabled},
        "tint": tint,
    })


@app.route("/api/library/toggle", methods=["POST"])
def api_library_toggle():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "bad body"}), 400
    kind = body.get("kind")
    item = body.get("id", "")
    enabled = bool(body.get("enabled", True))
    if kind == "dmd_game":
        if not item:
            return jsonify({"ok": False, "error": "missing id"}), 400
        games = CFG.data.setdefault("dmd", {}).setdefault("games", {})
        games[item] = enabled
    elif kind == "dmd_anim":
        name = item.split("/")[-1]
        if not name:
            return jsonify({"ok": False, "error": "missing id"}), 400
        lst = CFG.data.setdefault("dmd", {}).setdefault(
            "disabled_animations", [])
        if enabled:
            CFG.data["dmd"]["disabled_animations"] = [
                n for n in lst if n != name]
        elif name not in lst:
            lst.append(name)
    elif kind == "gif_category":
        if not item:
            return jsonify({"ok": False, "error": "missing id"}), 400
        cats = CFG.data.setdefault("gif", {}).setdefault("categories", {})
        cats[item] = enabled
    elif kind == "dmd_all":
        idx = _load_index()
        games = CFG.data.setdefault("dmd", {}).setdefault("games", {})
        for game in (idx.get("games") or {}):
            games[game] = enabled
        if enabled:
            CFG.data["dmd"]["disabled_animations"] = []
    elif kind == "gif_all":
        cats = CFG.data.setdefault("gif", {}).setdefault("categories", {})
        for name, _files in _gif_scan():
            cats[name] = enabled
    else:
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    CFG.save()
    player = ctl.send("reload_config")
    return jsonify({"ok": True, "player": player})


@app.route("/api/fonts")
def api_fonts():
    return jsonify({"fonts": _list_fonts()})


@app.route("/api/logs")
def api_logs():
    unit = request.args.get("unit", "player")
    unit = {"player": "rpi2dmd-player", "web": "rpi2dmd-web"}.get(unit, unit)
    if not re.match(r"^[A-Za-z0-9@._-]+$", unit):
        return jsonify({"ok": False, "error": "bad unit"}), 400
    if platform.system() != "Linux":
        return jsonify({"ok": False, "unit": unit,
                        "text": "journalctl unavailable"})
    try:
        out = subprocess.check_output(
            ["journalctl", "-u", unit, "-n", "200", "--no-pager"],
            stderr=subprocess.STDOUT)
        return jsonify({"ok": True, "unit": unit,
                        "text": out.decode("utf-8", "replace")})
    except (OSError, subprocess.CalledProcessError) as exc:
        return jsonify({"ok": False, "unit": unit,
                        "text": "journalctl unavailable: %s" % exc})


@app.route("/api/backup")
def api_backup():
    try:
        with open(CFG.path, "r", encoding="utf-8") as f:
            blob = f.read()
    except OSError:
        blob = json.dumps(CFG.data, indent=1, sort_keys=True)
    resp = Response(blob, mimetype="application/json")
    resp.headers["Content-Disposition"] = 'attachment; filename="rpi2dmd.json"'
    return resp


@app.route("/api/restore", methods=["POST"])
def api_restore():
    if "file" in request.files:
        blob = request.files["file"].read()
    else:
        blob = request.get_data()
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"ok": False, "error": "not valid JSON"}), 400
    if not isinstance(doc, dict):
        return jsonify({"ok": False,
                        "error": "config must be a JSON object"}), 400
    unknown = [k for k in doc if k not in config.DEFAULTS]
    if unknown:
        return jsonify({"ok": False, "error": "unknown config section(s): %s"
                        % ", ".join(sorted(unknown))}), 400
    merged = config.deep_merge(config.DEFAULTS, doc)
    try:
        merged = _validate(merged, config.DEFAULTS)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _apply_clamps(merged)
    CFG.data = merged
    CFG.save()
    player = ctl.send("reload_config")
    return jsonify({"ok": True, "player": player})


@app.route("/api/factory_reset", methods=["POST"])
def api_factory_reset():
    CFG.data = copy.deepcopy(config.DEFAULTS)
    CFG.save()
    player = ctl.send("reload_config")
    return jsonify({"ok": True, "player": player})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RPI2DMD v3 web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None,
                        help="override web.port from config")
    args = parser.parse_args()
    port = args.port
    if port is None:
        try:
            port = int(CFG.get("web.port", 80))
        except (TypeError, ValueError):
            port = 80
    app.run(host=args.host, port=port, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
