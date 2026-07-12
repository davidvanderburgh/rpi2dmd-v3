# RPI2DMD v3 — Architecture

v3 turns the RPI2DMD from a GIF slideshow into a Run-DMD-class DMD clock:
the clock is the idle screen, Run-DMD's 2,379 pinball animations (with their
original per-animation clock behavior) plus the v2/DLC GIF library are
interleaved with it, and everything is configured from a web interface.

## Hardware / base

- Raspberry Pi driving HUB75 panels via rpi-rgb-led-matrix (default:
  2 chained 64x32 = **128x32**, GPIO slowdown 2, PWM 11 bits — same as v2).
- Base OS: the proven `RPI2DMD_v2_standard.img` Raspbian Buster (armhf),
  DS1307 RTC, audio disabled (hardware-pulse timing), tmpfs logs.
- v2's compiled renderers stay at `/opt/RPI2DMD/` as a fallback but are no
  longer autostarted (the `.bashrc` hook is removed).

## Processes

```
systemd ─┬─ rpi2dmd-player.service   (python3, root)   the renderer/scheduler
         └─ rpi2dmd-web.service      (python3, root)   Flask web UI on :80
```

IPC: web → player over a line-delimited-JSON unix socket
(`/run/rpi2dmd/control.sock`); player → web via `/run/rpi2dmd/status.json`
(rewritten atomically on scene changes). Apache2 is disabled.

## Player (`player/rpi2dmd/`)

- `rda.py` — RDA animation container (see rda-format.md), 16-step tint
  palettes. Shared by converter, player, web preview.
- `config.py` — typed config: load/save/merge/defaults + migration from v2
  `config.txt`. Config lives at `/media/usb/config/rpi2dmd.json` (FAT
  partition → editable over SMB like v2; mtime-watched). Wi-Fi/hostname keys
  still honored from legacy `config.txt` for headless first setup.
- `clock.py` — clock/date renderer: TTF fonts (media partition `fonts/`),
  pixel-perfect builtin DMD digit style, color/pattern fills, optional
  animated GIF background, 12/24h, seconds, AM/PM, blinking colon,
  alignment/position, shade. Renders onto a PIL canvas; used identically by
  the player and by the web UI's live preview endpoint.
- `scenes.py` — scene providers: ClockScene, DmdAnimationScene (RDA, honors
  per-anim clock type/position/frame-range with global override
  AUTO/FRONT/BACK/OFF), GifScene (category GIFs, optional clock overlay),
  DateScene, WeatherScene (OpenWeather), MessageScene (scrolling custom
  text/marquee with movement modes), IpScene, NameScene (animation title
  scroll before/after), TestScene.
- `scheduler.py` — Run-DMD playback model: clock idles for the configured
  "animation frequency" gap (random 1–20 s default … fixed … disabled), then
  plays a random enabled animation (weighted across DMD library + GIF
  categories), interleaving date/weather/custom-message at their own
  frequencies; sleep/wake daily schedule + per-hour brightness table;
  manual pause/resume/skip/"play this now" via control socket.
- `transitions.py` — clock appear/hide transitions: scroll up/down
  combinations, fade, none, random (Run-DMD CLK TRANSITION parity).
- `matrix.py` — output drivers: `RgbMatrixDriver` (rpi-rgb-led-matrix
  Python bindings, double-buffered SwapOnVSync) and `SimDriver`
  (PNG/GIF dump for development and CI on any machine).
- `library.py` — content index: RDA `index.json` + GIF category scan +
  per-item/per-group enable flags (stored in config), factory-disabled list.
- `control.py` — unix-socket command server inside the player: status,
  reload_config, pause, resume, skip, sleep, wake, play(item), preview
  brightness/tint, marquee(text), test_pattern, stop.
- `main.py` — daemon entry: config load, driver init, boot splash
  (authentic RUN-DMD logo extracted from B237), scheduler loop, SIGTERM
  clean shutdown (panel blank).

