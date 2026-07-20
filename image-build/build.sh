#!/bin/bash
# RPI2DMD v3 image assembly.
#
# Builds a flashable SD card image from the proven v2 base image, the v3
# software in this repo, and the staged media content. Must run as root on
# Linux (WSL2 Ubuntu is the reference environment) with: sfdisk, losetup -P,
# dosfstools, e2fsprogs, rsync, curl, python3, tar and qemu-arm-static
# (binfmt_misc registration for ARM required for the chroot stage).
#
# Usage:
#   build.sh --v2-img PATH --repo PATH --content PATH --out PATH
#            [--size-gb 7.0] [--lite]
#
#   --v2-img    RPI2DMD_v2_standard.img (read-only input)
#   --repo      rpi2dmd-v3 repo checkout (player/, webui/, image-build/)
#   --content   staged content dir (dmd/, media-base/, dlc10k/, bonus/)
#   --out       output image path (overwritten)
#   --size-gb   total image size in GiB (default 7.0; >= 3.2)
#   --lite      skip the 10K DLC + bonus GIF packs (for 4 GB cards)
#
# Partition layout (MBR, disk-id 0xab425f5e kept from v2 so the
# PARTUUID=ab425f5e-01/-02 references in fstab keep working):
#   p1  start 8192s   524288s (256 MiB)  type c  FAT32   boot
#   p2  start 532480s 5242880s (2.5 GiB) type 83 ext4    rootfs (RPI2DMDOS)
#   p3  start 5775360s rest              type c  FAT32   media (RPI2DMDGIF)
# v2's 512 KiB all-zero p4 is dropped. v2 has no bootable flag; neither
# does v3 (the Pi firmware does not use it).
#
# Environment:
#   RGB_MATRIX_SHA   pin the rpi-rgb-led-matrix commit. Default: a3eea99
#                    (2024-07), the last commit with the classic
#                    Makefile/setup.py/pre-generated-cython build. Newer
#                    commits ported the bindings to cython 3.0.x and then
#                    to scikit-build-core/cmake — neither is buildable on
#                    Buster (python 3.7, cython 0.29), so do NOT pin
#                    current master without checking bindings/python.

set -u

log() { echo "[build $(date '+%H:%M:%S')] $*"; }
fail() { echo "[build] FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
V2IMG= REPO= CONTENT= OUT=
SIZE_GB=7.0
LITE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --v2-img)   V2IMG=$2; shift 2 ;;
        --repo)     REPO=$2; shift 2 ;;
        --content)  CONTENT=$2; shift 2 ;;
        --out)      OUT=$2; shift 2 ;;
        --size-gb)  SIZE_GB=$2; shift 2 ;;
        --lite)     LITE=1; shift ;;
        *) fail "unknown argument: $1" ;;
    esac
done
[ -n "$V2IMG" ] && [ -n "$REPO" ] && [ -n "$CONTENT" ] && [ -n "$OUT" ] \
    || fail "usage: build.sh --v2-img PATH --repo PATH --content PATH --out PATH [--size-gb 7.0] [--lite]"

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[ "$(id -u)" = 0 ] || fail "must run as root"
for tool in sfdisk losetup mkfs.vfat mkfs.ext4 rsync curl python3 tar \
            truncate blockdev chroot du find; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing tool: $tool"
done
[ -x /usr/bin/qemu-arm-static ] || fail "/usr/bin/qemu-arm-static missing"
[ -f "$V2IMG" ] || fail "v2 image not found: $V2IMG"
[ -f "$REPO/player/rpi2dmd/main.py" ] \
    || fail "player entry point missing: $REPO/player/rpi2dmd/main.py"
[ -f "$REPO/webui/app.py" ] || fail "web UI missing: $REPO/webui/app.py"
[ -f "$CONTENT/dmd/index.json" ] || fail "RDA index missing: $CONTENT/dmd/index.json"
[ -d "$CONTENT/media-base/gif" ] || fail "media-base gif dir missing"
[ -d "$CONTENT/media-base/fonts" ] || fail "media-base fonts dir missing"
for unit in rpi2dmd-player rpi2dmd-web rpi2dmd-firstboot; do
    [ -f "$SCRIPT_DIR/systemd/$unit.service" ] || fail "missing unit: $unit.service"
