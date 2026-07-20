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
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence

from . import clock, paths, rda
from .rgf import StripFrame

CANVAS = (128, 32)
MIN_FRAME_MS = 20
GIF_DEFAULT_FRAME_MS = 100
GIF_MIN_TOTAL_MS = 1500
CLOCK_TICK_MS = 100
# Played GIFs are NOT frame-capped: the whole point is the full animation.
# (MAX_GIF_FRAMES below is only a decompression-bomb backstop.) The prefetch
# queue + paced decode absorb the cost of long clips instead.
PLAYBACK_MAX_GIF_FRAMES = None

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

def _display(cfg, game=None):
    """-> (tint, gamma) from the display config.

    When a game is given and dmd.tint_mode is "per_game", the machine's real
    display color wins (Williams/Bally 90s plasma = amber, Stern LED = red),
    falling back to display.tint for games with no mapping.
    """
    tint = cfg.get("display.tint", rda.DEFAULT_TINT)
    if game and cfg.get("dmd.tint_mode", "per_game") == "per_game":
        mapped = (cfg.get("dmd.game_tints", {}) or {}).get(game)
        if mapped is None:
            mapped = rda.GAME_TINTS.get(game)
        if mapped:
            tint = mapped
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


# A 128x32 panel never needs huge sources; reject decompression bombs and
# keep memory bounded on a Pi (frames are downscaled as they are decoded).
Image.MAX_IMAGE_PIXELS = 32 * 1024 * 1024
# Pure decompression-bomb backstop, set above the real library's longest
# clip (PINBALL_STORY_STTNG01 = 4958 frames) so no genuine animation is ever
# truncated. A 128x32 RGB frame is ~12KB, so even this is bounded memory.
MAX_GIF_FRAMES = 8000


class DecodeBudgetExceeded(Exception):
    """A decode outran its wall-time budget (prefetch skips the file)."""


def _load_image_frames(path, target=None, max_frames=None,
                       pace_every=0, pace_s=0.0, abort_after_s=0.0):
    """Load a (possibly animated) image -> list of (RGB image, duration_ms).

    Frames are composited over the previous frame so partial/transparent
    GIF frames render correctly. When target=(w, h) is given, each frame is
    cover-fitted to that size as it is decoded, so a large source GIF never
    holds full-resolution frames in memory.

    pace_every/pace_s: voluntary sleep every N decoded frames — used by the
    prefetcher so a long decode never monopolizes the GIL while the clock's
    render thread is trying to hit its second boundary.

    abort_after_s: raise DecodeBudgetExceeded once the decode has run this
    long (0 = no budget). A monster GIF can take a minute+ on the Pi; with
    a single decode worker that stalls every animation behind it, so the
    prefetcher bounds each attempt and moves on.
    """
    limit = max_frames or MAX_GIF_FRAMES
    t0 = time.time()
    img = Image.open(path)
    frames = []
    base = None
    for frame in ImageSequence.Iterator(img):
        if abort_after_s and time.time() - t0 > abort_after_s:
            raise DecodeBudgetExceeded(
                "%s: %d frames decoded in %.1fs, budget %.0fs"
                % (os.path.basename(path), len(frames),
                   time.time() - t0, abort_after_s))
        dur = frame.info.get("duration", GIF_DEFAULT_FRAME_MS)
        try:
            dur = max(MIN_FRAME_MS, int(dur))
        except (TypeError, ValueError):
            dur = GIF_DEFAULT_FRAME_MS
        fr = frame.convert("RGBA")
        if base is None:
            base = Image.new("RGBA", img.size, (0, 0, 0, 255))
        base.paste(fr, (0, 0), fr)
        out = base.convert("RGB")
        if target is not None:
            out = _fit_cover(out, target[0], target[1])
        frames.append((out, dur))
        if len(frames) >= limit:
            break
        if pace_every and len(frames) % pace_every == 0:
            time.sleep(pace_s)
    if not frames:
        out = img.convert("RGB")
        if target is not None:
            out = _fit_cover(out, target[0], target[1])
        frames = [(out, GIF_DEFAULT_FRAME_MS)]
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

