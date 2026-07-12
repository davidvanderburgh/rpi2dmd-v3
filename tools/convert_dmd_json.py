"""Convert extracted Run-DMD JSON animations to the packed RDA library.

Input:  D:\\Pinball\\dmd\\<GAME>\\<GAME>_NNN.json  (native 128x32 4bpp frames,
        rows encoded as "|" + 128 hex nibbles + "|", plus clock metadata)
Output: <out>/<GAME>/<GAME>_NNN.rda plus a library-wide index.json

Runs on the host (Windows, Python 3.10+). Usage:
    python convert_dmd_json.py --src "D:\\Pinball\\dmd" --out "..\\..\\v3-content\\dmd"
"""

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "player"))
from rpi2dmd import rda  # noqa: E402


def parse_bitmap(bitmap_rows):
    """32 rows of '|<128 hex>|' -> 2048-byte packed frame."""
    if len(bitmap_rows) != rda.HEIGHT:
        raise ValueError("expected %d rows, got %d" % (rda.HEIGHT, len(bitmap_rows)))
    packed = bytearray(rda.FRAME_BYTES)
    pos = 0
    for row in bitmap_rows:
        if len(row) != rda.WIDTH + 2 or row[0] != "|" or row[-1] != "|":
            raise ValueError("bad row format: %r" % row[:12])
        hexrow = row[1:-1]
        rowbytes = bytes.fromhex(hexrow)  # 2 nibbles/byte, high nibble first
        packed[pos:pos + 64] = rowbytes
        pos += 64
    return bytes(packed)


def convert_one(job):
    src_path, out_path, game, name = job
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    h = data["header"]
    frames = []
    durations = []
    for fr in data["frames"]:
        frames.append(parse_bitmap(fr["bitmap"]))
        durations.append(int(fr["duration"]))
    header = {
        "name": name,
        "game": game,
        "durations": durations,
        "clock": {
            "type": h["clock_type"],
            "size": h["clock_size"],
            "x": h["clock_position_x"],
            "y": h["clock_position_y"],
            "start_frame": h["clock_start_frame"],
            "end_frame": h["clock_end_frame"],
        },
        "intro_transition": h["intro_transition"],
        "outro_transition": h["outro_transition"],
    }
    rda.write_rda(out_path, header, frames)
    total_ms = sum(max(d, 20) for d in durations)
    return {
        "game": game,
        "name": name,
        "file": "%s/%s.rda" % (game, name),
        "frames": len(frames),
        "duration_ms": total_ms,
        "clock_type": h["clock_type"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=r"D:\Pinball\dmd")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    jobs = []
    for game in sorted(os.listdir(args.src)):
        gdir = os.path.join(args.src, game)
        if not os.path.isdir(gdir):
            continue
        os.makedirs(os.path.join(args.out, game), exist_ok=True)
        for fn in sorted(os.listdir(gdir)):
            if not fn.lower().endswith(".json"):
                continue
            name = os.path.splitext(fn)[0]
            jobs.append((
                os.path.join(gdir, fn),
                os.path.join(args.out, game, name + ".rda"),
                game,
                name,
            ))

    print("converting %d animations from %s" % (len(jobs), args.src))
    entries = []
    errors = []
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for i, result in enumerate(ex.map(convert_one, jobs, chunksize=16)):
            entries.append(result)
            if (i + 1) % 400 == 0:
                print("  %d/%d" % (i + 1, len(jobs)))

    games = {}
    for e in entries:
        games.setdefault(e["game"], []).append(
            {k: e[k] for k in ("name", "file", "frames", "duration_ms", "clock_type")}
        )
    index = {
        "format": "rda1",
        "source": "Run-DMD B237",
        "num_games": len(games),
        "num_animations": len(entries),
        "games": {g: sorted(v, key=lambda x: x["name"]) for g, v in sorted(games.items())},
    }
    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)

    total_frames = sum(e["frames"] for e in entries)
    print("done: %d games, %d animations, %d frames" %
          (len(games), len(entries), total_frames))
    if errors:
        print("ERRORS: %d" % len(errors))
        for e in errors[:10]:
            print("  ", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
