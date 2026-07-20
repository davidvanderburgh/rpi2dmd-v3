"""One-time desktop-side RGF cache builder for the RPI2DMD v3 GIF library.

Pillow's GIF plugin costs ~90-230ms PER FRAME on the Pi Zero (measured),
so long clips took tens of seconds to a minute+ to decode — the panel sat
on the clock (or, pre-fix, froze) meanwhile. This tool decodes each heavy
GIF once on the PC using the player's own pipeline (identical look) and
writes a gif-cache/<Category>/<name>.gif.rgf sidecar the player loads in
~a second. The .gif files themselves are untouched — the web UI and any
GIF not cached keep working exactly as before.

Usage:
    python build_gif_cache.py SRC_GIF_DIR OUT_CACHE_DIR
        [--min-frames 100] [--workers N] [--force] [--limit N]

SRC_GIF_DIR contains the category folders (a gif/ directory);
OUT_CACHE_DIR gets matching category folders of .gif.rgf files.
Existing outputs are skipped (resumable); --force rebuilds.
"""

import argparse
import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "player"))

TARGET = (128, 32)
MIN_FRAMES = 100

# driver.show() costs ~33ms p50 / ~50ms p90 on the Pi Zero (the matrix
# binding's SetImage walks every pixel even on its fast path), so holds
# below ~66ms cannot be paced reliably — such clips stuttered. Clips whose
# median hold is faster are retimed onto a uniform 66ms grid (15fps):
# standard resampling, total duration preserved, frame chosen per tick.
MIN_HOLD_MS = 66


def retime_fast_clip(frames, min_hold=MIN_HOLD_MS):
    """[(img, ms)] -> same clip resampled to uniform >=min_hold holds when
    its median hold is below min_hold; unchanged otherwise."""
    if not frames:
        return frames
    holds = sorted(d for _, d in frames)
    if holds[len(holds) // 2] >= min_hold:
        return frames
    total = sum(d for _, d in frames)
    starts = []
    s = 0
    for _, d in frames:
        starts.append(s)
        s += d
    out = []
    idx = 0
    tick = 0
    while tick < total:
        while idx + 1 < len(frames) and starts[idx + 1] <= tick:
            idx += 1
        out.append([frames[idx][0], min_hold])
        tick += min_hold
    out[-1][1] = max(20, total - min_hold * (len(out) - 1))
    return [(img, d) for img, d in out]


def build_one(job):
    """(src, dst, force) -> (status, src, frames, src_b, out_b, err)."""
    src, dst, force = job
    from PIL import Image
    from rpi2dmd import rgf, scenes

    try:
        if os.path.exists(dst) and not force:
            return ("skipped", src, 0, 0, os.path.getsize(dst), "")
        with Image.open(src) as probe:
            nf = getattr(probe, "n_frames", 1)
        if nf < MIN_FRAMES:
            return ("under_threshold", src, nf, 0, 0, "")
        frames = scenes._load_image_frames(src, target=TARGET)
        frames = retime_fast_clip(frames)
        rgf.write_rgf(dst, frames, src_size=os.path.getsize(src))
        return ("built", src, len(frames), os.path.getsize(src),
                os.path.getsize(dst), "")
    except Exception as e:
        return ("failed", src, 0, 0, 0, "%s: %s" % (type(e).__name__, e))


def find_jobs(src_root, out_root, force):
    jobs = []
    for cat in sorted(os.listdir(src_root)):
        cdir = os.path.join(src_root, cat)
        if not os.path.isdir(cdir):
            continue
        for f in sorted(os.listdir(cdir)):
            if f.lower().endswith(".gif"):
                jobs.append((os.path.join(cdir, f),
                             os.path.join(out_root, cat, f + ".rgf"),
                             force))
    return jobs


def main():
    global TARGET, MIN_FRAMES
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--size", default="128x32")
    ap.add_argument("--min-frames", type=int, default=100)
    ap.add_argument("--workers", type=int,
                    default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    w, h = args.size.lower().split("x")
    TARGET = (int(w), int(h))
    MIN_FRAMES = args.min_frames

    jobs = find_jobs(args.src, args.out, args.force)
    if args.limit:
        jobs = jobs[:args.limit]
    print("%d gifs, %d workers, target %dx%d, min-frames %d"
          % (len(jobs), args.workers, TARGET[0], TARGET[1], MIN_FRAMES))

    t0 = time.time()
    counts = {"built": 0, "under_threshold": 0, "skipped": 0, "failed": 0}
    built_frames = built_src = built_out = 0
    failures = []
    pool = multiprocessing.Pool(args.workers)
    try:
        for i, (status, src, nf, sb, ob, err) in enumerate(
                pool.imap_unordered(build_one, jobs, chunksize=16), 1):
            counts[status] += 1
            if status == "built":
                built_frames += nf
                built_src += sb
                built_out += ob
            elif status == "failed":
                failures.append((src, err))
            if i % 500 == 0 or i == len(jobs):
                rate = i / max(0.001, time.time() - t0)
                print("%6d/%d  %.0f/s  eta %4.0fs  built=%d cache=%.0fMB"
                      % (i, len(jobs), rate,
                         (len(jobs) - i) / max(0.001, rate),
                         counts["built"], built_out / 1e6))
                sys.stdout.flush()
    finally:
        pool.close()
        pool.join()

    print("\ndone in %.0fs: %s" % (time.time() - t0, counts))
    print("cache: %d clips, %d frames, %.0f MB gif -> %.0f MB rgf "
          "(%.2f KB/frame)"
          % (counts["built"], built_frames, built_src / 1e6,
             built_out / 1e6,
             built_out / 1e3 / max(1, built_frames)))
    if failures:
        print("\nFAILED (%d):" % len(failures))
        for src, err in failures[:50]:
            print("  %s  %s" % (src, err))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