def _clock_overlay(ck, w, h, tint, gamma, now=None, outline=False):
    """The clock text as (color layer, alpha mask) built from the cached
    intensity grid. Rebuilding this costs real CPU, but the text only
    changes once a second — callers cache it on (text, suffix, colon).

    outline: bake the 1px black halo INTO the layer/alpha (dilated alpha,
    black ring pixels). The halo used to cost 4 paste ops on every frame
    of an animated background; baked here it costs a few ops once per
    cached state instead."""
    grid, _ = clock.render_indexed(ck, w, h, now=now)
    color = clock._text_color(ck, tint, gamma)
    alpha = Image.frombytes(
        "L", (w, h), bytes(bytearray(v * 17 for row in grid for v in row)))
    layer = Image.new("RGB", (w, h), color)
    if outline:
        dil = alpha
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            dil = ImageChops.lighter(dil, ImageChops.offset(alpha, dx, dy))
        ring = Image.new("RGB", (w, h))          # black
        ring.paste(layer, (0, 0), alpha)         # digits keep their color
        layer, alpha = ring, dil
    return layer, alpha


def clock_scene(cfg, canvas=CANVAS, dwell_ms=None, backgrounds=None, rng=None,
                time_fn=None, extend_while=None, extend_cap_ms=120000):
    """Standalone clock for dwell_ms.

    The text layer is rebuilt only when the displayed time actually changes
    (once a second), and each frame's hold lands exactly on the next second
    boundary — a 10Hz full re-render used to miss beats on the Pi and made
    the colon blink irregular. Animated backgrounds advance per their own
    frame durations; the cached text is pasted over them (C-speed).

    time_fn: the scheduler's clock (simulated under --fast).
    extend_while: keep showing the clock past dwell while this returns True
    (the scheduler uses it to wait for the next animation's prefetch, so the
    panel never goes black between clock and animation). The cap is a
    safety net against a wedged prefetch, set high on purpose: a huge GIF
    can legitimately take a minute to decode on the Pi, and a ticking
    clock beats a frozen panel for the whole of it (measured 45s frozen
    with the old 15s cap when the scene fell back to a synchronous load).
    """
    rng = rng or random
    time_fn = time_fn or time.time
    w, h = canvas
    ck = cfg["clock"]
    dwell = int(dwell_ms if dwell_ms is not None
                else ck.get("idle_dwell_ms", 6000))
    tint, gamma = _display(cfg)

    bg_frames = None
    bg_path = _resolve_background(ck, backgrounds, rng)
    if bg_path:
        try:
            bg_frames = _load_image_frames(bg_path, target=(w, h))
        except (OSError, ValueError, Image.DecompressionBombError):
            bg_frames = None

    outline = bool(ck.get("outline", True)) and bg_frames is not None
    black = Image.new("RGB", (w, h))

    elapsed = 0
    idx = 0
    remaining = bg_frames[0][1] if bg_frames else 0
    overlay_lru = {}          # (text, suffix, colon) -> (layer, alpha):
                              # blink alternates 2 states/minute, so per-run
                              # caching kills the per-second rebuild
    layer = alpha = None
    frame = None
    frame_key = None          # (overlay state, bg frame) the frame shows
    extended = 0
    while True:
        if elapsed >= dwell:
            if extend_while is None or extended >= extend_cap_ms \
                    or not extend_while():
                break
        # Frames are aimed at second boundaries, but wake lag can put
        # generation a hair *before* the boundary the frame will display
        # on — sampling the clock right here then showed a stale beat
        # (skipped flip + 20ms double-blink). Sample one MIN_FRAME ahead
        # so a near-boundary frame carries the upcoming second's state.
        now_dt = datetime.datetime.now() \
            + datetime.timedelta(milliseconds=MIN_FRAME_MS)
        text, suffix = clock.time_text(ck, now_dt)
        colon = clock.colon_visible(ck, now_dt)
        key = (text, suffix, colon)
        hit = overlay_lru.get(key)
        if hit is None:
            if len(overlay_lru) >= 8:     # minute rollovers during a long
                overlay_lru.clear()       # extend; keys never mutate
            hit = _clock_overlay(ck, w, h, tint, gamma, now=now_dt,
                                 outline=outline)
            overlay_lru[key] = hit
        layer, alpha = hit

        # Recompose only when the visible content actually changed (new
        # colon/text state or a new background frame): the composed frame
        # is immutable downstream, so yielding the same image again is
        # free — the no-background clock now costs zero PIL ops between
        # second boundaries.
        want = (key, idx if bg_frames else -1)
        if frame is None or want != frame_key:
            base = bg_frames[idx][0] if bg_frames else black
            frame = base.copy()
            frame.paste(layer, (0, 0), alpha)
            frame_key = want

        hold = min(remaining, CLOCK_TICK_MS) if bg_frames else 1000

        # land the next frame exactly on the second so the colon flips on
        # the beat (and the minute rolls over on time)
        to_second = int((1.0 - (time_fn() % 1.0)) * 1000) + 1
        hold = min(hold, to_second)
        if elapsed < dwell:
            hold = min(hold, dwell - elapsed)
        hold = max(MIN_FRAME_MS, hold)
        yield frame, hold
        elapsed += hold
        if elapsed > dwell:
            extended += hold
        if bg_frames:
            remaining -= hold
            if remaining <= 0:
                idx = (idx + 1) % len(bg_frames)
                remaining = bg_frames[idx][1]


