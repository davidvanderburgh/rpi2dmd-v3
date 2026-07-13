"""Scheduler: the Run-DMD playback model.

Clock idles for the configured animation-frequency gap, then one animation
plays (DMD with probability playback.dmd_share%, else GIF), interleaving
date/weather/message scenes at their own frequencies. Sleep window,
per-hour brightness, config hot-reload and the control commands
(pause/skip/play/marquee/test) are all handled here; per-scene exceptions
are logged and swallowed so the appliance never dies.

Python 3.7 compatible.
"""

import collections
import datetime
import os
import random
import sys
import threading
import time
import traceback

from PIL import Image

from . import paths, rda, scenes, transitions


class Prefetcher(object):
    """Loads the next animation in the background while the clock is up.

    Reading an .rda (or decoding a 1.8MB GIF) off the SD card takes long
    enough on a Pi Zero to leave the panel black between scenes. Doing it
    during the clock scene removes that dead space entirely.
    """

    def __init__(self, canvas):
        self.canvas = canvas
        self._lock = threading.Lock()
        self._pick = None       # ("dmd"|"gif", item tuple)
        self._data = None       # loaded payload for that pick
        self._thread = None

    def start(self, pick):
        """Begin loading `pick` in the background (no-op if already loading
        the same thing)."""
        if pick is None:
            return
        with self._lock:
            if self._pick == pick and (self._data is not None
                                       or self._thread is not None):
                return
            self._pick = pick
            self._data = None
            self._thread = threading.Thread(
                target=self._load, args=(pick,), name="prefetch")
            self._thread.daemon = True
            self._thread.start()

    def _load(self, pick):
        try:
            # Linux nice() is per-thread: keep this off the render loop's back
            # so decoding the next animation can't jitter the clock's blink.
            os.nice(10)
        except (OSError, AttributeError):
            pass
        try:
            kind, item = pick
            if kind == "dmd":
                header, frames = rda.read_rda(item[2])
                # Unpacking 4bpp -> indexes is the render loop's most
                # expensive per-frame step; do it here instead.
                indexes = [rda.unpack_frame(f) for f in frames]
                payload = (header, frames, indexes)
            else:
                payload = scenes.load_gif_frames(item[2], self.canvas)
        except Exception:
            traceback.print_exc()
            payload = None
        with self._lock:
            if self._pick == pick:      # still the item we want
                self._data = payload
                self._thread = None

    def take(self, pick, timeout=6.0):
        """-> loaded payload for `pick`, waiting briefly if it is still in
        flight; None means "load it yourself"."""
        with self._lock:
            same = (self._pick == pick)
            thread = self._thread
            data = self._data
        if not same:
            return None
        if data is None and thread is not None:
            thread.join(timeout)
            with self._lock:
                data = self._data if self._pick == pick else None
        with self._lock:
            if self._pick == pick:
                self._pick = None
                self._data = None
        return data


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

MESSAGE_FREQ_S = {"1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300}
WEATHER_SHOW_INTERVAL_S = 3600
SNAPSHOT_INTERVAL_S = 2.0
MIN_FRAME_S = 0.02        # never pace faster than 50fps
MAX_LAG_S = 0.25          # beyond this we resync rather than catch up
NICE_NORMAL = -5          # display first: hit frame deadlines
NICE_WEB_ACTIVE = 15      # someone is using the web UI: get out of its way


def _blank_scene(canvas, total_ms, tick_ms=500):
    img = Image.new("RGB", canvas)
    left = int(total_ms)
    while left > 0:
        hold = min(tick_ms, left)
        yield img, hold
        left -= hold


def _in_sleep_window(cfg, now):
    if not cfg.get("schedule.enabled", False):
        return False

    def minutes(s):
        try:
            hh, mm = str(s).split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, TypeError):
            return None

    start = minutes(cfg.get("schedule.sleep", "23:30"))
    end = minutes(cfg.get("schedule.wake", "06:30"))
    if start is None or end is None or start == end:
        return False
    t = now.hour * 60 + now.minute
    if start < end:
        return start <= t < end
    return t >= start or t < end  # window crosses midnight


