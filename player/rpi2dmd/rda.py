"""RDA (RPI2DMD DMD Animation) file format.

Packed storage for Run-DMD style 128x32 4-bit grayscale animations with
per-animation clock metadata. Written by the host-side converter, read by
the on-device player and web UI.

File layout (little-endian):
    offset 0   : magic b"RDA1"
    offset 4   : uint32 header_len (bytes of UTF-8 JSON that follows)
    offset 8   : header JSON
    offset 8+N : frames, num_frames x 2048 bytes
                 (128x32 pixels, 4bpp, row-major, 2 pixels/byte,
                  high nibble = left pixel, values 0..15)

Header JSON keys:
    name             str   animation name, e.g. "ATTACK_FROM_MARS_006"
    game             str   game folder name, e.g. "ATTACK_FROM_MARS"
    width, height    int   always 128, 32 for Run-DMD content
    num_frames       int
    durations        [int] per-frame display time in milliseconds
    clock: {
        type         str   "NoClock" | "ClockOnTop" | "ClockBehind"
        size         str   "ClockSmall" | "ClockLarge"
        x, y         int   clock position on the 128x32 canvas
        start_frame  int   first frame index showing the clock (0 = start)
        end_frame    int   last frame index showing the clock (0 = all)
    }
    intro_transition str   "Enable" | "Disable"
    outro_transition str   "Enable" | "Disable"

NOTE: this module must stay Python 3.7 compatible (Raspbian Buster) and
must not depend on numpy. Pillow is optional (only needed for rendering).
"""

import json
import struct

MAGIC = b"RDA1"
WIDTH = 128
HEIGHT = 32
FRAME_BYTES = WIDTH * HEIGHT // 2  # 4bpp packed

# Nibble-unpack table: byte -> 2 palette-index bytes.
_UNPACK = [bytes((b >> 4, b & 0x0F)) for b in range(256)]

CLOCK_NONE = "NoClock"
CLOCK_ON_TOP = "ClockOnTop"
CLOCK_BEHIND = "ClockBehind"


def write_rda(path, header, frames):
    """Write an RDA file. frames: iterable of 2048-byte packed frames."""
    frames = list(frames)
    header = dict(header)
    header["width"] = WIDTH
    header["height"] = HEIGHT
    header["num_frames"] = len(frames)
    if len(header.get("durations", [])) != len(frames):
        raise ValueError("durations length != num_frames for %s" % path)
    blob = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", len(blob)))
        f.write(blob)
        for fr in frames:
            if len(fr) != FRAME_BYTES:
                raise ValueError("bad frame size %d in %s" % (len(fr), path))
            f.write(fr)


def read_header(path):
    """Read only the JSON header of an RDA file (cheap)."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != MAGIC:
            raise ValueError("not an RDA file: %s" % path)
        (hlen,) = struct.unpack("<I", f.read(4))
        return json.loads(f.read(hlen).decode("utf-8"))


def read_rda(path):
    """Read a full RDA file -> (header dict, list of 2048-byte frames)."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != MAGIC:
            raise ValueError("not an RDA file: %s" % path)
        (hlen,) = struct.unpack("<I", f.read(4))
        header = json.loads(f.read(hlen).decode("utf-8"))
        n = header["num_frames"]
        frames = []
        for _ in range(n):
            fr = f.read(FRAME_BYTES)
            if len(fr) != FRAME_BYTES:
                raise ValueError("truncated RDA file: %s" % path)
            frames.append(fr)
        return header, frames


def unpack_frame(packed):
    """2048 packed bytes -> 4096 bytes of palette indexes 0..15."""
    return b"".join([_UNPACK[b] for b in packed])


def pack_frame(indexes):
    """4096 bytes of palette indexes 0..15 -> 2048 packed bytes."""
    if len(indexes) != WIDTH * HEIGHT:
        raise ValueError("bad index buffer size %d" % len(indexes))
    out = bytearray(FRAME_BYTES)
    for i in range(FRAME_BYTES):
        out[i] = ((indexes[2 * i] & 0x0F) << 4) | (indexes[2 * i + 1] & 0x0F)
    return bytes(out)


# ---------------------------------------------------------------------------
# Palettes: 16-step tint ramps applied at render time so users can recolor
# the whole DMD library from the web UI.
# ---------------------------------------------------------------------------

TINTS = {
    # The authentic 90s Williams/Bally gas-plasma DMD orange: the VPinMAME
    # built-in default (dmd_red=255, dmd_green=88, dmd_blue=32), which
    # freezy/dmd-extensions independently uses as its virtual-DMD tint.
    "amber": (255, 88, 32),
    "amber_deep": (255, 48, 0),    # dmd-extensions --color default
    "amber_yellow": (255, 224, 32),  # PinMAME core.c palette (very yellow)
    "red": (255, 0, 0),            # Stern LED DMD (AC/DC 2012 onward)
    "orange_red": (255, 69, 0),
    "green": (0, 255, 70),
    "blue": (64, 128, 255),
    "cyan": (0, 220, 255),
    "purple": (190, 80, 255),
    "white": (255, 255, 255),
    "yellow": (255, 210, 0),
}

