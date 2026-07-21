"""Player daemon entry point: python3 -m rpi2dmd.main

Startup order: config load -> network bootstrap (Linux) -> driver ->
control server -> boot splash -> scheduler loop. SIGTERM/SIGINT stop the
loop cleanly (panel blanked, final status write).

Python 3.7 compatible.
"""

import argparse
import gc
import os
import random
import signal
import sys
import time

from . import config, control, library, matrix, netsetup, paths, \
    scenes, scheduler, transitions, weather


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="rpi2dmd", description="RPI2DMD v3 player")
    p.add_argument("--sim", action="store_true",
                   help="use the simulator driver (no LED hardware)")
    p.add_argument("--frames", type=int, default=None,
                   help="stop after N frames (testing)")
    p.add_argument("--snapshot", default=None,
                   help="write a periodic PNG snapshot to this path (sim)")
    p.add_argument("--config", default=None,
                   help="config file path (default: media partition)")
    p.add_argument("--once-scene", default=None,
                   help="play a single scene and exit: clock|date|message|"
                        "ip|test|splash|dmd|gif|name|weather")
    p.add_argument("--fast", action="store_true",
                   help="do not sleep between frames (testing)")
    p.add_argument("--seed", type=int, default=None,
                   help="seed the scheduler RNG (testing)")
    return p.parse_args(argv)


def build_once_scene(name, cfg, sched, lib, rng):
    """--once-scene helper: scene generator for a single named scene."""
    canvas = sched.canvas
    if name == "clock":
        sc = scenes.clock_scene(cfg, canvas,
                                cfg.get("clock.idle_dwell_ms", 6000),
                                sched._backgrounds, rng=rng)
        return transitions.wrap(sc, cfg.get("clock.transition", "random"),
                                rng)
    if name == "date":
        return scenes.date_scene(cfg, canvas)
    if name == "message":
        return scenes.message_scene(cfg, canvas=canvas, rng=rng)
    if name == "ip":
        return scenes.ip_scene(cfg, canvas)
    if name == "test":
        return scenes.test_scene(cfg, canvas)
    if name == "splash":
        return scenes.boot_splash_scene(cfg, canvas)
    if name == "name":
        return scenes.name_scene(cfg, "RPI2DMD V3", canvas)
    if name == "weather":
        data = {"temp": 72, "condition": "Clear", "city": "Preview"}
        return scenes.weather_scene(cfg, data, canvas)
    if name == "dmd":
        item = lib.pick_dmd(rng, cfg)
        if item is None:
            return None
        return scenes.dmd_scene(cfg, item[2], canvas=canvas)
    if name == "gif":
        item = lib.pick_gif(rng, cfg)
        if item is None:
            return None
        return scenes.gif_scene(cfg, item[2], canvas=canvas)
    return None


SAFE_PANEL = {"pwm_bits": 7, "limit_refresh_hz": 120, "pwm_dither_bits": 0,
              "pwm_lsb_nanoseconds": 0, "gpio_slowdown": 2}
CRASH_WINDOW_S = 90
CRASH_LIMIT = 3


def _crash_loop_guard(cfg):
    """If we keep dying seconds after start, the panel settings are the most
    likely culprit (they drive a realtime thread that can starve the box).
    Fall back to values known to run, rather than restarting forever."""
    marker = os.path.join(paths.run_dir(), "starts")
    now = time.time()
    starts = []
    try:
        with open(marker, "r") as f:
            starts = [float(x) for x in f.read().split() if x.strip()]
    except (OSError, ValueError):
        starts = []
    starts = [t for t in starts if now - t < CRASH_WINDOW_S]
    starts.append(now)
    try:
        with open(marker, "w") as f:
            f.write(" ".join("%.0f" % t for t in starts))
    except OSError:
        pass
    if len(starts) >= CRASH_LIMIT:
        sys.stderr.write(
            "rpi2dmd: %d starts in %ds - falling back to safe panel "
            "settings (%s)\n" % (len(starts), CRASH_WINDOW_S, SAFE_PANEL))
        for k, v in SAFE_PANEL.items():
            cfg.data["panel"][k] = v
    return cfg


def _gc_watch():
    """Log garbage-collection pauses long enough to steal a frame. The
    render loop shares the interpreter; a full (gen-2) pass scans every
    live object — library index included — holding the GIL throughout,
    and is the prime suspect for scattered 300-1000ms frame stalls."""
    t0 = [0.0]

    def cb(phase, info):
        if phase == "start":
            t0[0] = time.time()
        else:
            ms = (time.time() - t0[0]) * 1000.0
            if ms >= 50:
                sys.stderr.write(
                    "gc: gen%s pause %.0fms (collected %s)\n"
                    % (info.get("generation"), ms, info.get("collected")))

    gc.callbacks.append(cb)


def main(argv=None):
    # Shorter GIL slices: the prefetch decoder shares the interpreter with
    # the frame-pacing render loop, and the default 5ms switch interval is
    # visible jitter at second-boundary precision.
    sys.setswitchinterval(0.002)
    _gc_watch()
    args = parse_args(argv)
    cfg = config.Config(args.config)
    if not args.sim:
        _crash_loop_guard(cfg)
    try:
        netsetup.apply(cfg)
    except Exception as e:
        sys.stderr.write("netsetup failed: %s\n" % e)

    driver = matrix.create_driver(
        cfg, sim=args.sim, out_dir=os.path.join(paths.run_dir(), "sim-out"))
    lib = library.Library()
    state = control.PlayerState(cfg, lib)
    server = control.ControlServer(state)
    server.start()
    weather_svc = weather.WeatherService(cfg).start()

    rng = random.Random(args.seed) if args.seed is not None \
        else random.Random()
    sched = scheduler.Scheduler(cfg, driver, state, lib, rng=rng,
                                weather=weather_svc, fast=args.fast,
                                max_frames=args.frames,
                                snapshot_path=args.snapshot)

    # Everything allocated so far (module code, the 13k-item library
    # index) is immortal for this process: freeze it out of every future
    # GC scan. Then disable AUTOMATIC collection outright — even gen-0
    # passes measured 60-150ms here (freeing dead strip slabs) and they
    # fire mid-animation, each one a dropped frame. Reference counting
    # still frees everything acyclic immediately; actual cycles wait for
    # the scheduler's explicit collect at scene boundaries (clock time),
    # where the pause lands on a ~1s hold and is invisible.
    gc.freeze()
    gc.disable()

    def _stop_handler(signum, frame):
        state.request_stop()

    for signame in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                signal.signal(signum, _stop_handler)
            except (ValueError, OSError):
                pass

    exit_code = 0
    try:
        if args.once_scene:
            sc = build_once_scene(args.once_scene, cfg, sched, lib, rng)
            if sc is None:
                sys.stderr.write("unknown or empty scene: %s\n"
                                 % args.once_scene)
                exit_code = 2
            else:
                sched.play_scene(sc, "animation")
        else:
            sched.play_scene(
                scenes.boot_splash_scene(cfg, sched.canvas), "animation",
                {"type": "clock", "game": "", "name": "startup",
                 "started_at": state.started_at, "duration_ms": 0},
                log="splash")
            if not state.stop_requested:
                sched.run()
    except KeyboardInterrupt:
        pass
    finally:
        state.request_stop()
        weather_svc.stop()
        server.stop()
        try:
            driver.clear()
            driver.close()
        except Exception:
            pass
        state.write_status(force=True)
        server.join(2.0)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