class Scheduler(object):
    """Owns the main loop; drives scenes to the driver."""

    def __init__(self, cfg, driver, state, library, rng=None, weather=None,
                 fast=False, max_frames=None, snapshot_path=None):
        self.cfg = cfg
        self.driver = driver
        self.state = state
        self.library = library
        self.rng = rng or random.Random()
        self.weather = weather
        self.fast = fast
        self.max_frames = max_frames
        self.snapshot_path = snapshot_path
        if snapshot_path:
            d = os.path.dirname(snapshot_path)
            if d and not os.path.isdir(d):
                try:
                    os.makedirs(d)
                except OSError:
                    pass
        self.canvas = (driver.width, driver.height)
        self.frames_shown = 0
        # scene identifiers, for tests/debugging (bounded: 24/7 appliance)
        self.scene_log = collections.deque(maxlen=1000)
        self._sim_now = time.time()
        self._last_snapshot = 0.0
        self._last_hour = None
        self._prev_window = None
        self._last_message = 0.0
        self._last_weather = 0.0
        self._cycle = 0
        self._backgrounds = scenes.scan_backgrounds()
        self.prefetch = Prefetcher(self.canvas)
        self._ui_checked_at = 0.0
        self._ui_active = False
        self._nice_now = None

    # -- time --------------------------------------------------------------
    def _now(self):
        return self._sim_now if self.fast else time.time()

    def _advance_ms(self, ms):
        if self.fast:
            self._sim_now += ms / 1000.0

    def _sleep_ms(self, ms):
        if self.fast:
            self._advance_ms(ms)
            return
        self._sleep_until(time.time() + ms / 1000.0)

    def _sleep_until(self, deadline):
        """Sleep until an absolute deadline, waking early for control flags."""
        st = self.state
        while True:
            if st.stop_requested or st.skip_pending or st.paused:
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))

    # -- frame pump ----------------------------------------------------------
    def play_scene(self, scene, state_name, now_playing=None, log=None):
        """Drive one scene to the driver, honoring control flags.
        Exceptions inside the scene are logged and swallowed."""
        st = self.state
        self.scene_log.append(log or state_name)
        st.set_scene(state_name, now_playing)
        # Absolute frame deadlines. Sleeping `hold` *after* each frame ignores
        # the time spent decoding/compositing the next one, so every frame ran
        # long by a varying amount — animations played slow and stuttered.
        deadline = self._now()
        try:
            for img, hold in scene:
                if st.stop_requested:
                    break
                if st.take_skip():
                    break
                if state_name == "animation" and self.web_active():
                    break   # someone opened the web UI: yield the core
                if st.paused:
                    if not self._pause_gate(state_name):
                        break
                    deadline = self._now()   # resync after a pause
                self._apply_brightness()
                self.driver.show(img)
                self.frames_shown += 1
                self._maybe_snapshot()
                st.tick()
                if self.max_frames is not None \
                        and self.frames_shown >= self.max_frames:
                    st.request_stop()
                    break
                deadline += max(MIN_FRAME_S, hold / 1000.0)
                if self.fast:
                    self._advance_ms(hold)
                    continue
                now = time.time()
                if deadline < now - MAX_LAG_S:
                    # Too far behind (a big GIF, a config reload): resync
                    # instead of sprinting through frames to catch up.
                    deadline = now
                else:
                    self._sleep_until(deadline)
        except Exception:
            sys.stderr.write("scene %r failed:\n" % (state_name,))
            traceback.print_exc()
        finally:
            # consume a skip that landed at the very end of the scene so it
            # cannot leak into (and instantly cancel) the next scene
            st.take_skip()

    def _pause_gate(self, resume_state):
        """Blank the panel and hold while paused. -> False if stopping."""
        st = self.state
        try:
            self.driver.clear()
        except Exception:
            pass
        st.set_scene("paused")
        while st.paused and not st.stop_requested:
            time.sleep(0.02)
            self._advance_ms(100)
            st.tick()
        st.set_scene(resume_state)
        return not st.stop_requested

    def _maybe_snapshot(self):
        if not self.snapshot_path:
            return
        snap = getattr(self.driver, "snapshot", None)
        if snap is None:
            return
        now = time.time()
        if now - self._last_snapshot >= SNAPSHOT_INTERVAL_S:
            self._last_snapshot = now
            try:
                snap(self.snapshot_path)
            except OSError:
                pass

    # -- housekeeping ---------------------------------------------------------
    def _apply_brightness(self):
        st = self.state
        hour = datetime.datetime.now().hour
        if hour != self._last_hour:
            if self._last_hour is not None:
                st.clear_brightness_override()
            self._last_hour = hour
        pct = st.brightness_override
        if pct is None:
            pct = self.cfg.brightness_now(hour)
        if pct != st.brightness:
            try:
                self.driver.set_brightness(pct)
            except Exception:
                pass
            st.set_brightness(pct)

    def _boundary(self):
        """Scene-boundary housekeeping: hot reload + brightness + status."""
        st = self.state
        if st.take_reload() or self.cfg.changed_on_disk():
            try:
                self.cfg.load()
                self.library.refresh()
                self._backgrounds = scenes.scan_backgrounds()
            except Exception:
                traceback.print_exc()
            try:
                st.set_counts(self.library.counts(self.cfg))
            except Exception:
                traceback.print_exc()
        self._apply_brightness()
        st.tick()

    # -- web-UI back-off ---------------------------------------------------
    def web_active(self):
        """Is someone using the web UI right now? (mtime of a tmpfs file the
        web app touches per request — cheap enough to poll per frame.)"""
        if self.fast:
            return False
        now = time.time()
        if now - self._ui_checked_at < 0.5:      # cache: 2 stats/second max
            return self._ui_active
        self._ui_checked_at = now
        mode = self.cfg.get("web.on_activity", "clock_only")
        if mode == "none":
            self._ui_active = False
            return False
        try:
            age = now - os.path.getmtime(paths.ui_active_path())
        except OSError:
            self._ui_active = False
            return False
        try:
            window = float(self.cfg.get("web.activity_timeout_s", 20))
        except (TypeError, ValueError):
            window = 20.0
        self._ui_active = age < window
        return self._ui_active

    def _set_nice(self, value):
        """Renice this process's normal threads. The panel's refresh thread
        runs at realtime priority, so it is unaffected and the display keeps
        its timing — only frame *preparation* yields to the web server."""
        if self._nice_now == value or self.fast:
            return
        try:
            os.setpriority(os.PRIO_PROCESS, 0, value)
            self._nice_now = value
        except (OSError, AttributeError, PermissionError):
            self._nice_now = value    # not permitted: stop retrying

    def _web_gate(self):
        """While the web UI is in use, stop animating and give the core to
        the web server. -> True if we handled the panel here."""
        if not self.web_active():
            self._set_nice(NICE_NORMAL)
            return False
        mode = self.cfg.get("web.on_activity", "clock_only")
        st = self.state
        self._set_nice(NICE_WEB_ACTIVE)
        self.scene_log.append("web_ui")
        st.set_scene("web_ui", {"type": "clock", "game": "", "name": "web_ui",
                                "started_at": time.time(), "duration_ms": 0})
        blank = Image.new("RGB", self.canvas)
        while not st.stop_requested and self.web_active():
            if st.paused:
                if not self._pause_gate("web_ui"):
                    break
            try:
                if mode == "pause":
                    self.driver.show(blank)
                else:
                    # A single clock frame per second: negligible CPU, and
                    # the thing on the wall is still a clock.
                    self._apply_brightness()
                    self.driver.show(scenes.clock_still(self.cfg, self.canvas))
            except Exception:
                traceback.print_exc()
            self.frames_shown += 1
            self._maybe_snapshot()
            st.tick()
            if self.max_frames is not None \
                    and self.frames_shown >= self.max_frames:
                st.request_stop()
                break
            self._sleep_ms(1000)
        self._set_nice(NICE_NORMAL)
        return True

    def _sleep_gate(self):
        """Handle the sleep window / manual sleep. -> True if it slept."""
        st = self.state
        now = datetime.datetime.now()
        window = _in_sleep_window(self.cfg, now)
        if window != self._prev_window:
            self._prev_window = window
            st.clear_sleep_override()
        ov = st.sleep_override
        if not (ov == "sleep" or (window and ov != "wake")):
            return False
        st.set_scene("sleeping", {"type": "clock", "game": "", "name": "",
                                  "started_at": time.time(),
                                  "duration_ms": 0})
        self.scene_log.append("sleeping")
        black = Image.new("RGB", self.canvas)
        while not st.stop_requested:
            now = datetime.datetime.now()
            window = _in_sleep_window(self.cfg, now)
            if window != self._prev_window:
                self._prev_window = window
                st.clear_sleep_override()
            ov = st.sleep_override
            if not (ov == "sleep" or (window and ov != "wake")):
                break
            st.take_skip()  # skip is meaningless while sleeping
            try:
                self.driver.show(black)
            except Exception:
                traceback.print_exc()
            self.frames_shown += 1
            self._maybe_snapshot()
            st.tick()
            if self.max_frames is not None \
                    and self.frames_shown >= self.max_frames:
                st.request_stop()
                break
            self._sleep_ms(1000)
        return True

    # -- content picking ---------------------------------------------------
    def _pick_animation(self):
        """-> ('dmd'|'gif', item tuple) or None, per sources + dmd_share."""
        cfg = self.cfg
        sources = cfg.get("playback.sources", {}) or {}
        dmd_ok = bool(sources.get("dmd", True))
        gif_ok = bool(sources.get("gif", True))
        try:
            share = float(cfg.get("playback.dmd_share", 60))
        except (TypeError, ValueError):
            share = 60
        want_dmd = dmd_ok and (not gif_ok or
                               self.rng.uniform(0, 100) < share)
        if want_dmd:
            item = self.library.pick_dmd(self.rng, cfg)
            if item is not None:
                return "dmd", item
        if gif_ok:
            item = self.library.pick_gif(self.rng, cfg)
            if item is not None:
                return "gif", item
        if dmd_ok and not want_dmd:
            item = self.library.pick_dmd(self.rng, cfg)
            if item is not None:
                return "dmd", item
        return None

    def _play_name_card(self, title):
        self.play_scene(scenes.name_scene(self.cfg, title, self.canvas),
                        "animation", log="name")

    def _play_dmd(self, game, name, path, preloaded=None):
        cfg = self.cfg
        show = cfg.get("playback.show_name", "hide")
        title = name.replace("_", " ")
        if show == "before":
            self._play_name_card(title)
        if self.state.stop_requested:
            return
        found = self.library.get_dmd(game, name)
        duration = found[0].get("duration_ms", 0) if found else 0
        np = {"type": "dmd", "game": game, "name": name,
              "started_at": time.time(), "duration_ms": duration}
        header, frames, indexes = (preloaded if preloaded
                                   else (None, None, None))
        self.play_scene(scenes.dmd_scene(cfg, path, header=header,
                                         frames=frames, indexes=indexes,
                                         canvas=self.canvas),
                        "animation", np, log=("dmd", game, name))
        if show == "after" and not self.state.stop_requested:
            self._play_name_card(title)

    def _play_gif(self, category, filename, path, preloaded=None):
        cfg = self.cfg
        show = cfg.get("playback.show_name", "hide")
        title = os.path.splitext(filename)[0].replace("_", " ")
        if show == "before":
            self._play_name_card(title)
        if self.state.stop_requested:
            return
        np = {"type": "gif", "game": category, "name": filename,
              "started_at": time.time(), "duration_ms": 0}
        self.play_scene(scenes.gif_scene(cfg, path, canvas=self.canvas,
                                         frames=preloaded),
                        "animation", np, log=("gif", category, filename))
        if show == "after" and not self.state.stop_requested:
            self._play_name_card(title)

    def _play_queued(self, item):
        kind = item.get("type")
        parts = str(item.get("id", "")).split("/", 1)
        if len(parts) != 2:
            return
        if kind == "dmd":
            found = self.library.get_dmd(parts[0], parts[1])
            if found is not None:
                self._play_dmd(parts[0], parts[1], found[1])
        elif kind == "gif":
            path = self.library.gif_path(parts[0], parts[1])
            if path is not None:
                self._play_gif(parts[0], parts[1], path)

    # -- scene slots ----------------------------------------------------------
    def _play_clock_cycle(self, gap_seconds):
        cfg = self.cfg
        if cfg.get("clock.enabled", True):
            if gap_seconds is not None:
                dwell = int(gap_seconds * 1000)
            else:
                dwell = _safe_int(cfg.get("clock.idle_dwell_ms", 6000), 6000)
            sc = scenes.clock_scene(cfg, self.canvas, dwell,
                                    self._backgrounds, rng=self.rng,
                                    time_fn=self._now)
            sc = transitions.wrap(sc, cfg.get("clock.transition", "random"),
                                  self.rng)
            np = {"type": "clock", "game": "", "name": "clock",
                  "started_at": time.time(), "duration_ms": dwell}
            self.play_scene(sc, "clock", np)
        else:
            dwell = int(gap_seconds * 1000) if gap_seconds is not None \
                else 1000
            self.play_scene(_blank_scene(self.canvas, dwell), "clock",
                            log="blank")

    def _interleaves(self):
        cfg = self.cfg
        st = self.state
        every = _safe_int(cfg.get("date.every_n_cycles", 4) or 0, 4)
        if cfg.get("date.enabled", True) and every > 0 \
                and self._cycle % every == 0:
            np = {"type": "clock", "game": "", "name": "date",
                  "started_at": time.time(),
                  "duration_ms": _safe_int(cfg.get("date.dwell_ms", 2500),
                                           2500)}
            self.play_scene(scenes.date_scene(cfg, self.canvas), "clock",
                            np, log="date")
        if st.stop_requested:
            return
        wdata = self.weather.data() if self.weather is not None else None
        if cfg.get("weather.enabled", False) and wdata is not None \
                and self._now() - self._last_weather >= WEATHER_SHOW_INTERVAL_S:
            self._last_weather = self._now()
            np = {"type": "clock", "game": "", "name": "weather",
                  "started_at": time.time(),
                  "duration_ms": _safe_int(cfg.get("weather.dwell_ms", 3500),
                                           3500)}
            self.play_scene(scenes.weather_scene(cfg, wdata, self.canvas),
                            "clock", np, log="weather")
        if st.stop_requested:
            return
        freq = MESSAGE_FREQ_S.get(cfg.get("message.frequency", "off"))
        if cfg.get("message.enabled", False) and freq \
                and self._now() - self._last_message >= freq:
            self._last_message = self._now()
            np = {"type": "message", "game": "",
                  "name": cfg.get("message.text", ""),
                  "started_at": time.time(), "duration_ms": 0}
            self.play_scene(
                scenes.message_scene(cfg, canvas=self.canvas, rng=self.rng),
                "message", np, log="message")

    def _maybe_show_ip(self):
        if not self.cfg.get("system.show_ip_on_change", True):
            return
        ips = scenes.get_ip_list()
        if not ips:
            # No IP at all: say so on the panel, otherwise the device looks
            # healthy while the web UI is unreachable for no visible reason.
            self.play_scene(scenes.no_network_scene(self.cfg, self.canvas),
                            "clock", log="no_network")
            return
        current = ",".join(ips)
        marker = os.path.join(paths.run_dir(), "last_ip.txt")
        previous = None
        try:
            with open(marker, "r") as f:
                previous = f.read().strip()
        except OSError:
            pass
        if current and current != previous:
            self.play_scene(scenes.ip_scene(self.cfg, self.canvas),
                            "clock", log="ip")
            try:
                with open(marker, "w") as f:
                    f.write(current)
            except OSError:
                pass

    # -- main loop -----------------------------------------------------------
    def run(self):
        st = self.state
        try:
            st.set_counts(self.library.counts(self.cfg))
        except Exception:
            traceback.print_exc()
        try:
            self._maybe_show_ip()
        except Exception:
            traceback.print_exc()
        while not st.stop_requested:
            try:
                self._run_once()
            except Exception:
                # Catch-all: a hand-edited config (SMB) or transient error
                # must never turn the daemon into a systemd crash loop.
                traceback.print_exc()
                if not self.fast:
                    time.sleep(1.0)
                else:
                    self._advance_ms(1000)
                    if self.max_frames is not None \
                            and self.frames_shown >= self.max_frames:
                        st.request_stop()

    def _run_once(self):
        st = self.state
        self._boundary()
        if self._sleep_gate():
            return
        if self._web_gate():
            return
        if st.stop_requested:
            return
        if st.take_test():
            np = {"type": "clock", "game": "", "name": "test_pattern",
                  "started_at": time.time(), "duration_ms": 0}
            self.play_scene(scenes.test_scene(self.cfg, self.canvas),
                            "test", np)
            return
        marquee = st.take_marquee()
        if marquee is not None:
            np = {"type": "message", "game": "", "name": marquee,
                  "started_at": time.time(), "duration_ms": 0}
            self.play_scene(
                scenes.message_scene(self.cfg, text_override=marquee,
                                     canvas=self.canvas, rng=self.rng),
                "message", np, log="marquee")
            return
        queued = st.take_play()
        if queued is not None:
            st.take_skip()  # play sets skip to kill the previous scene
            self._play_queued(queued)
            return

        gap = self.cfg.animation_gap_seconds(self.rng)

        # Choose the next animation BEFORE the clock plays and load it in the
        # background, so clock -> animation is seamless. Reading an .rda or
        # decoding a multi-MB GIF off the SD card otherwise leaves the panel
        # black for seconds after the clock fades out.
        picked = None
        if gap is not None:
            picked = self._pick_animation()
            self.prefetch.start(picked)

        self._play_clock_cycle(gap)
        if st.stop_requested:
            return
        self._cycle += 1
        self._interleaves()
        if st.stop_requested:
            return
        if gap is None:
            return  # animations disabled: clock forever
        queued = st.take_play()
        if queued is not None:
            st.take_skip()
            self._play_queued(queued)
            return
        if picked is None:
            return
        kind, item = picked
        preloaded = self.prefetch.take(picked)
        if kind == "dmd":
            self._play_dmd(item[0], item[1], item[2], preloaded=preloaded)
        else:
            self._play_gif(item[0], item[1], item[2], preloaded=preloaded)
