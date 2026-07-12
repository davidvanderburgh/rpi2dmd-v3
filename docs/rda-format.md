# RDA v1 — packed DMD animation format

Container for Run-DMD style 128x32, 4-bit grayscale animations, converted
from the Run-DMD B237 SD image (via the `D:\Pinball\dmd` JSON extraction).

## Layout (little-endian)

| offset | size | field |
|--------|------|-------|
| 0      | 4    | magic `RDA1` |
| 4      | 4    | `uint32` header length N |
| 8      | N    | UTF-8 JSON header |
| 8+N    | 2048 × num_frames | packed frames |

Frame: 128×32 pixels, 4 bits per pixel (brightness 0–15), row-major,
2 pixels per byte, **high nibble = left pixel**. 2048 bytes per frame.

## Header JSON

```json
{
  "name": "ATTACK_FROM_MARS_006",
  "game": "ATTACK_FROM_MARS",
  "width": 128, "height": 32,
  "num_frames": 24,
  "durations": [100, 100, ...],
  "clock": {
    "type": "ClockBehind",      // NoClock | ClockOnTop | ClockBehind
    "size": "ClockLarge",       // ClockSmall | ClockLarge
    "x": 0, "y": 0,             // clock anchor on the 128x32 canvas
    "start_frame": 0,           // 0 = from first frame
    "end_frame": 0              // 0 = through last frame
  },
  "intro_transition": "Enable",
  "outro_transition": "Disable"
}
```

Durations are per-frame milliseconds from the original Run-DMD data.
Color is applied at render time from a 16-step tint ramp
(`rpi2dmd.rda.build_palette`), so the DMD color is user-configurable
without touching the library.

A library-wide `index.json` (written by `tools/convert_dmd_json.py`) lists
every game and animation with frame counts, total durations, and clock
types, and is what the player/web UI use to browse without opening files.