def clock_still(cfg, canvas=CANVAS):
    """One clock frame, no background. Used while the web UI is in use: the
    panel still shows the time, at a cost of one render per second."""
    w, h = canvas
    tint, gamma = _display(cfg)
    return clock.render_scene(cfg["clock"], w, h, tint=tint, gamma=gamma,
                              fonts_dir=paths.fonts_dir())


# ---------------------------------------------------------------------------
# DMD animation scene
# ---------------------------------------------------------------------------

def _clock_beat(ck, time_fn):
    """-> (now_dt, to_change_ms): the clock state to render right now and
    the milliseconds until that state next changes.

    Sampled one MIN_FRAME ahead (same guard as clock_scene) so a frame
    generated a hair before a boundary carries the state it will actually
    display in. The state changes every second while the colon blinks (or
    seconds are shown), else at the minute rollover.
    """
    t = time_fn() + MIN_FRAME_MS / 1000.0
    per_second = ck.get("colon", "blink") == "blink" \
        or "sec" in str(ck.get("format", "12h"))
    step = 1.0 if per_second else 60.0
    to_change = int((step - (t % step)) * 1000) + 1
    return datetime.datetime.fromtimestamp(t), to_change


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
    # Small clocks anchor at metadata x/y (top-left of the digits); large
    # ones are full-screen faces whose raw x/y are always 0 — centered.
    if size_hint == "small":
        override_xy = (int(meta.get("x", 0)), int(meta.get("y", 0)))
    else:
        override_xy = None
    start = max(0, int(meta.get("start_frame", 0)))
    end = int(meta.get("end_frame", 0))
    if end <= 0 or end < start:
        # 0 = "until the animation ends" (B237 sentinel); end<start occurs
        # on 3 animations as an extractor artifact — show rather than drop
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


# bytes.translate table: transparency index -> opaque mask value, else 0.
# Turns a frame's index buffer into an L-mode "clock may show through here"
# mask with one C call (a Python pixel loop costs ~10ms/frame on the Pi).
_TRANSP_MASK = bytes(255 if i == rda.TRANSPARENT else 0 for i in range(256))


