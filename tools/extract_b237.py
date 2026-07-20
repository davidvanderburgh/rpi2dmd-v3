#!/usr/bin/env python3
"""Extract a Run-DMD B237 SD image directly into the v3 RDA library.

Users supply their own Run-DMD image (dump the SD card of a Run-DMD clock
they own, e.g. with HDDRawCopy); this tool converts it straight to
<out>/<GAME>/<GAME>_NNN.rda plus a library-wide index.json — no
third-party extraction step, no intermediate JSON.

Raw format (byte-level verified against B237):
  Main header @0: b"DGD", uint16be total_animations @3, version @495.
  512-byte animation records from 0xC800:
    [0:2] global_id BE16, [2] flags (bit0 = enabled), [3] num_bitmaps,
    [4:8] frames_addr BE32 (x512), [8] total_frames, [9] w, [10] h,
    [11] clock_type (0=NoClock,1=ClockBehind,2=ClockOnTop),
    [12] intro (1=Enable), [13] outro, [14] clock_size (0=Large,1=Small),
    [15] clock_x, [16] clock_y,
    [17] clock_start (1-based bitmap number, 0 = from animation start),
    [18] clock_end (1-based bitmap number, 0 = until animation end),
    [20:52] name (NUL-padded, 1-based per-game suffix).
  At frames_addr*512: 512-byte indirection table, total_frames entries of
  (bitmap_num 1-based [0 = pure-transparency frame], duration byte:
  low 6 bits x granularity {2,10,100,1000}ms selected by the high 2 bits),
  followed by num_bitmaps x 2048-byte 4bpp bitmaps (high nibble = left
  pixel; nibble 10 = transparency, see rpi2dmd/rda.py).

Clock windows use the corrected derivation (see rederive_clock_windows.py):
first frame referencing the start bitmap through the LAST frame
referencing the end bitmap, with 0 as the from-start/until-end sentinel.
An unreferenced start bitmap forces NoClock (matches the reference
firmware behavior for those two library animations); an unreferenced end
bitmap means "until the end". Disabled records (flags bit0 clear) are
still extracted — the player's factory config disables them by name.

Output naming matches the established library: files are numbered 0-based
per game in record ("rip") order, e.g. the raw record MEDIEVAL_MADNESS_004
becomes MEDIEVAL_MADNESS/MEDIEVAL_MADNESS_003.rda.

Usage:
    python extract_b237.py --img B237.img --out content/dmd [--limit N]
"""

import argparse
import json
import mmap
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "player"))
from rpi2dmd import rda  # noqa: E402

BLOCK = 512
RECORDS_AT = 0xC800
BITMAP_BYTES = 2048
CLOCK_TYPES = {0: "NoClock", 1: "ClockBehind", 2: "ClockOnTop"}
CLOCK_SIZES = {0: "ClockLarge", 1: "ClockSmall"}
ENABLE = {0: "Disable", 1: "Enable"}
DUR_GRAN = (2, 10, 100, 1000)
TRANSPARENT_FRAME = b"\xaa" * BITMAP_BYTES


def parse_record(rec):
    return {
        "flags": rec[2],
        "num_bitmaps": rec[3],
        "frames_addr": int.from_bytes(rec[4:8], "big") * BLOCK,
        "total_frames": rec[8],
        "clock_type": CLOCK_TYPES.get(rec[11], "NoClock"),
        "intro": ENABLE.get(rec[12] & 1, "Disable"),
        "outro": ENABLE.get(rec[13] & 1, "Disable"),
        "clock_size": CLOCK_SIZES.get(rec[14], "ClockLarge"),
        "clock_x": rec[15],
        "clock_y": rec[16],
        "clock_start_raw": rec[17],
        "clock_end_raw": rec[18],
        "name": rec[20:52].rstrip(b"\x00").decode("ascii", "replace"),
    }


def decode_duration(enc):
    return (enc & 0x3F) * DUR_GRAN[(enc >> 6) & 0x3]


