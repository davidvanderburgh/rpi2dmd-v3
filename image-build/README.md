# RPI2DMD v3 — image build

Assembles the flashable `RPI2DMD_v3.img` SD card image from:

- the proven v2 base image (`RPI2DMD_v2_standard.img`, Raspbian Buster,
  kernel 4.19, DS1307 RTC, tmpfs logs) — boot + rootfs are carried over,
- the v3 software in this repo (`player/`, `webui/`),
- python3 / Flask / Pillow / fatresize from the legacy Buster archives and
  the rpi-rgb-led-matrix Python bindings built inside an ARM chroot
  (qemu-arm-static),
- the staged content (`v3-content/`: RDA library, v2 stock GIFs + fonts,
  ULTIMATE 10K DLC + bonus pack).

## Partition layout

MBR, **disk-id 0xab425f5e kept from v2** so `PARTUUID=ab425f5e-01/-02`
in `cmdline.txt`/`fstab` keep working. v2's empty 512 KiB p4 is dropped.

| # | start (sectors) | size | type | fs | content |
|---|----------------|------|------|----|---------|
| 1 | 8192 | 256 MiB | c | FAT32 | boot (v2 kernel/firmware, unchanged) |
| 2 | 532480 | 2.5 GiB | 83 | ext4 `RPI2DMDOS` | v2 rootfs + v3 overlay |
| 3 | 5775360 | rest | c | FAT32 `RPI2DMDGIF` | media (gif/dmd/fonts/config) |

On first boot `rpi2dmd-firstboot.service` grows p3 to fill the card
(sfdisk + fatresize; gated on the `/boot/rpi2dmd-expand` flag, which is
always removed afterwards so a failure can never brick the boot).

## Building (Windows + WSL2)

Requirements: WSL2 Ubuntu default distro with `sfdisk losetup dosfstools
e2fsprogs rsync curl python3 qemu-user-static` (binfmt ARM registration),
internet access, ~13 GB free in the WSL disk.

```powershell
cd rpi2dmd-v3\image-build
.\build.ps1                    # full image: 7.0 GiB, all content
.\build.ps1 -Lite -SizeGb 3.7  # 4 GB-card variant without the 10K DLC
```

`build.ps1` copies the inputs into `~/rpi2dmd-build` inside WSL (loop
mounts + the qemu chroot are far faster off the Windows drive), runs
`build.sh`, verifies with `verify-image.sh`, and drops
`RPI2DMD_v3.img` (+ `.build-info.txt` with the pinned versions) next to
the v2 image. The chroot stage (apt install + cython build of the
rpi-rgb-led-matrix bindings under qemu) is the slow part: expect 30–90+
minutes.

### Directly under Linux/WSL

```bash
sudo bash build.sh \
  --v2-img ~/rpi2dmd-build/RPI2DMD_v2_standard.img \
  --repo   ~/rpi2dmd-build/repo \
  --content ~/rpi2dmd-build/content \
  --out    ~/rpi2dmd-build/RPI2DMD_v3.img \
  [--size-gb 7.0] [--lite]
sudo bash verify-image.sh --img ~/rpi2dmd-build/RPI2DMD_v3.img [--lite]
```

The rpi-rgb-led-matrix commit is pinned (see `VERSIONS.md` — newer
commits dropped the python-3.7-compatible build) and can be overridden
with `RGB_MATRIX_SHA=<sha>` in the environment; the SHA used is recorded
in the `.build-info.txt` next to the image.

If `legacy.raspbian.org` / `archive.raspberrypi.org` are unreachable the
build **stops** rather than shipping an image without the python stack.

## What the build changes vs. v2

- `/opt/rpi2dmd-v3/{player,webui}` installed; `rpi2dmd-player`,
  `rpi2dmd-web`, `rpi2dmd-firstboot` systemd units enabled.
- apache2 autostart disabled (the v3 web UI takes port 80); v2 binaries
  stay at `/opt/RPI2DMD/` but the `.bashrc` go.sh autostart is commented
  out.
- `python3 python3-flask python3-pil fatresize parted` installed; build
  toolchain purged again after compiling the rgbmatrix bindings.
- `/etc/tmpfiles.d/rpi2dmd.conf` creates `/run/rpi2dmd` at boot.
- media partition rebuilt: `gif/` (v2 stock + 10K DLC + bonus, later packs
  win on name collisions), `dmd/` (2,379 RDA + index.json), `fonts/`,
  `config/` (v2-style `config.txt` Wi-Fi template + README).

## Flashing

Any of:

- **Raspberry Pi Imager** — "Choose OS → Use custom" → `RPI2DMD_v3.img`
  (skip the OS customization dialog: Wi-Fi is set via `config/config.txt`).
- **Win32DiskImager / balenaEtcher** — write `RPI2DMD_v3.img` to the card.
- Linux: `sudo dd if=RPI2DMD_v3.img of=/dev/sdX bs=4M conv=fsync status=progress`

Card must be ≥ 8 GB for the full image (≥ 4 GB for `-Lite -SizeGb 3.7`).

First boot: the media partition is expanded to fill the card (can take a
few minutes; the log ends up at `/boot/rpi2dmd-expand.log`), then the
player starts. To join Wi-Fi headlessly, edit `config/config.txt` on the
`RPI2DMDGIF` partition before the first boot. Web UI:
`http://rpi2dmd.local/` or the device IP.

## Files

- `build.sh` — main build (run as root; see usage above)
- `chroot-setup.sh` — runs inside the ARM chroot: apt + rgbmatrix build +
  import verification (before and after purging the toolchain)
- `stage-media.sh` — media partition population + config templates
- `verify-image.sh` — read-only PASS/FAIL verification of a built image
- `firstboot/rpi2dmd-expand.sh` — on-device p3 expansion (never fails boot)
- `systemd/*.service` — the three v3 units
- `build.ps1` — Windows/WSL wrapper (copy in → build → verify → copy out)
- `VERSIONS.md` — pinned rpi-rgb-led-matrix commit + package versions
