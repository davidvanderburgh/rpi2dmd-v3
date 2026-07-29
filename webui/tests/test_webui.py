"""End-to-end tests for the RPI2DMD v3 web UI.

Plain runnable script (no pytest): builds a small test media tree,
starts webui/app.py in a subprocess with RPI2DMD_* env overrides
(player deliberately offline), then exercises every page and API
endpoint over HTTP with urllib and Pillow.

Run:  python webui/tests/test_webui.py
Exit code 0 = all checks passed.
"""

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.dirname(HERE)
REPO = os.path.dirname(WEBUI)
PLAYER = os.path.join(REPO, "player")
sys.path.insert(0, PLAYER)

from rpi2dmd import rda  # noqa: E402

from PIL import Image  # noqa: E402

CONTENT = os.environ.get(
    "RPI2DMD_CONTENT",
    os.path.join(os.path.dirname(REPO), "v3-content"))

if os.name == "nt":
    WORK = os.environ.get("RPI2DMD_TEST_WORK", r"C:\tmp\rpi2dmd-v3-work")
else:
    WORK = os.environ.get("RPI2DMD_TEST_WORK", "/tmp/rpi2dmd-v3-work")

MEDIA = os.path.join(WORK, "testmedia-web")
RUN = os.path.join(WORK, "run-web")
PREVIEWS = os.path.join(WORK, "previews")
PORT = 8093
BASE = "http://127.0.0.1:%d" % PORT

# games copied from the real library (name -> how many animations)
GAMES = {"ATTACK_FROM_MARS": 3, "AC#DC": 2}


# ---------------------------------------------------------------------------
# Test media tree
# ---------------------------------------------------------------------------

