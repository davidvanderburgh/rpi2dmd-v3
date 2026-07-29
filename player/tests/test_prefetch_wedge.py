"""Regression: skipped picks must never wedge the prefetch worker.

Field failure (2026-07-27): a pick skipped as 'payload not ready' stayed
in _pending; when its decode later completed, the payload sat in _done as
an orphan nobody would ever take, still counting toward the frame budget.
Once orphans reached PREFETCH_FRAME_BUDGET the worker parked at its gate
FOREVER (only take() trimmed _done, and no take could ever succeed again).
Every slot skipped, the clock extended to cap in a loop for 31 hours.

Fix under test: Prefetcher.discard() (called on skip; also cancels an
in-flight decode) and Prefetcher.retain() (kickoff purges everything not
on the scheduler's current want-list)."""
import os
import sys
import time

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-wedge")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import scheduler  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


# synthetic decoder: controllable frame counts and decode delays
orig = scheduler.Prefetcher._decode
SIZES = {}
DELAYS = {}


def fake_decode(self, pick):
    time.sleep(DELAYS.get(pick, 0.02))
    return [(b"frame", 100)] * SIZES.get(pick, 10)


def wait_ready(pf, picks, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not all(pf.ready(p) for p in picks):
        time.sleep(0.05)
    return all(pf.ready(p) for p in picks)


scheduler.Prefetcher._decode = fake_decode

# -- 1. the wedge itself: a completed orphan over budget parks the worker --
pf = scheduler.Prefetcher((128, 32), depth=3)
orphan = ("gif", ("C", "orphan.gif", "/orphan"))
smalls = [("gif", ("C", "s%d.gif" % i, "/s%d" % i)) for i in range(3)]
SIZES[orphan] = 3000            # alone >= PREFETCH_FRAME_BUDGET (2500)
for s in smalls:
    SIZES[s] = 20

pf.ensure([orphan])
check("orphan decodes", wait_ready(pf, [orphan]))
# scheduler skipped it: nobody will take it. Queue the next slot's picks.
pf.ensure(smalls)
time.sleep(0.6)
check("worker parks at budget gate behind the orphan (the field wedge)",
      not any(pf.ready(s) for s in smalls))

# kickoff's retain() with the current want-list must unpark it
pf.retain(smalls)
check("retain() purges the orphan and the worker resumes",
      wait_ready(pf, smalls))
check("orphan payload gone after retain", not pf.ready(orphan))

# -- 2. discard() cancels an in-flight decode so no orphan ever forms ----
pf2 = scheduler.Prefetcher((128, 32), depth=3)
slow = ("gif", ("C", "slow.gif", "/slow"))
SIZES[slow] = 3000
DELAYS[slow] = 0.8
pf2.ensure([slow])
time.sleep(0.2)                  # worker is mid-decode
pf2.discard(slow)                # scheduler skipped the slot
pf2.ensure(smalls)
check("smalls decode after in-flight discard", wait_ready(pf2, smalls))
check("discarded pick's payload was dropped, not stored",
      not pf2.ready(slow))

# -- 3. discard of a queued (not yet started) pick just dequeues it ------
pf3 = scheduler.Prefetcher((128, 32), depth=3)
a, b = smalls[0], smalls[1]
with pf3._cv:                    # queue without starting the worker
    pf3._pending.extend([a, b])
pf3.discard(a)
check("queued discard removes from pending", pf3._pending == [b])

# -- 4. a re-wanted pick decodes again after an earlier discard ----------
pf2.ensure([slow])
DELAYS[slow] = 0.02
check("pick queued again after discard decodes fresh",
      wait_ready(pf2, [slow]) and len(pf2.take(slow, timeout=1.0)) == 3000)

scheduler.Prefetcher._decode = orig
print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("WEDGE TESTS PASSED")
