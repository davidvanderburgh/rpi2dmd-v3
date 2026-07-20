#!/bin/bash
# Verify a built RPI2DMD v3 image (read-only): partition table, boot files,
# rootfs overlay, installed python stack, media content, plus qemu-chroot
# import checks. Prints PASS/FAIL lines; exits nonzero if anything FAILs.
#
# Usage: verify-image.sh --img PATH [--lite]
#                        [--min-rdas N] [--min-gifs N] [--min-cats N]
#
# The --min-* overrides exist for builder-assembled images where content
# is user-supplied and cherry-picked: the orchestrator passes the counts
# it actually staged instead of the full-library defaults.

set -u

IMG=
LITE=0
MIN_RDAS= MIN_GIFS= MIN_CATS=
while [ $# -gt 0 ]; do
    case "$1" in
        --img)  IMG=$2; shift 2 ;;
        --lite) LITE=1; shift ;;
        --min-rdas) MIN_RDAS=$2; shift 2 ;;
        --min-gifs) MIN_GIFS=$2; shift 2 ;;
        --min-cats) MIN_CATS=$2; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$IMG" ] && [ -f "$IMG" ] || { echo "usage: verify-image.sh --img PATH [--lite]" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 2; }

NPASS=0; NFAIL=0
ok()  { echo "PASS: $*"; NPASS=$((NPASS + 1)); }
bad() { echo "FAIL: $*"; NFAIL=$((NFAIL + 1)); }
check() { # check <description> <command...>
    local desc=$1; shift
    if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}

WORK=$(mktemp -d /tmp/rpi2dmd-verify.XXXXXX)
B="$WORK/boot"; R="$WORK/root"; M="$WORK/media"
mkdir -p "$B" "$R" "$M"
LOOP=

