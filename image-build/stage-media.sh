#!/bin/bash
# Stage the media partition (FAT32 "RPI2DMDGIF", mounted at /media/usb on
# the Pi): gif/, dmd/, fonts/, config/. Called by build.sh with the new
# p3 already mounted, but also usable standalone against any mount point
# (e.g. to refresh a card over a card reader).
#
# Usage: stage-media.sh <p3-mount> <content-dir> <repo-dir> <lite 0|1>
#
# gif/ merge order (later wins on the same relative path):
#   1. media-base/gif   (v2 stock categories)
#   2. dlc10k .../gif   (ULTIMATE GIFS DLC 10K)      [skipped when lite=1]
#   3. bonus  .../gif   (DLC bonus pack)             [skipped when lite=1]

set -u

P3=${1:?usage: stage-media.sh <p3-mount> <content-dir> <repo-dir> <lite 0|1>}
CONTENT=${2:?content dir required}
REPO=${3:?repo dir required}
LITE=${4:-0}

fail() { echo "[stage-media] FAIL: $*" >&2; exit 1; }
log() { echo "[stage-media $(date '+%H:%M:%S')] $*"; }

[ -d "$P3" ] || fail "mount point missing: $P3"
[ -d "$CONTENT/media-base/gif" ] || fail "missing $CONTENT/media-base/gif"
[ -d "$CONTENT/media-base/fonts" ] || fail "missing $CONTENT/media-base/fonts"
[ -f "$CONTENT/dmd/index.json" ] || fail "missing $CONTENT/dmd/index.json"

# FAT: no owners/links; keep timestamps with a 2s window (FAT resolution)
RS() { rsync -rt --modify-window=2 "$@"; }

log "gif/ <- media-base"
RS "$CONTENT/media-base/gif/" "$P3/gif/" || fail "media-base gif copy failed"

if [ "$LITE" != 1 ]; then
    DLC_GIF=$(find "$CONTENT/dlc10k" -maxdepth 4 -type d -iname gif 2>/dev/null | head -n 1)
    [ -n "$DLC_GIF" ] || fail "dlc10k gif dir not found under $CONTENT/dlc10k"
    log "gif/ <- dlc10k ($DLC_GIF)"
    RS "$DLC_GIF/" "$P3/gif/" || fail "dlc10k gif copy failed"

    BON_GIF=$(find "$CONTENT/bonus" -maxdepth 4 -type d -iname gif 2>/dev/null | head -n 1)
    [ -n "$BON_GIF" ] || fail "bonus gif dir not found under $CONTENT/bonus"
    log "gif/ <- bonus ($BON_GIF)"
    RS "$BON_GIF/" "$P3/gif/" || fail "bonus gif copy failed"
else
    log "lite build: skipping dlc10k + bonus packs"
fi

log "dmd/ <- RDA library"
RS "$CONTENT/dmd/" "$P3/dmd/" || fail "dmd copy failed"

log "fonts/ <- media-base fonts"
RS "$CONTENT/media-base/fonts/" "$P3/fonts/" || fail "fonts copy failed"

log "config/ <- templates"
mkdir -p "$P3/config"

cat > "$P3/config/config.txt" <<'EOF'
# RPI2DMD v3 — pre-boot network setup (legacy v2 config format).
#
# Edit this file BEFORE the first boot (this partition is readable on any
# PC) to get the device onto your Wi-Fi. On first start these values are
# migrated into rpi2dmd.json (which appears next to this file); after that,
# use the web interface for everything else.

# WIFI Configuration
Wifi_country=US
Wifi_ssid=Your_SSID
Wifi_psk=Your_password

# Network name (hostname; the web UI is at http://<Network_name>.local/)
Network_name=RPI2DMD

# Timezone (see /usr/share/zoneinfo)
Clock_TZ=America/New_York
EOF

cat > "$P3/config/README.txt" <<'EOF'
RPI2DMD v3
==========

This is the media partition of an RPI2DMD v3 (Run-DMD-class DMD clock).

First start
-----------
1. Optional (Wi-Fi): before the first boot, edit config.txt in this folder
   and fill in Wifi_country / Wifi_ssid / Wifi_psk. Ethernet needs no setup.
2. Boot the Pi. The first boot expands this partition to fill your SD card
   (this can take a few minutes on big cards — let it finish).
3. Open the web interface: http://rpi2dmd.local/ — or use the device IP
   (shown on the display when it changes, and visible on your router).

Configuration
-------------
- rpi2dmd.json appears in this folder after the first boot. It is the ONLY
  v3 configuration document; the web interface edits it for you, but you
  can also edit it here (or over the network share) — changes are picked
  up automatically.
- config.txt above is only read while rpi2dmd.json does not exist yet
  (its Wi-Fi/hostname/timezone values are migrated once).

Content on this partition
-------------------------
  gif\<Category>\*.gif   GIF library (v2 stock + ULTIMATE 10K DLC + bonus)
  dmd\<GAME>\*.rda       Run-DMD animation library (2,379 animations)
  fonts\                 clock fonts, patterns and background images
  config\                this folder

The guest-writable SMB network share of this partition is still available,
like v2: \\RPI2DMD (or the device IP) — drop new GIFs into gif\<Category>\.
Enable/disable content from the web interface (Library page).
EOF

# ---------------------------------------------------------------------------
# Sanity counts
# ---------------------------------------------------------------------------
GIFS=$(find "$P3/gif" -type f -iname '*.gif' | wc -l)
CATS=$(find "$P3/gif" -mindepth 1 -maxdepth 1 -type d | wc -l)
RDAS=$(find "$P3/dmd" -type f -name '*.rda' | wc -l)
FONTS=$(find "$P3/fonts" -type f | wc -l)
log "counts: $GIFS gifs in $CATS categories, $RDAS rda, $FONTS font files"

[ -f "$P3/dmd/index.json" ] || fail "dmd/index.json missing after copy"
SRC_RDAS=$(find "$CONTENT/dmd" -type f -name '*.rda' | wc -l)
[ "$RDAS" -eq "$SRC_RDAS" ] || fail "rda count mismatch: staged $RDAS != source $SRC_RDAS"
[ "$FONTS" -ge 10 ] || fail "fonts look incomplete ($FONTS files)"
if [ "$LITE" != 1 ]; then
    [ "$GIFS" -ge 10200 ] || fail "gif count too low for a full build: $GIFS"
    [ "$CATS" -ge 15 ] || fail "gif category count too low for a full build: $CATS"
else
    [ "$GIFS" -ge 550 ] || fail "gif count too low for a lite build: $GIFS"
    [ "$CATS" -ge 8 ] || fail "gif category count too low for a lite build: $CATS"
fi

log "OK"
exit 0