def materialize_dmd(cfg, header, indexes, slab_frames=16, pace_s=0.0):
    """Pre-render RDA frames to StripFrame windows, worker-side.

    -> (strips, masks) or None when strips can't be used at all
    (name-during mode pastes a per-frame layer).

    strips: list aligned with `indexes`, a StripFrame per frame — the
    render loop blits non-overlay frames directly (zero PIL ops) and
    builds overlay frames as realize()+paste over the pre-rendered base
    instead of the classic composite+frombytes+putpalette+convert chain.

    masks: only for ClockBehind anims — an L image per overlay-window
    frame marking the B237 transparency pixels the clock shows through;
    None otherwise / outside the window.

    Slabs of `slab_frames`, exactly like RgfClip.materialize(): one
    whole-clip convert is a single C call that can hold the GIL for
    seconds on the Pi; 32-frame slabs bound every hold to tens of ms.
    pace_s: voluntary sleep between slabs (prefetch worker politeness).
    """
    if cfg.get("playback.show_name", "hide") == "during":
        return None
    w, h = rda.WIDTH, rda.HEIGHT
    mode, _, _, start, end = _resolve_overlay(cfg, header)
    tint, gamma = _display(cfg, game=header.get("game"))
    palette = rda.build_palette(tint, gamma)
    n = len(indexes)
    strips = [None] * n
    for s0 in range(0, n, slab_frames):
        s1 = min(s0 + slab_frames, n)
        raw = b"".join(rda.flatten_transparency(indexes[i])
                       for i in range(s0, s1))
        slab = Image.frombytes("P", (w, h * (s1 - s0)), raw)
        slab.putpalette(palette)
        slab = slab.convert("RGB")
        slab.load()
        for i in range(s0, s1):
            strips[i] = StripFrame(slab, (i - s0) * h, w, h)
        if pace_s:
            time.sleep(pace_s)
    masks = None
    if mode == "back":
        masks = [None] * n
        for k, i in enumerate(range(start, min(end + 1, n))):
            masks[i] = Image.frombytes(
                "L", (w, h), bytes(indexes[i]).translate(_TRANSP_MASK))
            if pace_s and k % 16 == 15:
                time.sleep(pace_s)
    return strips, masks


def _clock_patch(ck_ov, palette, now_dt, size_hint, override_xy, outline):
    """The overlay clock as an (RGB layer, L alpha) pair, built from the
    cached intensity grid with index-composite semantics: digit pixels
    REPLACE art with palette[v] (alpha 255, not blended), and the outline
    ring is opaque black. Costs a few ms once per clock state; the old
    per-segment index composite + frombytes + putpalette + convert chain
    cost ~40ms on every animation frame inside the overlay window."""
    grid, _ = clock.render_indexed(ck_ov, rda.WIDTH, rda.HEIGHT, now=now_dt,
                                   size_hint=size_hint,
                                   override_xy=override_xy)
    lit, ring = clock._grid_sparse(grid)
    npx = rda.WIDTH * rda.HEIGHT
    layer_b = bytearray(npx * 3)
    alpha_b = bytearray(npx)
    for i, v in lit:
        alpha_b[i] = 255
        layer_b[3 * i:3 * i + 3] = palette[3 * v:3 * v + 3]
    if outline:
        for i in ring:
            alpha_b[i] = 255          # layer stays black there
    layer = Image.frombytes("RGB", (rda.WIDTH, rda.HEIGHT), bytes(layer_b))
    alpha = Image.frombytes("L", (rda.WIDTH, rda.HEIGHT), bytes(alpha_b))
    return layer, alpha