def _synthetic_rda(path, game, name, num_frames=8):
    frames = []
    for i in range(num_frames):
        idx = bytearray(rda.WIDTH * rda.HEIGHT)
        for y in range(rda.HEIGHT):
            for x in range(rda.WIDTH):
                idx[y * rda.WIDTH + x] = ((x + y + i * 4) // 8) % 16
        frames.append(rda.pack_frame(bytes(idx)))
    header = {
        "name": name, "game": game,
        "durations": [100] * num_frames,
        "clock": {"type": "ClockOnTop", "size": "ClockLarge",
                  "x": 32, "y": 8, "start_frame": 0, "end_frame": 0},
        "intro_transition": "Enable", "outro_transition": "Enable",
    }
    rda.write_rda(path, header, frames)
    return {"name": name, "file": "%s/%s.rda" % (game, name),
            "frames": num_frames, "duration_ms": 100 * num_frames,
            "clock_type": "ClockOnTop"}


def _synthetic_gif(path, frames=4):
    images = []
    for i in range(frames):
        img = Image.new("RGB", (128, 32), (10 * i, 40, 90))
        images.append(img)
    images[0].save(path, save_all=True, append_images=images[1:],
                   duration=120, loop=0)


def _find_fallback_ttf():
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def build_media():
    if os.path.isdir(MEDIA):
        shutil.rmtree(MEDIA)
    if os.path.isdir(RUN):
        shutil.rmtree(RUN)
    for d in ("dmd", "gif", "fonts", "config"):
        os.makedirs(os.path.join(MEDIA, d))
    os.makedirs(RUN)
    os.makedirs(PREVIEWS, exist_ok=True)

    # -- DMD library ------------------------------------------------------
    dmd_dst = os.path.join(MEDIA, "dmd")
    src_dmd = os.path.join(CONTENT, "dmd")
    src_index = os.path.join(src_dmd, "index.json")
    games_out = {}
    num_anims = 0
    if os.path.isfile(src_index):
        with open(src_index, "r", encoding="utf-8") as f:
            real = json.load(f)
        for game, take in GAMES.items():
            entries = (real.get("games") or {}).get(game, [])[:take]
            copied = []
            for e in entries:
                src = os.path.join(src_dmd, e["file"].replace("/", os.sep))
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(dmd_dst, e["file"].replace("/", os.sep))
                dstdir = os.path.dirname(dst)
                if not os.path.isdir(dstdir):
                    os.makedirs(dstdir)
                shutil.copyfile(src, dst)
                copied.append(e)
            if copied:
                games_out[game] = copied
                num_anims += len(copied)
    if not games_out:  # no sample content: synthesize
        for game, take in GAMES.items():
            gdir = os.path.join(dmd_dst, game)
            os.makedirs(gdir)
            entries = []
            for i in range(take):
                name = "%s_%03d" % (game, i)
                entries.append(_synthetic_rda(
                    os.path.join(gdir, name + ".rda"), game, name))
            games_out[game] = entries
            num_anims += take
    index = {"format": "rda1", "source": "test subset",
             "num_games": len(games_out), "num_animations": num_anims,
             "games": games_out}
    with open(os.path.join(dmd_dst, "index.json"), "w",
              encoding="utf-8") as f:
        json.dump(index, f, indent=1)

    # -- GIF library ------------------------------------------------------
    gif_dst = os.path.join(MEDIA, "gif")
    src_gif = os.path.join(CONTENT, "media-base", "gif")
    plan = [("Arcade", 2), ("Logo", 1)]
    for cat, take in plan:
        cdir = os.path.join(gif_dst, cat)
        os.makedirs(cdir)
        src_cat = os.path.join(src_gif, cat)
        copied = 0
        if os.path.isdir(src_cat):
            for f in sorted(os.listdir(src_cat)):
                if f.lower().endswith(".gif") and copied < take:
                    shutil.copyfile(os.path.join(src_cat, f),
                                    os.path.join(cdir, f))
                    copied += 1
        while copied < take:
            _synthetic_gif(os.path.join(
                cdir, "%s_test_%d.gif" % (cat.upper(), copied)))
            copied += 1

    # -- Fonts / backgrounds ------------------------------------------------
    fonts_dst = os.path.join(MEDIA, "fonts")
    src_fonts = os.path.join(CONTENT, "media-base", "fonts")
    fallback = _find_fallback_ttf()
    main_ttf = os.path.join(src_fonts, "GOUDYSTO.TTF")
    if os.path.isfile(main_ttf):
        shutil.copyfile(main_ttf, os.path.join(fonts_dst, "GOUDYSTO.TTF"))
    elif fallback:
        shutil.copyfile(fallback, os.path.join(fonts_dst, "GOUDYSTO.TTF"))
    pol_dst = os.path.join(fonts_dst, "Polices")
    os.makedirs(pol_dst)
    src_pol = os.path.join(src_fonts, "Polices")
    copied = False
    if os.path.isdir(src_pol):
        for f in sorted(os.listdir(src_pol)):
            if f.lower().endswith(".ttf"):
                shutil.copyfile(os.path.join(src_pol, f),
                                os.path.join(pol_dst, f))
                copied = True
                break
    if not copied and fallback:
        shutil.copyfile(fallback, os.path.join(pol_dst, "FALLBACK.ttf"))
    bg_dst = os.path.join(fonts_dst, "Background")
    os.makedirs(bg_dst)
    bg_done = 0
    if os.path.isdir(src_fonts):
        for f in sorted(os.listdir(src_fonts)):
            if (f.startswith("Background") and bg_done < 2
                    and f.lower().endswith((".gif", ".jpg", ".png"))):
                shutil.copyfile(os.path.join(src_fonts, f),
                                os.path.join(fonts_dst, f))
                bg_done += 1
    if os.path.isdir(os.path.join(src_fonts, "Background")):
        for f in sorted(os.listdir(os.path.join(src_fonts, "Background"))):
            if f.lower().endswith(".gif"):
                shutil.copyfile(
                    os.path.join(src_fonts, "Background", f),
                    os.path.join(bg_dst, f))
                break
    if bg_done == 0:
        _synthetic_gif(os.path.join(fonts_dst, "Background_test.gif"))

    # -- Config -------------------------------------------------------------
    with open(os.path.join(MEDIA, "config", "rpi2dmd.json"), "w",
              encoding="utf-8") as f:
        json.dump({"web": {"port": PORT}}, f)

    return index


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http(method, path, body=None, headers=None):
    """-> (status, headers dict, body bytes); never raises for HTTP errors."""
    url = BASE + path
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
        else:
            data = str(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers,
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.getcode(), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def get(path, headers=None):
    return http("GET", path, headers=headers)


def post(path, body=None, headers=None):
    return http("POST", path, body=body, headers=headers)


def get_json(path, headers=None):
    status, _hdrs, body = get(path, headers)
    return status, json.loads(body.decode("utf-8"))


def post_json(path, body=None, headers=None):
    status, _hdrs, resp = post(path, body, headers)
    return status, json.loads(resp.decode("utf-8"))


def read_disk_config():
    with open(os.path.join(MEDIA, "config", "rpi2dmd.json"), "r",
              encoding="utf-8") as f:
        return json.load(f)


def basic_auth(user, password):
    token = base64.b64encode(
        ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}


# ---------------------------------------------------------------------------
# Assertion bookkeeping
# ---------------------------------------------------------------------------

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    line = "[%s] %s" % (mark, name)
    if detail and not cond:
        line += "  -- " + str(detail)[:200]
    print(line)
    return bool(cond)


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

def test_pages():
    markers = {
        "/": ["Dashboard", "now-playing", "Quick toggles", "tile-dmd"],
        "/clock": ["clock-preview", "Classic Run-DMD", "align-grid",
                   "DMD color (global)"],
        "/library": ["lib-tabs", "Enable all DMD", "pane-gif"],
        "/playback": ["content_filter", "Run-DMD default", "clock_overlay",
                      "show_name"],
        "/message": ["msg-text", "Send now", "{time}"],
        "/schedule": ["bright-bars", "Day preset", "schedule.sleep"],
        "/network": ["wifi-psk", "America/New_York", "openweathermap"],
        "/system": ["factory-reset", "log-view", "/api/backup",
                    "restore-file"],
    }
    for path, needles in markers.items():
        status, _hdrs, body = get(path)
        text = body.decode("utf-8", "replace")
        check("page %s -> 200" % path, status == 200, status)
        for needle in needles:
            check("page %s contains %r" % (path, needle), needle in text)


def test_status_offline():
    status, doc = get_json("/api/status")
    check("/api/status -> 200", status == 200, status)
    check("/api/status offline-graceful", doc.get("state") == "offline", doc)


def test_config_api():
    status, doc = get_json("/api/config")
    check("/api/config GET -> 200", status == 200, status)
    for key in ("panel", "display", "clock", "playback", "gif", "dmd",
                "message", "weather", "schedule", "network", "web",
                "system", "date"):
        check("config has section %r" % key, key in doc)

    # partial POST round-trips
    status, resp = post_json("/api/config", {"clock": {"format": "24h"}})
    check("POST clock.format -> ok", status == 200 and resp.get("ok"), resp)
    status, doc = get_json("/api/config")
    check("clock.format round-trips", doc["clock"]["format"] == "24h", doc["clock"])
    check("clock.format persisted to disk",
          read_disk_config()["clock"]["format"] == "24h")
    check("other clock keys preserved", doc["clock"]["style"] in
          ("rundmd", "digital", "ttf"))

    # coercion
    status, resp = post_json("/api/config",
                             {"clock": {"enabled": "false", "x": "7"}})
    check("POST string coercion -> ok", resp.get("ok"), resp)
    _s, doc = get_json("/api/config")
    check("bool coerced", doc["clock"]["enabled"] is False, doc["clock"])
    check("int coerced", doc["clock"]["x"] == 7, doc["clock"])
    post_json("/api/config", {"clock": {"enabled": True, "x": 0}})

    # clamping
    _s, doc = get_json("/api/config")
    font_size0 = doc["clock"]["font_size"]
    _s, resp = post_json("/api/config", {"clock": {"shade": 99,
                                                   "font_size": 999}})
    check("POST out-of-range -> ok", resp.get("ok"), resp)
    _s, doc = get_json("/api/config")
    check("shade clamped to 15", doc["clock"]["shade"] == 15, doc["clock"])
    check("font_size clamped to 64",
          doc["clock"]["font_size"] == 64, doc["clock"])
    post_json("/api/config", {"clock": {"font_size": font_size0}})

    # bad posts -> 400
    status, resp = post_json("/api/config", {"bogus_section": {"a": 1}})
    check("unknown top-level key -> 400", status == 400, (status, resp))
    status, resp = post_json("/api/config", [1, 2, 3])
    check("non-dict body -> 400", status == 400, (status, resp))
    status, resp = post_json(
        "/api/config", {"display": {"brightness_by_hour": [1, 2, 3]}})
    check("bad brightness table -> 400", status == 400, (status, resp))
    status, resp = post_json("/api/config", {"clock": {"x": "banana"}})
    check("uncoercible int -> 400", status == 400, (status, resp))

    # hex color accepted
    _s, resp = post_json("/api/config", {"clock": {"color": "#ff0000"}})
    check("hex color POST -> ok", resp.get("ok"), resp)
    _s, doc = get_json("/api/config")
    check("hex color -> [r,g,b]", doc["clock"]["color"] == [255, 0, 0],
          doc["clock"]["color"])


def _check_png(name, path_qs, save_as=None):
    status, hdrs, body = get(path_qs)
    ok = check("preview %s -> 200" % name, status == 200, status)
    if not ok:
        return None
    img = None
    try:
        img = Image.open(io.BytesIO(body))
        img.load()
    except Exception as exc:  # noqa: BLE001
        check("preview %s decodes" % name, False, exc)
        return None
    check("preview %s is PNG" % name, img.format == "PNG", img.format)
    check("preview %s is 512x128" % name, img.size == (512, 128), img.size)
    check("preview %s no-store" % name,
          "no-store" in hdrs.get("Cache-Control", ""), hdrs)
    if save_as:
        with open(os.path.join(PREVIEWS, save_as), "wb") as f:
            f.write(body)
    return img.convert("RGB")


def test_clock_preview():
    img = _check_png("default", "/api/preview/clock.png",
                     save_as="clock_default.png")
    if img is not None:
        r, g, b = [band.getextrema() for band in img.split()]
        # Authentic Williams/Bally plasma amber is (255, 88, 32): strongly
        # red-dominant with a little green and a trace of blue.
        expect = rda.TINTS["amber"]
        check("default preview renders the plasma amber tint",
              r[1] == expect[0] and g[1] == expect[1] and b[1] == expect[2]
              and r[1] > g[1] > b[1], (r, g, b, expect))

    _check_png("rundmd colon-on",
               "/api/preview/clock.png?style=rundmd&format=12h_ampm"
               "&colon=on&shade=15&tint=amber",
               save_as="clock_rundmd_ampm.png")

    img = _check_png("ttf 24pt",
                     "/api/preview/clock.png?style=ttf&font=GOUDYSTO.TTF"
                     "&font_size=24&colon=on&color_mode=tint&tint=cyan",
                     save_as="clock_ttf_cyan.png")
    if img is not None:
        r, g, b = [band.getextrema() for band in img.split()]
        check("ttf cyan preview has cyan pixels",
              g[1] > 150 and b[1] > 150 and r[1] < 100, (r, g, b))

    img = _check_png("digital solid red 24h",
                     "/api/preview/clock.png?style=digital&format=24h"
                     "&colon=on&color_mode=solid&color=%23ff0000&shade=15",
                     save_as="clock_digital_red.png")
    if img is not None:
        r, g, b = [band.getextrema() for band in img.split()]
        check("solid red preview pure red",
              r[1] == 255 and g[1] == 0 and b[1] == 0, (r, g, b))

    _check_png("random background",
               "/api/preview/clock.png?style=ttf&font=GOUDYSTO.TTF"
               "&font_size=20&background=random&outline=true"
               "&color_mode=solid&color=%23ffffff&colon=on",
               save_as="clock_bg_random.png")

    # named background (whatever is in the test tree)
    _s, hdrs, body = get("/clock")
    text = body.decode("utf-8", "replace")
    import re as _re
    m = _re.search(r'value="(Background[^"]+)"', text)
    if m:
        _check_png("named background",
                   "/api/preview/clock.png?background=" +
                   urllib.parse.quote(m.group(1)) +
                   "&style=ttf&font=GOUDYSTO.TTF&font_size=18"
                   "&color_mode=solid&color=%23ffffff&colon=on",
                   save_as="clock_bg_named.png")


def test_anim_preview(index):
    # pick the animation with the most frames: Pillow merges identical
    # consecutive GIF frames, so static logo anims can save as 1 frame
    game, anim, best = None, None, -1
    for g, entries in index["games"].items():
        for e in entries:
            if e.get("frames", 0) > best:
                game, anim, best = g, e["name"], e.get("frames", 0)
    q = "/api/preview/anim/%s/%s.gif?tint=amber" % (
        urllib.parse.quote(game, safe=""), urllib.parse.quote(anim, safe=""))
    status, hdrs, body = get(q)
    check("anim preview -> 200", status == 200, status)
    try:
        img = Image.open(io.BytesIO(body))
        check("anim preview is GIF", img.format == "GIF", img.format)
        check("anim preview 2x scale", img.size == (256, 64), img.size)
        check("anim preview multi-frame",
              getattr(img, "n_frames", 1) > 1, getattr(img, "n_frames", 1))
    except Exception as exc:  # noqa: BLE001
        check("anim preview decodes", False, exc)
    check("anim preview long cache",
          "max-age" in hdrs.get("Cache-Control", ""), hdrs)

    # special characters in game name (AC#DC)
    if "AC#DC" in index["games"]:
        anim2 = index["games"]["AC#DC"][0]["name"]
        q2 = "/api/preview/anim/%s/%s.gif" % (
            urllib.parse.quote("AC#DC", safe=""),
            urllib.parse.quote(anim2, safe=""))
        status, _h, body = get(q2)
        check("anim preview handles '#' in game", status == 200, status)

    # traversal rejected
    status, _h, _b = get("/api/preview/anim/..%2f..%2fconfig/x.gif")
    check("anim preview traversal -> 404", status == 404, status)

    # cache file exists
    cache_dir = os.path.join(RUN, "preview-cache")
    check("preview cached in run dir",
          os.path.isdir(cache_dir) and len(os.listdir(cache_dir)) >= 1)


def test_gif_preview():
    _s, lib = get_json("/api/library")
    cats = lib["gif"]["categories"]
    check("gif categories present", len(cats) >= 2, cats)
    cat = cats[0]
    if cat["files"]:
        q = "/api/preview/gif/%s/%s" % (
            urllib.parse.quote(cat["category"], safe=""),
            urllib.parse.quote(cat["files"][0], safe=""))
        status, _h, body = get(q)
        check("raw gif serve -> 200", status == 200, status)
        try:
            img = Image.open(io.BytesIO(body))
            check("raw gif decodes", img.format == "GIF", img.format)
        except Exception as exc:  # noqa: BLE001
            check("raw gif decodes", False, exc)


def test_library_api(index):
    status, lib = get_json("/api/library")
    check("/api/library -> 200", status == 200, status)
    games = dict((g["game"], g) for g in lib["dmd"]["games"])
    for game, entries in index["games"].items():
        check("library lists game %r" % game, game in games)
        if game in games:
            check("game %r count" % game,
                  games[game]["count"] == len(entries),
                  (games[game]["count"], len(entries)))
    total = sum(len(v) for v in index["games"].values())
    check("dmd total count", lib["dmd"]["total"] == total,
          (lib["dmd"]["total"], total))
    g0 = lib["dmd"]["games"][0]
    a0 = g0["animations"][0]
    for key in ("name", "frames", "duration_ms", "clock_type", "enabled"):
        check("animation entry has %r" % key, key in a0, a0)
    check("gif counts present",
          lib["gif"]["total"] >= 3 and lib["gif"]["enabled"] >= 0,
          lib["gif"])


def test_library_toggle(index):
    game = sorted(index["games"].keys())[0]
    anim = index["games"][game][0]["name"]

    _s, resp = post_json("/api/library/toggle",
                         {"kind": "dmd_game", "id": game, "enabled": False})
    check("toggle dmd_game -> ok", resp.get("ok"), resp)
    disk = read_disk_config()
    check("dmd_game toggle persisted",
          disk["dmd"]["games"].get(game) is False, disk["dmd"]["games"])
    _s, lib = get_json("/api/library")
    gmap = dict((g["game"], g) for g in lib["dmd"]["games"])
    check("library reflects disabled game", gmap[game]["enabled"] is False)
    post_json("/api/library/toggle",
              {"kind": "dmd_game", "id": game, "enabled": True})

    _s, resp = post_json("/api/library/toggle",
                         {"kind": "dmd_anim",
                          "id": "%s/%s" % (game, anim), "enabled": False})
    check("toggle dmd_anim -> ok", resp.get("ok"), resp)
    disk = read_disk_config()
    check("dmd_anim persisted to disabled_animations",
          anim in disk["dmd"]["disabled_animations"],
          disk["dmd"]["disabled_animations"])
    post_json("/api/library/toggle",
              {"kind": "dmd_anim", "id": anim, "enabled": True})
    disk = read_disk_config()
    check("dmd_anim re-enable removes entry",
          anim not in disk["dmd"]["disabled_animations"])

    _s, resp = post_json("/api/library/toggle",
                         {"kind": "gif_category", "id": "Arcade",
                          "enabled": False})
    check("toggle gif_category -> ok", resp.get("ok"), resp)
    disk = read_disk_config()
    check("gif_category persisted",
          disk["gif"]["categories"].get("Arcade") is False,
          disk["gif"]["categories"])
    post_json("/api/library/toggle",
              {"kind": "gif_category", "id": "Arcade", "enabled": True})

    _s, resp = post_json("/api/library/toggle",
                         {"kind": "dmd_all", "id": "", "enabled": False})
    check("bulk dmd_all -> ok", resp.get("ok"), resp)
    _s, lib = get_json("/api/library")
    check("bulk disable zeroes enabled count", lib["dmd"]["enabled"] == 0,
          lib["dmd"]["enabled"])
    post_json("/api/library/toggle",
              {"kind": "dmd_all", "id": "", "enabled": True})
    _s, lib = get_json("/api/library")
    check("bulk enable restores count",
          lib["dmd"]["enabled"] == lib["dmd"]["total"], lib["dmd"])

    _s, resp = post_json("/api/library/toggle", {"kind": "nope", "id": "x"})
    check("unknown toggle kind -> 400", _s == 400 or not resp.get("ok"), resp)


def test_fonts():
    status, doc = get_json("/api/fonts")
    check("/api/fonts -> 200", status == 200, status)
    fonts = doc.get("fonts", [])
    check("fonts include a root TTF",
          any("/" not in f for f in fonts), fonts)
    check("fonts include Polices/",
          any(f.startswith("Polices/") for f in fonts), fonts)


def test_control_offline():
    status, resp = post_json("/api/control/pause")
    check("control pause offline -> ok:false",
          status == 200 and resp.get("ok") is False, (status, resp))
    check("control offline error message",
          resp.get("error") == "player offline", resp)
    status, resp = post_json("/api/control/marquee", {"text": "HELLO"})
    check("control marquee offline graceful",
          status == 200 and resp.get("ok") is False, (status, resp))
    status, resp = post_json("/api/control/hackme")
    check("unknown control -> 400", status == 400, (status, resp))
    if os.name == "nt":
        status, resp = post_json("/api/control/reboot")
        check("reboot on non-Linux refused",
              status == 200 and resp.get("ok") is False, resp)


def test_logs():
    status, doc = get_json("/api/logs?unit=player")
    check("/api/logs -> 200", status == 200, status)
    if sys.platform.startswith("linux"):
        check("logs return text", "text" in doc, doc)
    else:
        check("logs unavailable off-Linux",
              "journalctl unavailable" in doc.get("text", ""), doc)


def test_backup_restore():
    status, hdrs, body = get("/api/backup")
    check("/api/backup -> 200", status == 200, status)
    check("backup content-disposition",
          "rpi2dmd.json" in hdrs.get("Content-Disposition", ""), hdrs)
    doc = json.loads(body.decode("utf-8"))
    check("backup is the config doc",
          isinstance(doc, dict) and "clock" in doc)

    status, resp = post_json("/api/restore", {"clock": {"format": "24h_sec"}})
    check("restore -> ok", status == 200 and resp.get("ok"), (status, resp))
    _s, doc = get_json("/api/config")
    check("restore applied", doc["clock"]["format"] == "24h_sec",
          doc["clock"])
    status, resp = post_json("/api/restore", [1, 2])
    check("restore non-dict -> 400", status == 400, (status, resp))
    status, _h, _b = post("/api/restore", b"{not json")
    check("restore bad JSON -> 400", status == 400, status)


def test_factory_reset():
    status, resp = post_json("/api/factory_reset")
    check("factory reset -> ok", status == 200 and resp.get("ok"),
          (status, resp))
    disk = read_disk_config()
    check("factory reset wrote defaults (clock.format)",
          disk["clock"]["format"] == "12h", disk["clock"])
    check("factory reset wrote defaults (web.port)",
          disk["web"]["port"] == 80, disk["web"])


def test_auth_cycle():
    _s, resp = post_json("/api/config",
                         {"web": {"auth_enabled": True,
                                  "username": "admin",
                                  "password": "secret"}})
    check("enable auth -> ok", resp.get("ok"), resp)
    status, hdrs, _b = get("/")
    check("auth on: no creds -> 401", status == 401, status)
    check("auth on: WWW-Authenticate header",
          "Basic" in hdrs.get("WWW-Authenticate", ""), hdrs)
    status, _h, _b = get("/api/status")
    check("auth on: API also gated", status == 401, status)
    status, _h, _b = get("/static/style.css")
    check("auth on: static excluded", status == 200, status)
    status, _h, _b = get("/", headers=basic_auth("admin", "secret"))
    check("auth on: good creds -> 200", status == 200, status)
    status, _h, _b = get("/", headers=basic_auth("admin", "wrong"))
    check("auth on: bad creds -> 401", status == 401, status)
    status, resp = post_json("/api/config",
                             {"web": {"auth_enabled": False}},
                             headers=basic_auth("admin", "secret"))
    check("disable auth with creds -> ok", resp.get("ok"), resp)
    status, _h, _b = get("/")
    check("auth off again -> 200", status == 200, status)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def wait_for_server(proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            sock.close()
            return True
        except OSError:
            time.sleep(0.25)
    return False


def main():
    print("Building test media tree at %s" % MEDIA)
    index = build_media()

    env = dict(os.environ)
    env["RPI2DMD_MEDIA"] = MEDIA
    env["RPI2DMD_RUN"] = RUN
    env["RPI2DMD_CTL_TCP"] = "127.0.0.1:9078"  # nothing listens: offline

    log_path = os.path.join(WORK, "webui-test-server.log")
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(WEBUI, "app.py"),
         "--host", "127.0.0.1", "--port", str(PORT)],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=WEBUI)
    try:
        if not wait_for_server(proc):
            print("FATAL: server did not start; log follows:")
            log.flush()
            with open(log_path, "r") as f:
                print(f.read())
            return 2

        test_pages()
        test_status_offline()
        test_config_api()
        test_clock_preview()
        test_anim_preview(index)
        test_gif_preview()
        test_library_api(index)
        test_library_toggle(index)
        test_fonts()
        test_control_offline()
        test_logs()
        test_backup_restore()
        test_factory_reset()
        test_auth_cycle()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()

    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("-" * 60)
    print("%d checks, %d failed" % (len(RESULTS), len(failed)))
    for name, detail in failed:
        print("  FAILED: %s  %s" % (name, str(detail)[:200]))
    print("preview images saved under %s" % PREVIEWS)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
