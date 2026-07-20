# Patched rpi-rgb-led-matrix bindings (SetImageBytes)

The stock python bindings (pinned `a3eea997`) push a frame with
`Canvas.SetImage`, whose fast path (`SetPixelsPillow` in
`bindings/python/rgbmatrix/core.pyx`) has two problems on a Pi Zero:

1. **Column-outer iteration.** The inner loop walks *rows* of Pillow's
   `image32` buffer, so every pixel read strides 512 bytes — a cache miss
   per pixel on the ARM11. Measured cost on the device: ~26 ms of the
   ~33 ms `driver.show()` p50.
2. **Pointer-cast hazard.** It reads Pillow's internal buffer pointer via
   `image.im.unsafe_ptrs['image32']` and casts through `size_t`; images
   allocated on worker threads live in high glibc arenas whose addresses
   are negative as signed 32-bit, raising `OverflowError` (the reason
   `matrix.py` keeps a main-thread scratch image at all).

## The patch (v3 image builds carry it; see `image-build/`)

- `SetPixelsPillow` iterates **row-outer** (sequential memory) and runs the
  loop under `nogil`.
- New method `Canvas.SetImageBytes(buf, width, height, offset_x=0,
  offset_y=0)`: blits raw RGB24 bytes (`image.tobytes()`, row-major) with a
  sequential `nogil` loop. No PIL object, no pointer casts — buffers from
  any thread are fine. `matrix.py` uses it when present and falls back to
  the classic paths on a stock binding.

## Rebuilding

The patched `core.pyx` is applied to the pinned tarball and `core.cpp`
regenerated with Buster's `cython3` (0.29, language_level 2 — the source
relies on implicit relative cimports) inside the armhf chroot, exactly like
the normal image build. One-off rebuild for a live device: overlay-mount
the built image's rootfs read-only + qemu chroot, `apt install
build-essential python3-dev python3-setuptools cython3`, then

    cython3 --cplus rgbmatrix/core.pyx -o rgbmatrix/core.cpp
    make build-python PYTHON=/usr/bin/python3

and copy `rgbmatrix/core.cpython-37m-arm-linux-gnueabihf.so` over
`/usr/local/lib/python3.7/dist-packages/rgbmatrix/` on the device (keep a
backup; the player falls back gracefully but an import failure would not).
