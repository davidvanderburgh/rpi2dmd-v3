"""Verify: the prefetch decode budget aborts a too-slow GIF (payload None,
still 'ready' so the clock never waits on it) and normal decodes are
unaffected."""
import glob
import os
import sys
import time

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-budget2")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import scheduler, scenes  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


gifs = glob.glob(os.path.join(os.environ["RPI2DMD_MEDIA"], "gif", "*", "*.gif"))
if not gifs:
    print("SKIP: no test gifs available")
    sys.exit(0)
pick = ("gif", ("Cat", os.path.basename(gifs[0]), gifs[0]))


def wait_ready(pf, p, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pf.ready(p):
            return True
        time.sleep(0.05)
    return False


# 1. absurdly small budget -> abort -> ready with payload None
old = scheduler.DECODE_BUDGET_S
scheduler.DECODE_BUDGET_S = 1e-9
pf = scheduler.Prefetcher((128, 32))
pf.start(pick)
check("budget-aborted decode reports ready", wait_ready(pf, pick))
check("budget-aborted payload is None", pf.take(pick, timeout=1.0) is None)

# 2. normal budget -> full decode
scheduler.DECODE_BUDGET_S = old
pf2 = scheduler.Prefetcher((128, 32))
pf2.start(pick)
check("normal decode reports ready", wait_ready(pf2, pick))
payload = pf2.take(pick, timeout=1.0)
check("normal decode returns frames", bool(payload), payload and len(payload))

# 3. direct: DecodeBudgetExceeded raised, not swallowed, by the loader
try:
    scenes.load_gif_frames(gifs[0], (128, 32), abort_after_s=1e-9)
    check("loader raises DecodeBudgetExceeded", False, "no exception")
except scenes.DecodeBudgetExceeded:
    check("loader raises DecodeBudgetExceeded", True)

# 4. no budget (0) -> never aborts
frames = scenes.load_gif_frames(gifs[0], (128, 32), abort_after_s=0.0)
check("budget 0 disables the abort", len(frames) > 0, len(frames))

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("DECODE BUDGET TESTS PASSED")