def dmd_scene(cfg, rda_path, header=None, frames=None, canvas=CANVAS,
              indexes=None, time_fn=None, strips=None, masks=None):
    """Play an RDA animation honoring per-frame durations and the clock
    overlay rules (playback.clock_overlay / per-animation metadata).

    indexes: optional pre-unpacked frames (the prefetcher supplies these so
    the render loop does not pay for nibble expansion per frame).

    strips/masks: optional materialize_dmd() output. Non-overlay frames
    yield their StripFrame directly (driver blits it with one clipped
    paste; the full 3-PIL-op per-frame rebuild was the reason short-hold
    RDA anims could never keep up on the Pi). Overlay-window frames build
    as realize() + one paste of a cached RGB clock patch — ClockBehind
    pastes through the frame's pre-computed transparency mask.

    time_fn: wall clock for landing the overlay clock's colon flip on the
    second even inside a long animation frame — the frame's hold is split
    at the beat and the overlay recomposited (total duration unchanged, so
    animation pacing is untouched). None (tests, --fast) = never split;
    library-wide the flip would otherwise run ~109ms late on average and
    can miss a whole beat on long holds (max 2200ms).
    """
    if header is None or frames is None:
        header, frames = rda.read_rda(rda_path)
    w, h = canvas
    ck = cfg["clock"]
    tint, gamma = _display(cfg, game=header.get("game"))
    palette = rda.build_palette(tint, gamma)
    mode, size_hint, override_xy, start, end = _resolve_overlay(cfg, header)
    outline = bool(cfg.get("clock.outline", True))

    name_layer = None
    if cfg.get("playback.show_name", "hide") == "during":
        name_layer = _name_overlay(
            header.get("name", "").replace("_", " "), w, h)

    # The overlay clock is placed by animation metadata (small) or centered
    # (large full-screen faces) — never by the user's standalone clock-scene
    # align/x/y, which used to drag the overlay around on every ClockLarge
    # animation. Style/font/shade/colon settings still apply.
    ck_ov = dict(ck)
    ck_ov["align"] = "center"
    ck_ov["x"] = 0
    ck_ov["y"] = 0

    if strips is not None and ((rda.WIDTH, rda.HEIGHT) != (w, h)
                               or name_layer is not None):
        strips = None    # strip windows are native-size, no per-frame paste

    patch_lru = {}       # clock state -> (layer, alpha); blink alternates
                         # 2 states/minute so this amortizes to nothing

    durations = header.get("durations", [])
    for i, packed in enumerate(frames):
        dur = durations[i] if i < len(durations) else GIF_DEFAULT_FRAME_MS
        left = max(MIN_FRAME_MS, int(dur))
        overlay = mode is not None and start <= i <= end
        sf = strips[i] if strips is not None else None
        if sf is not None and not overlay:
            # pre-rendered worker-side: zero PIL ops in the render loop
            yield sf, left
            continue
        mask = masks[i] if masks is not None else None
        if sf is not None and (mode == "front" or mask is not None):
            # overlay over the pre-rendered base: one crop + one paste
            # per segment, patch cached per clock state
            while True:
                now_dt = to_change = None
                if time_fn is not None:
                    now_dt, to_change = _clock_beat(ck, time_fn)
                sample = now_dt or datetime.datetime.now()
                text, suffix = clock.time_text(ck_ov, sample)
                pk = (text, suffix, clock.colon_visible(ck_ov, sample))
                patch = patch_lru.get(pk)
                if patch is None:
                    if len(patch_lru) >= 8:
                        patch_lru.clear()
                    patch = _clock_patch(ck_ov, palette, now_dt, size_hint,
                                         override_xy,
                                         outline and mode == "front")
                    patch_lru[pk] = patch
                img = sf.realize()
                a = patch[1] if mask is None \
                    else ImageChops.darker(patch[1], mask)
                img.paste(patch[0], (0, 0), a)
                if to_change is not None \
                        and MIN_FRAME_MS <= to_change <= left - MIN_FRAME_MS:
                    yield img, to_change
                    left -= to_change
                    continue
                yield img, left
                break
            continue
        base = indexes[i] if indexes is not None else rda.unpack_frame(packed)
        while True:
            now_dt = to_change = None
            if overlay and time_fn is not None:
                now_dt, to_change = _clock_beat(ck, time_fn)
            idx = base
            if overlay:
                # composite copies `base`; each segment must start from the
                # pristine frame — 'back' mode fills transparency pixels
                # and 'front' burns an outline, so recompositing over the
                # previous segment's output would bake in the old colon
                grid, _ = clock.render_indexed(
                    ck_ov, rda.WIDTH, rda.HEIGHT, now=now_dt,
                    size_hint=size_hint, override_xy=override_xy)
                idx = clock.composite_clock_indexed(
                    idx, grid, mode, outline=(outline and mode == "front"))
            # B237 transparency (index 10) renders as black once the clock
            # has been composited into it — it used to draw at 67%
            # brightness, washing whole backgrounds in amber
            idx = rda.flatten_transparency(idx)
            img = Image.frombytes("P", (rda.WIDTH, rda.HEIGHT), idx)
            img.putpalette(palette)
            img = img.convert("RGB")
            if (rda.WIDTH, rda.HEIGHT) != (w, h):
                img = img.resize((w, h), Image.NEAREST)
            if name_layer is not None:
                img.paste(name_layer, (0, 0), name_layer)
            if to_change is not None \
                    and MIN_FRAME_MS <= to_change <= left - MIN_FRAME_MS:
                yield img, to_change
                left -= to_change
                continue
            yield img, left
            break