done
[ -f "$SCRIPT_DIR/chroot-setup.sh" ] || fail "chroot-setup.sh missing"
[ -f "$SCRIPT_DIR/stage-media.sh" ] || fail "stage-media.sh missing"
[ -f "$SCRIPT_DIR/firstboot/rpi2dmd-expand.sh" ] || fail "firstboot/rpi2dmd-expand.sh missing"

# Extra GIF sources, either of:
#   content/gif-extra/<Category>/*.gif  — pre-merged tree assembled by the
#                                         builder from user-supplied packs
#   content/dlc10k + content/bonus      — legacy layout (both required)
# Neither present (or --lite) = stock media-base gifs only.
DLC_GIF= BON_GIF= GIF_EXTRA=
if [ "$LITE" != 1 ]; then
    if [ -d "$CONTENT/gif-extra" ]; then
        GIF_EXTRA="$CONTENT/gif-extra"
        log "extra gifs: builder-merged tree ($GIF_EXTRA)"
    elif [ -d "$CONTENT/dlc10k" ] || [ -d "$CONTENT/bonus" ]; then
        DLC_GIF=$(find "$CONTENT/dlc10k" -maxdepth 4 -type d -iname gif 2>/dev/null | head -n 1)
        [ -n "$DLC_GIF" ] || fail "dlc10k gif dir not found under $CONTENT/dlc10k (use --lite to skip)"
        BON_GIF=$(find "$CONTENT/bonus" -maxdepth 4 -type d -iname gif 2>/dev/null | head -n 1)
        [ -n "$BON_GIF" ] || fail "bonus gif dir not found under $CONTENT/bonus (use --lite to skip)"
    else
        log "no extra gif packs staged: stock media-base gifs only"
    fi
fi

# ---------------------------------------------------------------------------
# Layout + content sanity
# ---------------------------------------------------------------------------
SIZE_BYTES=$(python3 -c "import sys; print(int(float(sys.argv[1]) * 1024**3) // 512 * 512)" "$SIZE_GB") \
    || fail "bad --size-gb value: $SIZE_GB"
TOTAL_SECTORS=$((SIZE_BYTES / 512))
P1_START=8192;   P1_SIZE=524288
P2_START=532480; P2_SIZE=5242880
P3_START=5775360
P3_SECTORS=$((TOTAL_SECTORS - P3_START))
[ "$P3_SECTORS" -ge 262144 ] || fail "--size-gb $SIZE_GB too small: media partition would be <128 MiB"
P3_BYTES=$((P3_SECTORS * 512))

GIF_CACHE=
[ -d "$CONTENT/gif-cache" ] && GIF_CACHE="$CONTENT/gif-cache"
PAYLOAD=$(du -sb --total "$CONTENT/dmd" "$CONTENT/media-base/gif" \
          "$CONTENT/media-base/fonts" ${DLC_GIF:+"$DLC_GIF"} ${BON_GIF:+"$BON_GIF"} \
          ${GIF_EXTRA:+"$GIF_EXTRA"} ${GIF_CACHE:+"$GIF_CACHE"} \
          2>/dev/null | tail -n 1 | cut -f1)
MARGIN=$((256 * 1024 * 1024))   # FAT overhead + headroom
if [ $((PAYLOAD + MARGIN)) -gt "$P3_BYTES" ]; then
    fail "staged content ($PAYLOAD bytes) exceeds media partition layout ($P3_BYTES bytes at --size-gb $SIZE_GB); increase --size-gb or use --lite"
fi
log "layout OK: image $SIZE_BYTES bytes, media partition $P3_BYTES bytes, content payload $PAYLOAD bytes"

# ---------------------------------------------------------------------------
# rpi-rgb-led-matrix source (resolve + download on the host)
# ---------------------------------------------------------------------------
# Last commit with the classic python build (see header). Current master
# (checked 2026-07: 41809e40) removed it in favor of scikit-build-core,
# which cannot be built with Buster's python 3.7 / cython 0.29.
RGB_MATRIX_DEFAULT_SHA=a3eea997a9254b83ab2de97ae80d83588f696387
RGB_MATRIX_SHA=${RGB_MATRIX_SHA:-$RGB_MATRIX_DEFAULT_SHA}
log "rpi-rgb-led-matrix commit: $RGB_MATRIX_SHA"

