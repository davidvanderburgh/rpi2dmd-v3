"""Clock and date rendering.

Two output forms:

- render_indexed(...): an intensity grid (0..15) used when compositing the
  clock with DMD animations in index space (Run-DMD parity: ClockOnTop /
  ClockBehind at per-animation positions).
- render_scene(...): a full-color RGB frame for the standalone clock scene
  (any style incl. TTF fonts, solid colors, image/GIF backgrounds).

Used identically by the player and the web UI's live preview endpoint.
Python 3.7 / Pillow 5.4 compatible (no ImageDraw stroke_width).
"""

import datetime
import os

from . import pixelfont, rda

FONTS_DIR = "/media/usb/fonts"

FORMATS = {
    "12h":      ("%I:%M", False),
    "12h_ampm": ("%I:%M", True),
    "12h_sec":  ("%I:%M:%S", False),
    "24h":      ("%H:%M", False),
    "24h_sec":  ("%H:%M:%S", False),
}


def time_text(cfg_clock, now=None):
    """-> (main_text, suffix) e.g. ("3:07", "PM") — leading zero stripped
    for 12h formats."""
    now = now or datetime.datetime.now()
    fmt, ampm = FORMATS.get(cfg_clock.get("format", "12h"), FORMATS["12h"])
    text = now.strftime(fmt)
    if fmt.startswith("%I") and text.startswith("0"):
        text = text[1:]
    suffix = now.strftime("%p") if ampm else ""
    return text, suffix


def colon_visible(cfg_clock, now=None):
    mode = cfg_clock.get("colon", "blink")
    if mode == "off":
        return False
    if mode == "on":
        return True
    now = now or datetime.datetime.now()
    return now.second % 2 == 0


def _bitmap_font(cfg_clock, size_hint, colon_on):
    style = cfg_clock.get("style", "rundmd")
    if style == "rundmd":
        f = pixelfont.rundmd_font(size_hint, colon_on)
        if f is not None:
            return f
    return pixelfont.digital_font(size_hint, colon_on)


def _place(canvas_w, canvas_h, text_w, text_h, cfg_clock, override_xy=None):
    if override_xy is not None:
        return override_xy
    align = cfg_clock.get("align", "center")
    ox = int(cfg_clock.get("x", 0))
    oy = int(cfg_clock.get("y", 0))
    if align == "xy":
        return ox, oy
    horiz = {"nw": "w", "w": "w", "sw": "w",
             "ne": "e", "e": "e", "se": "e"}.get(align, "c")
    vert = {"nw": "n", "n": "n", "ne": "n",
            "sw": "s", "s": "s", "se": "s"}.get(align, "c")
    if horiz == "w":
        x = 1
    elif horiz == "e":
        x = canvas_w - text_w - 1
    else:
        x = (canvas_w - text_w) // 2
    if vert == "n":
        y = 1
    elif vert == "s":
        y = canvas_h - text_h - 1
    else:
        y = (canvas_h - text_h) // 2
    return x + ox, y + oy


# Rendering the glyph grid costs thousands of Python ops. During an
# animation it is redrawn for every frame (~30/s) even though the clock
# only changes once a second, which pegged the CPU on a Pi and starved the
# web UI. Cache on everything the output depends on.
_grid_cache = {}
_GRID_CACHE_MAX = 8


def render_indexed(cfg_clock, canvas_w=128, canvas_h=32, now=None,
                   size_hint="large", override_xy=None, shade=None):
    """Render the clock as an intensity grid.

    -> (grid, bbox): grid is list[canvas_h][canvas_w] of 0..15,
    bbox = (x, y, w, h) of the drawn text block. The grid is shared/cached,
    so callers must treat it as read-only.
    """
    now = now or datetime.datetime.now()
    text, suffix = time_text(cfg_clock, now)
    colon_on = colon_visible(cfg_clock, now)
    if shade is None:
        shade = int(cfg_clock.get("shade", 15))

    key = (text, suffix, colon_on, shade, canvas_w, canvas_h, size_hint,
           override_xy, cfg_clock.get("style"), cfg_clock.get("align"),
           cfg_clock.get("x"), cfg_clock.get("y"))
    hit = _grid_cache.get(key)
    if hit is not None:
        return hit

    font = _bitmap_font(cfg_clock, size_hint, colon_on)
    tw, th = font.measure(text)
    sw = 0
    sfont = None
    if suffix:
        sfont = pixelfont.BitmapFont(
            {c: pixelfont.tiny_letter(c) for c in "APM"}, 5)
        sw = sfont.measure(suffix)[0] + 2
    x, y = _place(canvas_w, canvas_h, tw + sw, th, cfg_clock, override_xy)

    level = max(0.0, min(1.0, shade / 15.0))

    grid = [[0] * canvas_w for _ in range(canvas_h)]
    font.draw(grid, text, x, y, scale_level=level)
    if sfont is not None:
        sfont.draw(grid, suffix, x + tw + 2, y + th - 5, scale_level=level)

    result = (grid, (x, y, tw + sw, th))
    if len(_grid_cache) >= _GRID_CACHE_MAX:
        _grid_cache.clear()
        _sparse_cache.clear()   # keyed by id(grid); must not outlive them
    _grid_cache[key] = result
    return result


