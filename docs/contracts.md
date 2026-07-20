# v3 runtime contracts (authoritative)

All components MUST code to these. Python 3.7 / Pillow 5.4 / Flask 1.0.2
compatible (Raspbian Buster). No third-party deps beyond Flask + Pillow.
No network resources at runtime except OpenWeather (optional) — the web UI
must work fully offline (no CDN links).

## Filesystem layout (on the Pi)

```
/opt/rpi2dmd-v3/player/rpi2dmd/   python package (rda, config, clock, ...)
/opt/rpi2dmd-v3/webui/            Flask app (app.py, templates/, static/)
/opt/RPI2DMD/                     v2 binaries, kept but not autostarted
/media/usb/dmd/                   RDA library: <GAME>/<NAME>.rda + index.json
/media/usb/gif/<Category>/*.gif   GIF library (v2 + DLC)
/media/usb/gif-cache/<Category>/<file>.gif.rgf
                                  pre-decoded frame cache (rpi2dmd/rgf.py;
                                  built on a PC by tools/build_gif_cache.py;
                                  optional — player falls back to on-device
                                  GIF decode, ~100ms/frame on the Zero, with
                                  a 20s budget + skip; stale-guarded by the
                                  source .gif's byte size)
/media/usb/fonts/                 TTF fonts, patterns, Background_*.gif
/media/usb/config/rpi2dmd.json    the ONLY v3 config document
/media/usb/config/config.txt      v2 legacy (wifi bootstrap + migration)
/run/rpi2dmd/status.json          player -> world (atomic replace)
/run/rpi2dmd/control.sock         world -> player (AF_UNIX, JSON lines)
```

Dev machines: every path above must be overridable — `rpi2dmd.paths`
module exposes `media_root()`, `run_dir()` honoring env vars
`RPI2DMD_MEDIA` (default `/media/usb`) and `RPI2DMD_RUN` (default
`/run/rpi2dmd`). On platforms without AF_UNIX the control server binds
TCP 127.0.0.1:9077 instead (env `RPI2DMD_CTL_TCP` forces it).

## Control socket protocol

Newline-delimited JSON request/response, one request per connection.
Request: `{"cmd": "<name>", ...args}` → Response `{"ok": true, ...}` or
`{"ok": false, "error": "msg"}`.

| cmd | args | effect |
|-----|------|--------|
| `status` | – | returns the same object as status.json |
| `reload_config` | – | re-read rpi2dmd.json, apply live |
| `pause` / `resume` | – | blank panel & hold / continue |
| `skip` | – | end current scene immediately |
| `sleep` / `wake` | – | manual sleep mode toggle |
| `play` | `type`: "dmd"\|"gif", `id`: "GAME/NAME" (dmd) or "Category/file.gif" (gif) | queue this item next, skip current |
| `marquee` | `text` | scroll a message once, then resume |
| `test_pattern` | – | run the DMD test loop once |
| `brightness` | `percent` (0-100) | temporary override until next hour tick |
| `stop` | – | graceful daemon shutdown |

## status.json shape

```json
{
  "state": "clock|animation|paused|sleeping|message|test",
  "now_playing": {"type": "dmd|gif|clock|message", "game": "", "name": "",
                   "started_at": 1234567890.0, "duration_ms": 0},
  "brightness": 60, "tint": "amber",
  "uptime_s": 123, "started_at": 1234567890.0,
  "counts": {"dmd_animations": 2379, "dmd_enabled": 2375,
              "gif_files": 10500, "gif_enabled": 9800},
  "version": "3.0.0", "updated_at": 1234567890.0
}
```

## Scene/scheduler model (player)

A scene is an iterator yielding `(PIL.Image RGB canvas-sized, hold_ms)`.
The main loop: pick scene per scheduler → for each frame: check control
flags (skip/pause/reload) → driver.show(frame) → sleep hold_ms.
Scheduler implements the Run-DMD model:

