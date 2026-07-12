# Pinned versions (RPI2DMD v3 image)

## rpi-rgb-led-matrix

- Repo: https://github.com/hzeller/rpi-rgb-led-matrix
- Commit: `a3eea997a9254b83ab2de97ae80d83588f696387` (2024-07-08) — pinned
  deliberately, NOT current master:
  - `52c75c8b` (2024-07-10) ported the python bindings to cython 3.0.x
    (Buster only has cython 0.29.2),
  - `fd969bcf` (2024-07-10) removed the pre-generated cython files,
  - `52257469` (2026-02-23) removed the classic python build entirely in
    favor of scikit-build-core/cmake (not installable on python 3.7).
  `a3eea997` is the parent of that series: the last commit with the
  classic `bindings/python` Makefile + setup.py + pre-generated
  `core.cpp`, i.e. the build that Buster-era Pis have always used.
  (Master at build time was `41809e40e912b7f278ad34046f20abf5609b2b07`,
  verified unbuildable on Buster.)
- Built inside the armhf Buster chroot with
  `make build-python PYTHON=/usr/bin/python3` in `bindings/python`, then
  `python3 setup.py install` (plain distutils layout, no egg).
- Override for future builds: `RGB_MATRIX_SHA=<sha> build.sh ...`

## Base OS

- Raspbian GNU/Linux 10 (buster) armhf, kernel 4.19 (`kernel7.img`) —
  carried over unchanged from `RPI2DMD_v2_standard.img` (Dec 2019).

## apt packages (legacy.raspbian.org / archive.raspberrypi.org, buster)

Recorded from the actual chroot install of the 2026-07-12 build (also
embedded in `RPI2DMD_v3.img.build-info.txt` next to the built image):

| package | version |
|---------|---------|
| python3 | 3.7.3-1 (python3.7 3.7.3-2+deb10u7) |
| python3-flask | 1.0.2-3+deb10u1 |
| python3-pil | 5.4.1-2+deb10u6 |
| python3-jinja2 | 2.10-2+deb10u1 |
| python3-werkzeug | 0.14.1+dfsg1-4+deb10u2 |
| fatresize | 1.0.2-11 |
| parted / libparted2 | 3.2-25 |
| python3-setuptools (build only, purged) | 40.8.0-1 |
| cython3 (build only, purged) | 0.29.2-2 |
| build-essential (build only, purged) | 12.6 |

Note: apt runs with `--allow-releaseinfo-change` +
`Acquire::Check-Valid-Until=false` because the frozen buster archives
changed their Release metadata after EOL.

fatresize 1.0.2 caveats, all handled by `rpi2dmd-expand.sh` and verified
against this image in a qemu chroot with mmcblk-style device names:

- no `-s max` keyword → resize to partition-size-minus-1MiB;
- the exact partition byte size crashes libparted → never passed;
- exit code is unreliable while / is mounted on the same disk (the final
  kernel re-inform fails after a successful resize) → success judged by
  the FAT cluster count growing;
- its libparted commit REGENERATES the MBR disk identifier → the script
  writes 0xab425f5e back (dd to offset 440) so the PARTUUID mounts
  survive, and restores the boot-sector volume label with `fatlabel`.