# The real display color of each machine in the Run-DMD library.
#
# Every 128x32 pinball DMD was one of two technologies: orange gas-plasma
# (Williams/Bally WPC, Data East, Sega, Capcom, and — for US machines —
# Stern up to and including 2011) or red LED (Stern from AC/DC, 2012 on,
# once plasma supply dried up). No machine ever shipped green/blue/etc.
#
# Note the common misconception: Stern did NOT switch to red in 2006. US
# Stern SAM games through 2011 (Iron Man, Tron, Avatar, CSI, ...) were
# plasma amber; only European RoHS units of that era had LED panels.
GAME_TINTS = {
    # --- Stern LED era: red ---
    "AC#DC": "red",                    # Stern 2012 - the switchover game
    "AVENGERS": "red",                 # Stern 2012
    "X-MEN": "red",                    # Stern 2012
    "METALLICA": "red",                # Stern 2013
    "STAR_TREK": "red",                # Stern 2013
    "MUSTANG": "red",                  # Stern 2014
    "THE_WALKING_DEAD": "red",         # Stern 2014
    "KISS": "red",                     # Stern 2015
    "WRESTLEMANIA": "red",             # Stern 2015
    "GHOSTBUSTERS": "red",             # Stern 2016
    "SPIDERMAN_VE": "red",             # Stern Vault Edition 2016
    "TRANSFORMERS": "red",             # Stern 2011, first with the LED panel
    "STERN_LOGOTYPE": "red",
}
# Everything else in the library is plasma amber (Williams/Bally/Data East/
# Sega/Capcom, plus the US-plasma Stern SAM games), which is the fallback.

DEFAULT_TINT = "amber"
# Linear by default: rpi-rgb-led-matrix already applies its own CIE1931
# luminance correction, so an extra gamma here double-darkens. Run-DMD
# content is dominated by level 10 (the "lit" shade), which a 1.6 gamma
# crushed to 52% brightness — the panel looked washed out.
DEFAULT_GAMMA = 1.0


def build_palette(tint=DEFAULT_TINT, gamma=DEFAULT_GAMMA):
    """Return a 768-byte (256*3) PIL palette for 4-bit levels.

    tint: name from TINTS or an (r, g, b) tuple.
    """
    if isinstance(tint, str):
        base = TINTS.get(tint, TINTS[DEFAULT_TINT])
    else:
        base = tuple(tint)
    pal = []
    for i in range(16):
        f = (i / 15.0) ** gamma
        pal.extend([int(round(c * f)) for c in base])
    # pad remaining 240 entries
    pal.extend([0, 0, 0] * 240)
    return pal


def frame_to_image(packed, palette=None):
    """2048-byte packed frame -> PIL 'P' mode Image with palette applied."""
    from PIL import Image

    img = Image.frombytes("P", (WIDTH, HEIGHT), unpack_frame(packed))
    img.putpalette(palette if palette is not None else build_palette())
    return img


def rda_to_gif(path_or_pair, out_path, tint=DEFAULT_TINT, gamma=DEFAULT_GAMMA,
               scale=1, min_duration_ms=20):
    """Render an RDA animation to an animated GIF (for previews/export).

    path_or_pair: RDA file path, or a (header, frames) tuple.
    """
    if isinstance(path_or_pair, tuple):
        header, frames = path_or_pair
    else:
        header, frames = read_rda(path_or_pair)
    if not frames:
        raise ValueError("animation has no frames")
    palette = build_palette(tint, gamma)
    images = []
    durations = []
    for packed, dur in zip(frames, header["durations"]):
        img = frame_to_image(packed, palette)
        if scale != 1:
            img = img.resize((WIDTH * scale, HEIGHT * scale), resample=0)  # NEAREST
        images.append(img)
        durations.append(max(int(dur), min_duration_ms))
    if len(images) == 1 or len(set(frames)) == 1:
        # Single (or all-identical) frame: Pillow 5.4 dies on a duration
        # list when the GIF writer collapses to one frame.
        images[0].save(out_path, duration=sum(durations), optimize=False)
        return
    try:
        images[0].save(
            out_path,
            save_all=True,
            append_images=images[1:],
            duration=durations,
            loop=0,
            optimize=False,
            disposal=1,
        )
    except TypeError:
        # Pillow 5.4: identical consecutive frames collapsed to one and the
        # single-frame writer divided our duration list by 10.
        images[0].save(out_path, duration=sum(durations), optimize=False)
