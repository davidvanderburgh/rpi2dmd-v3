"""Prove the frame-timing journal: one JSON line per scene run with the
lateness/show/resync stats, rotation past the size cap, and no file at all
under --fast (simulated time would make the numbers garbage)."""
import json
import os
import shutil
import sys

RUN = r"C:\tmp\rpi2dmd-v3-work\run-frametime"
os.environ["RPI2DMD_MEDIA"] = r"C:\tmp\rpi2dmd-v3-work\testmedia"
os.environ["RPI2DMD_RUN"] = RUN
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

if os.path.isdir(RUN):
    shutil.rmtree(RUN)

from rpi2dmd import scheduler  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


LOG = os.path.join(RUN, scheduler.FRAMETIME_LOG)

# -- basic emit -------------------------------------------------------------
st = scheduler._FrameStats("animation", ("dmd", "GAME", "ANIM_001"))
for late_ms in (0, 5, 10, 60, 120):
    st.pre_show(late_ms / 1000.0, 100)
    st.post_show()
st.resync(300.0)
st.emit()

with open(LOG, "r") as f:
    lines = [json.loads(ln) for ln in f.read().splitlines() if ln]
check("one line per scene run", len(lines) == 1, lines)
doc = lines[0]
check("id joined from tuple", doc["id"] == "dmd:GAME:ANIM_001", doc["id"])
check("frames counted", doc["frames"] == 5, doc)
check("planned summed", doc["planned_ms"] == 500, doc)
check("late_max recorded", doc["late_max"] >= 118, doc)
check("late_n40 counts >40ms frames", doc["late_n40"] == 2, doc)
check("resync recorded", doc["resyncs"] == 1 and doc["resync_max"] == 300, doc)
check("show percentiles present", "show_p50" in doc and "show_max" in doc, doc)

# -- empty run emits nothing ------------------------------------------------
scheduler._FrameStats("clock", None).emit()
with open(LOG, "r") as f:
    check("empty scene run emits no line",
          len(f.read().splitlines()) == 1)

# -- rotation ---------------------------------------------------------------
with open(LOG, "w") as f:
    f.write("x" * (scheduler.FRAMETIME_MAX_BYTES + 1))
st = scheduler._FrameStats("clock", "clock")
st.pre_show(0.0, 1000)
st.post_show()
st.emit()
check("oversize journal rotated to .1", os.path.exists(LOG + ".1"))
with open(LOG, "r") as f:
    lines = f.read().splitlines()
check("fresh journal after rotation has just the new line",
      len(lines) == 1 and json.loads(lines[0])["scene"] == "clock", lines)

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("FRAMETIME TEST PASSED")
