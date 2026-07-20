"""Verify: RGF write/read round-trip, prefetcher cache-first loading with
the stale-source guard, and gif_scene playing an RgfClip."""
import glob
import os
import shutil
import sys
import time

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-rgf")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from PIL import Image  # noqa
from rpi2dmd import config, rgf, scenes, scheduler  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


media = os.environ["RPI2DMD_MEDIA"]
cache_root = os.path.join(media, "gif-cache")
shutil.rmtree(cache_root, ignore_errors=True)

gifs = sorted(glob.glob(os.path.join(media, "gif", "*", "*.gif")))
assert gifs, "no test gifs"
src = gifs[0]
cat = os.path.basename(os.path.dirname(src))
fname = os.path.basename(src)

# 1. round-trip: identical frame count, durations, sizes; close pixels
frames = scenes._load_image_frames(src, target=(128, 32))
rgf_path = rgf.cache_path(cat, fname)
rgf.write_rgf(rgf_path, frames, src_size=os.path.getsize(src))
clip = rgf.RgfClip(rgf_path)
check("frame count preserved", len(clip) == len(frames),
      (len(clip), len(frames)))
check("durations preserved",
      clip.durations == [d for _, d in frames])
check("total_ms", clip.total_ms == sum(d for _, d in frames))
back = list(clip)
check("frame size preserved", back[0][0].size == (128, 32),
      back[0][0].size)


def close(a, b, tol=3):
    pa = list(a.convert("RGB").getdata())
    pb = list(b.convert("RGB").getdata())
    worst = max(max(abs(x - y) for x, y in zip(p, q))
                for p, q in zip(pa, pb))
    return worst <= tol


check("pixels survive palette round-trip (first frame)",
      close(frames[0][0], back[0][0]))
check("pixels survive palette round-trip (last frame)",
      close(frames[-1][0], back[-1][0]))
check("clip iterable twice", len(list(clip)) == len(clip))

# 2. prefetcher prefers the cache
pick = ("gif", (cat, fname, src))
pf = scheduler.Prefetcher((128, 32))
pf.start(pick)
deadline = time.time() + 15
while not pf.ready(pick) and time.time() < deadline:
    time.sleep(0.05)
payload = pf.take(pick, timeout=1.0)
check("prefetch returns materialized frames from cache",
      isinstance(payload, list) and len(payload) == len(clip) and
      payload[0][0].mode == "RGB", type(payload))

# 3. stale guard: wrong src_size -> cache ignored, real decode used
rgf.write_rgf(rgf_path, frames, src_size=os.path.getsize(src) + 999)
pf2 = scheduler.Prefetcher((128, 32))
pf2.start(pick)
deadline = time.time() + 30
while not pf2.ready(pick) and time.time() < deadline:
    time.sleep(0.05)
payload2 = pf2.take(pick, timeout=1.0)
check("stale cache ignored (falls back to gif decode)",
      isinstance(payload2, list), type(payload2))

# 4. gif_scene plays an RgfClip (frames param) with correct pacing
cfg = config.Config()
cfg.data = config.deep_merge(config.DEFAULTS, {})
clip = rgf.RgfClip(rgf.cache_path(cat, fname))  # rewrite good one first
rgf.write_rgf(rgf_path, frames, src_size=os.path.getsize(src))
clip = rgf.RgfClip(rgf_path)
out = list(scenes.gif_scene(cfg, src, canvas=(128, 32), frames=clip))
check("gif_scene yields every cached frame (or looped passes)",
      len(out) >= len(clip), (len(out), len(clip)))
check("gif_scene durations match clip",
      [h for _, h in out][:len(clip)] == clip.durations)
check("gif_scene frames render", out[0][0].size == (128, 32))

# 5. _payload_frames counts RgfClip for the prefetch frame budget
check("frame budget counts RgfClip",
      scheduler._payload_frames(clip) == len(clip))

# 6. bulk materialize: StripFrames, identical pixels to lazy iteration
mat = clip.materialize()
check("materialize returns all frames", mat is not None and
      len(mat) == len(clip), mat and len(mat))
lazy = list(clip)
same = all(m[0].realize().tobytes() ==
           l[0].convert("RGB").tobytes() and m[1] == l[1]
           for m, l in zip(mat, lazy))
check("materialized == lazy (pixels + durations)", same)
check("materialized frames are RGB StripFrames",
      mat[0][0].mode == "RGB" and hasattr(mat[0][0], "strip"),
      type(mat[0][0]))
import math  # noqa: E402
n_strips = len(set(id(m[0].strip) for m in mat))
check("StripFrames grouped into slab strips",
      n_strips <= math.ceil(len(mat) / 32.0), n_strips)

# 6b. drivers render StripFrames: sim driver realizes correctly
from rpi2dmd import matrix  # noqa: E402
sim = matrix.SimDriver(cfg, out_dir=os.environ["RPI2DMD_RUN"])
sim.show(mat[1][0])
check("SimDriver renders StripFrame",
      sim.last_image.tobytes() == lazy[1][0].convert("RGB").tobytes())

# 7. over-long clips refuse bulk materialization (stay lazy)
old_max = rgf.MATERIALIZE_MAX_FRAMES
rgf.MATERIALIZE_MAX_FRAMES = len(clip) - 1
check("too-long clip stays lazy", clip.materialize() is None)
rgf.MATERIALIZE_MAX_FRAMES = old_max

# 8. v1 container still readable (lazy path)
import json as _json  # noqa: E402
import struct as _struct  # noqa: E402
import zlib as _zlib  # noqa: E402
v1_frames = [f for f, _ in frames[:3]]
v1_chunks = []
for img in v1_frames:
    p = img.convert("P", palette=Image.ADAPTIVE, colors=256)
    pal = (p.getpalette() or []) + [0] * 768
    v1_chunks.append(_zlib.compress(bytes(bytearray(pal[:768])) +
                                    p.tobytes(), 9))
v1_header = _json.dumps({
    "version": 1, "width": 128, "height": 32, "num_frames": 3,
    "durations": [100, 100, 100],
    "chunk_lengths": [len(c) for c in v1_chunks],
    "src_size": 0}).encode()
v1_path = os.path.join(cache_root, "v1test.rgf")
with open(v1_path, "wb") as f:
    f.write(b"RGF1")
    f.write(_struct.pack("<I", len(v1_header)))
    f.write(v1_header)
    for c in v1_chunks:
        f.write(c)
v1 = rgf.RgfClip(v1_path)
check("v1 clip readable", len(v1) == 3 and v1.version == 1)
check("v1 lazy frames render", list(v1)[0][0].size == (128, 32))
check("v1 refuses bulk materialize", v1.materialize() is None)

shutil.rmtree(cache_root, ignore_errors=True)
print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("RGF TESTS PASSED")