cleanup() {
    set +e
    for m in "$M" "$R" "$B"; do
        mountpoint -q "$m" 2>/dev/null && { umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null; }
    done
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Partition table
# ---------------------------------------------------------------------------
DUMP=$(sfdisk -d "$IMG" 2>/dev/null | tr -s ' ')
check "disk-id is 0xab425f5e" \
    grep -q "label-id: 0xab425f5e" <<<"$DUMP"
NPARTS=$(grep -c 'start=' <<<"$DUMP")
if [ "$NPARTS" = 3 ]; then ok "exactly 3 partitions (v2's empty p4 dropped)"; else bad "expected 3 partitions, found $NPARTS"; fi
check "p1: start 8192, 524288 sectors, type c" \
    grep -q '1 : start= 8192, size= 524288, type=c' <<<"$DUMP"
check "p2: start 532480, 5242880 sectors, type 83" \
    grep -q '2 : start= 532480, size= 5242880, type=83' <<<"$DUMP"
check "p3: start 5775360, type c (FAT32 media)" \
    grep -Eq '3 : start= 5775360, size= [0-9]+, type=c' <<<"$DUMP"

LOOP=$(losetup -f --show -P -r "$IMG") || { bad "losetup failed"; echo "RESULT: $NPASS passed, $((NFAIL)) failed"; exit 1; }
for _ in 1 2 3 4 5; do
    [ -b "${LOOP}p3" ] && break
    partprobe "$LOOP" 2>/dev/null; sleep 1
done
mount -o ro "${LOOP}p1" "$B" || bad "mount p1"
mount -o ro "${LOOP}p2" "$R" || bad "mount p2"
mount -o ro "${LOOP}p3" "$M" || bad "mount p3"

# ---------------------------------------------------------------------------
# p1: boot
# ---------------------------------------------------------------------------
check "p1: kernel7.img present"    test -f "$B/kernel7.img"
check "p1: config.txt present"     test -f "$B/config.txt"
check "p1: cmdline.txt present"    test -f "$B/cmdline.txt"
check "p1: overlays/ present"      test -d "$B/overlays"
check "p1: i2c-rtc overlay configured" grep -q 'dtoverlay=i2c-rtc,ds1307' "$B/config.txt"
check "p1: cmdline root=/dev/mmcblk0p2 (v2 exact)" grep -q 'root=/dev/mmcblk0p2' "$B/cmdline.txt"
check "p1: rpi2dmd-expand first-boot flag present" test -f "$B/rpi2dmd-expand"

# ---------------------------------------------------------------------------
# p2: rootfs + overlay
# ---------------------------------------------------------------------------
check "p2: /opt/rpi2dmd-v3/player/rpi2dmd/main.py" test -f "$R/opt/rpi2dmd-v3/player/rpi2dmd/main.py"
check "p2: /opt/rpi2dmd-v3/webui/app.py"           test -f "$R/opt/rpi2dmd-v3/webui/app.py"
check "p2: v2 renderers kept at /opt/RPI2DMD"      test -d "$R/opt/RPI2DMD"
for unit in rpi2dmd-player rpi2dmd-web rpi2dmd-firstboot; do
    check "p2: $unit.service installed" test -f "$R/etc/systemd/system/$unit.service"
    check "p2: $unit.service enabled"   test -L "$R/etc/systemd/system/multi-user.target.wants/$unit.service"
done
check "p2: apache2 wants symlink removed" \
    test ! -e "$R/etc/systemd/system/multi-user.target.wants/apache2.service"
check "p2: .bashrc go.sh autostart commented out" \
    grep -q '^#/opt/RPI2DMD/go.sh' "$R/home/pi/.bashrc"
if grep -q '^/opt/RPI2DMD/go.sh' "$R/home/pi/.bashrc" 2>/dev/null; then
    bad "p2: .bashrc still runs go.sh"
else
    ok "p2: .bashrc go.sh inactive"
fi
check "p2: tmpfiles.d /run/rpi2dmd rule" \
    grep -q '^d /run/rpi2dmd 0755 root root -$' "$R/etc/tmpfiles.d/rpi2dmd.conf"
check "p2: expand script installed+executable" test -x "$R/usr/local/sbin/rpi2dmd-expand.sh"
check "p2: fstab PARTUUID=ab425f5e-01 /boot"   grep -q 'PARTUUID=ab425f5e-01' "$R/etc/fstab"
check "p2: fstab PARTUUID=ab425f5e-02 /"       grep -q 'PARTUUID=ab425f5e-02' "$R/etc/fstab"
check "p2: fstab /dev/mmcblk0p3 -> /media/usb" grep -q '^/dev/mmcblk0p3' "$R/etc/fstab"
check "p2: python3.7 installed"                test -x "$R/usr/bin/python3.7"
check "p2: flask in dist-packages"             test -d "$R/usr/lib/python3/dist-packages/flask"
check "p2: PIL in dist-packages"               test -d "$R/usr/lib/python3/dist-packages/PIL"
if ls "$R"/usr/local/lib/python3.7/dist-packages/rgbmatrix* >/dev/null 2>&1 \
   || ls "$R"/usr/local/lib/python3.7/site-packages/rgbmatrix* >/dev/null 2>&1; then
    ok "p2: rgbmatrix bindings installed"
else
    bad "p2: rgbmatrix bindings missing from /usr/local/lib/python3.7"
fi
check "p2: qemu-arm-static removed" test ! -e "$R/usr/bin/qemu-arm-static"
check "p2: ld.so.preload restored (libarmmem active)" \
    grep -q '^/usr/lib' "$R/etc/ld.so.preload"
check "p2: fatresize installed" test -e "$R/usr/sbin/fatresize"
# the v2 base ships no toolchain, so none of these may exist post-build
if [ -e "$R/usr/bin/gcc" ] || [ -e "$R/usr/bin/g++" ] \
   || [ -e "$R/usr/bin/make" ] || [ -e "$R/usr/bin/cython3" ] \
   || [ -e "$R/usr/include/python3.7m/Python.h" ]; then
    bad "p2: build toolchain was not purged"
else
    ok "p2: build toolchain purged (v2 parity)"
fi

# ---------------------------------------------------------------------------
# p3: media
# ---------------------------------------------------------------------------
check "p3: dmd/index.json" test -f "$M/dmd/index.json"
RDAS=$(find "$M/dmd" -type f -name '*.rda' 2>/dev/null | wc -l)
MINRDAS=${MIN_RDAS:-2379}
if [ "$RDAS" -ge "$MINRDAS" ]; then ok "p3: $RDAS RDA animations (>=$MINRDAS)"; else bad "p3: only $RDAS RDA animations (<$MINRDAS)"; fi
CATS=$(find "$M/gif" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
MINCATS=${MIN_CATS:-15}
[ -z "$MIN_CATS" ] && [ "$LITE" = 1 ] && MINCATS=8
if [ "$CATS" -ge "$MINCATS" ]; then ok "p3: $CATS gif categories (>=$MINCATS)"; else bad "p3: only $CATS gif categories (<$MINCATS)"; fi
GIFS=$(find "$M/gif" -type f -iname '*.gif' 2>/dev/null | wc -l)
MINGIFS=${MIN_GIFS:-10200}
[ -z "$MIN_GIFS" ] && [ "$LITE" = 1 ] && MINGIFS=550
if [ "$GIFS" -ge "$MINGIFS" ]; then ok "p3: $GIFS gif files (>=$MINGIFS)"; else bad "p3: only $GIFS gif files (<$MINGIFS)"; fi
FONTS=$(find "$M/fonts" -type f 2>/dev/null | wc -l)
if [ "$FONTS" -ge 10 ]; then ok "p3: fonts/ populated ($FONTS files)"; else bad "p3: fonts/ looks empty ($FONTS files)"; fi
check "p3: config/config.txt template" test -f "$M/config/config.txt"
check "p3: config/README.txt"          test -f "$M/config/README.txt"

# ---------------------------------------------------------------------------
# qemu-chroot import checks (needs binfmt_misc arm registration with the
# F flag, since qemu-arm-static is deliberately absent from the image)
# ---------------------------------------------------------------------------
if chroot "$R" /usr/bin/python3 -c "import flask, PIL, rgbmatrix" >/dev/null 2>&1; then
    ok "chroot: python3 imports flask, PIL, rgbmatrix"
else
    bad "chroot: import flask/PIL/rgbmatrix failed"
fi
if chroot "$R" /usr/bin/python3 -c "import sys; sys.path.insert(0, '/opt/rpi2dmd-v3/player'); import rpi2dmd.main" >/dev/null 2>&1; then
    ok "chroot: import rpi2dmd.main"
else
    bad "chroot: import rpi2dmd.main failed"
fi

echo "RESULT: $NPASS passed, $NFAIL failed"
[ "$NFAIL" -eq 0 ] || exit 1
exit 0