# ---------------------------------------------------------------------------
# GIF scene
# ---------------------------------------------------------------------------

def load_gif_frames(path, canvas=CANVAS, max_frames=None,
                    pace_every=0, pace_s=0.0, abort_after_s=0.0):
    """Decode a GIF to canvas-sized frames. Slow on a Pi (big GIFs take
    seconds), so the scheduler prefetches this off the critical path."""
    if max_frames is None:
        max_frames = PLAYBACK_MAX_GIF_FRAMES
    return _load_image_frames(path, target=canvas, max_frames=max_frames,
                              pace_every=pace_every, pace_s=pace_s,
                              abort_after_s=abort_after_s)


def gif_scene(cfg, path, canvas=CANVAS, frames=None, time_fn=None):
    """Play a GIF file: per-frame durations, cover-scale to the canvas,
    single pass (looped until >= 1.5s total), optional clock overlay.

    time_fn: as in dmd_scene — lands the overlay clock's colon flip on the
    beat by splitting a frame's hold (GIF holds are unbounded, so without
    this a long hold freezes the colon for its whole duration)."""
    w, h = canvas
    if frames is None:
        frames = _load_image_frames(path, target=(w, h),
                                    max_frames=PLAYBACK_MAX_GIF_FRAMES)
    # RgfClip exposes total_ms so we never decompress the whole clip just
    # to sum durations; plain frame lists are summed as before
    total = getattr(frames, "total_ms", None)
    if total is None:
        total = sum(dur for _, dur in frames)
    passes = 1
    if total > 0:
        while total * passes < GIF_MIN_TOTAL_MS:
            passes += 1
    overlay = bool(cfg.get("playback.gif_clock_overlay", False))
    ck = cfg["clock"]
    for _ in range(passes):
        for img, dur in frames:
            left = dur
            while True:
                out = img
                now_dt = to_change = None
                if overlay:
                    if time_fn is not None:
                        now_dt, to_change = _clock_beat(ck, time_fn)
                    # copy for drawing; StripFrame realizes to a PIL image
                    out = img.realize() if hasattr(img, "realize") \
                        else img.convert("RGB")
                    _draw_clock_rgb(out, cfg, outline=True, now=now_dt)
                if to_change is not None \
                        and MIN_FRAME_MS <= to_change <= left - MIN_FRAME_MS:
                    yield out, to_change
                    left -= to_change
                    continue
                yield out, left
                break


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


def no_network_scene(cfg, canvas=CANVAS):
    """Shown at startup when the device has no IP: without this the panel
    looks perfectly healthy while the web UI is silently unreachable."""
    w, h = canvas
    lines = ["NO NETWORK", "set Wifi_ssid + Wifi_psk", "in config/config.txt"]
    color = _bright_color(cfg)
    font = _load_ttf(None, 8)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    heights = [_measure(d, ln, font) for ln in lines]
    total = sum(m[1] for m in heights) + (len(lines) - 1)
    y = max(0, (h - total) // 2)
    for ln, (tw, th, ox, oy) in zip(lines, heights):
        d.text(((w - tw) // 2 - ox, y - oy), ln, font=font, fill=color)
        y += th + 1
    yield img, 6000


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
                frames = _load_image_frames(path, target=(w, h))
            except (OSError, ValueError, Image.DecompressionBombError):
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
