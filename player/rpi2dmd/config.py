"""RPI2DMD v3 configuration.

Single JSON document at /media/usb/config/rpi2dmd.json (FAT partition, so
it is also editable over the guest SMB share, like v2's config.txt).
Unknown keys are preserved; missing keys fall back to DEFAULTS via deep
merge, so firmware updates can add settings without migrations.

Also performs a one-time migration from v2's config.txt when
rpi2dmd.json does not exist yet.

Python 3.7 compatible; stdlib only.
"""

import copy
import json
import os
import re
import tempfile

from . import paths

FREQUENCY_CHOICES = [
    "random_1_20", "random_5_60",
    "1s", "2s", "3s", "5s", "10s", "15s", "30s",
    "1m", "5m", "10m", "off",
]

DEFAULTS = {
    "panel": {
        "cols": 64,
        "rows": 32,
        "chain": 2,
        "parallel": 1,
        "gpio_slowdown": 2,
        "rgb_order": "RGB",
        # Tuned for the single-core Pi Zero W these units usually run on:
        # the matrix library's realtime refresh thread will otherwise eat
        # the whole core and starve the web UI (pages took 15 s). 7 bits is
        # ample for 16-shade DMD content, and capping the refresh rate cuts
        # the thread's CPU dramatically. Raise both on a Pi 3/4.
        "pwm_bits": 7,
        "limit_refresh_hz": 120,
        # Measured on the Pi Zero: dithering made the refresh thread WORSE
        # (75% -> 88% CPU), so leave it off. Kept configurable only because
        # it may behave differently on other panels.
        "pwm_dither_bits": 0,
        "pwm_lsb_nanoseconds": 0,  # 0 = library default
    },
    "display": {
        # per-hour brightness, index = hour 0..23, percent 0..100.
        # Full brightness by default — this is a display appliance; use the
        # Schedule page (or the sleep window) to dim it at night.
        "brightness_by_hour": [100] * 24,
        "tint": "amber",          # rpi2dmd.rda.TINTS key or [r,g,b]
        "gamma": 1.0,             # linear; see rda.DEFAULT_GAMMA
    },
    "clock": {
        "enabled": True,           # v3 requirement: clock on by default
        "style": "rundmd",         # rundmd | digital | ttf
        "format": "12h",           # 12h | 12h_ampm | 12h_sec | 24h | 24h_sec
        "colon": "blink",          # blink | on | off
        "font": "GOUDYSTO.TTF",    # for style=ttf, file in /media/usb/fonts
        "font_size": 20,
        "color_mode": "tint",      # tint | solid
        "color": [255, 140, 0],
        "background": "none",      # none | random | fonts/ image filename
        "align": "center",         # center|n|s|e|w|nw|ne|sw|se or "xy"
        "x": 0,
        "y": 0,
        "shade": 15,               # 1..15 digit intensity (tint mode)
        "outline": True,           # 1px dark outline when drawn over content
        "transition": "random",    # random|up_up|down_down|up_down|down_up|fade|none
        "idle_dwell_ms": 6000,     # standalone clock scene duration per cycle
    },
    "date": {
        "enabled": True,
        "format": "%a %b %-d",
        "dwell_ms": 2500,
        "every_n_cycles": 4,       # show date every Nth clock cycle
        "style": "ttf",
        "font": "GOUDYSTO.TTF",
        "font_size": 13,
        "color": [255, 140, 0],
        "align": "center",
        "x": 0,
        "y": 0,
    },
    "playback": {
        "animations_enabled": True,
        "frequency": "random_1_20",     # gap of clock time between animations
        "clock_overlay": "auto",        # auto | front | back | off
        "show_name": "hide",            # hide | before | during | after
        "content_filter": "enabled_only",  # enabled_only | show_all
        "sources": {"dmd": True, "gif": True},
        "dmd_share": 60,                # % of animation slots from DMD library
        "startup_splash": "rundmd",     # rundmd | rpi2dmd | none
        "gif_clock_overlay": False,     # draw clock over RGB GIF clips too
    },
    "gif": {
        # category name -> enabled; categories discovered on the media
        # partition that are missing here default to enabled
        "categories": {"XXX_Mature": False, "Test_Suite": False},
    },
    "dmd": {
        "games": {},                # game -> enabled (missing = enabled)
        # How DMD animations are colored:
        #   per_game - use the real machine's display color (Williams/Bally
        #              90s plasma = amber, Stern LED era = red); falls back
        #              to display.tint for games not in the table
        #   global   - always use display.tint
        "tint_mode": "per_game",
        "game_tints": {},           # game -> tint name; see GAME_TINTS
        "disabled_animations": [    # factory-disabled in the B237 image
            "STAR_TREK_031", "STAR_TREK_032",
            "STAR_TREK_033", "STAR_TREK_034",
        ],
    },
    "message": {
        "enabled": False,
        "text": "RPI2DMD V3",
        "speed": "normal",         # very_slow|slow|normal|fast|very_fast|insane
        "text_mode": "enhanced",   # enhanced | black_shadow | plain
        "clock_position": "behind",  # in_front | behind | random | no_clock
        "frequency": "off",        # off|1s|5s|15s|30s|1m|5m
        "movement": "horizontal",  # horizontal|bounce|loop|diagonal|vertical|static|random
        "position": "top",         # top|high|middle|low|bottom|random
    },
    "weather": {
        "enabled": False,
        "api_key": "",
        "zip_code": "",
        "country": "US",
        "units": "imperial",
        "dwell_ms": 3500,
        "color": [255, 255, 0],
        "refresh_min": 60,
    },
    "schedule": {
        "enabled": False,
        "sleep": "23:30",
        "wake": "06:30",
    },
    "network": {
        "hostname": "RPI2DMD",
        "wifi_country": "US",
        "wifi_ssid": "",
        "wifi_psk": "",
        "timezone": "America/New_York",
    },
    "web": {
        "port": 80,
        "auth_enabled": False,
        "username": "admin",
        "password": "",
        # What the panel does while you are actively using the web UI. On a
        # single-core Pi the render loop and the web server fight for the
        # one core; backing off here makes the UI responsive and stops
        # animations from stuttering mid-render.
        #   clock_only - keep showing the clock, pause animations (default)
        #   pause      - blank the panel entirely
        #   none       - carry on regardless (needs a faster Pi)
        "on_activity": "clock_only",
        "activity_timeout_s": 20,
    },
    "system": {
        "show_ip_on_change": True,
    },
}


