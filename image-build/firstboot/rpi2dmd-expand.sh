#!/bin/bash
# RPI2DMD v3 first boot: grow the media partition (/dev/mmcblk0p3, FAT32)
# to fill the SD card, then resize its filesystem with fatresize.
#
# Invoked by rpi2dmd-firstboot.service, which is gated on the flag file
# /boot/rpi2dmd-expand (written by the image build). EVERY exit path —
# including any failure — removes the flag and exits 0 so a problem here
# can never brick the boot; the worst case is an unexpanded media
# partition, which still works.

FLAG=/boot/rpi2dmd-expand
LOG=/boot/rpi2dmd-expand.log
DISK=/dev/mmcblk0
PART=/dev/mmcblk0p3
# MBR disk identifier the fstab/cmdline PARTUUIDs depend on, little-endian
# at offset 440. fatresize's libparted commit regenerates it (verified),
# so it must be restored after any fatresize run.
DISK_ID_BYTES='\x5e\x5f\x42\xab'   # 0xab425f5e

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null
    logger -t rpi2dmd-expand "$*" 2>/dev/null
}

restore_disk_id() {
    printf "$DISK_ID_BYTES" | dd of="$DISK" bs=1 seek=440 count=4 \
        conv=notrunc >> "$LOG" 2>&1
    sync
}

finish() {
    rm -f "$FLAG" 2>/dev/null
    sync
    exit 0
}
trap finish EXIT

log "starting media partition expansion"

if [ ! -b "$PART" ]; then
    log "no $PART block device; nothing to do"
    exit 0
fi

OLD_SIZE=$(blockdev --getsize64 "$PART" 2>/dev/null)

umount /media/usb >> "$LOG" 2>&1
umount "$PART" >> "$LOG" 2>&1

if ! echo ', +' | sfdisk -N 3 --no-reread "$DISK" >> "$LOG" 2>&1; then
    log "sfdisk grow failed; leaving partition as-is"
    mount /media/usb >> "$LOG" 2>&1
    exit 0
fi
log "partition table updated"

# tell the kernel about the new p3 size (BLKPG resize on partition 3 only
# — p1/p2 are mounted and a full BLKRRPART re-read would fail here)
partx -u --nr 3 "$DISK" >> "$LOG" 2>&1 \
    || partx -u "$DISK" >> "$LOG" 2>&1 \
    || partprobe "$DISK" >> "$LOG" 2>&1
sleep 1

SIZE=$(blockdev --getsize64 "$PART" 2>/dev/null)
if [ -z "$SIZE" ] || [ -z "$OLD_SIZE" ] \
        || [ $((SIZE - OLD_SIZE)) -lt 8388608 ]; then
    # card is (nearly) the shipped image size — a resize would only shrink
    log "no meaningful growth ($OLD_SIZE -> ${SIZE:-?}); skipping filesystem resize"
    mount /media/usb >> "$LOG" 2>&1 || mount "$PART" /media/usb >> "$LOG" 2>&1
    exit 0
fi

fsck.fat -a "$PART" >> "$LOG" 2>&1

total_clusters() {
    fsck.fat -n "$PART" 2>/dev/null \
        | sed -n 's/.*[0-9][0-9]*\/\([0-9][0-9]*\) clusters.*/\1/p' \
        | tail -n 1
}

# Buster's fatresize 1.0.2: no "max" keyword, and the exact partition
# byte size makes libparted crash — resize to 1 MiB short of the end
# (verified working in a qemu chroot of this very image). Its exit code
# is unreliable (the final kernel re-inform fails while / is mounted on
# the same disk), so success is judged by the cluster count growing.
CL_BEFORE=$(total_clusters)
log "resizing FAT32 filesystem to fill $PART ($SIZE bytes; this is slow)"
fatresize -s $((SIZE - 1048576)) "$PART" >> "$LOG" 2>&1
FR_RC=$?

# fatresize commits the partition table through libparted, which
# REGENERATES the MBR disk identifier — restore it or the
# PARTUUID=ab425f5e-01/-02 mounts would fail on the next boot.
restore_disk_id
partx -u --nr 3 "$DISK" >> "$LOG" 2>&1 || true

fsck.fat -a "$PART" >> "$LOG" 2>&1
CL_AFTER=$(total_clusters)

if [ -n "$CL_BEFORE" ] && [ -n "$CL_AFTER" ] && [ "$CL_AFTER" -gt "$CL_BEFORE" ]; then
    log "filesystem resized: $CL_BEFORE -> $CL_AFTER clusters (fatresize rc=$FR_RC)"
elif fatresize -s max "$PART" >> "$LOG" 2>&1; then
    restore_disk_id
    fsck.fat -a "$PART" >> "$LOG" 2>&1
    log "fatresize -s max OK (fallback)"
else
    log "fatresize failed (rc=$FR_RC, clusters $CL_BEFORE -> ${CL_AFTER:-?}); media partition stays at its shipped size — still usable"
fi

# fatresize regenerates the FAT boot sector without its volume-label
# field; put the label back (the root-directory copy survives anyway)
fatlabel "$PART" RPI2DMDGIF >> "$LOG" 2>&1 || true

if mount /media/usb >> "$LOG" 2>&1 || mount "$PART" /media/usb >> "$LOG" 2>&1; then
    log "media partition remounted"
else
    log "remount failed; the fstab mount will pick it up on the next boot"
fi

log "done"
exit 0
