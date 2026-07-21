"""Prove bounded late-frame dropping: frames whose whole display window
already passed are skipped (staying on the beat) instead of played in slow
motion, drops are bounded per run of consecutive lateness, the first frame
is never dropped, and the journal records the count."""
import json
import os
import shutil
import sys
import time

RUN = r"C:\tmp\rpi2dmd-v3-work\run-drop"
os.environ["RPI2DMD_MEDIA"] = r"C:\tmp\rpi2dmd-v3-work\testmedia"
os.environ["RPI2DMD_RUN"] = RUN
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

if os.path.isdir(RUN):
    shutil.rmtree(RUN)

from rpi2dmd import config, control, scheduler  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


class CountingDriver(object):
    width = 128
    height = 32

    def __init__(self):
        self.shown = []

    def show(self, img):
        self.shown.append(img)

    def set_brightness(self, pct):
        pass


cfg = config.Config()
cfg.data = config.deep_merge(config.DEFAULTS, {})
state = control.PlayerState(cfg)
driver = CountingDriver()
sched = scheduler.Scheduler(cfg, driver, state, library=None)


def slow_scene(n, hold_ms, gen_s):
    """Yields numbered frames; simulates slow generation with a sleep
    BEFORE each yield (like a decode stall)."""
    for k in range(n):
        if k:
            time.sleep(gen_s)
        yield k, hold_ms


# 1. all frames on time -> nothing dropped
driver.shown = []
sched.play_scene(slow_scene(5, 30, 0.0), "animation", log="ontime")
check("on-time scene drops nothing", driver.shown == [0, 1, 2, 3, 4],
      driver.shown)

# 2. generation 100ms per 30ms frame -> windows pass -> some frames drop,
# but never the first, and progress continues (bounded consecutive drops)
driver.shown = []
sched.play_scene(slow_scene(10, 30, 0.1), "animation", log="late")
check("late scene dropped frames", len(driver.shown) < 10, driver.shown)
check("first frame never dropped", driver.shown[0] == 0, driver.shown)
check("bounded: shows at least 1 in 4", len(driver.shown) >= 3,
      driver.shown)

# 3. journal records the drops
with open(os.path.join(RUN, scheduler.FRAMETIME_LOG)) as f:
    rows = [json.loads(ln) for ln in f.read().splitlines() if ln]
late_row = [r for r in rows if r["id"] == "late"][0]
ontime_row = [r for r in rows if r["id"] == "ontime"][0]
check("journal: dropped counted", late_row["dropped"] == 10 - len(driver.shown),
      late_row)
check("journal: on-time run has no drops", ontime_row["dropped"] == 0)

# 4. a one-frame scene under extreme lag still shows its frame
driver.shown = []


def one_frame():
    time.sleep(0.2)
    yield "card", 100


sched.play_scene(one_frame(), "clock", log="card")
check("single-frame scene always shows", driver.shown == ["card"])

# 5. prefetch quiet gate: set while an animation scene is on screen (the
# decode worker naps hard then), cleared afterwards, untouched by clock
flags = []


def probe_scene():
    yield "a", 30
    flags.append(sched.prefetch.quiet.is_set())
    yield "b", 30


sched.play_scene(probe_scene(), "animation", log="probe")
check("quiet set while animation plays", flags == [True], flags)
check("quiet cleared after scene", not sched.prefetch.quiet.is_set())
sched.play_scene(iter([("c", 30)]), "clock", log="clockprobe")
check("clock scene does not set quiet",
      not sched.prefetch.quiet.is_set())

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("FRAME DROP TESTS PASSED")
