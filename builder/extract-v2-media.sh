#!/bin/bash
# Extract gif/ and fonts/ from the media partition (p3) of a user-supplied
# RPI2DMD v2 image. Run as root inside WSL/Linux by builder/build.py.
#
# Usage: extract-v2-media.sh <v2.img> <out-dir>

set -u
IMG=${1:?usage: extract-v2-media.sh <v2.img> <out-dir>}
OUT=${2:?out dir required}

fail() { echo "[v2-media] FAIL: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "must run as root"
[ -f "$IMG" ] || fail "image not found: $IMG"

LOOP=$(losetup -P -f --show -r "$IMG") || fail "losetup failed"
M=$(mktemp -d)
cleanup() { umount "$M" 2>/dev/null; losetup -d "$LOOP" 2>/dev/null; rmdir "$M" 2>/dev/null; }
trap cleanup EXIT

PART="${LOOP}p3"
[ -b "$PART" ] || fail "media partition ${PART} not found (is this a v2 image?)"
mount -o ro "$PART" "$M" || fail "mount of media partition failed"
[ -d "$M/gif" ] || fail "no gif/ on the media partition (is this a v2 image?)"
[ -d "$M/fonts" ] || fail "no fonts/ on the media partition"

mkdir -p "$OUT"
rsync -rt --delete "$M/gif/" "$OUT/gif/" || fail "gif copy failed"
rsync -rt --delete "$M/fonts/" "$OUT/fonts/" || fail "fonts copy failed"
GIFS=$(find "$OUT/gif" -type f -iname '*.gif' | wc -l)
FONTS=$(find "$OUT/fonts" -type f | wc -l)
echo "[v2-media] OK: $GIFS stock gifs, $FONTS font files"
