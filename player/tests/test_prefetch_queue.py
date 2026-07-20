"""Verify the depth-N prefetch queue: decodes ahead, in order, flushes,
survives failures, and honors the GIF frame cap."""
import glob
import os
import sys
import time

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-q")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import scenes, scheduler  # noqa
from PIL import Image  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


media = os.environ["RPI2DMD_MEDIA"]
gifs = sorted(glob.glob(os.path.join(media, "gif", "*", "*.gif")))[:3]
rdas = sorted(glob.glob(os.path.join(media, "dmd", "*", "*.rda")))[:2]
picks = [("gif", ("Cat", os.path.basename(p), p)) for p in gifs] + \
        [("dmd", ("G", os.path.basename(p)[:-4], p)) for p in rdas]

pf = scheduler.Prefetcher((128, 32), depth=3)

# 1. queue several, all become ready without any take()
pf.ensure(picks[:4])
deadline = time.time() + 30
while time.time() < deadline and not all(pf.ready(p) for p in picks[:4]):
    time.sleep(0.1)
check("4 queued picks all decoded in background",
      all(pf.ready(p) for p in picks[:4]))

# 2. take in play order returns real payloads instantly
t0 = time.time()
got = [pf.take(p, timeout=0.2) for p in picks[:4]]
check("takes are instant when prefetched", time.time() - t0 < 0.5)
check("payloads real", all(g is not None for g in got))
kinds_ok = (isinstance(got[0], list)         # gif -> frame list
            and isinstance(got[-1], tuple)
            and len(got[-1]) == 4)  # dmd: (header, frames, indexes, strips)
check("payload shapes per kind", kinds_ok,
      [type(g).__name__ for g in got])

# 3. a failing pick is marked ready with None (never waited on forever)
bad = ("gif", ("X", "nope.gif", os.path.join(media, "gif", "nope.gif")))
pf.ensure([bad])
deadline = time.time() + 10
while time.time() < deadline and not pf.ready(bad):
    time.sleep(0.05)
check("failed decode still reports ready", pf.ready(bad))
check("failed decode payload is None", pf.take(bad, timeout=0.2) is None)

# 4. flush clears queued work
pf.ensure(picks)
pf.flush()
check("flush leaves nothing ready", not any(pf.ready(p) for p in picks))

# 5. explicit max_frames still works, but the default does NOT truncate
frames = scenes.load_gif_frames(gifs[0], (128, 32), max_frames=5)
check("explicit max_frames honored", len(frames) <= 5, len(frames))
check("no playback frame cap by default",
      scenes.PLAYBACK_MAX_GIF_FRAMES is None, scenes.PLAYBACK_MAX_GIF_FRAMES)
full = scenes.load_gif_frames(gifs[0], (128, 32))
img = Image.open(gifs[0])
n = getattr(img, "n_frames", 1)
check("full GIF decoded to all its frames", len(full) == n,
      "%d of %d" % (len(full), n))
check("bomb backstop is above the real library max",
      scenes.MAX_GIF_FRAMES >= 5000, scenes.MAX_GIF_FRAMES)

# 6. memory budget: a long clip holds back further prefetch
big = scheduler._payload_frames
check("frame counter: gif payload", big(full) == len(full))
check("frame counter: dmd payload",
      big(("h", [b"x"] * 7, [b"y"] * 7)) == 7)

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("QUEUE TESTS PASSED")
