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
            and isinstance(got[-1], tuple) and len(got[-1]) == 3)  # dmd
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

# 5. GIF frame cap honored in playback loads
frames = scenes.load_gif_frames(gifs[0], (128, 32), max_frames=5)
check("max_frames honored", len(frames) <= 5, len(frames))
default_cap = scenes.load_gif_frames(gifs[0], (128, 32))
check("default cap is playback cap",
      len(default_cap) <= scenes.PLAYBACK_MAX_GIF_FRAMES, len(default_cap))

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("QUEUE TESTS PASSED")
