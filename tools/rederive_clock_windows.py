#!/usr/bin/env python3
"""Re-derive per-animation clock frame windows from a raw Run-DMD image.

Background
----------
The original Run-DMD extractor (RunDMD-Utils RunDmdImage.py) converted the
raw per-animation clock window, which is stored as a pair of 1-based BITMAP
numbers, into 0-based FRAME indexes by taking the FIRST frame that
references each bitmap. That is correct for the start bound but wrong for
the end bound: when the end bitmap is shown by several frames (or when
frames revisit bitmaps out of order) the derived end_frame lands before the
real end of the window, producing artifacts such as end_frame == 0 or
end_frame < start_frame, which the player then misinterprets.

This tool re-derives the window from the raw image for every animation
whose raw record enables a clock, and repairs the RDA library headers that
diverge. Frame data is never modified; after each header rewrite the file
is re-read and every frame byte is asserted identical.

Raw format facts (byte-level verified against B237.img)
-------------------------------------------------------
* 512-byte animation records start at offset 0xC800.
  [0:2]  global_id (BE16)          [3]  num_bitmaps
  [4:8]  frames_addr (BE32, x512)  [8]  total_frames
  [11]   clock_type (0=NoClock, 1=ClockBehind, 2=ClockOnTop)
  [14]   clock_size  [15] clock_x  [16] clock_y
  [17]   clock_start: 1-based bitmap number, 0 = "from animation start"
  [18]   clock_end:   1-based bitmap number, 0 = "until animation end"
  [20:52] name (NUL-padded ASCII, 1-based per-game suffix)
* At frames_addr*512: a 512-byte indirection table with total_frames
  entries of (bitmap_num 1-based; 0 = pure-transparency frame, duration).
  The bitmaps (2048 bytes each) follow the table.
* RDA file <GAME>/<GAME>_NNN.rda maps to the (NNN+1)-th raw record of that
  game in raw record order (the extractor renumbered 0-based in rip order).

Correct window derivation (0-based, inclusive)
----------------------------------------------
  start_frame = 0                 if raw clock_start == 0
              = index of FIRST frame with bitmap_num == raw clock_start
  end_frame   = total_frames - 1  if raw clock_end == 0
              = index of LAST  frame with bitmap_num == raw clock_end
If a referenced bitmap number never appears in the indirection table, the
existing header value for that bound is kept and the case is reported as
an anomaly (the extractor forced such animations to NoClock).

Usage
-----
  python rederive_clock_windows.py --img B237.img --lib v3-content/dmd
      [--apply] [--report changes.json] [--changed-list files.txt]
      [--max-changes 400]

Default is a dry run: it prints what would change and writes nothing.
"""

import argparse
import copy
import json
import mmap
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "player"))
from rpi2dmd import rda  # noqa: E402

BLOCK = 512
HDR0 = 0xC800          # first animation record
NAME_OFF, NAME_LEN = 20, 32
CLOCK_TYPES = {0: "NoClock", 1: "ClockBehind", 2: "ClockOnTop"}


def parse_record(rec):
    """52 header bytes of a raw animation record -> field dict."""
    name = rec[NAME_OFF:NAME_OFF + NAME_LEN].rstrip(b"\x00")
    return {
        "global_id": int.from_bytes(rec[0:2], "big"),
        "num_bitmaps": rec[3],
        "frames_addr": int.from_bytes(rec[4:8], "big") * BLOCK,
        "total_frames": rec[8],
        "clock_type": rec[11],
        "clock_start": rec[17],   # 1-based bitmap number, 0 = from start
        "clock_end": rec[18],     # 1-based bitmap number, 0 = until end
        "name": name.decode("ascii", "replace"),
    }


def load_records(mm):
    """Parse every animation record; attach the extractor file name.

    The extractor named files <GAME>_<NNN> with NNN counting that game's
    records 0-based in raw record order, so a per-game counter over the
    records in image order reproduces the on-disk file names exactly.
    """
    if mm[0:3] != b"DGD":
        raise SystemExit("not a Run-DMD image (missing DGD marker)")
    total = int.from_bytes(mm[3:5], "big")
    records = []
    per_game_count = {}
    for i in range(total):
        off = HDR0 + i * BLOCK
        f = parse_record(bytes(mm[off:off + 52]))
        game = f["name"][:f["name"].rfind("_")]
        n = per_game_count.get(game, 0)
        per_game_count[game] = n + 1
        f["game"] = game
        f["file_name"] = "%s_%03d" % (game, n)
        records.append(f)
    return records


def indirection_bitmaps(mm, f):
    """Frame indirection table -> list of 1-based bitmap numbers per frame."""
    t = mm[f["frames_addr"]:f["frames_addr"] + BLOCK]
    return [t[i * 2] for i in range(f["total_frames"])]


