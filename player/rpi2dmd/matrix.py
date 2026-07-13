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
    """hzeller rpi-rgb-led-matrix Python bindings, double buffered."""

    def __init__(self, cfg):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions

        panel = cfg["panel"]
        opts = RGBMatrixOptions()
        opts.cols = int(panel["cols"])
        opts.rows = int(panel["rows"])
        opts.chain_length = int(panel["chain"])
        opts.parallel = int(panel["parallel"])
        opts.gpio_slowdown = int(panel["gpio_slowdown"])
        opts.led_rgb_sequence = str(panel.get("rgb_order", "RGB"))
        opts.pwm_bits = int(panel.get("pwm_bits", 11))
        limit = int(panel.get("limit_refresh_hz", 0) or 0)
        if limit:
            opts.limit_refresh_rate_hz = limit
        opts.drop_privileges = False
        self._matrix = RGBMatrix(options=opts)
        self._canvas = self._matrix.CreateFrameCanvas()
        self.width = opts.cols * opts.chain_length
        self.height = opts.rows * opts.parallel

    def show(self, image):
        if image.mode != "RGB":
            image = image.convert("RGB")
        if image.size != (self.width, self.height):
            # The bindings index straight into the buffer; a wrong-sized
            # frame makes them compute a negative length and raise
            # OverflowError, which would kill the whole scene.
            image = image.resize((self.width, self.height))
        try:
            self._canvas.SetImage(image)
        except (OverflowError, ValueError) as e:
            # One bad frame must not abort the animation.
            sys.stderr.write(
                "matrix: dropped frame (%s) size=%r mode=%r\n"
                % (e, image.size, image.mode))
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