def clock_window(rec, table):
    """-> (clock_type, start_frame, end_frame), corrected derivation."""
    ctype = rec["clock_type"]
    last = max(0, len(table) - 1)
    if ctype == "NoClock":
        return ctype, 0, last
    refs = {}
    for fr, (bn, _d) in enumerate(table):
        refs.setdefault(bn, []).append(fr)
    sb = rec["clock_start_raw"]
    if sb == 0:
        start = 0
    elif sb in refs:
        start = refs[sb][0]
    else:
        return "NoClock", 0, last     # firmware-parity forced NoClock
    eb = rec["clock_end_raw"]
    if eb == 0 or eb not in refs:
        end = last
    else:
        end = refs[eb][-1]
    return ctype, start, end


def extract(img_path, out_dir, limit=None):
    fh = open(img_path, "rb")
    mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        if mm[0:3] != b"DGD":
            raise ValueError("not a Run-DMD image (missing DGD marker): %s"
                             % img_path)
        total = int.from_bytes(mm[3:5], "big")
        version = mm[495:499].decode("ascii", "replace")
        print("Run-DMD image: %d animations, version %s" % (total, version))
        if limit:
            total = min(total, limit)

        per_game = {}
        entries = []
        for i in range(total):
            off = RECORDS_AT + i * BLOCK
            rec = parse_record(bytes(mm[off:off + 52]))
            if not rec["name"] or rec["total_frames"] == 0:
                continue
            game = rec["name"][:rec["name"].rfind("_")]
            n = per_game.get(game, 0)
            per_game[game] = n + 1
            name = "%s_%03d" % (game, n)

            taddr = rec["frames_addr"]
            table = [(mm[taddr + j * 2], mm[taddr + j * 2 + 1])
                     for j in range(rec["total_frames"])]
            frames = []
            durations = []
            for bn, denc in table:
                if bn == 0:
                    frames.append(TRANSPARENT_FRAME)
                else:
                    a = taddr + BLOCK + (bn - 1) * BITMAP_BYTES
                    frames.append(bytes(mm[a:a + BITMAP_BYTES]))
                durations.append(decode_duration(denc))

            ctype, start, end = clock_window(rec, table)
            header = {
                "name": name,
                "game": game,
                "durations": durations,
                "clock": {
                    "type": ctype,
                    "size": rec["clock_size"],
                    "x": rec["clock_x"],
                    "y": rec["clock_y"],
                    "start_frame": start,
                    "end_frame": end,
                },
                "intro_transition": rec["intro"],
                "outro_transition": rec["outro"],
            }
            gdir = os.path.join(out_dir, game)
            if not os.path.isdir(gdir):
                os.makedirs(gdir)
            rda.write_rda(os.path.join(gdir, name + ".rda"), header, frames)
            entries.append({
                "game": game,
                "name": name,
                "file": "%s/%s.rda" % (game, name),
                "frames": len(frames),
                "duration_ms": sum(max(d, 20) for d in durations),
                "clock_type": ctype,
            })
            if (i + 1) % 400 == 0:
                print("  %d/%d" % (i + 1, total))

        games = {}
        for e in entries:
            games.setdefault(e["game"], []).append(
                {k: e[k] for k in ("name", "file", "frames", "duration_ms",
                                   "clock_type")})
        index = {
            "format": "rda1",
            "source": "Run-DMD %s" % version,
            "num_games": len(games),
            "num_animations": len(entries),
            "games": {g: sorted(v, key=lambda x: x["name"])
                      for g, v in sorted(games.items())},
        }
        with open(os.path.join(out_dir, "index.json"), "w",
                  encoding="utf-8") as f:
            json.dump(index, f, indent=1)
        print("done: %d games, %d animations, %d frames"
              % (len(games), len(entries),
                 sum(e["frames"] for e in entries)))
        return len(entries)
    finally:
        mm.close()
        fh.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--img", required=True, help="Run-DMD .img (e.g. B237)")
    ap.add_argument("--out", required=True, help="output dmd library dir")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    n = extract(args.img, args.out, args.limit)
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