1. Clock scene idles for the configured animation-frequency gap
   (`config.animation_gap_seconds(rng)`, None = animations off → clock
   forever, interleaving date/weather/message at their frequencies).
2. Then one animation: DMD RDA with probability `playback.dmd_share`%,
   else GIF; chosen uniformly among enabled items
   (`playback.content_filter` == "show_all" ignores the enabled flags).
3. Clock overlay during DMD animations: `playback.clock_overlay` =
   `auto` (per-anim RDA metadata: type/size/x/y/start-end frames) |
   `front` | `back` | `off`. In index space via
   `clock.composite_clock_indexed`, palette applied after.
4. `playback.show_name`: before/after = scrolling title card, during =
   static lower-left small text, hide.
5. Date scene every `date.every_n_cycles` clock cycles; weather scene
   when configured & fetched; message scene per its frequency.
6. Sleep window (schedule.sleep→wake, if enabled): blank panel, state
   "sleeping"; manual sleep/wake commands override until next boundary.
7. Brightness: per-hour table applied at scene boundaries + hour ticks.

Config hot-reload: `Config.changed_on_disk()` polled at scene boundaries
(SMB edits), plus `reload_config` command applies immediately.

## Web UI (Flask 1.0 API)

Binds `0.0.0.0:<web.port>` (default 80). Pages (server-rendered Jinja +
vanilla JS fetch): Dashboard `/`, Clock `/clock`, Library `/library`,
Playback `/playback`, Message `/message`, Schedule `/schedule`,
Network `/network`, System `/system`.

JSON API under `/api/`:
- `GET /api/status` — proxy of control `status` (fallback: status.json,
  else `{"state": "offline"}`).
- `GET/POST /api/config` — full config document; POST validates, saves via
  `Config.save()`, then control `reload_config`.
- `POST /api/control/<cmd>` — pause/resume/skip/sleep/wake/reboot/
  shutdown/restart_player/test_pattern (reboot/shutdown/restart via
  systemctl/shutdown subprocess).
- `GET /api/preview/clock.png?<overrides>` — renders `clock.render_scene`
  at 4x NEAREST upscale with query-param overrides merged over saved
  clock config (style, format, colon, align, x, y, shade, tint, font,
  font_size, color_mode, color, background) — the live preview.
- `GET /api/library` — RDA index.json + GIF category scan + enabled flags.
- `GET /api/preview/anim/<game>/<name>.gif` — `rda.rda_to_gif` at 2x,
  cached in `<run_dir>/preview-cache/`.
- `GET /api/preview/gif/<category>/<file>` — serves the raw GIF.
- `POST /api/library/toggle` — `{"kind": "dmd_game"|"dmd_anim"|
  "gif_category", "id": "...", "enabled": bool}` → updates config doc.
- `GET /api/logs?unit=rpi2dmd-player` — `journalctl -u <unit> -n 200`.
- `GET /api/backup` — download rpi2dmd.json; `POST /api/restore` upload.
- Optional Basic auth when `web.auth_enabled` (before_request hook).

## systemd units (image-build)

- `rpi2dmd-player.service`: After=local-fs.target, WorkingDirectory
  /opt/rpi2dmd-v3/player, Environment PYTHONPATH, ExecStart
  `/usr/bin/python3 -m rpi2dmd.main`, Restart=always, RestartSec=3.
- `rpi2dmd-web.service`: After=network.target rpi2dmd-player.service,
  ExecStart `/usr/bin/python3 /opt/rpi2dmd-v3/webui/app.py`,
  Restart=always.
- `rpi2dmd-firstboot.service`: ConditionPathExists=/boot/rpi2dmd-expand,
  Before=rpi2dmd-player.service, expands p3 to fill the SD card
  (parted resizepart + fatresize — see image-build), removes the flag.
- Wi-Fi/hostname bootstrap from config stays: player main applies
  network.* config at startup like v2 go.sh (write wpa_supplicant.conf +
  hostname, reboot if changed).
