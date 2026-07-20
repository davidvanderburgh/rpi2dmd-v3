# Building your own RPI2DMD v3 image

This repository contains **no copyrighted content** — only the player,
web UI, tools, and build scripts. You supply the source artifacts you
own, and the builder assembles an SD image identical in behavior to the
reference device, containing exactly (and only) the content you provided.

## What you need

| Input | Required? | What it provides | How to obtain |
|---|---|---|---|
| RPI2DMD **v2 base image** (`.img`) | **yes** | the OS base, the stock ~600 GIFs, fonts | dump the SD card of an RPI2DMD v2 you own (e.g. with HDDRawCopy or Win32DiskImager "Read"), or use the vendor's v2 image file |
| **Run-DMD SD image** (`.img` or a `.zip` containing it, e.g. `B237`) | no | the 2,379 pinball DMD animations with authentic clock overlay metadata | dump the SD card of a Run-DMD clock you own |
| **GIF packs** (`.zip` or plain folders) | no | extra GIF animations | packs you own (e.g. the ULTIMATE 10K DLC), or any folder/zip of your own GIFs laid out as `gif/<Category>/*.gif` (or just `<Category>/*.gif`) |

Skip anything you don't have — the image builds with whatever you supply.
No Run-DMD image → no pinball animations (GIFs + clock still work). No
GIF packs → only the stock GIFs from your v2 image. Supply several GIF
packs and they merge (later/higher-priority packs win on filename
collisions).

## Host requirements

- Windows 10/11 with **WSL2** and an Ubuntu-like distro
  (`sfdisk losetup dosfstools e2fsprogs rsync curl python3 tar
  qemu-user-static` installed, binfmt ARM registration active),
  **or** a Linux machine (run as root).
- **Python 3.10+** with Pillow (`pip install pillow`) on the host.
- ~20 GB free disk, network access (the build downloads OS packages
  and the pinned rpi-rgb-led-matrix source).

## Build

```
mkdir inputs
#  ... copy your artifacts into inputs\ (any filenames; they are
#      recognized by name patterns and by content sniffing) ...
python builder\build.py --inputs inputs --out RPI2DMD_v3.img
```

The builder prints what it recognized before doing anything. Steps, in
order: extract packs → convert the Run-DMD image to the RDA library
(`tools/extract_b237.py`) → pull stock media from your v2 image → merge
GIF packs → pre-decode the RGF playback cache (this is what makes GIF
playback fast on the Pi Zero) → assemble and build the image in WSL →
verify. The chroot stage takes 30–90 minutes; everything is resumable
(re-running skips work already done in `--work`).

Flash the result with Raspberry Pi Imager ("Use custom") to an 8 GB+
card. First boot expands the media partition to fill the card. Then see
`config/README.txt` on the card's media partition for Wi-Fi setup.

## Adding new pack types

Unknown zips/folders that contain `<Category>/*.gif` trees are picked up
automatically as generic GIF packs. To give a pack a fixed identity,
password handling, or merge priority, drop a descriptor into
`builder/packs/`:

```json
{
  "id": "my-pack",
  "title": "My GIF Pack",
  "kind": "gif-pack",
  "match": ["*my pack*.zip"],
  "password": "filename_token",
  "priority": 40
}
```

- `kind`: `gif-pack`, `rundmd-image`, or `base-image`
- `match`: case-insensitive filename globs applied to the inputs dir
- `password`: a literal string, or `"filename_token"` meaning the last
  whitespace-separated token of the filename (some packs ship with the
  password embedded in the name)
- `priority`: gif-pack merge order; higher wins on collisions
  (stock v2 gifs are priority 0, generic unmatched packs 10)

## Content ownership

The builder exists precisely so that images with third-party content are
built privately by the person who owns that content. Don't redistribute
built images containing content you don't have the rights to.