WORK=$(mktemp -d /tmp/rpi2dmd-build.XXXXXX)
RGB_TARBALL="$WORK/rgb-matrix.tar.gz"
log "downloading rpi-rgb-led-matrix tarball"
curl -fsSL --retry 3 -o "$RGB_TARBALL" \
    "https://github.com/hzeller/rpi-rgb-led-matrix/archive/$RGB_MATRIX_SHA.tar.gz" \
    || fail "download of rpi-rgb-led-matrix $RGB_MATRIX_SHA failed"

# ---------------------------------------------------------------------------
# Cleanup handling
# ---------------------------------------------------------------------------
LOOP= V2LOOP=
NB="$WORK/new-boot"; NR="$WORK/new-root"; NM="$WORK/new-media"
VB="$WORK/v2-boot";  VR="$WORK/v2-root"
mkdir -p "$NB" "$NR" "$NM" "$VB" "$VR"

unmount_chroot_binds() {
    for m in "$NR/dev/pts" "$NR/dev" "$NR/sys" "$NR/proc" "$NR/run"; do
        mountpoint -q "$m" 2>/dev/null && { umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null; }
    done
    return 0
}

cleanup() {
    set +e
    unmount_chroot_binds
    for m in "$NM" "$NR" "$NB" "$VR" "$VB"; do
        mountpoint -q "$m" 2>/dev/null && { umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null; }
    done
    [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
    [ -n "$V2LOOP" ] && losetup -d "$V2LOOP" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

wait_for_parts() {
    dev=$1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -b "${dev}p1" ] && [ -b "${dev}p2" ] && [ -b "${dev}p3" ] && return 0
        partprobe "$dev" 2>/dev/null
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------------------
# Step 1: sparse image + partition table
# ---------------------------------------------------------------------------
log "step 1: creating sparse image $OUT ($SIZE_GB GiB)"
rm -f "$OUT"
truncate -s "$SIZE_BYTES" "$OUT" || fail "truncate failed"
sfdisk --quiet "$OUT" <<EOF || fail "sfdisk partitioning failed"
label: dos
label-id: 0xab425f5e
unit: sectors

start=$P1_START, size=$P1_SIZE, type=c
start=$P2_START, size=$P2_SIZE, type=83
start=$P3_START, type=c
EOF

# ---------------------------------------------------------------------------
# Step 2: loop devices + filesystems
# ---------------------------------------------------------------------------
log "step 2: loop devices + mkfs"
LOOP=$(losetup -f --show -P "$OUT") || fail "losetup (new image) failed"
V2LOOP=$(losetup -f --show -P -r "$V2IMG") || fail "losetup (v2 image) failed"
wait_for_parts "$LOOP" || fail "partition nodes for $LOOP did not appear"
wait_for_parts "$V2LOOP" || fail "partition nodes for $V2LOOP did not appear"

mkfs.vfat -F 32 "${LOOP}p1" >/dev/null || fail "mkfs.vfat p1 failed"
# Old 4.19 kernel + Buster e2fsprogs compatibility. Newer mke2fs releases
# also default-enable orphan_file/metadata_csum_seed which 4.19 cannot
# mount, so try to disable those too and fall back if unknown.
if ! mkfs.ext4 -q -L RPI2DMDOS \
        -O ^metadata_csum,^64bit,^orphan_file,^metadata_csum_seed \
        "${LOOP}p2" 2>/dev/null; then
    mkfs.ext4 -q -L RPI2DMDOS -O ^metadata_csum,^64bit "${LOOP}p2" \
        || fail "mkfs.ext4 p2 failed"
fi
mkfs.vfat -F 32 -n RPI2DMDGIF "${LOOP}p3" >/dev/null || fail "mkfs.vfat p3 failed"

# ---------------------------------------------------------------------------
# Step 3: copy v2 boot + rootfs
# ---------------------------------------------------------------------------
log "step 3: copying v2 boot + rootfs"
mount -o ro "${V2LOOP}p1" "$VB" || fail "mount v2 boot failed"
mount -o ro "${V2LOOP}p2" "$VR" || fail "mount v2 root failed"
mount "${LOOP}p1" "$NB" || fail "mount new boot failed"
mount "${LOOP}p2" "$NR" || fail "mount new root failed"
mount "${LOOP}p3" "$NM" || fail "mount new media failed"

rsync -rt --modify-window=2 --exclude 'System Volume Information' \
    "$VB/" "$NB/" || fail "boot rsync failed"
rsync -aHAXx --numeric-ids "$VR/" "$NR/" || fail "rootfs rsync failed"
umount "$VR"; umount "$VB"

# The LED panel is driven over GPIO, not the GPU, and these units are
# usually a 512MB Pi Zero — v2's gpu_mem=256 left only 242MB for the OS.
if grep -q '^gpu_mem=' "$NB/config.txt"; then
    sed -i 's/^gpu_mem=.*/gpu_mem=64/' "$NB/config.txt"
else
    echo "gpu_mem=64" >> "$NB/config.txt"
fi
log "boot: gpu_mem set to 64 (headless; frees RAM for the OS)"

FREE_MB=$(df -m --output=avail "$NR" | tail -n 1 | tr -d ' ')
[ "$FREE_MB" -ge 400 ] || fail "only ${FREE_MB}MB free on new rootfs; need >=400MB for the package install"
log "rootfs copied (${FREE_MB}MB free)"

# ---------------------------------------------------------------------------
# Step 4: v3 overlay on the new rootfs
# ---------------------------------------------------------------------------
log "step 4: applying v3 overlay"
RSX=(--exclude 'tests' --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache')
mkdir -p "$NR/opt/rpi2dmd-v3"
rsync -a --delete "${RSX[@]}" "$REPO/player/" "$NR/opt/rpi2dmd-v3/player/" \
    || fail "player rsync failed"
rsync -a --delete "${RSX[@]}" "$REPO/webui/" "$NR/opt/rpi2dmd-v3/webui/" \
    || fail "webui rsync failed"

install -m 644 "$SCRIPT_DIR/systemd/rpi2dmd-player.service" \
               "$SCRIPT_DIR/systemd/rpi2dmd-web.service" \
               "$SCRIPT_DIR/systemd/rpi2dmd-firstboot.service" \
               "$NR/etc/systemd/system/" || fail "unit install failed"
for unit in rpi2dmd-player rpi2dmd-web rpi2dmd-firstboot; do
    ln -sf "/etc/systemd/system/$unit.service" \
        "$NR/etc/systemd/system/multi-user.target.wants/$unit.service"
done

# apache2 no longer autostarts (the v3 web UI takes port 80)
rm -f "$NR/etc/systemd/system/multi-user.target.wants/apache2.service"

# neutralize the v2 autostart block in /home/pi/.bashrc (keep the rest)
if grep -q '^/opt/RPI2DMD/go.sh' "$NR/home/pi/.bashrc"; then
    sed -i '\%^if \[ ! -f /tmp/rpi2xxxxx_start \]%,\%^fi[[:space:]]*$%s/^/#/' \
        "$NR/home/pi/.bashrc"
    printf '\n# ^ v2 autostart disabled by the RPI2DMD v3 image build (systemd runs the v3 player)\n' \
        >> "$NR/home/pi/.bashrc"
fi
grep -q '^/opt/RPI2DMD/go.sh' "$NR/home/pi/.bashrc" \
    && fail ".bashrc go.sh autostart still active after edit"

mkdir -p "$NR/etc/tmpfiles.d"
echo 'd /run/rpi2dmd 0755 root root -' > "$NR/etc/tmpfiles.d/rpi2dmd.conf"

install -m 755 "$SCRIPT_DIR/firstboot/rpi2dmd-expand.sh" \
    "$NR/usr/local/sbin/rpi2dmd-expand.sh" || fail "expand script install failed"

# first-boot flag on the boot partition (consumed by rpi2dmd-firstboot)
echo "expand the media partition (p3) to fill the SD card on first boot" \
    > "$NB/rpi2dmd-expand"

# ---------------------------------------------------------------------------
# Step 5: chroot package install + rgbmatrix build (qemu-arm-static)
# ---------------------------------------------------------------------------
log "step 5: chroot setup (apt + rpi-rgb-led-matrix build; this takes a long time under qemu)"
cp /usr/bin/qemu-arm-static "$NR/usr/bin/qemu-arm-static"
cp "$SCRIPT_DIR/chroot-setup.sh" "$NR/tmp/chroot-setup.sh"
cp "$RGB_TARBALL" "$NR/tmp/rgb-matrix.tar.gz"

# resolv.conf: stage the host's for the chroot, restore the original after
cp -a "$NR/etc/resolv.conf" "$NR/etc/resolv.conf.rpi2dmd-orig" 2>/dev/null || true
rm -f "$NR/etc/resolv.conf"
cat /etc/resolv.conf > "$NR/etc/resolv.conf"

# libarmmem ld.so.preload breaks qemu-user; comment it out for the chroot
if [ -f "$NR/etc/ld.so.preload" ]; then
    cp -a "$NR/etc/ld.so.preload" "$NR/etc/ld.so.preload.rpi2dmd-orig"
    sed -i 's/^/#/' "$NR/etc/ld.so.preload"
fi

# keep maintainer scripts from starting services inside the chroot
cat > "$NR/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 755 "$NR/usr/sbin/policy-rc.d"

mkdir -p "$NR/proc" "$NR/sys" "$NR/dev" "$NR/run"
mount -t proc proc "$NR/proc" || fail "mount proc failed"
mount --bind /sys "$NR/sys" || fail "mount sys failed"
mount --bind /dev "$NR/dev" || fail "mount dev failed"
mount --bind /dev/pts "$NR/dev/pts" || fail "mount devpts failed"
mount -t tmpfs tmpfs "$NR/run" || fail "mount run failed"

chroot "$NR" /bin/bash /tmp/chroot-setup.sh "$RGB_MATRIX_SHA"
CHROOT_RC=$?
if [ "$CHROOT_RC" -ne 0 ]; then
    unmount_chroot_binds
    fail "chroot-setup.sh failed (rc=$CHROOT_RC) — NOT shipping a broken image. See output above (apt repo unreachable? cython build failure?)"
fi

VERSIONS_OUT="$OUT.build-info.txt"
cp "$NR/tmp/rpi2dmd-versions.txt" "$VERSIONS_OUT" 2>/dev/null \
    || log "warning: version manifest missing from chroot"

unmount_chroot_binds

# undo chroot scaffolding
rm -f "$NR/usr/bin/qemu-arm-static" "$NR/usr/sbin/policy-rc.d"
rm -f "$NR/tmp/chroot-setup.sh" "$NR/tmp/rgb-matrix.tar.gz" \
      "$NR/tmp/rpi2dmd-versions.txt"
if [ -f "$NR/etc/ld.so.preload.rpi2dmd-orig" ]; then
    mv "$NR/etc/ld.so.preload.rpi2dmd-orig" "$NR/etc/ld.so.preload"
fi
rm -f "$NR/etc/resolv.conf"
if [ -f "$NR/etc/resolv.conf.rpi2dmd-orig" ]; then
    mv "$NR/etc/resolv.conf.rpi2dmd-orig" "$NR/etc/resolv.conf"
fi
find "$NR/tmp" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null

FREE_MB=$(df -m --output=avail "$NR" | tail -n 1 | tr -d ' ')
log "chroot stage done (${FREE_MB}MB free on rootfs)"

# ---------------------------------------------------------------------------
# Step 6: media partition
# ---------------------------------------------------------------------------
log "step 6: staging media partition"
bash "$SCRIPT_DIR/stage-media.sh" "$NM" "$CONTENT" "$REPO" "$LITE" \
    || fail "stage-media.sh failed"

# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------
log "finalizing (sync + unmount)"
GIF_COUNT=$(find "$NM/gif" -type f -iname '*.gif' | wc -l)
RDA_COUNT=$(find "$NM/dmd" -type f -name '*.rda' | wc -l)
{
    echo "image: $OUT"
    echo "built: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "size-gb: $SIZE_GB"
    echo "lite: $LITE"
    echo "gif-files: $GIF_COUNT"
    echo "rda-files: $RDA_COUNT"
    cat "$VERSIONS_OUT" 2>/dev/null
} > "$VERSIONS_OUT.tmp" && mv "$VERSIONS_OUT.tmp" "$VERSIONS_OUT"

sync
umount "$NM" || fail "umount media failed"
umount "$NR" || fail "umount root failed"
umount "$NB" || fail "umount boot failed"
losetup -d "$LOOP"; LOOP=
losetup -d "$V2LOOP"; V2LOOP=

log "DONE: $OUT ($GIF_COUNT gifs, $RDA_COUNT RDAs; rpi-rgb-led-matrix $RGB_MATRIX_SHA)"
log "build info: $VERSIONS_OUT"
exit 0
