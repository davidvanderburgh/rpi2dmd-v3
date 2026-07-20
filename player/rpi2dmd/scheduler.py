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

from . import paths, rda, rgf, scenes, transitions


PREFETCH_DEPTH = 3          # decoded animations kept ahead of playback
PREFETCH_PACE_EVERY = 8     # frames decoded between voluntary sleeps
PREFETCH_PACE_S = 0.012     # the sleep: caps GIL bursts so the clock's
                            # render thread never waits behind a decode
# One decode worker feeds playback, so a monster GIF (60s+ to decode on the
# Pi's leftover ~25% of a core) stalls every animation behind it and the
# panel shows only clock meanwhile. Bound each attempt; the offender is
# skipped with a journal line naming it. Generous on purpose: pre-scaled
# 128x32 library GIFs decode in ~1-2s.
DECODE_BUDGET_S = 20.0
# Memory ceiling for buffered-ahead frames. A 128x32 RGB frame is ~12KB, so
# 2500 frames ~= 30MB. This is what actually bounds the queue: full-length
# GIFs (up to ~5000 frames) are NOT truncated, they just reduce how many
# animations we buffer ahead of them, so we never hold three huge clips at
# once on a 512MB Pi.
PREFETCH_FRAME_BUDGET = 2500


def _cached_gif(category, filename, gif_path):
    """RgfClip from the pre-decoded sidecar cache, or None.

    One file read instead of ~100ms/frame of Pillow GIF decode on this
    hardware. Guarded by source file size so a replaced .gif never plays
    a stale cache. Used by the prefetcher AND the user-queued play path
    (which never goes through the prefetcher).
    """
    cache = rgf.cache_path(category, filename)
    if not os.path.exists(cache):
        return None
    try:
        clip = rgf.RgfClip(cache)
        if not clip.src_size or clip.src_size == os.path.getsize(gif_path):
            return clip
        sys.stderr.write("prefetch: stale cache ignored for %s/%s\n"
                         % (category, filename))
    except Exception:
        traceback.print_exc()
    return None


def _payload_frames(payload):
    if payload is None:
        return 0
    if isinstance(payload, tuple):      # dmd: (header, frames, indexes)
        return len(payload[1])
    return len(payload)                 # gif: list of (image, duration)