Run-DMD feature parity map (from the v3.54 menu spec): TIME FORMAT ✓,
CLOCK STYLE → fonts/styles incl. authentic pixel digits ✓, CLOCK SHADE ✓,
CLOCK DOTS ✓, CLK TRANSITION ✓, ANIMATIONS FREQUENCY/BROWSE/BY GROUP/
ENABLE-DISABLE ALL/CLOCK OVERLAY/SHOW NAME ✓ (web UI instead of 4-button
menu), CUSTOM MESSAGE (text/speed/mode/clock-position/frequency/movement/
position) ✓, DMD BRIGHTNESS → per-hour table + global ✓, SLEEP/WAKE ✓,
STARTUP IMAGE ✓, CONTENT FILTER ✓, TOOLS>DMD TEST ✓, temperature (CPU) in
message tokens ✓. Hardware-only features (RF power control, EXP ambient
light, button/DMD-type detection) are out of scope; physical buttons are
replaced by the web UI (v2 never had buttons wired by default).

## Web UI (`webui/`)

Flask (Buster apt python3-flask) + vanilla JS single-page style, no CDN.
Pages/API:

- **Dashboard** — now playing (live), IP/hostname/uptime/CPU temp/version,
  pause-resume/skip/sleep, quick toggles (clock/animations/date/weather).
- **Clock designer** — every clock.py knob with a **live server-rendered
  preview** (`/api/preview/clock.png` renders through the exact player code
  path at 4x scale), presets, font upload to the media partition.
- **Library** — DMD games and GIF categories: browse, per-item preview
  (`/api/preview/anim/<id>.gif` rendered from RDA on demand, cached in
  /tmp), enable/disable item/group/all, "play now".
- **Playback** — animation frequency, clock overlay mode, show-name mode,
  transitions, tint, order, startup image behavior.
- **Message** — custom message editor + immediate marquee send.
- **Schedule** — sleep/wake times, 24-hour brightness curve editor.
- **Network** — hostname, Wi-Fi country/SSID/PSK (applies wpa_supplicant
  like v2 go.sh did), weather API settings, timezone.
- **System** — restart player / reboot / shutdown, logs tail, config
  backup/restore (download/upload JSON), factory reset to defaults.

Optional HTTP Basic auth (off by default, matching v2's trusted-LAN
posture — the device also has a guest-writable SMB share).

## Image build (`image-build/`)

Runs in WSL2 Ubuntu as root (`wsl -u root`); host stages content first.

1. Assemble a **fresh** image file (default 7.2 GiB for 8 GB+ cards;
   `--lite` variant sized ≈3.7 GiB without the 10K DLC pack for 4 GB cards):
   MBR **disk id ab425f5e kept from v2** so `PARTUUID=ab425f5e-01/-02` in
   fstab/cmdline keep working; p1 FAT32 boot 256 MiB, p2 ext4 rootfs
   2.5 GiB, p3 FAT32 `RPI2DMDGIF` media (rest). The all-zero 512 KiB p4 of
   v2 is dropped.
2. p1 ← v2 boot files unchanged (kernel 4.19, i2c-rtc overlay, config.txt).
3. p2 ← rsync of v2 rootfs, then the v3 overlay: `/opt/rpi2dmd-v3/`
   software, systemd units enabled, apache2 + old `.bashrc` autostart
   disabled, `qemu-arm-static` chroot to `apt install` python3/Flask/Pillow
   from legacy Buster archives and to build the rpi-rgb-led-matrix Python
   bindings (pinned commit) natively.
4. p3 ← media content: `gif/` (v2 stock + ULTIMATE 10K DLC + bonus pack
   merged), `dmd/` (RDA library, 142 MB), `fonts/` (v2 fonts + patterns +
   backgrounds), `config/` (defaults + migrated v2 keys).
5. First boot: `rpi2dmd-firstboot.service` grows p3 to fill the card,
   then disables itself.

## Content staging (`v3-content/`, not in git)

- `dmd/` — RDA library (converter output)
- `media-base/` — v2 stock gif/ + fonts/
- `dlc10k/`, `bonus/` — extracted DLC packs
