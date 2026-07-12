# RPI2DMD v3

Turns an RPI2DMD (Raspberry Pi + HUB75 LED matrix, 128x32) into a
Run-DMD-class pinball DMD clock:

- **Clock-first**: the clock is the idle screen, on by default, with fully
  configurable look (pixel DMD digits or any TTF font, tints/colors,
  backgrounds, 12/24 h, blinking colon, transitions).
- **The complete Run-DMD animation library**: all 2,379 animations from the
  Run-DMD B237 image (71 games), converted at native 128x32 with their
  original per-animation clock behavior (clock in front / behind / hidden,
  position, frame range) — plus the classic RPI2DMD GIF packs.
- **Web interface** on port 80: live status, clock designer with live
  preview, animation library browser with per-game/per-animation toggles,
  playback tuning, custom messages, sleep schedule, brightness curve,
  network/weather settings, logs, backup/restore.
- Run-DMD playback model: random animations separated by configurable
  clock intervals (random 1–20 s default), show-name modes, content
  filter, startup splash (authentic RUN-DMD logo), sleep/wake schedule.

## Layout

| dir | what |
|-----|------|
| `player/` | the display daemon (`rpi2dmd` Python package) |
| `webui/` | Flask web interface |
| `tools/` | host-side converters (Run-DMD JSON → RDA library) |
| `image-build/` | WSL scripts that build the flashable SD image from the v2 base |
| `docs/` | architecture, runtime contracts, RDA format |

## Building the image

See `image-build/README.md`. In short (Windows + WSL2):

```powershell
cd image-build
./build.ps1        # produces RPI2DMD_v3.img (~7 GiB, for 8 GB+ cards)
```

Flash with Raspberry Pi Imager / Win32DiskImager. First boot expands the
media partition to fill the card. The media partition stays a plain FAT32
volume shared over SMB (guest) — drop GIFs into `gif/<Category>/`, fonts
into `fonts/`, and edit `config/rpi2dmd.json` directly if you prefer.

## Development

The player and web UI run on a dev machine without LED hardware:

```
set RPI2DMD_MEDIA=C:\path\to\test-media
set RPI2DMD_RUN=C:\tmp\rpi2dmd-run
set RPI2DMD_CTL_TCP=127.0.0.1:9077
python -m rpi2dmd.main --sim --frames 400    # from player/
python webui/app.py                          # web UI on :80
```

All on-device code targets Python 3.7 / Pillow 5.4 / Flask 1.0 (Raspbian
Buster, matching the proven v2 base image).
