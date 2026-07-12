"""Scene generators.

Every scene is a generator yielding (PIL RGB image canvas-sized, hold_ms)
tuples; the scheduler pushes the frames to the output driver and sleeps
hold_ms between them (checking control flags), so scenes never touch the
hardware and are directly testable.

Python 3.7 / Pillow 5.4 compatible.
"""

import datetime
import json
import os
import random
import re
import socket
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont, ImageSequence

from . import clock, paths, rda

CANVAS = (128, 32)
MIN_FRAME_MS = 20
GIF_DEFAULT_FRAME_MS = 100
GIF_MIN_TOTAL_MS = 1500
CLOCK_TICK_MS = 100

# message speeds -> pixels per 40ms frame
SPEEDS = {
    "very_slow": 0.5,
    "slow": 1.0,
    "normal": 2.0,
    "fast": 3.0,
    "very_fast": 5.0,
    "insane": 10.0,
}

_DEJAVU_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/opt/RPI2DMD/arial.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _display(cfg):
    """-> (tint, gamma) from the display config."""
    tint = cfg.get("display.tint", rda.DEFAULT_TINT)
    if isinstance(tint, list):
        tint = tuple(tint)
    try:
        gamma = float(cfg.get("display.gamma", rda.DEFAULT_GAMMA))
    except (TypeError, ValueError):
        gamma = rda.DEFAULT_GAMMA
    return tint, gamma


def _bright_color(cfg):
    """Brightest step of the configured tint ramp as an (r, g, b) tuple."""
    tint, gamma = _display(cfg)
    pal = rda.build_palette(tint, gamma)
    return tuple(pal[45:48])


def _load_ttf(name, size, fonts_dir=None):
    """Load a TTF: media fonts dir first, then bundled DejaVu-ish fallbacks."""
    fonts_dir = fonts_dir or paths.fonts_dir()
    candidates = []
    if name:
        candidates.append(os.path.join(fonts_dir, name))
        candidates.append(os.path.join(fonts_dir, "Polices", name))
    candidates.extend(_DEJAVU_CANDIDATES)
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, int(size))
            except OSError:
                continue
    return ImageFont.load_default()


def _measure(draw, text, font):
    """-> (w, h, offset_x, offset_y); dual path for Pillow 5.4 / modern."""
    try:
        x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
        return x1 - x0, y1 - y0, x0, y0
    except AttributeError:  # Pillow < 8
        w, h = draw.textsize(text, font=font)
        return w, h, 0, 0


def format_date(fmt, now=None):
    """strftime with manual %-X (no leading zero) handling for all platforms."""
    now = now or datetime.datetime.now()

    def sub(m):
        try:
            v = now.strftime("%" + m.group(1))
        except ValueError:
            return m.group(1)
        stripped = v.lstrip("0")
        return stripped if stripped else "0"

    fmt = re.sub(r"%-([A-Za-z])", sub, fmt)
    try:
        return now.strftime(fmt)
    except ValueError:
        return now.strftime("%a %b %d")