def derive_window(f, bitmaps):
    """-> (start_frame, end_frame, anomalies) with None for an
    unresolvable bound (referenced bitmap absent from the table)."""
    anomalies = []
    if f["clock_start"] == 0:
        start = 0
    else:
        hits = [i for i, bn in enumerate(bitmaps) if bn == f["clock_start"]]
        if hits:
            start = hits[0]
        else:
            start = None
            anomalies.append("start bitmap %d unreferenced" % f["clock_start"])
    if f["clock_end"] == 0:
        end = f["total_frames"] - 1
    else:
        hits = [i for i, bn in enumerate(bitmaps) if bn == f["clock_end"]]
        if hits:
            end = hits[-1]
        else:
            end = None
            anomalies.append("end bitmap %d unreferenced" % f["clock_end"])
    return start, end, anomalies


def rewrite_header(path, new_start, new_end):
    """Rewrite only clock.start_frame/end_frame; verify frames untouched."""
    header, frames = rda.read_rda(path)
    new_header = copy.deepcopy(header)
    new_header["clock"]["start_frame"] = new_start
    new_header["clock"]["end_frame"] = new_end
    rda.write_rda(path, new_header, frames)

    # Verification: frames byte-identical, all other header fields intact.
    got_header, got_frames = rda.read_rda(path)
    if len(got_frames) != len(frames) or any(
            a != b for a, b in zip(got_frames, frames)):
        raise SystemExit("FRAME CORRUPTION after rewrite of %s" % path)
    expect = copy.deepcopy(header)
    expect["clock"]["start_frame"] = new_start
    expect["clock"]["end_frame"] = new_end
    if got_header != expect:
        raise SystemExit("HEADER DIVERGENCE after rewrite of %s" % path)


def main():
    ap = argparse.ArgumentParser(
        description="Re-derive clock frame windows from a raw Run-DMD "
                    "image and fix diverging RDA library headers.")
    ap.add_argument("--img", required=True, help="raw Run-DMD image (B237.img)")
    ap.add_argument("--lib", required=True,
                    help="RDA library root (…/v3-content/dmd)")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite diverging headers (default: dry run)")
    ap.add_argument("--report", help="write change report JSON here")
    ap.add_argument("--changed-list",
                    help="write changed .rda absolute paths here, one per line")
    ap.add_argument("--max-changes", type=int, default=400,
                    help="safety gate: refuse to apply if more than this many "
                         "headers would change (default 400)")
    args = ap.parse_args()

    fh = open(args.img, "rb")
    mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    records = load_records(mm)
    clocked = [f for f in records if f["clock_type"] != 0]
    print("records: %d total, %d with clock enabled" %
          (len(records), len(clocked)))

    changes, anomalies, missing = [], [], []
    for f in clocked:
        path = os.path.abspath(os.path.join(
            args.lib, f["game"], f["file_name"] + ".rda"))
        if not os.path.exists(path):
            missing.append(f["file_name"])
            continue
        header = rda.read_header(path)
        clk = header["clock"]
        old_start, old_end = clk["start_frame"], clk["end_frame"]

        bitmaps = indirection_bitmaps(mm, f)
        start, end, notes = derive_window(f, bitmaps)
        for n in notes:
            anomalies.append({
                "name": f["file_name"], "game": f["game"], "reason": n,
                "raw_start": f["clock_start"], "raw_end": f["clock_end"],
                "rda_clock_type": clk["type"],
            })
        if start is None:
            start = old_start   # unresolvable bound: keep header value
        if end is None:
            end = old_end

        if (start, end) != (old_start, old_end):
            changes.append({
                "game": f["game"], "name": f["file_name"],
                "old_start": old_start, "old_end": old_end,
                "new_start": start, "new_end": end,
                "num_frames": f["total_frames"], "path": path,
            })

    mm.close()
    fh.close()

    print("scanned %d clocked animations with RDA files; %d headers %s; "
          "%d anomalies; %d missing RDA files" %
          (len(clocked) - len(missing), len(changes),
           "changed" if args.apply else "would change",
           len(anomalies), len(missing)))
    for c in changes:
        print("  %-34s frames=%-3d  start %3d -> %3d   end %3d -> %3d" %
              (c["name"], c["num_frames"], c["old_start"], c["new_start"],
               c["old_end"], c["new_end"]))
    for a in anomalies:
        print("  ANOMALY %-30s %s (raw start=%d end=%d, rda type=%s)" %
              (a["name"], a["reason"], a["raw_start"], a["raw_end"],
               a["rda_clock_type"]))
    for m in missing:
        print("  MISSING RDA for %s" % m)

    if len(changes) > args.max_changes:
        raise SystemExit(
            "SAFETY GATE: %d changes exceeds --max-changes %d; not applying."
            % (len(changes), args.max_changes))

    if args.apply:
        for c in changes:
            rewrite_header(c["path"], c["new_start"], c["new_end"])
        print("applied %d header rewrites; all frame bytes verified "
              "identical" % len(changes))
    else:
        print("dry run: no files written (use --apply)")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump([{k: c[k] for k in ("game", "name", "old_start",
                                          "old_end", "new_start", "new_end",
                                          "num_frames")}
                       for c in changes], f, indent=1)
        print("report: %s" % args.report)
    if args.changed_list:
        with open(args.changed_list, "w", encoding="utf-8") as f:
            for c in changes:
                f.write(c["path"] + "\n")
        print("changed-file list: %s" % args.changed_list)


if __name__ == "__main__":
    main()
