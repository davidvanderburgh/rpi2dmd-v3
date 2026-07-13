"""Output drivers: real LED matrix (rpi-rgb-led-matrix) and simulator.

The player always renders to a PIL RGB image of size
(panel.cols * panel.chain, panel.rows * panel.parallel) — 128x32 by
default — and hands it to a driver.

Python 3.7 compatible.
"""

import os
import sys
import time


class BaseDriver(object):
    width = 128
    height = 32

    def show(self, image):
        raise NotImplementedError

    def set_brightness(self, percent):
        pass

    def clear(self):
        from PIL import Image
        self.show(Image.new("RGB", (self.width, self.height)))

    def close(self):
        pass


class RgbMatrixDriver(BaseDriver):
    """hzeller rpi-rgb-led-matrix Python bindings, double buffered.

    32-bit ARM hazard: the binding's fast path reads Pillow's internal buffer
    pointer and casts it to size_t. Pillow images allocated on a *worker*
    thread come from a glibc arena mmap'd high in the address space, so that
    pointer is negative as a signed 32-bit int and SetImage raises
    "OverflowError: can't convert negative value to size_t" — every frame of
    every animation decoded off-thread (our prefetcher) failed this way.
    We therefore blit each frame into one scratch image allocated on the
    thread that owns the matrix, and hand the binding only that.
    """

    def __init__(self, cfg):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        panel = cfg["panel"]

        def clamped(key, default, low, high):
            """Panel values reach the matrix library's realtime thread, which
            runs SCHED_FIFO at priority 99 on a single core. A silly value
            there can starve the whole system to the point where even sshd
            cannot answer, so nothing hostile gets through."""
            try:
                v = int(panel.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, v))

        opts = RGBMatrixOptions()
        opts.cols = clamped("cols", 64, 8, 256)
        opts.rows = clamped("rows", 32, 8, 128)
        opts.chain_length = clamped("chain", 2, 1, 12)
        opts.parallel = clamped("parallel", 1, 1, 3)
        opts.gpio_slowdown = clamped("gpio_slowdown", 2, 0, 5)
        opts.led_rgb_sequence = str(panel.get("rgb_order", "RGB"))
        pwm_bits = clamped("pwm_bits", 7, 1, 11)
        opts.pwm_bits = pwm_bits
        limit = clamped("limit_refresh_hz", 120, 0, 400)
        if limit:
            opts.limit_refresh_rate_hz = limit
        # Dithering skips the least-significant PWM planes. It is NOT a CPU
        # win on a Pi Zero (measured: refresh thread went 75% -> 88%), so it
        # defaults off; it must also stay below pwm_bits to be meaningful.
        dither = clamped("pwm_dither_bits", 0, 0, 2)
        if dither and dither < pwm_bits:
            try:
                opts.pwm_dither_bits = dither
            except AttributeError:
                pass
        lsb_ns = clamped("pwm_lsb_nanoseconds", 0, 0, 3000)
        if lsb_ns:
            try:
                opts.pwm_lsb_nanoseconds = lsb_ns
            except AttributeError:
                pass
        opts.drop_privileges = False
        self._matrix = RGBMatrix(options=opts)
        self._canvas = self._matrix.CreateFrameCanvas()
        self.width = opts.cols * opts.chain_length
        self.height = opts.rows * opts.parallel
        self._safe_path = False
        self._scratch = self._make_scratch()

    def _make_scratch(self):
        """An RGB buffer this thread owns that the binding accepts. Rejected
        candidates are kept alive so the allocator hands out a new address."""
        from PIL import Image

        rejected = []
        for _ in range(64):
            img = Image.new("RGB", (self.width, self.height))
            try:
                self._canvas.SetImage(img)
                return img
            except OverflowError:
                rejected.append(img)
        # Never found a usable address: fall back to the binding's safe (but
        # slow) per-pixel path rather than showing nothing.
        self._safe_path = True
        sys.stderr.write("matrix: no low-address buffer available; using the "
                         "slow SetImage path\n")
        return Image.new("RGB", (self.width, self.height))

    def show(self, image):
        if image.mode != "RGB":
            image = image.convert("RGB")
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        self._scratch.paste(image, (0, 0))   # in place: address never moves
        try:
            if self._safe_path:
                self._canvas.SetImage(self._scratch, 0, 0, False)
            else:
                self._canvas.SetImage(self._scratch)
        except (OverflowError, ValueError) as e:
            sys.stderr.write("matrix: dropped frame (%s)\n" % e)
            return
        self._canvas = self._matrix.SwapOnVSync(self._canvas)

    def set_brightness(self, percent):
        self._matrix.brightness = max(0, min(100, int(percent)))

    def close(self):
        self._matrix.Clear()


class SimDriver(BaseDriver):
    """Development driver: keeps the latest frame as PNG (atomic replace)
    and can record frame sequences for inspection/tests."""

    def __init__(self, cfg, out_dir="sim-out", record=False, scale=4):
        panel = cfg["panel"]
        self.width = int(panel["cols"]) * int(panel["chain"])
        self.height = int(panel["rows"]) * int(panel["parallel"])
        self.out_dir = out_dir
        self.record = record
        self.scale = scale
        self.brightness = 100
        self.frame_count = 0
        self.frames = []          # (image, wallclock) when recording
        self.last_image = None
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)

    def show(self, image):
        if image.mode != "RGB":
            image = image.convert("RGB")
        self.last_image = image
        self.frame_count += 1
        if self.record:
            self.frames.append((image.copy(), time.time()))

    def snapshot(self, path=None):
        """Write the latest frame as an upscaled PNG."""
        if self.last_image is None:
            return None
        img = self.last_image.resize(
            (self.width * self.scale, self.height * self.scale), resample=0)
        path = path or os.path.join(self.out_dir, "latest.png")
        tmp = path + ".tmp"
        img.save(tmp, "PNG")  # explicit format: PIL can't infer from .tmp
        os.replace(tmp, path)
        return path

    def set_brightness(self, percent):
        self.brightness = max(0, min(100, int(percent)))

    def save_recording_gif(self, path, max_frames=400):
        if not self.frames:
            return None
        frames = self.frames[-max_frames:]
        images = []
        durations = []
        for i, (img, ts) in enumerate(frames):
            images.append(img.resize(
                (self.width * self.scale, self.height * self.scale), resample=0))
            if i + 1 < len(frames):
                durations.append(max(20, int((frames[i + 1][1] - ts) * 1000)))
            else:
                durations.append(100)
        try:
            images[0].save(path, save_all=True, append_images=images[1:],
                           duration=durations, loop=0)
        except TypeError:
            # Pillow 5.4: frames collapsed to one, duration list unsupported
            images[0].save(path, duration=sum(durations))
        return path


def create_driver(cfg, sim=False, **sim_kwargs):
    import platform
    import sys

    if sim:
        return SimDriver(cfg, **sim_kwargs)
    try:
        return RgbMatrixDriver(cfg)
    except ImportError as e:
        if platform.system() == "Linux" and platform.machine().startswith(
                ("arm", "aarch")):
            # On the actual device a missing rgbmatrix binding is fatal —
            # a silent simulator fallback would leave the panel dark while
            # everything reports healthy.
            raise RuntimeError(
                "rgbmatrix bindings unavailable on device: %s" % e)
        print("rpi2dmd: rgbmatrix not available (%s); using simulator"
              % e, file=sys.stderr)
        return SimDriver(cfg, **sim_kwargs)
