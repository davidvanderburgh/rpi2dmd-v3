"""Verify B237 transparency semantics (index 10) and overlay placement:
transparency renders black, ClockBehind fills ONLY transparency (never
opaque black or art), ClockLarge placement ignores the user's clock-scene
position settings, ClockSmall anchors at metadata x/y (even 0,0), and an
end<start window is rescued instead of silently dropped."""
import os
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-transp")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import config, rda, scenes  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


def make_cfg(clock_over=None):
    cfg = config.Config()
    over = {"clock": dict({"style": "digital", "format": "12h",
                           "colon": "on", "background": "none"},
                          **(clock_over or {}))}
    cfg.data = config.deep_merge(config.DEFAULTS, over)
    return cfg


def make_header(n, ctype="ClockBehind", size="ClockLarge", x=0, y=0,
                start=0, end=0):
    return {"name": "T", "game": "T", "width": 128, "height": 32,
            "num_frames": n, "durations": [100] * n,
            "clock": {"type": ctype, "size": size, "x": x, "y": y,
                      "start_frame": start, "end_frame": end}}


def render(header, index_frames, cfg=None):
    cfg = cfg or make_cfg()
    sc = scenes.dmd_scene(cfg, None, header=header,
                          frames=[b""] * len(index_frames),
                          indexes=index_frames)
    return [img for img, _ in sc]


# frame thirds: left = opaque black (0), middle = transparency (10),
# right = art (12)
mixed = bytes(bytearray(
    (0 if (i % 128) < 42 else (10 if (i % 128) < 85 else 12))
    for i in range(4096)))

# 1. transparency renders black without any clock
imgs = render(make_header(1, ctype="NoClock"), [mixed])
px = imgs[0].load()
check("transparency renders black (no clock)",
      px[60, 16] == (0, 0, 0), px[60, 16])
check("opaque black stays black", px[10, 16] == (0, 0, 0), px[10, 16])
check("art pixels lit", px[100, 16] != (0, 0, 0), px[100, 16])

# 2. pure-transparency frame (the 'all LEDs ON' bug) is black now
full_trans = bytes([10]) * 4096
imgs = render(make_header(1, ctype="NoClock"), [full_trans])
check("pure-transparency frame is dark",
      imgs[0].getcolors() == [(4096, (0, 0, 0))], imgs[0].getcolors())

# 3. ClockBehind: digits appear ONLY inside the transparency band
base = render(make_header(1, ctype="NoClock"), [mixed])[0]
back = render(make_header(1, ctype="ClockBehind"), [mixed])[0]
pa, pb = base.load(), back.load()
in_trans = in_black = in_art = 0
for yy in range(32):
    for xx in range(128):
        if pa[xx, yy] != pb[xx, yy]:
            if xx < 42:
                in_black += 1
            elif xx < 85:
                in_trans += 1
            else:
                in_art += 1
check("back mode fills transparency", in_trans > 30, in_trans)
check("back mode never touches opaque black", in_black == 0, in_black)
check("back mode never touches art", in_art == 0, in_art)

# 4. ClockLarge overlay ignores the user's clock-scene position settings
cfg_moved = make_cfg({"align": "xy", "x": 90, "y": 20})
a = render(make_header(1, ctype="ClockOnTop"), [full_trans])[0]
b = render(make_header(1, ctype="ClockOnTop"), [full_trans], cfg_moved)[0]
check("ClockLarge overlay unaffected by clock-scene x/y/align",
      a.tobytes() == b.tobytes())

# 5. ClockSmall at metadata (0,0) is honored (latent falsy-gate fix)
s = render(make_header(1, ctype="ClockOnTop", size="ClockSmall",
                       x=0, y=0), [full_trans])[0]
ps = s.load()
lit_left = sum(1 for yy in range(12) for xx in range(40)
               if ps[xx, yy] != (0, 0, 0))
lit_right = sum(1 for yy in range(20, 32) for xx in range(88, 128)
                if ps[xx, yy] != (0, 0, 0))
check("ClockSmall (0,0) anchors top-left", lit_left > 20 and lit_right == 0,
      (lit_left, lit_right))

# 6. end<start window is rescued (clock shows from start to last frame)
n = 8
hdr = make_header(n, ctype="ClockOnTop", start=5, end=2)
plain = render(make_header(n, ctype="NoClock"), [full_trans] * n)
withc = render(hdr, [full_trans] * n)
changed = [i for i in range(n)
           if plain[i].tobytes() != withc[i].tobytes()]
check("end<start rescued: clock on frames >= start", changed == [5, 6, 7],
      changed)

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("TRANSPARENCY TESTS PASSED")