def grid_to_mask(grid):
    """Intensity grid -> PIL 'L' image (values 0..15)."""
    from PIL import Image
    h = len(grid)
    w = len(grid[0]) if h else 0
    return Image.frombytes(
        "L", (w, h), bytes(bytearray(v for row in grid for v in row)))


# The clock lights only a few hundred of the 4096 pixels, so walking the
# whole canvas per frame was pure waste. Precompute the lit pixels (and the
# outline ring) once per distinct grid.
_sparse_cache = {}


def _grid_sparse(grid):
    """-> (lit, ring): lit = [(i, value)...], ring = [i...] outline pixels."""
    key = id(grid)
    hit = _sparse_cache.get(key)
    if hit is not None:
        return hit
    h = len(grid)
    w = len(grid[0]) if h else 0
    flat = [v for row in grid for v in row]
    lit = [(i, v) for i, v in enumerate(flat) if v]
    litset = set(i for i, _ in lit)
    ring = []
    for i in litset:
        x = i % w
        for nb, ok in ((i - 1, x > 0), (i + 1, x < w - 1),
                       (i - w, i >= w), (i + w, i + w < len(flat))):
            if ok and nb not in litset:
                ring.append(nb)
    ring = list(set(ring))
    if len(_sparse_cache) >= _GRID_CACHE_MAX * 2:
        _sparse_cache.clear()
    _sparse_cache[key] = (lit, ring)
    return lit, ring


def composite_clock_indexed(anim_indexes, grid, mode, outline=False):
    """Composite clock intensity grid with a DMD animation frame, both in
    index space (bytes of 0..15, len = w*h).

    mode: "front" (clock over animation) or "back" (animation over clock,
    clock visible through black animation pixels).
    -> new bytes of indexes.
    """
    out = bytearray(anim_indexes)
    lit, ring = _grid_sparse(grid)
    if mode == "front":
        if outline:
            for i in ring:
                out[i] = 0  # dark halo keeps the digits readable over art
        for i, v in lit:
            out[i] = v
    else:  # back
        for i, v in lit:
            if out[i] == 0:
                out[i] = v
    return bytes(out)


# ---------------------------------------------------------------------------
# Standalone clock scene (RGB)
# ---------------------------------------------------------------------------

def _load_ttf(cfg_clock, fonts_dir):
    from PIL import ImageFont
    name = cfg_clock.get("font", "") or ""
    size = int(cfg_clock.get("font_size", 20))
    candidates = []
    if name:
        candidates.append(os.path.join(fonts_dir, name))
        candidates.append(os.path.join(fonts_dir, "Polices", name))
    candidates.append("/opt/RPI2DMD/arial.ttf")
    candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    for c in candidates:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _text_color(cfg_clock, tint, gamma):
    if cfg_clock.get("color_mode", "tint") == "solid":
        c = cfg_clock.get("color", [255, 140, 0])
        return tuple(int(v) for v in c[:3])
    pal = rda.build_palette(tint, gamma)
    shade = max(1, min(15, int(cfg_clock.get("shade", 15))))
    return tuple(pal[shade * 3: shade * 3 + 3])


def render_scene(cfg_clock, canvas_w=128, canvas_h=32, now=None,
                 background=None, tint=rda.DEFAULT_TINT,
                 gamma=rda.DEFAULT_GAMMA, fonts_dir=FONTS_DIR,
                 draw_outline=None):
    """Full-color clock frame. background: optional PIL RGB image (already
    sized) e.g. a frame of an animated background GIF."""
    from PIL import Image, ImageDraw

    now = now or datetime.datetime.now()
    if background is not None:
        frame = background.convert("RGB")
        if frame.size != (canvas_w, canvas_h):
            frame = frame.resize((canvas_w, canvas_h))
    else:
        frame = Image.new("RGB", (canvas_w, canvas_h))

    style = cfg_clock.get("style", "rundmd")
    if style in ("rundmd", "digital"):
        grid, _ = render_indexed(cfg_clock, canvas_w, canvas_h, now)
        color = _text_color(cfg_clock, tint, gamma)
        px = frame.load()
        for y, row in enumerate(grid):
            for x, v in enumerate(row):
                if v:
                    f = v / 15.0
                    px[x, y] = (int(color[0] * f), int(color[1] * f),
                                int(color[2] * f))
        return frame

    # TTF path
    draw = ImageDraw.Draw(frame)
    text, suffix = time_text(cfg_clock, now)
    if not colon_visible(cfg_clock, now):
        text = text.replace(":", " ")
    if suffix:
        text = text + " " + suffix
    font = _load_ttf(cfg_clock, fonts_dir)
    try:
        x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
        tw, th = x1 - x0, y1 - y0
    except AttributeError:  # Pillow < 8
        tw, th = draw.textsize(text, font=font)
        x0 = y0 = 0
    x, y = _place(canvas_w, canvas_h, tw, th, cfg_clock)
    x -= x0
    y -= y0
    color = _text_color(cfg_clock, tint, gamma)
    outline = cfg_clock.get("outline", True) if draw_outline is None else draw_outline
    if outline and background is not None:
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=color)
    return frame
