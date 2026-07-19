"""Prove the prefetch frame-budget caps buffered memory without truncating
any single clip."""
import os
import sys
import time

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-bud")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import scheduler  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


# monkeypatch the decoder to make synthetic clips of known frame counts,
# so we can test the budget without huge real files
orig = scheduler.Prefetcher._decode
SIZES = {}


def fake_decode(self, pick):
    n = SIZES.get(pick, 10)
    time.sleep(0.02)
    return [(b"frame", 100)] * n     # gif-shaped payload of n frames


scheduler.Prefetcher._decode = fake_decode

pf = scheduler.Prefetcher((128, 32), depth=3)

# one huge clip (3000 frames > 2500 budget) then several small ones
huge = ("gif", ("C", "huge.gif", "/huge"))
smalls = [("gif", ("C", "s%d.gif" % i, "/s%d" % i)) for i in range(5)]
SIZES[huge] = 3000
for s in smalls:
    SIZES[s] = 50

pf.ensure([huge] + smalls)
time.sleep(1.5)   # let the worker run

# with the huge clip buffered (>budget), the worker should NOT have decoded
# the small ones yet
buffered = pf._buffered_frames()
check("huge clip decoded", pf.ready(huge))
check("budget holds back further prefetch while huge clip buffered",
      not pf.ready(smalls[1]), "buffered=%d" % buffered)
check("buffered frames near budget, not unbounded",
      buffered <= 3000 + 50, buffered)

# consume the huge clip -> budget frees -> smalls decode
got = pf.take(huge, timeout=2.0)
check("huge clip returned in full (not truncated)", got is not None
      and len(got) == 3000, len(got) if got else None)
time.sleep(1.5)
check("smalls decode once budget freed", pf.ready(smalls[0]))

scheduler.Prefetcher._decode = orig
print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("BUDGET TEST PASSED")
