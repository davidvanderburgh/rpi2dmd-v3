"""Built-in pixel fonts for the clock.

Two sources:
- "digital": programmatic 7-segment style digits (always available),
  generated at any size from segment rectangles — the classic LED clock look.
- "rundmd": authentic glyphs recovered from the Run-DMD firmware image,
  loaded from assets/glyphs.json when present.

Glyphs are intensity bitmaps: dict with "width", "height", "rows" where
rows is a list of bytes-like (list of ints 0..15 per pixel).

Python 3.7 compatible, stdlib only.
"""

import json
import os

_ASSETS = os.path.join(os.path.dirname(__file__), "assets")

# 7-segment membership per digit: segments A..G
#   A = top, B = top-right, C = bottom-right, D = bottom,
#   E = bottom-left, F = top-left, G = middle
_SEGMENTS = {
    "0": "ABCDEF",
    "1": "BC",
    "2": "ABDEG",
    "3": "ABCDG",
    "4": "BCFG",
    "5": "ACDFG",
    "6": "ACDEFG",
    "7": "ABC",
    "8": "ABCDEFG",
    "9": "ABCDFG",
}

# tiny 3x5 letters for AM/PM suffix
_TINY = {
    "A": ["010", "101", "111", "101", "101"],
    "P": ["110", "101", "110", "100", "100"],
    "M": ["101", "111", "111", "101", "101"],
}


def _blank(w, h):
    return [[0] * w for _ in range(h)]


def seven_segment_digit(ch, width=10, height=16, stroke=2, level=15):
    """Render one 7-segment digit as an intensity bitmap."""
    if ch not in _SEGMENTS:
        return {"width": width, "height": height, "rows": _blank(width, height)}
    segs = _SEGMENTS[ch]
    g = _blank(width, height)
    t = max(1, int(stroke))
    mid_top = (height - t) // 2
    inset = 1  # horizontal segments inset so corners read cleanly

    def hbar(y0):
        for y in range(y0, min(y0 + t, height)):
            for x in range(inset, width - inset):
                g[y][x] = level

    def vbar(x0, y0, y1):
        for y in range(y0, y1):
            for x in range(x0, min(x0 + t, width)):
                g[y][x] = level

    if "A" in segs:
        hbar(0)
    if "G" in segs:
        hbar(mid_top)
    if "D" in segs:
        hbar(height - t)
    if "F" in segs:
        vbar(0, 1, mid_top + t - 1)
    if "B" in segs:
        vbar(width - t, 1, mid_top + t - 1)
    if "E" in segs:
        vbar(0, mid_top + 1, height - 1)
    if "C" in segs:
        vbar(width - t, mid_top + 1, height - 1)
    return {"width": width, "height": height, "rows": g}


def colon_glyph(height=16, stroke=2, level=15, on=True):
    w = max(2, stroke)
    g = _blank(w, height)
    if on:
        yq = height // 4
        for dy in range(stroke):
            for x in range(w):
                if 0 <= yq + dy < height:
                    g[yq + dy][x] = level
                if 0 <= height - 1 - yq - dy < height:
                    g[height - 1 - yq - dy][x] = level
    return {"width": w, "height": height, "rows": g}


def tiny_letter(ch, level=15):
    pat = _TINY.get(ch.upper())
    if pat is None:
        return {"width": 3, "height": 5, "rows": _blank(3, 5)}
    rows = [[level if c == "1" else 0 for c in line] for line in pat]
    return {"width": 3, "height": 5, "rows": rows}


def space_glyph(width, height):
    return {"width": width, "height": height, "rows": _blank(width, height)}


class BitmapFont(object):
    """A set of glyphs at a fixed height, with rendering helpers."""

    def __init__(self, glyphs, height, letter_spacing=1):
        self.glyphs = glyphs
        self.height = height
        self.letter_spacing = letter_spacing

    def glyph(self, ch):
        return self.glyphs.get(ch)

    def measure(self, text):
        w = 0
        for i, ch in enumerate(text):
            gl = self.glyph(ch)
            if gl is None:
                continue
            w += gl["width"]
            if i + 1 < len(text):
                w += self.letter_spacing
        return w, self.height

    def draw(self, target, text, x0, y0, scale_level=1.0):
        """Draw text into target (2-D list of ints), clipping to bounds."""
        th = len(target)
        tw = len(target[0]) if th else 0
        x = x0
        for ch in text:
            gl = self.glyph(ch)
            if gl is None:
                continue
            rows = gl["rows"]
            for gy in range(gl["height"]):
                ty = y0 + gy
                if ty < 0 or ty >= th:
                    continue
                row = rows[gy]
                for gx in range(gl["width"]):
                    v = row[gx]
                    if not v:
                        continue
                    tx = x + gx
                    if 0 <= tx < tw:
                        nv = int(round(v * scale_level))
                        if nv > target[ty][tx]:
                            target[ty][tx] = nv
            x += gl["width"] + self.letter_spacing
        return x


def digital_font(size="large", colon_on=True):
    """7-segment font. large: 10x16 digits; small: 6x9 digits."""
    if size == "large":
        w, h, t = 10, 16, 2
    else:
        w, h, t = 6, 9, 1
    glyphs = {}
    for d in "0123456789":
        glyphs[d] = seven_segment_digit(d, w, h, t)
    glyphs[":"] = colon_glyph(h, t, on=colon_on)
    glyphs[" "] = space_glyph(max(2, w // 3), h)
    for ch in "APM":
        glyphs[ch] = tiny_letter(ch)
    return BitmapFont(glyphs, h)


_rundmd_cache = {}
_fallback_noted = [False]


def rundmd_font(size="large", colon_on=True):
    """Authentic Run-DMD glyphs from assets/glyphs.json, or None if absent.

    The Run-DMD clock digits live in the original board's MCU firmware,
    not on its SD card, so unless a glyphs.json has been produced some
    other way this returns None and style "rundmd" renders with the
    built-in 7-segment font (visually equivalent to style "digital").
    """
    key = (size, colon_on)
    if key in _rundmd_cache:
        return _rundmd_cache[key]
    path = os.path.join(_ASSETS, "glyphs.json")
    font = None
    if not os.path.exists(path) and not _fallback_noted[0]:
        _fallback_noted[0] = True
        import sys
        sys.stderr.write(
            "rpi2dmd: no assets/glyphs.json; clock style 'rundmd' uses the "
            "built-in 7-segment digits\n")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            setname = "large" if size == "large" else "small"
            gset = data["sets"].get(setname) or next(iter(data["sets"].values()))
            height = int(gset["height"])
            glyphs = {}
            for ch, gd in gset["glyphs"].items():
                rows = [[int(c, 16) for c in r] for r in gd["rows"]]
                glyphs[ch] = {"width": int(gd["width"]),
                              "height": len(rows), "rows": rows}
            if ":" in glyphs and not colon_on:
                w = glyphs[":"]["width"]
                glyphs[":"] = space_glyph(w, height)
            if " " not in glyphs:
                glyphs[" "] = space_glyph(3, height)
            for ch in "APM":
                if ch not in glyphs:
                    glyphs[ch] = tiny_letter(ch)
            font = BitmapFont(glyphs, height)
        except (ValueError, KeyError, OSError):
            font = None
    _rundmd_cache[key] = font
    return font
