"""Prove DMD strip materialization: every frame pre-rendered worker-side,
strip frames pixel-identical to the classic per-frame render path, overlay
frames (ClockOnTop AND ClockBehind) composed from strip + cached RGB patch
pixel-identical to the classic index-space composite, masks mark exactly
the B237 transparency, and the name-during mode disables strips."""
import os
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-strips")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import config, rda, scenes  # noqa
from rpi2dmd.rgf import StripFrame  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


def make_cfg(over=None):
    cfg = config.Config()
    merged = {"clock": {"style": "digital", "format": "12h",
                        "colon": "blink", "background": "none"}}
    merged.update(over or {})
    cfg.data = config.deep_merge(config.DEFAULTS, merged)
    return cfg


def make_header(n, ctype="NoClock", start=0, end=0):
    return {"name": "T", "game": "T", "width": 128, "height": 32,
            "num_frames": n, "durations": [50] * n,
            "clock": {"type": ctype, "size": "ClockLarge", "x": 0, "y": 0,
                      "start_frame": start, "end_frame": end}}


# synthetic frames: transparency stripe on the left, art gradient elsewhere
def make_index_frame(seed):
    return bytes(bytearray(
        (10 if (i % 128) < 40 else ((i + seed) % 16))
        for i in range(4096)))


FIXED_T = 1784582400.25          # frozen wall clock for determinism
time_fn = lambda: FIXED_T  # noqa: E731

N = 70   # > 2 slabs of 32
indexes = [make_index_frame(k) for k in range(N)]
cfg = make_cfg()


def render(header, strips=None, masks=None):
    sc = scenes.dmd_scene(cfg, None, header=header, frames=[b""] * N,
                          indexes=indexes, strips=strips, masks=masks,
                          time_fn=time_fn)
    return [img for img, _ in sc]


def rgb(o):
    return o.realize().tobytes() if isinstance(o, StripFrame) \
        else o.tobytes()


# 1. no-overlay anim: every frame stripped, no masks, pixels identical
prep = scenes.materialize_dmd(cfg, make_header(N), indexes)
strips, masks = prep
check("all frames stripped", all(s is not None for s in strips))
check("no masks without ClockBehind", masks is None)
palette = rda.build_palette(*scenes._display(cfg, game="T"))
old = rda.frame_to_image(rda.pack_frame(bytearray(indexes[33])),
                         palette).convert("RGB")
check("strip pixels identical to classic render path",
      old.tobytes() == strips[33].realize().tobytes())

# 2. ClockOnTop: strips everywhere, scene output pixel-identical to the
# classic index-composite path, strips yielded outside the window
hdr = make_header(N, ctype="ClockOnTop", start=10, end=20)
strips2, masks2 = scenes.materialize_dmd(cfg, hdr, indexes)
check("on-top: all frames stripped, no masks",
      all(s is not None for s in strips2) and masks2 is None)
plain = render(hdr)
fast = render(hdr, strips=strips2, masks=masks2)
check("on-top: window frames pixel-identical",
      all(rgb(fast[i]) == rgb(plain[i]) for i in range(10, 21)))
check("on-top: non-window frames pixel-identical",
      all(rgb(fast[i]) == rgb(plain[i]) for i in (0, 9, 21, N - 1)))
check("on-top: strips yielded outside window",
      isinstance(fast[9], StripFrame) and isinstance(fast[21], StripFrame)
      and not isinstance(fast[15], StripFrame))

# 3. ClockBehind: masks inside the window mark exactly the transparency,
# scene output pixel-identical to the classic path
hdrb = make_header(N, ctype="ClockBehind", start=5, end=60)
strips3, masks3 = scenes.materialize_dmd(cfg, hdrb, indexes)
check("behind: masks only inside window",
      masks3 is not None and masks3[4] is None and masks3[5] is not None
      and masks3[60] is not None and masks3[61] is None)
mask_px = masks3[30].load()
check("behind: mask marks exactly the transparency stripe",
      mask_px[10, 5] == 255 and mask_px[100, 5] == 0,
      (mask_px[10, 5], mask_px[100, 5]))
plainb = render(hdrb)
fastb = render(hdrb, strips=strips3, masks=masks3)
check("behind: all frames pixel-identical",
      all(rgb(fastb[i]) == rgb(plainb[i]) for i in (0, 5, 30, 60, 61)))

# 4. name-during mode disables strips
cfg_nd = make_cfg({"playback": {"show_name": "during"}})
check("show_name=during disables strips",
      scenes.materialize_dmd(cfg_nd, make_header(N), indexes) is None)

# 5. durations preserved on both strip paths
sc = scenes.dmd_scene(cfg, None, header=hdr, frames=[b""] * N,
                      indexes=indexes, strips=strips2, masks=masks2,
                      time_fn=time_fn)
durs = [d for _, d in sc]
check("durations preserved", durs == [50] * N, durs[:5])

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("DMD STRIPS TESTS PASSED")
