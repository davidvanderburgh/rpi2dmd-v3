"""Verify: colon flips land on second boundaries; clock extends while the
prefetch is not ready; render work is ~1/sec not 10/sec."""
import os
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-blink")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import clock, config, scenes  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


cfg = config.Config()
cfg.data = config.deep_merge(config.DEFAULTS, {
    "clock": {"style": "digital", "format": "12h", "colon": "blink",
              "background": "none"}})

# fake clock we advance by each frame's hold
state = {"t": 1000.35}   # deliberately off-boundary start


def fake_time():
    return state["t"]


# 1. every frame boundary after the first lands exactly on a whole second
sc = scenes.clock_scene(cfg, (128, 32), dwell_ms=6000, backgrounds=[],
                        time_fn=fake_time)
boundaries = []
for frame, hold in sc:
    state["t"] += hold / 1000.0
    boundaries.append(state["t"])
on_second = [abs(b - round(b)) < 0.005 for b in boundaries[:-1]]  # last is dwell-capped
check("frame boundaries land on whole seconds",
      all(on_second), ["%.3f" % b for b in boundaries])
check("~1 frame per second (not 10)", len(boundaries) <= 8, len(boundaries))

# 2. render work is cached: count overlay rebuilds via the grid cache
clock._grid_cache.clear()
state["t"] = 2000.2
sc = scenes.clock_scene(cfg, (128, 32), dwell_ms=5000, backgrounds=[],
                        time_fn=fake_time)
n = 0
for frame, hold in sc:
    state["t"] += hold / 1000.0
    n += 1
# 5s of 12h blink-mode clock: text changes 1/sec (colon), so <= ~7 distinct
# grid renders (cache misses), even though more frames could be yielded
check("overlay rebuilt ~once per second", len(clock._grid_cache) <= 8,
      len(clock._grid_cache))

# 3. extend_while keeps the clock alive past dwell, then stops
state["t"] = 3000.0
countdown = {"n": 3}


def not_ready():
    countdown["n"] -= 1
    return countdown["n"] > 0


sc = scenes.clock_scene(cfg, (128, 32), dwell_ms=1000, backgrounds=[],
                        time_fn=fake_time, extend_while=not_ready)
total = 0
for frame, hold in sc:
    state["t"] += hold / 1000.0
    total += hold
check("clock extended past dwell while prefetch not ready", total > 1000, total)
check("clock stopped once ready", countdown["n"] <= 0, countdown["n"])

# 4. extend cap: never extends forever
state["t"] = 4000.0
sc = scenes.clock_scene(cfg, (128, 32), dwell_ms=500, backgrounds=[],
                        time_fn=fake_time, extend_while=lambda: True,
                        extend_cap_ms=3000)
total = 0
for frame, hold in sc:
    state["t"] += hold / 1000.0
    total += hold
check("extension capped", 500 < total <= 5000, total)

# 5. animated background still advances between second boundaries
from PIL import Image  # noqa: E402
bg = os.path.join(os.environ["RPI2DMD_MEDIA"], "fonts")
gifs = [os.path.join(bg, f) for f in os.listdir(bg)
        if f.startswith("pattern_animated")]
if gifs:
    cfg.data["clock"]["background"] = os.path.basename(gifs[0])
    state["t"] = 5000.0
    sc = scenes.clock_scene(cfg, (128, 32), dwell_ms=2000,
                            backgrounds=[gifs[0]], time_fn=fake_time)
    frames = []
    for frame, hold in sc:
        state["t"] += hold / 1000.0
        frames.append((frame.tobytes(), hold))
    distinct = len(set(f for f, _ in frames))
    check("animated background produces distinct frames", distinct > 2, distinct)
    check("bg frames hold <= tick", all(h <= 1000 for _, h in frames))

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("BLINK/EXTEND TESTS PASSED")