class Prefetcher(object):
    """Decodes the next few animations ahead of playback.

    Two problems this solves on a single-core Pi:
    - Reading/decoding a multi-MB GIF takes seconds, so decoding must happen
      well before the animation is due (a queue, not just the next item —
      one huge GIF followed by small ones is absorbed by the lookahead).
    - The decoder shares the interpreter with the render loop. OS priorities
      do not arbitrate the GIL, so an unthrottled decode makes the clock's
      frame thread wait mid-draw — that is jitter. Decoding is therefore
      paced with voluntary sleeps; each animation takes a little longer to
      load, but the queue hides that and the clock stays on the beat.

    Failed decodes are recorded (payload None) so callers never wait on
    something that will never arrive.
    """

    def __init__(self, canvas, depth=PREFETCH_DEPTH):
        self.canvas = canvas
        self.depth = depth
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._pending = []                       # picks to decode, in order
        self._done = collections.OrderedDict()   # pick -> payload (or None)
        self._thread = None

    # -- producer side -----------------------------------------------------
    def ensure(self, picks):
        """Queue any of `picks` not already decoded or in flight."""
        with self._cv:
            for p in picks:
                if p is not None and p not in self._done \
                        and p not in self._pending:
                    self._pending.append(p)
            if self._pending:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run, name="prefetch", daemon=True)
                    self._thread.start()
                self._cv.notify_all()

    def start(self, pick):
        """Back-compat single-pick entry point."""
        if pick is not None:
            self.ensure([pick])

    def flush(self):
        """Config changed: queued picks may no longer be enabled/valid."""
        with self._cv:
            del self._pending[:]
            self._done.clear()

    # -- consumer side -----------------------------------------------------
    def ready(self, pick):
        """Non-blocking; True also for a failed decode (payload None) so the
        clock never extends waiting for something that cannot arrive."""
        with self._lock:
            return pick in self._done

    def take(self, pick, timeout=1.0):
        """-> payload for `pick` (None = load it yourself), waiting briefly."""
        deadline = time.time() + timeout
        with self._cv:
            while pick not in self._done:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            payload = self._done.pop(pick)
            # drop stale leftovers (skipped items) beyond the lookahead
            while len(self._done) > self.depth * 2:
                self._done.popitem(last=False)
            # buffered frames just dropped: let the worker resume if the
            # budget was holding it back
            self._cv.notify_all()
            return payload

    # -- worker ------------------------------------------------------------
    def _buffered_frames(self):
        return sum(_payload_frames(p) for p in self._done.values())

    def _run(self):
        try:
            os.nice(5)
        except (OSError, AttributeError):
            pass
        while True:
            with self._cv:
                # wait for work, and for the buffered frames to drain below
                # budget (a long clip already decoded holds back the next),
                # but always allow at least one item so we never stall the
                # thing that is due next
                while not self._pending or (
                        self._done and
                        self._buffered_frames() >= PREFETCH_FRAME_BUDGET):
                    self._cv.wait(5.0)
                pick = self._pending.pop(0)
            payload = self._decode(pick)
            with self._cv:
                self._done[pick] = payload
                self._cv.notify_all()

    def _decode(self, pick):
        try:
            kind, item = pick
            if kind == "dmd":
                header, frames = rda.read_rda(item[2])
                indexes = []
                for i, f in enumerate(frames):
                    indexes.append(rda.unpack_frame(f))
                    if i % (PREFETCH_PACE_EVERY * 2) == 0:
                        time.sleep(PREFETCH_PACE_S)
                return (header, frames, indexes)
            clip = _cached_gif(item[0], item[1], item[2])
            if clip is not None:
                return clip
            return scenes.load_gif_frames(
                item[2], self.canvas,
                pace_every=PREFETCH_PACE_EVERY, pace_s=PREFETCH_PACE_S,
                abort_after_s=DECODE_BUDGET_S)
        except scenes.DecodeBudgetExceeded as e:
            sys.stderr.write("prefetch: skipping slow GIF (%s)\n" % (e,))
            return None
        except Exception:
            traceback.print_exc()
            return None


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
        self._upcoming = []       # picks queued ahead of playback
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
    def play_scene(self, scene, state_name, now_playing=None, log=None,
                   on_first_frame=None):
        """Drive one scene to the driver, honoring control flags.
        Exceptions inside the scene are logged and swallowed.

        on_first_frame: called once, right after the first frame is shown.
        The clock cycle uses it to start picking/prefetching the next
        animation *after* the panel has switched — doing that work first
        held the previous scene's dead frame on screen for seconds."""
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
                if on_first_frame is not None:
                    hook, on_first_frame = on_first_frame, None
                    try:
                        hook()
                    except Exception:
                        traceback.print_exc()
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
                # queued picks may reference newly-disabled content
                del self._upcoming[:]
                self.prefetch.flush()
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
        # real wall clock only: --fast paces on simulated time, where a
        # beat-split would sample the real clock and break determinism
        beat_fn = None if self.fast else time.time
        self.play_scene(scenes.dmd_scene(cfg, path, header=header,
                                         frames=frames, indexes=indexes,
                                         canvas=self.canvas,
                                         time_fn=beat_fn),
                        "animation", np, log=("dmd", game, name))
        if show == "after" and not self.state.stop_requested:
            self._play_name_card(title)

    def _play_gif(self, category, filename, path, preloaded=None):
        cfg = self.cfg
        if preloaded is None and not self.fast:
            preloaded = _cached_gif(category, filename, path)
        show = cfg.get("playback.show_name", "hide")
        title = os.path.splitext(filename)[0].replace("_", " ")
        if show == "before":
            self._play_name_card(title)
        if self.state.stop_requested:
            return
        np = {"type": "gif", "game": category, "name": filename,
              "started_at": time.time(), "duration_ms": 0}
        self.play_scene(scenes.gif_scene(cfg, path, canvas=self.canvas,
                                         frames=preloaded,
                                         time_fn=None if self.fast
                                         else time.time),
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
    def _play_clock_cycle(self, gap_seconds, extend_while=None,
                          on_first_frame=None):
        cfg = self.cfg
        if cfg.get("clock.enabled", True):
            if gap_seconds is not None:
                dwell = int(gap_seconds * 1000)
            else:
                dwell = _safe_int(cfg.get("clock.idle_dwell_ms", 6000), 6000)
            sc = scenes.clock_scene(cfg, self.canvas, dwell,
                                    self._backgrounds, rng=self.rng,
                                    time_fn=self._now,
                                    extend_while=extend_while)
            sc = transitions.wrap(sc, cfg.get("clock.transition", "random"),
                                  self.rng)
            np = {"type": "clock", "game": "", "name": "clock",
                  "started_at": time.time(), "duration_ms": dwell}
            self.play_scene(sc, "clock", np, on_first_frame=on_first_frame)
        else:
            dwell = int(gap_seconds * 1000) if gap_seconds is not None \
                else 1000
            self.play_scene(_blank_scene(self.canvas, dwell), "clock",
                            log="blank", on_first_frame=on_first_frame)

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

        # Keep a queue of upcoming animations decoding in the background
        # (depth absorbs one big GIF followed by small ones), so clock ->
        # animation is seamless. If the head of the queue is still decoding
        # when the clock's dwell ends, the clock simply keeps showing (via
        # extend_while) — the panel must never wait on a black screen.
        # Picking + prefetch start only AFTER the first clock frame is on
        # the panel (on_first_frame): building the pick lists and starting
        # the decode before it held the dead last animation frame on
        # screen for seconds.
        picked = None
        extend = None
        kickoff = None
        if gap is not None:
            if self.fast:
                # tests: pick one, decode synchronously below — no threads,
                # no wall-clock waits, fully deterministic
                picked = self._pick_animation()
            else:
                def kickoff():
                    while len(self._upcoming) < self.prefetch.depth:
                        nxt = self._pick_animation()
                        if nxt is None or nxt in self._upcoming:
                            break
                        self._upcoming.append(nxt)
                    if self._upcoming:
                        self.prefetch.ensure(self._upcoming)

                def extend():
                    # extend only while NOTHING queued is ready — a failed
                    # or budget-skipped decode counts as ready (payload
                    # None), so a monster GIF can never pin the clock past
                    # its decode budget
                    ups = self._upcoming
                    return bool(ups) and not any(
                        self.prefetch.ready(p) for p in ups)

        self._play_clock_cycle(gap, extend_while=extend,
                               on_first_frame=kickoff)
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
        if picked is None and not self.fast and self._upcoming:
            # play the first READY item; strict head order would wait on a
            # slow decode while a finished one sits behind it
            for i, p in enumerate(self._upcoming):
                if self.prefetch.ready(p):
                    picked = self._upcoming.pop(i)
                    break
            else:
                picked = self._upcoming.pop(0)
        if picked is None:
            return
        kind, item = picked
        if self.fast:
            preloaded = None            # deterministic sync load in the scene
        else:
            # after the extend gate this is normally instant; the short
            # timeout only covers the extend-cap case
            preloaded = self.prefetch.take(picked, timeout=1.0)
            if kind == "gif" and preloaded is None:
                # failed or budget-skipped decode: loading it synchronously
                # in the scene froze the panel for its whole decode (45s
                # measured). Skip it; the clock plays this slot instead.
                sys.stderr.write("scheduler: skipping %s/%s (no decoded "
                                 "frames)\n" % (item[0], item[1]))
                return
        if kind == "dmd":
            self._play_dmd(item[0], item[1], item[2], preloaded=preloaded)
        else:
            self._play_gif(item[0], item[1], item[2], preloaded=preloaded)