def deep_merge(base, override):
    """Return base with override merged in (dicts recursively, rest replaced)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config(object):
    """Loaded configuration with mtime-based change detection."""

    def __init__(self, path=None):
        self.path = path or paths.config_path()
        self.data = copy.deepcopy(DEFAULTS)
        self._mtime = None
        self.load()

    # -- access helpers ---------------------------------------------------
    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def __getitem__(self, key):
        return self.data[key]

    # -- persistence ------------------------------------------------------
    def load(self):
        user = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    user = json.load(f)
                if not isinstance(user, dict):
                    user = {}
            except (ValueError, OSError):
                # corrupt/mid-edit config: keep the previous good data (or
                # defaults on first load), don't crash the appliance
                user = None
            try:
                # remember the mtime even when parsing failed, so a corrupt
                # file is not re-read at every scene boundary
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                pass
            if user is None:
                return self
        elif os.path.exists(paths.v2_config_path()):
            try:
                user = migrate_v2(paths.v2_config_path())
                if not isinstance(user, dict):
                    user = {}
            except (ValueError, OSError):
                user = {}
        self.data = deep_merge(DEFAULTS, user)
        return self

    def save(self):
        """Atomic write (write temp + rename) — FAT-safe enough for our use."""
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())  # FAT32 + power cuts: land it on disk
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._mtime = os.path.getmtime(self.path)

    def changed_on_disk(self):
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return False
        return self._mtime is None or m != self._mtime

    # -- derived values ---------------------------------------------------
    def brightness_now(self, hour):
        table = self.get("display.brightness_by_hour", [50] * 24)
        try:
            return max(0, min(100, int(table[hour % 24])))
        except (TypeError, ValueError, IndexError):
            return 50

    def animation_gap_seconds(self, rng):
        """Clock-only gap between animations, or None if animations off."""
        freq = str(self.get("playback.frequency", "random_1_20"))
        if freq == "off" or not self.get("playback.animations_enabled", True):
            return None
        if freq == "random_1_20":
            return rng.uniform(1, 20)
        if freq == "random_5_60":
            return rng.uniform(5, 60)
        m = re.match(r"^(\d+)([sm])$", freq)
        if m:
            n = int(m.group(1))
            return n * 60 if m.group(2) == "m" else n
        return rng.uniform(1, 20)


# ---------------------------------------------------------------------------
# v2 config.txt migration
# ---------------------------------------------------------------------------

def _parse_v2(path):
    kv = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv


def migrate_v2(path):
    """Map v2 config.txt keys onto the v3 schema (best effort)."""
    kv = _parse_v2(path)
    out = {"panel": {}, "display": {}, "clock": {}, "date": {},
           "weather": {}, "network": {}, "gif": {"categories": {}},
           "playback": {}}

    def geti(key, default=None):
        try:
            return int(kv[key])
        except (KeyError, ValueError):
            return default

    p = out["panel"]
    if geti("Panel_XSize") is not None:
        p["cols"] = geti("Panel_XSize")
    if geti("Panel_YSize") is not None:
        p["rows"] = geti("Panel_YSize")
    if geti("Panel_XNumber") is not None:
        p["chain"] = geti("Panel_XNumber")
    if geti("Panel_YNumber") is not None:
        p["parallel"] = geti("Panel_YNumber")
    if geti("GPIO_Slowdown") is not None:
        p["gpio_slowdown"] = geti("GPIO_Slowdown")
    if kv.get("RGB_Order"):
        p["rgb_order"] = kv["RGB_Order"]
    # PWM_Bits is deliberately NOT migrated: v2's 11 is too expensive for the
    # Python stack on a single-core Pi (it starves the web UI). The v3 default
    # is tuned instead; change it on the Playback page if your Pi can afford it.

    bright = []
    for h in range(24):
        bright.append(geti("Panel_Brightness_%d" % h, 30))
    out["display"]["brightness_by_hour"] = bright

    c = out["clock"]
    c["enabled"] = kv.get("Clock_Active", "1") == "1"
    if kv.get("Clock_Font"):
        c["font"] = kv["Clock_Font"]
        c["style"] = "ttf"
    if geti("Clock_Font_Size") is not None:
        c["font_size"] = geti("Clock_Font_Size")
    fmt = kv.get("Clock_Format", "")
    if fmt:
        c["format"] = "24h_sec" if "%S" in fmt else "24h"
        if "%I" in fmt or "%l" in fmt:
            c["format"] = "12h_sec" if "%S" in fmt else "12h"
    bg = kv.get("Clock_Background", "")
    c["background"] = "random" if "*" in bg else (bg or "none")
    if geti("Clock_Display_Time") is not None:
        c["idle_dwell_ms"] = geti("Clock_Display_Time")

    d = out["date"]
    d["enabled"] = kv.get("Date_Active", "1") == "1"
    if kv.get("Date_Font"):
        d["font"] = kv["Date_Font"]
    if geti("Date_Font_Size") is not None:
        d["font_size"] = geti("Date_Font_Size")
    if geti("Date_Display_Time") is not None:
        d["dwell_ms"] = geti("Date_Display_Time")

    w = out["weather"]
    w["enabled"] = bool(kv.get("OpenWeather_ID")) and kv.get("Weather_Active", "1") == "1"
    if kv.get("OpenWeather_ID"):
        w["api_key"] = kv["OpenWeather_ID"]
    if kv.get("OpenWeather_ZipCode"):
        w["zip_code"] = kv["OpenWeather_ZipCode"]
    if kv.get("OpenWeather_Country"):
        w["country"] = kv["OpenWeather_Country"]

    n = out["network"]
    if kv.get("Network_name"):
        n["hostname"] = kv["Network_name"]
    if kv.get("Wifi_country"):
        n["wifi_country"] = kv["Wifi_country"]
    if kv.get("Wifi_ssid") and kv["Wifi_ssid"] != "Your_SSID":
        n["wifi_ssid"] = kv["Wifi_ssid"]
    if kv.get("Wifi_psk") and kv["Wifi_psk"] != "Your_password":
        n["wifi_psk"] = kv["Wifi_psk"]
    if kv.get("Clock_TZ"):
        n["timezone"] = kv["Clock_TZ"]

    out["playback"]["animations_enabled"] = kv.get("Gif_Active", "1") == "1"
    return out
