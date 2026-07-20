#!/usr/bin/env python3
"""RPI2DMD v3 image builder — one command from your own source content
to a flashable SD image.

This repo ships NO copyrighted content. You supply the artifacts you own
in an inputs directory (see builder/BUILDING.md for how to obtain each);
every pack except the v2 base image is optional — cherry-pick freely:

  RPI2DMD v2 base image (.img)      REQUIRED   OS base + stock media
  Run-DMD SD dump (.img or .zip)    optional   2,379 DMD animations
  GIF packs (.zip or folders)       optional   any number, merged

Pipeline: identify inputs (builder/packs.py) -> extract -> convert the
Run-DMD image to the RDA library (tools/extract_b237.py) -> pull stock
media from the v2 image (WSL) -> merge GIF packs -> pre-decode the RGF
cache (tools/build_gif_cache.py) -> assemble + build the image in WSL
(image-build/build.sh) -> verify (image-build/verify-image.sh).

Host requirements: Windows 10/11 with WSL2 (Ubuntu-like distro with
sfdisk/losetup/dosfstools/e2fsprogs/rsync/curl/python3/qemu-arm-static),
or run the WSL steps natively on Linux as root. Python 3.10+, ~20 GB
free disk, network access (the chroot stage downloads packages).

Usage:
    python builder/build.py --inputs <dir> [--out RPI2DMD_v3.img]
        [--size-gb 7.0] [--work <dir>] [--content-only] [--skip-verify]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.join(REPO, "player"))

import packs  # noqa: E402

WSL_BUILD_DIR = "/root/rpi2dmd-build"


def log(msg):
    print("[builder] %s" % msg, flush=True)


def fail(msg):
    print("[builder] ERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def wsl_path(win_path):
    p = os.path.abspath(win_path)
    return "/mnt/%s%s" % (p[0].lower(), p[2:].replace("\\", "/"))


def run_wsl(args, what):
    log(what)
    r = subprocess.run(["wsl", "-u", "root", "-e"] + args)
    if r.returncode != 0:
        fail("%s failed (exit %d)" % (what, r.returncode))


def on_windows():
    return os.name == "nt"


def check_host():
    if on_windows():
        r = subprocess.run(["wsl", "-u", "root", "-e", "true"],
                           capture_output=True)
        if r.returncode != 0:
            fail("WSL2 with a root-capable distro is required "
                 "(install Ubuntu from the Microsoft Store)")
    elif os.geteuid() != 0:
        fail("on Linux, run as root (the image build loop-mounts)")


def count_gifs(root):
    if not os.path.isdir(root):
        return 0, 0
    files, cats = 0, 0
    for entry in os.listdir(root):
        sub = os.path.join(root, entry)
        if os.path.isdir(sub):
            n = len([f for f in os.listdir(sub)
                     if f.lower().endswith(".gif")])
            if n:
                cats += 1
                files += n
    return files, cats


def union_gif_count(roots):
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cat in os.listdir(root):
            cdir = os.path.join(root, cat)
            if os.path.isdir(cdir):
                for f in os.listdir(cdir):
                    if f.lower().endswith(".gif"):
                        # case-fold both parts: the target FAT volume is
                        # case-insensitive
                        seen.add((cat.lower(), f.lower()))
    return len(seen)


def main():
    ap = argparse.ArgumentParser(
        description="Build an RPI2DMD v3 SD image from your own content")
    ap.add_argument("--inputs", required=True,
                    help="directory with your source artifacts")
    ap.add_argument("--out", default=os.path.join(os.getcwd(),
                                                  "RPI2DMD_v3.img"))
    ap.add_argument("--size-gb", type=float, default=7.0)
    ap.add_argument("--work",
                    default=os.path.join(os.getcwd(), "builder-work"))
    ap.add_argument("--content-only", action="store_true",
                    help="assemble content only; skip the WSL image build")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.inputs):
        fail("inputs directory not found: %s" % args.inputs)
    check_host()
    os.makedirs(args.work, exist_ok=True)
    content = os.path.join(args.work, "content")
    os.makedirs(content, exist_ok=True)

    # ---- identify -------------------------------------------------------
    found, unknown = packs.scan_inputs(args.inputs)
    log("inputs: %s" % args.inputs)
    for p in found:
        log("  found %-13s %s  (%s)"
            % (p.kind, os.path.basename(p.path), p.title))
    for name in unknown:
        log("  UNRECOGNIZED (ignored): %s" % name)

    base = [p for p in found if p.kind == "base-image"]
    rundmd = [p for p in found if p.kind == "rundmd-image"]
    gifpacks = [p for p in found if p.kind == "gif-pack"]
    if not base:
        fail("no v2 base image found in inputs — this one is required "
             "(see builder/BUILDING.md)")
    if len(base) > 1 or len(rundmd) > 1:
        fail("multiple base or Run-DMD images found — keep exactly one "
             "of each in the inputs directory")
    if not rundmd:
        log("NOTE: no Run-DMD image supplied — the built image will have "
            "no DMD animation library (GIFs and clock only)")
    if not gifpacks:
        log("NOTE: no GIF packs supplied — only the stock v2 GIFs from "
            "your base image will be included")

    # ---- extract images -------------------------------------------------
    v2_img = packs.extract_image(base[0], args.work)
    log("base image: %s" % v2_img)

    # ---- DMD library ----------------------------------------------------
    dmd_dir = os.path.join(content, "dmd")
    if rundmd:
        rd_img = packs.extract_image(rundmd[0], args.work)
        marker = os.path.join(dmd_dir, "index.json")
        if os.path.exists(marker) and \
                json.load(open(marker)).get("num_animations", 0) > 0:
            log("DMD library already extracted (delete %s to redo)"
                % dmd_dir)
        else:
            log("extracting DMD animation library from %s"
                % os.path.basename(rd_img))
            import extract_b237
            os.makedirs(dmd_dir, exist_ok=True)
            extract_b237.extract(rd_img, dmd_dir)
    else:
        os.makedirs(dmd_dir, exist_ok=True)
        with open(os.path.join(dmd_dir, "index.json"), "w") as f:
            json.dump({"format": "rda1", "source": "none", "num_games": 0,
                       "num_animations": 0, "games": {}}, f)

    # ---- stock media from the v2 image ----------------------------------
    media_base = os.path.join(content, "media-base")
    if not os.path.isdir(os.path.join(media_base, "fonts")):
        helper = os.path.join(HERE, "extract-v2-media.sh")
        if on_windows():
            run_wsl(["bash", wsl_path(helper), wsl_path(v2_img),
                     wsl_path(media_base)],
                    "extracting stock media from the v2 image (WSL)")
        else:
            r = subprocess.run(["bash", helper, v2_img, media_base])
            if r.returncode != 0:
                fail("v2 media extraction failed")
    else:
        log("stock media already extracted")

    # ---- GIF packs ------------------------------------------------------
    extra = os.path.join(content, "gif-extra")
    trees = []
    for p in sorted(gifpacks, key=lambda x: x.priority):
        root = packs.extract_gif_pack(p, args.work)
        if root:
            trees.append((p.priority, root))
    if trees:
        n = packs.merge_gif_trees(trees, extra)
        log("gif-extra: %d gifs from %d pack(s)" % (n, len(trees)))

    # ---- RGF cache (pre-decoded GIFs; see docs/contracts.md) ------------
    merged = os.path.join(args.work, "gif-merged")
    src_trees = [(0, os.path.join(media_base, "gif"))] + trees
    packs.merge_gif_trees(src_trees, merged)
    cache_dir = os.path.join(content, "gif-cache")
    log("building the pre-decoded GIF cache (this is the fast part of "
        "playback on the Pi — a few minutes on a desktop)")
    import build_gif_cache
    rc = build_gif_cache.main_with_args(merged, cache_dir, min_frames=8)
    if rc not in (0, None):
        log("WARNING: some GIFs failed to convert (they will be skipped "
            "on the device); continuing")

    # ---- counts for verification ----------------------------------------
    total_gifs = union_gif_count([os.path.join(media_base, "gif"), extra])
    rdas = sum(1 for _, _, files in os.walk(dmd_dir)
               for f in files if f.endswith(".rda"))
    _, cats = count_gifs(merged)
    log("content ready: %d gifs in %d categories, %d dmd files"
        % (total_gifs, cats, rdas))

    if args.content_only:
        log("content-only run complete: %s" % content)
        return 0

    # ---- image build in WSL ---------------------------------------------
    if on_windows():
        B = WSL_BUILD_DIR
        run_wsl(["mkdir", "-p", B + "/content"], "prepare WSL build dir")
        run_wsl(["rsync", "-a", "--delete", "--exclude", ".git",
                 "--exclude", "__pycache__", wsl_path(REPO) + "/",
                 B + "/repo/"], "copy repo into WSL")
        run_wsl(["rsync", "-t", "--inplace", wsl_path(v2_img),
                 B + "/base.img"], "copy v2 base image into WSL")
        run_wsl(["rsync", "-a", "--delete", wsl_path(content) + "/",
                 B + "/content/"], "copy content into WSL (can take a while)")
        run_wsl(["bash", B + "/repo/image-build/build.sh",
                 "--v2-img", B + "/base.img", "--repo", B + "/repo",
                 "--content", B + "/content", "--out", B + "/out.img",
                 "--size-gb", "%g" % args.size_gb],
                "build.sh (chroot stage can take 30-90 min)")
        if not args.skip_verify:
            run_wsl(["bash", B + "/repo/image-build/verify-image.sh",
                     "--img", B + "/out.img",
                     "--min-rdas", str(max(0, rdas)),
                     "--min-gifs", str(total_gifs),
                     "--min-cats", str(max(1, cats))],
                    "verify-image.sh")
        out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
        run_wsl(["cp", B + "/out.img",
                 wsl_path(out_dir) + "/" + os.path.basename(args.out)],
                "copy image back to Windows")
    else:
        build_sh = os.path.join(REPO, "image-build", "build.sh")
        r = subprocess.run(["bash", build_sh, "--v2-img", v2_img,
                            "--repo", REPO, "--content", content,
                            "--out", args.out,
                            "--size-gb", "%g" % args.size_gb])
        if r.returncode != 0:
            fail("build.sh failed")
        if not args.skip_verify:
            verify = os.path.join(REPO, "image-build", "verify-image.sh")
            r = subprocess.run(["bash", verify, "--img", args.out,
                                "--min-rdas", str(max(0, rdas)),
                                "--min-gifs", str(total_gifs),
                                "--min-cats", str(max(1, cats))])
            if r.returncode != 0:
                fail("verification failed")

    log("DONE: %s" % args.out)
    log("Flash with Raspberry Pi Imager (Use custom) or Win32DiskImager.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