def cpu_temp_text():
    """CPU temperature string from the thermal zone, or '?' when absent."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return "%d°C" % (int(f.read().strip()) // 1000)
    except (OSError, ValueError):
        return "?"


def expand_tokens(cfg, text, now=None):
    """Substitute {time}, {temp} and {date} in a message string."""
    now = now or datetime.datetime.now()
    if "{time}" in text:
        t, suffix = clock.time_text(cfg["clock"], now)
        text = text.replace("{time}", (t + " " + suffix) if suffix else t)
    if "{temp}" in text:
        text = text.replace("{temp}", cpu_temp_text())
    if "{date}" in text:
        text = text.replace(
            "{date}", format_date(cfg.get("date.format", "%a %b %-d"), now))
    return text


def _fit_cover(img, w, h, resample=Image.BILINEAR):
    """Resize keeping aspect to cover (w, h), then center-crop."""
    if img.size == (w, h):
        return img
    sw, sh = img.size
    scale = max(w / float(sw), h / float(sh))
    nw = max(w, int(round(sw * scale)))
    nh = max(h, int(round(sh * scale)))
    img = img.resize((nw, nh), resample)
    x0 = (nw - w) // 2
    y0 = (nh - h) // 2
    return img.crop((x0, y0, x0 + w, y0 + h))


def _load_image_frames(path):
    """Load a (possibly animated) image -> list of (RGB image, duration_ms).

    Frames are composited over the previous frame so partial/transparent
    GIF frames render correctly.
    """
    img = Image.open(path)
    frames = []
    base = None
    for frame in ImageSequence.Iterator(img):
        dur = frame.info.get("duration", GIF_DEFAULT_FRAME_MS)
        try:
            dur = max(MIN_FRAME_MS, int(dur))
        except (TypeError, ValueError):
            dur = GIF_DEFAULT_FRAME_MS
        fr = frame.convert("RGBA")
        if base is None:
            base = Image.new("RGBA", img.size, (0, 0, 0, 255))
        base.paste(fr, (0, 0), fr)
        frames.append((base.convert("RGB"), dur))
    if not frames:
        frames = [(img.convert("RGB"), GIF_DEFAULT_FRAME_MS)]
    return frames


def scan_backgrounds(fonts_dir=None):
    """All candidate clock background files: fonts/Background_*.* plus
    everything under fonts/Background/."""
    fonts_dir = fonts_dir or paths.fonts_dir()
    out = []
    try:
        for f in sorted(os.listdir(fonts_dir)):
            if f.startswith("Background_"):
                p = os.path.join(fonts_dir, f)
                if os.path.isfile(p):
                    out.append(p)
    except OSError:
        pass
    sub = os.path.join(fonts_dir, "Background")
    try:
        for f in sorted(os.listdir(sub)):
            p = os.path.join(sub, f)
            if os.path.isfile(p):
                out.append(p)
    except OSError:
        pass
    return out


def _resolve_background(cfg_clock, backgrounds, rng):
    """Config background setting -> file path or None."""
    name = cfg_clock.get("background", "none") or "none"
    if name == "none":
        return None
    if name == "random":
        pool = backgrounds if backgrounds else scan_backgrounds()
        return rng.choice(pool) if pool else None
    for p in (os.path.join(paths.fonts_dir(), name),
              os.path.join(paths.fonts_dir(), "Background", name),
              name):
        if os.path.isfile(p):
            return p
    return None


def _draw_clock_rgb(img, cfg, outline=True, now=None):
    """Draw the (pixel-style) clock over an RGB frame in place: tinted
    glyph pixels with an optional 1px dark outline for readability."""
    ck = cfg["clock"]
    tint, gamma = _display(cfg)
    grid, bbox = clock.render_indexed(ck, img.width, img.height, now=now)
    color = clock._text_color(ck, tint, gamma)
    px = img.load()
    bx, by, bw, bh = bbox
    xa = max(0, bx - 1)
    xb = min(img.width, bx + bw + 1)
    ya = max(0, by - 1)
    yb = min(img.height, by + bh + 1)
    if outline:
        for y in range(ya, yb):
            row = grid[y]
            for x in range(xa, xb):
                if row[x]:
                    continue
                near = ((x > 0 and row[x - 1]) or
                        (x < img.width - 1 and row[x + 1]) or
                        (y > 0 and grid[y - 1][x]) or
                        (y < img.height - 1 and grid[y + 1][x]))
                if near:
                    px[x, y] = (0, 0, 0)
    for y in range(ya, yb):
        row = grid[y]
        for x in range(xa, xb):
            v = row[x]
            if v:
                f = v / 15.0
                px[x, y] = (int(color[0] * f), int(color[1] * f),
                            int(color[2] * f))


def _render_text_layer(text, font, color, mode):
    """Render text once as an RGBA layer with the requested effect:
    enhanced (vertical tint gradient) | black_shadow | plain."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    tw, th, ox, oy = _measure(probe, text, font)
    tw = max(1, tw)
    th = max(1, th)
    pad = 1 if mode == "black_shadow" else 0
    layer = Image.new("RGBA", (tw + pad, th + pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if mode == "black_shadow":
        d.text((1 - ox, 1 - oy), text, font=font, fill=(0, 0, 0, 255))
        d.text((-ox, -oy), text, font=font, fill=tuple(color) + (255,))
    elif mode == "enhanced":
        mask = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(mask).text((-ox, -oy), text, font=font, fill=255)
        grad = Image.new("RGB", (tw, th))
        gd = ImageDraw.Draw(grad)
        for y in range(th):
            f = 1.0 - 0.6 * (y / float(max(1, th - 1)))
            gd.line([(0, y), (tw, y)],
                    fill=(int(color[0] * f), int(color[1] * f),
                          int(color[2] * f)))
        layer.paste(grad, (0, 0), mask)
    else:
        d.text((-ox, -oy), text, font=font, fill=tuple(color) + (255,))
    return layer


# ---------------------------------------------------------------------------
# clock scene
# ---------------------------------------------------------------------------

def clock_scene(cfg, canvas=CANVAS, dwell_ms=None, backgrounds=None, rng=None):
    """Standalone clock for dwell_ms. Animated GIF backgrounds advance per
    their frame durations while the clock keeps re-rendering (hold capped
    at ~100ms so the blinking colon stays live)."""
    rng = rng or random
    w, h = canvas
    ck = cfg["clock"]
    dwell = int(dwell_ms if dwell_ms is not None
                else ck.get("idle_dwell_ms", 6000))
    tint, gamma = _display(cfg)
    fonts_dir = paths.fonts_dir()

    bg_frames = None
    bg_path = _resolve_background(ck, backgrounds, rng)
    if bg_path:
        try:
            bg_frames = [(_fit_cover(img, w, h), dur)
                         for img, dur in _load_image_frames(bg_path)]
        except (OSError, ValueError):
            bg_frames = None

    elapsed = 0
    idx = 0
    remaining = bg_frames[0][1] if bg_frames else 0
    while elapsed < dwell:
        if bg_frames:
            bg = bg_frames[idx][0]
            hold = min(remaining, CLOCK_TICK_MS)
        else:
            bg = None
            hold = CLOCK_TICK_MS
        hold = max(MIN_FRAME_MS, min(hold, dwell - elapsed))
        frame = clock.render_scene(ck, w, h, background=bg, tint=tint,
                                   gamma=gamma, fonts_dir=fonts_dir)
        yield frame, hold
        elapsed += hold
        if bg_frames:
            remaining -= hold
            if remaining <= 0:
                idx = (idx + 1) % len(bg_frames)
                remaining = bg_frames[idx][1]


# ---------------------------------------------------------------------------
# DMD animation scene
# ---------------------------------------------------------------------------

def _resolve_overlay(cfg, header):
    """-> (mode, size_hint, override_xy, start, end) for the clock overlay.
    mode is 'front', 'back' or None."""
    num = int(header.get("num_frames", 0))
    last = max(0, num - 1)
    setting = cfg.get("playback.clock_overlay", "auto")
    if setting == "off":
        return None, "large", None, 0, last
    if setting in ("front", "back"):
        # global override: all frames, centered, large
        return setting, "large", None, 0, last
    # auto: per-animation metadata
    meta = header.get("clock", {}) or {}
    ctype = meta.get("type", rda.CLOCK_NONE)
    if ctype == rda.CLOCK_ON_TOP:
        mode = "front"
    elif ctype == rda.CLOCK_BEHIND:
        mode = "back"
    else:
        return None, "large", None, 0, last
    size_hint = "small" if meta.get("size") == "ClockSmall" else "large"
    x = int(meta.get("x", 0))
    y = int(meta.get("y", 0))
    override_xy = (x, y) if (x or y) else None
    start = max(0, int(meta.get("start_frame", 0)))
    end = int(meta.get("end_frame", 0))
    if end <= 0:
        end = last
    return mode, size_hint, override_xy, start, end


def _name_overlay(text, w, h):
    """Small bottom-left title with a black outline, as an RGBA layer."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _load_ttf(None, 9)
    tw, th, ox, oy = _measure(d, text, font)
    x = 2
    y = h - th - 1
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((x + dx - ox, y + dy - oy), text, font=font,
               fill=(0, 0, 0, 255))
    d.text((x - ox, y - oy), text, font=font, fill=(230, 230, 230, 255))
    return layer


def dmd_scene(cfg, rda_path, header=None, frames=None, canvas=CANVAS):
    """Play an RDA animation honoring per-frame durations and the clock
    overlay rules (playback.clock_overlay / per-animation metadata)."""
    if header is None or frames is None:
        header, frames = rda.read_rda(rda_path)
    w, h = canvas
    tint, gamma = _display(cfg)
    palette = rda.build_palette(tint, gamma)
    mode, size_hint, override_xy, start, end = _resolve_overlay(cfg, header)
    outline = bool(cfg.get("clock.outline", True))

    name_layer = None
    if cfg.get("playback.show_name", "hide") == "during":
        name_layer = _name_overlay(
            header.get("name", "").replace("_", " "), w, h)

    durations = header.get("durations", [])
    for i, packed in enumerate(frames):
        idx = rda.unpack_frame(packed)
        if mode is not None and start <= i <= end:
            grid, _ = clock.render_indexed(
                cfg["clock"], rda.WIDTH, rda.HEIGHT,
                size_hint=size_hint, override_xy=override_xy)
            idx = clock.composite_clock_indexed(
                idx, grid, mode, outline=(outline and mode == "front"))
        img = Image.frombytes("P", (rda.WIDTH, rda.HEIGHT), idx)
        img.putpalette(palette)
        img = img.convert("RGB")
        if (rda.WIDTH, rda.HEIGHT) != (w, h):
            img = img.resize((w, h), Image.NEAREST)
        if name_layer is not None:
            img.paste(name_layer, (0, 0), name_layer)
        dur = durations[i] if i < len(durations) else GIF_DEFAULT_FRAME_MS
        yield img, max(MIN_FRAME_MS, int(dur))


# ---------------------------------------------------------------------------
# GIF scene
# ---------------------------------------------------------------------------

def gif_scene(cfg, path, canvas=CANVAS):
    """Play a GIF file: per-frame durations, cover-scale to the canvas,
    single pass (looped until >= 1.5s total), optional clock overlay."""
    w, h = canvas
    frames = [(_fit_cover(img, w, h), dur)
              for img, dur in _load_image_frames(path)]
    total = sum(dur for _, dur in frames)
    passes = 1
    if total > 0:
        while total * passes < GIF_MIN_TOTAL_MS:
            passes += 1
    overlay = bool(cfg.get("playback.gif_clock_overlay", False))
    for _ in range(passes):
        for img, dur in frames:
            out = img
            if overlay:
                out = img.copy()
                _draw_clock_rgb(out, cfg, outline=True)
            yield out, dur


# ---------------------------------------------------------------------------
# title card / date / message / weather / ip / test / splash
# ---------------------------------------------------------------------------

def name_scene(cfg, text, canvas=CANVAS):
    """Scrolling title card: text crosses the canvas right-to-left."""
    w, h = canvas
    font = _load_ttf(None, 14)
    layer = _render_text_layer(text, font, _bright_color(cfg), "plain")
    tw, th = layer.size
    y = max(0, (h - th) // 2)
    x = float(w)
    while x > -tw:
        img = Image.new("RGB", (w, h))
        img.paste(layer, (int(round(x)), y), layer)
        yield img, 40
        x -= 1.2  # ~30 px/s at 40ms per frame


def date_scene(cfg, canvas=CANVAS):
    """Date card per the date.* config."""
    w, h = canvas
    dc = cfg["date"]
    now = datetime.datetime.now()
    text = format_date(dc.get("format", "%a %b %-d"), now)
    color = tuple(int(v) for v in (dc.get("color") or [255, 140, 0])[:3])
    name = dc.get("font") if dc.get("style", "ttf") == "ttf" else None
    font = _load_ttf(name, int(dc.get("font_size", 13)))
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    tw, th, ox, oy = _measure(d, text, font)
    x, y = clock._place(w, h, tw, th, dc)
    d.text((x - ox, y - oy), text, font=font, fill=color)
    yield img, int(dc.get("dwell_ms", 2500))


def message_scene(cfg, text_override=None, canvas=CANVAS, rng=None):
    """Custom message with movement/speed/position/clock-layering per the
    message.* config. text_override plays an ad-hoc marquee."""
    rng = rng or random
    w, h = canvas
    mc = cfg["message"]
    text = text_override if text_override is not None else mc.get("text", "")
    text = expand_tokens(cfg, str(text))
    if not text:
        return
    movement = mc.get("movement", "horizontal")
    if movement == "random":
        movement = rng.choice(["horizontal", "bounce", "loop", "diagonal",
                               "vertical", "static"])
    speed = SPEEDS.get(mc.get("speed", "normal"), SPEEDS["normal"])
    clock_pos = mc.get("clock_position", "behind")
    if clock_pos == "random":
        clock_pos = rng.choice(["in_front", "behind", "no_clock"])
    color = _bright_color(cfg)
    font = _load_ttf(None, 13)
    layer = _render_text_layer(text, font, color,
                               mc.get("text_mode", "enhanced"))
    tw, th = layer.size
    span = max(0, h - th)
    posname = mc.get("position", "top")
    if posname == "random":
        posname = rng.choice(["top", "high", "middle", "low", "bottom"])
    y0 = {"top": 0, "high": span // 4, "middle": span // 2,
          "low": (3 * span) // 4, "bottom": span}.get(posname, 0)

    def emit(x, y, hold):
        img = Image.new("RGB", (w, h))
        if clock_pos == "behind":
            _draw_clock_rgb(img, cfg, outline=False)
        img.paste(layer, (int(round(x)), int(round(y))), layer)
        if clock_pos == "in_front":
            _draw_clock_rgb(img, cfg, outline=True)
        return img, hold

    if movement == "horizontal":
        x = float(w)
        while x > -tw:
            yield emit(x, y0, 40)
            x -= speed
    elif movement == "loop":
        gap = 24
        span_x = tw + gap
        off = 0.0
        while off < 2 * span_x:
            img = Image.new("RGB", (w, h))
            if clock_pos == "behind":
                _draw_clock_rgb(img, cfg, outline=False)
            bx = -(off % span_x)
            k = 0
            while bx + k * span_x < w:
                img.paste(layer, (int(round(bx + k * span_x)), y0), layer)
                k += 1
            if clock_pos == "in_front":
                _draw_clock_rgb(img, cfg, outline=True)
            yield img, 40
            off += speed
    elif movement == "vertical":
        x0 = (w - tw) // 2
        y = float(h)
        while y > -th:
            yield emit(x0, y, 40)
            y -= speed
    elif movement in ("bounce", "diagonal"):
        xmin = min(0, w - tw)
        xmax = max(0, w - tw)
        x = 0.0
        y = float(y0)
        dx = speed
        dy = speed * 0.5 if movement == "diagonal" else 0.0
        elapsed = 0
        while elapsed < 8000:
            yield emit(x, y, 40)
            x += dx
            if x >= xmax:
                x = float(xmax)
                dx = -abs(dx)
            elif x <= xmin:
                x = float(xmin)
                dx = abs(dx)
            if movement == "diagonal":
                y += dy
                if y >= span:
                    y = float(span)
                    dy = -abs(dy)
                elif y <= 0:
                    y = 0.0
                    dy = abs(dy)
            elapsed += 40
    else:  # static
        x0 = (w - tw) // 2
        elapsed = 0
        while elapsed < 4000:
            yield emit(x0, y0, CLOCK_TICK_MS)
            elapsed += CLOCK_TICK_MS


def weather_scene(cfg, data, canvas=CANVAS):
    """Temperature + condition + city card, colored per weather.color."""
    w, h = canvas
    color = tuple(int(v) for v in
                  (cfg.get("weather.color") or [255, 255, 0])[:3])
    units = cfg.get("weather.units", "imperial")
    deg = {"imperial": "°F", "metric": "°C"}.get(units, "K")
    try:
        temp = "%d%s" % (int(round(float(data.get("temp")))), deg)
    except (TypeError, ValueError):
        temp = "?"
    line1 = ("%s %s" % (temp, data.get("condition", ""))).strip()
    line2 = str(data.get("city", "") or "")
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    f1 = _load_ttf(None, 13)
    f2 = _load_ttf(None, 9)
    tw1, th1, ox1, oy1 = _measure(d, line1, f1)
    tw2, th2, ox2, oy2 = _measure(d, line2, f2)
    total = th1 + (th2 + 2 if line2 else 0)
    ytop = max(0, (h - total) // 2)
    d.text(((w - tw1) // 2 - ox1, ytop - oy1), line1, font=f1, fill=color)
    if line2:
        dim = (color[0] * 2 // 3, color[1] * 2 // 3, color[2] * 2 // 3)
        d.text(((w - tw2) // 2 - ox2, ytop + th1 + 2 - oy2), line2,
               font=f2, fill=dim)
    yield img, int(cfg.get("weather.dwell_ms", 3500))


def get_ip_list():
    """Non-loopback IPv4 addresses (Linux: `ip -4 addr`, else socket)."""
    ips = []
    if sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(["ip", "-4", "addr"])
            for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/",
                                 out.decode("utf-8", "replace")):
                ips.append(m.group(1))
        except (OSError, subprocess.CalledProcessError):
            pass
    if not ips:
        try:
            ips = list(socket.gethostbyname_ex(socket.gethostname())[2])
        except (socket.error, OSError):
            pass
    seen = set()
    out = []
    for ip in ips:
        if ip.startswith("127.") or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def ip_scene(cfg, canvas=CANVAS):
    """Hostname + IPv4 addresses for 5 seconds."""
    w, h = canvas
    lines = [socket.gethostname()]
    ips = get_ip_list()
    lines.extend(ips[:2] if ips else ["no network"])
    color = _bright_color(cfg)
    font = _load_ttf(None, 9)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    heights = [_measure(d, ln, font) for ln in lines]
    total = sum(m[1] for m in heights) + (len(lines) - 1)
    y = max(0, (h - total) // 2)
    for ln, (tw, th, ox, oy) in zip(lines, heights):
        d.text(((w - tw) // 2 - ox, y - oy), ln, font=font, fill=color)
        y += th + 1
    yield img, 5000


def test_scene(cfg, canvas=CANVAS):
    """DMD test loop (~10s): solid colors, tint ramp, moving pixel grid."""
    w, h = canvas
    for color in ((255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)):
        yield Image.new("RGB", (w, h), color), 800
    tint, gamma = _display(cfg)
    pal = rda.build_palette(tint, gamma)
    ramp = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(ramp)
    for i in range(16):
        xa = i * w // 16
        xb = (i + 1) * w // 16
        d.rectangle([xa, 0, xb - 1, h - 1], fill=tuple(pal[i * 3:i * 3 + 3]))
    yield ramp, 2000
    for step in range(40):
        img = Image.new("RGB", (w, h))
        px = img.load()
        off = step % 8
        for y in range(off, h, 8):
            for x in range(off, w, 8):
                px[x, y] = (255, 255, 255)
        yield img, 100


def boot_splash_scene(cfg, canvas=CANVAS):
    """Startup splash per playback.startup_splash: authentic Run-DMD logo
    frames ('rundmd'), the RPI2DMD logo GIF ('rpi2dmd'), or nothing."""
    mode = cfg.get("playback.startup_splash", "rundmd")
    if mode == "none":
        return
    w, h = canvas
    if mode == "rpi2dmd":
        path = os.path.join(paths.fonts_dir(), "_Rpi2DmdLogo.gif")
        if os.path.isfile(path):
            try:
                frames = [(_fit_cover(img, w, h), dur)
                          for img, dur in _load_image_frames(path)]
            except (OSError, ValueError):
                frames = []
            total = 0
            for img, dur in frames:
                yield img, dur
                total += dur
            if frames and total < GIF_MIN_TOTAL_MS:
                yield frames[-1][0], GIF_MIN_TOTAL_MS - total
            if frames:
                return
        # missing logo: fall through to the Run-DMD splash
    asset = os.path.join(os.path.dirname(__file__), "assets",
                         "boot_splash.json")
    try:
        with open(asset, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    tint, gamma = _display(cfg)
    pal = rda.build_palette(tint, gamma)
    fw = int(data.get("width", 128))
    fh = int(data.get("height", 32))
    for rows in data.get("frames", []):
        idx = bytearray()
        for row in rows:
            idx.extend(int(c, 16) for c in row)
        if len(idx) != fw * fh:
            continue
        img = Image.frombytes("P", (fw, fh), bytes(idx))
        img.putpalette(pal)
        img = img.convert("RGB")
        if img.size != (w, h):
            img = img.resize((w, h), Image.NEAREST)
        yield img, 1500
