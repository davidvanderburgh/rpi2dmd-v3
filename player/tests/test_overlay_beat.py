"""Verify: the clock overlay's colon flips on the beat inside long
animation frames (dmd_scene hold-splitting), total animation duration is
preserved, and the split never fires when it shouldn't."""
import os
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-beat")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import config, scenes  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


def make_cfg(**clock_over):
    ck = {"style": "digital", "format": "12h", "colon": "blink",
          "background": "none"}
    ck.update(clock_over)
    cfg = config.Config()
    cfg.data = config.deep_merge(config.DEFAULTS, {"clock": ck})
    return cfg


def make_header(durations):
    return {"name": "TEST_001", "game": "TEST", "width": 128, "height": 32,
            "num_frames": len(durations), "durations": list(durations),
            "clock": {"type": "ClockOnTop", "size": "ClockLarge",
                      "x": 0, "y": 0, "start_frame": 0, "end_frame": 0}}


def run_scene(durations, t0, cfg=None, time_fn_none=False):
    """Drive dmd_scene like play_scene does (advance fake clock by each
    hold). -> list of (t_at_show, img_bytes, hold)."""
    cfg = cfg or make_cfg()
    n = len(durations)
    frames = [b""] * n
    indexes = [bytes(128 * 32)] * n           # black animation frames
    state = {"t": t0}
    sc = scenes.dmd_scene(cfg, None, header=make_header(durations),
                          frames=frames, indexes=indexes,
                          time_fn=None if time_fn_none else
                          (lambda: state["t"]))
    out = []
    for img, hold in sc:
        out.append((state["t"], img.tobytes(), hold))
        state["t"] += hold / 1000.0
    return out


T0 = 1750000000.35          # realistic epoch, off-boundary start

# 1. long frames are split at second boundaries; total duration preserved
durs = [2500, 100, 30, 700]
res = run_scene(durs, T0)
total = sum(h for _, _, h in res)
check("total duration preserved", total == sum(durs), total)
check("long frame split into multiple segments", len(res) > len(durs),
      len(res))

# every segment boundary that is not a plain frame edge lands one
# MIN_FRAME guard-width before a whole second of the fake clock
guard = scenes.MIN_FRAME_MS / 1000.0
split_marks = []
acc = 0
frame_edges = set()
for d in durs:
    acc += d
    frame_edges.add(acc)
acc = 0
for _, _, h in res:
    acc += h
    if acc not in frame_edges:
        split_marks.append(T0 + acc / 1000.0)
ok = all(abs(((m + guard) % 1.0)) < 0.005 or
         abs(((m + guard) % 1.0) - 1.0) < 0.005 for m in split_marks)
check("splits land on the beat (guard-adjusted second boundaries)", ok,
      ["%.3f" % (m % 60) for m in split_marks])
# frame 1 spans x.35 -> x+2.85: crosses the two boundaries at x+1, x+2
check("2500ms frame split at both boundaries it crosses",
      len(split_marks) == 2, len(split_marks))

# 2. the image actually changes across a split (colon flipped)
changed = 0
by_time = res
for i in range(1, len(by_time)):
    if by_time[i][1] != by_time[i - 1][1]:
        changed += 1
check("colon flips across splits (image changes)", changed >= 3, changed)

# 3. no time_fn (tests / --fast): exactly one yield per frame, old behavior
res_none = run_scene(durs, T0, time_fn_none=True)
check("no time_fn -> no splits", len(res_none) == len(durs), len(res_none))
check("no time_fn -> holds are the (clamped) durations",
      [h for _, _, h in res_none] == [max(scenes.MIN_FRAME_MS, d)
                                      for d in durs],
      [h for _, _, h in res_none])

# 4. overlay disabled: no splits even with a time_fn
cfg_off = make_cfg()
cfg_off.data["playback"]["clock_overlay"] = "off"
res_off = run_scene(durs, T0, cfg=cfg_off)
check("overlay off -> no splits", len(res_off) == len(durs), len(res_off))

# 5. colon "on": no per-second splits, but the minute rollover still splits
cfg_on = make_cfg(colon="on")
minute_t0 = 1750000000.0
minute_t0 -= (minute_t0 % 60.0)
minute_t0 += 58.8                       # 1.2s before the minute rollover
res_on = run_scene([2500, 100, 30, 700], minute_t0, cfg=cfg_on)
check("colon 'on' -> minute-boundary split only",
      len(res_on) == len(durs) + 1, len(res_on))
mins_changed = sum(1 for i in range(1, len(res_on))
                   if res_on[i][1] != res_on[i - 1][1])
check("digits change at the minute split", mins_changed >= 1, mins_changed)

# 6. short frames never produce sub-MIN_FRAME segments
for _, _, h in res:
    if h < scenes.MIN_FRAME_MS:
        check("segment >= MIN_FRAME_MS", False, h)
        break
else:
    check("segment >= MIN_FRAME_MS", True)

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("OVERLAY BEAT TESTS PASSED")
