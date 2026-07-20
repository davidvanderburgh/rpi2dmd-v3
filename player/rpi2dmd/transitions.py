"""Clock scene enter/exit transitions (Run-DMD CLK TRANSITION parity).

wrap() adapts a frame-producing scene generator: it slides the first frame
in and the last frame out (vertical offset over ~300ms) or fades them,
per clock.transition: random|up_up|down_down|up_down|down_up|fade|none.

Python 3.7 / Pillow 5.4 compatible.
"""

import random

from PIL import Image, ImageEnhance

MODES = ["up_up", "down_down", "up_down", "down_up", "fade", "none"]
STEPS = 9
STEP_MS = 33


def _resolve(mode, rng):
    """-> (enter, exit) each one of 'up' | 'down' | 'fade' | None."""
    if mode == "random":
        mode = rng.choice([m for m in MODES if m != "none"])
    if mode == "none":
        return None, None
    if mode == "fade":
        return "fade", "fade"
    parts = mode.split("_")
    if len(parts) == 2 and parts[0] in ("up", "down") \
            and parts[1] in ("up", "down"):
        return parts[0], parts[1]
    return None, None


def _transition_frames(img, kind, entering, steps=STEPS, step_ms=STEP_MS):
    """Yield the transition frames for one edge of a scene.

    kind 'up': the frame slides upward (enters from the bottom edge,
    exits through the top). 'down' is the mirror. 'fade' scales
    brightness.
    """
    w, h = img.size
    for i in range(1, steps + 1):
        vis = i / float(steps) if entering else 1.0 - i / float(steps)
        if kind == "fade":
            out = ImageEnhance.Brightness(img).enhance(vis)
        else:
            mag = int(round((1.0 - vis) * h))
            if kind == "up":
                off = mag if entering else -mag
            else:
                off = -mag if entering else mag
            out = Image.new("RGB", (w, h))
            out.paste(img, (0, off))
        yield out, step_ms


def wrap(scene, mode, rng=None):
    """Generator adapter adding enter/exit transitions around a scene.

    Frames must be passed through the moment the scene yields them: scenes
    time their content and holds off the wall clock at generation (the clock
    lands its colon flip on the second boundary), so holding a frame back to
    peek at the next one displays every frame one slot late and turns the
    scheduler's deadline chain into an undamped oscillator — the colon
    blink cycled 877/986/1137ms forever. Only the last *image* is kept
    (for the exit transition), never an undisplayed frame.
    """
    rng = rng or random
    enter, exit_ = _resolve(mode, rng)
    it = iter(scene)
    try:
        first = next(it)
    except StopIteration:
        return
    if enter is not None:
        for f in _transition_frames(first[0], enter, True):
            yield f
    yield first
    last_img = first[0]
    for item in it:
        yield item
        last_img = item[0]
    if exit_ is not None:
        for f in _transition_frames(last_img, exit_, False):
            yield f
