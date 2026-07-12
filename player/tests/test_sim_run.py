"""Runnable test script for the v3 player runtime (no pytest).

Usage (from the player/ directory):
    python tests/test_sim_run.py

Points the player at the test media root via env vars, then exercises
config merge, library flags, scenes, the control server and a fast
scheduler run. Exits non-zero on the first failure.
"""

import json
import os
import random
import sys
import tempfile
import time

TEST_MEDIA = os.environ.setdefault(
    "RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
TEST_RUN = os.environ.setdefault(
    "RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-tests")
os.environ.setdefault("RPI2DMD_CTL_TCP", "127.0.0.1:9078")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from rpi2dmd import clock, config, control, library, matrix, paths, \
    scenes, scheduler, transitions

PASS = []


def ok(name):
    PASS.append(name)
    print("PASS %s" % name)


def make_cfg(overrides=None):
    cfg = config.Config(os.path.join(TEST_MEDIA, "config", "rpi2dmd.json"))
    if overrides:
        cfg.data = config.deep_merge(cfg.data, overrides)
    return cfg


# ---------------------------------------------------------------------------

def test_config_load_merge():
    tmp = os.path.join(tempfile.mkdtemp(), "rpi2dmd.json")
    with open(tmp, "w") as f:
        json.dump({"clock": {"format": "24h"},
                   "custom_section": {"a": 1}}, f)
    cfg = config.Config(tmp)
    assert cfg.get("clock.format") == "24h", "override applied"
    assert cfg.get("clock.style") == "rundmd", "default kept"
    assert cfg.get("panel.cols") == 64, "defaults merged"
    assert cfg.get("custom_section.a") == 1, "unknown keys preserved"
    assert not cfg.changed_on_disk(), "mtime tracked after load"
    time.sleep(0.05)
    os.utime(tmp, None)
    assert cfg.changed_on_disk(), "mtime change detected"
    # dotted set + save round trip
    cfg.set("clock.shade", 9)
    cfg.save()
    assert config.Config(tmp).get("clock.shade") == 9
    ok("config load/merge/mtime/save")


def test_library_flags():
    lib = library.Library()
    cfg = make_cfg()
    counts = lib.counts(cfg)
    assert counts["dmd_animations"] == 6, counts
    assert counts["gif_files"] == 6, counts

    rng = random.Random(7)
    # disable a game: it must never be picked
    cfg2 = make_cfg({"dmd": {"games": {"ATTACK_FROM_MARS": False}}})
    for _ in range(60):
        game, name, path = lib.pick_dmd(rng, cfg2)
        assert game != "ATTACK_FROM_MARS", "disabled game picked"
        assert os.path.isfile(path), path
    assert lib.counts(cfg2)["dmd_enabled"] == 3

    # disabled_animations list
    cfg3 = make_cfg({"dmd": {"disabled_animations": ["24_001", "24_002"]}})
    for _ in range(60):
        _, name, _ = lib.pick_dmd(rng, cfg3)
        assert name not in ("24_001", "24_002"), "disabled anim picked"

    # gif category flags
    cfg4 = make_cfg({"gif": {"categories": {"Logo": False}}})
    for _ in range(60):
        cat, fname, path = lib.pick_gif(rng, cfg4)
        assert cat != "Logo", "disabled category picked"
        assert os.path.isfile(path), path
    assert lib.counts(cfg4)["gif_enabled"] == 4

    # show_all ignores every flag
    cfg5 = make_cfg({"dmd": {"games": {"ATTACK_FROM_MARS": False, "24": False}},
                     "gif": {"categories": {"Logo": False, "Arcade": False}},
                     "playback": {"content_filter": "show_all"}})
    assert lib.pick_dmd(rng, cfg5) is not None, "show_all dmd"
    assert lib.pick_gif(rng, cfg5) is not None, "show_all gif"

    # lookup helpers
    entry, path = lib.get_dmd("ATTACK_FROM_MARS", "ATTACK_FROM_MARS_000")
    assert entry["clock_type"] == "ClockOnTop" and os.path.isfile(path)
    assert lib.get_dmd("NOPE", "NOPE_000") is None
    assert lib.gif_path("Arcade", "ARCADE_1942.gif")
    assert lib.gif_path("Arcade", "missing.gif") is None
    assert len(lib.dmd_index(cfg)) == 3
    assert len(lib.gif_index(cfg)) == 2
    ok("library flags/picks/counts/lookup")


def test_dmd_scene():
    lib = library.Library()
    entry, path = lib.get_dmd("ATTACK_FROM_MARS", "ATTACK_FROM_MARS_001")
    cfg_off = make_cfg({"playback": {"clock_overlay": "off"}})
    frames_off = list(scenes.dmd_scene(cfg_off, path))
    assert len(frames_off) == entry["frames"], \
        "frame count %d != %d" % (len(frames_off), entry["frames"])
    for img, hold in frames_off:
        assert img.size == (128, 32) and img.mode == "RGB"
        assert hold >= 20, "min frame duration"

    cfg_front = make_cfg({"playback": {"clock_overlay": "front"}})
    frames_front = list(scenes.dmd_scene(cfg_front, path))
    assert len(frames_front) == entry["frames"]
    diff = sum(1 for a, b in zip(frames_off, frames_front)
               if a[0].tobytes() != b[0].tobytes())
    assert diff == len(frames_off), \
        "clock front overlay changed only %d/%d frames" % (diff,
                                                           len(frames_off))

    # auto mode on a NoClock animation == off
    entry2, path2 = lib.get_dmd("ATTACK_FROM_MARS", "ATTACK_FROM_MARS_002")
    cfg_auto = make_cfg({"playback": {"clock_overlay": "auto"}})
    f_auto = list(scenes.dmd_scene(cfg_auto, path2))
    f_off = list(scenes.dmd_scene(cfg_off, path2))
    assert all(a[0].tobytes() == b[0].tobytes()
               for a, b in zip(f_auto, f_off)), "NoClock anim got a clock"

    # back mode: clock shows only through black animation pixels
    entry3, path3 = lib.get_dmd("TWILIGHT_ZONE", "TWILIGHT_ZONE_002")
    assert entry3["clock_type"] == "ClockBehind"
    f_back = list(scenes.dmd_scene(cfg_auto, path3))
    f_off3 = list(scenes.dmd_scene(cfg_off, path3))
    changed = 0
    for (a, _), (b, _) in zip(f_back, f_off3):
        pa = a.load()
        pb = b.load()
        for y in range(32):
            for x in range(128):
                if pa[x, y] != pb[x, y]:
                    assert pb[x, y] == (0, 0, 0), \
                        "back clock overwrote a lit pixel at %d,%d" % (x, y)
                    changed += 1
    assert changed > 100, "back overlay never visible (%d px)" % changed
    ok("dmd_scene frame count + overlay front/off/auto/back")


def test_gif_scene():
    cfg = make_cfg()
    path = os.path.join(paths.gif_dir(), "Arcade", "ARCADE_1942.gif")
    frames = list(scenes.gif_scene(cfg, path))
    assert frames, "no frames"
    total = 0
    for img, hold in frames:
        assert img.size == (128, 32) and img.mode == "RGB"
        assert hold >= 20
        total += hold
    assert total >= 1500, "short gif not looped to 1.5s (%dms)" % total

    # synthetic 64x64 gif -> cover-scaled and center-cropped to 128x32
    tmpgif = os.path.join(tempfile.mkdtemp(), "t.gif")
    a = Image.new("RGB", (64, 64), (255, 0, 0))
    b = Image.new("RGB", (64, 64), (0, 0, 255))
    a.save(tmpgif, save_all=True, append_images=[b], duration=100, loop=0)
    frames2 = list(scenes.gif_scene(cfg, tmpgif))
    assert frames2[0][0].size == (128, 32), frames2[0][0].size
    # 2 frames x 100ms = 200ms/pass -> 8 passes to reach 1500ms
    assert len(frames2) == 16, len(frames2)
    # cover-crop of a solid frame stays solid red
    colors = frames2[0][0].getcolors()
    assert colors and len(colors) == 1 and colors[0][1] == (255, 0, 0)

    # clock overlay changes pixels
    cfg_ov = make_cfg({"playback": {"gif_clock_overlay": True}})
    f_plain = list(scenes.gif_scene(cfg, tmpgif))[0][0]
    f_clock = list(scenes.gif_scene(cfg_ov, tmpgif))[0][0]
    assert f_plain.tobytes() != f_clock.tobytes(), "gif clock overlay noop"
    ok("gif_scene sizing/looping/overlay")


def test_message_tokens_and_scene():
    cfg = make_cfg()
    now = __import__("datetime").datetime.now()
    out = scenes.expand_tokens(cfg, "T={time} D={date} C={temp}", now)
    assert "{" not in out and "}" not in out, out
    t, suffix = clock.time_text(cfg["clock"], now)
    assert t in out, "time token wrong: %s" % out
    assert scenes.format_date("%a %b %-d", now).split()[-1] == \
        str(now.day), "%-d handling"

    cfg_m = make_cfg({"message": {
        "enabled": True, "text": "HELLO {time}", "movement": "horizontal",
        "speed": "insane", "clock_position": "behind",
        "text_mode": "enhanced", "position": "middle"}})
    frames = list(scenes.message_scene(cfg_m, rng=random.Random(3)))
    assert len(frames) > 5
    assert all(img.size == (128, 32) for img, _ in frames)
    nonblack = sum(1 for img, _ in frames
                   if img.getbbox() is not None)
    assert nonblack > 0, "message scene rendered nothing"
    # static mode with clock in front also renders
    cfg_s = make_cfg({"message": {"movement": "static", "text": "HI",
                                  "clock_position": "in_front"}})
    frames_s = list(scenes.message_scene(cfg_s, rng=random.Random(3)))
    assert frames_s and frames_s[0][0].getbbox() is not None
    ok("message tokens + message_scene modes")


def test_transitions():
    cfg = make_cfg()
    base = list(scenes.date_scene(cfg))
    for mode in ("up_up", "down_down", "up_down", "down_up", "fade", "none",
                 "random"):
        out = list(transitions.wrap(iter(base), mode, random.Random(5)))
        if mode == "none":
            assert len(out) == len(base), mode
        else:
            assert len(out) == len(base) + 18, \
                "%s: %d frames" % (mode, len(out))
        assert all(img.size == (128, 32) for img, _ in out)
    ok("transitions frame counts")


def test_control_roundtrip():
    lib = library.Library()
    cfg = make_cfg()
    state = control.PlayerState(cfg, lib)
    server = control.ControlServer(state)
    server.start()
    time.sleep(0.3)

    def cmd(**kw):
        return control.send_command(kw, timeout=5.0)

    r = cmd(cmd="status")
    assert r["ok"] and r["state"] == "clock" and "counts" in r, r
    assert cmd(cmd="pause")["ok"] and state.paused
    assert cmd(cmd="resume")["ok"] and not state.paused
    assert cmd(cmd="skip")["ok"] and state.take_skip()
    r = cmd(cmd="play", type="dmd", id="ATTACK_FROM_MARS/ATTACK_FROM_MARS_000")
    assert r["ok"], r
    q = state.take_play()
    assert q == {"type": "dmd",
                 "id": "ATTACK_FROM_MARS/ATTACK_FROM_MARS_000"}, q
    assert state.take_skip(), "play must skip current"
    r = cmd(cmd="play", type="gif", id="Arcade/ARCADE_1942.gif")
    assert r["ok"], r
    state.take_play()
    r = cmd(cmd="play", type="dmd", id="NOPE/NOPE_000")
    assert not r["ok"], "invalid play accepted"
    assert cmd(cmd="marquee", text="HELLO")["ok"]
    assert state.take_marquee() == "HELLO"
    assert cmd(cmd="brightness", percent=33)["ok"]
    assert state.brightness_override == 33
    assert cmd(cmd="test_pattern")["ok"] and state.take_test()
    assert cmd(cmd="sleep")["ok"] and state.sleep_override == "sleep"
    assert cmd(cmd="wake")["ok"] and state.sleep_override == "wake"
    r = cmd(cmd="bogus")
    assert not r["ok"], "unknown command accepted"
    assert cmd(cmd="stop")["ok"] and state.stop_requested
    server.stop()
    server.join(3.0)
    # status.json written and valid
    state.write_status(force=True)
    doc = json.load(open(paths.status_path()))
    for key in ("state", "now_playing", "brightness", "tint", "uptime_s",
                "counts", "version", "updated_at"):
        assert key in doc, key
    ok("control server TCP round-trip + status.json shape")


def test_scheduler_sequence():
    cfg = make_cfg({
        "playback": {"frequency": "1s", "show_name": "hide"},
        "clock": {"idle_dwell_ms": 800, "transition": "none",
                  "background": "none"},
        "date": {"enabled": True, "every_n_cycles": 3, "dwell_ms": 500},
        "message": {"enabled": True, "frequency": "1s", "text": "MSG",
                    "movement": "horizontal", "speed": "insane",
                    "clock_position": "no_clock"},
        "system": {"show_ip_on_change": False},
    })
    driver = matrix.SimDriver(cfg.data,
                              out_dir=os.path.join(TEST_RUN, "sim-out"))
    lib = library.Library()
    state = control.PlayerState(cfg, lib)
    rng = random.Random(42)
    sched = scheduler.Scheduler(cfg, driver, state, lib, rng=rng,
                                fast=True, max_frames=4000)
    sched.run()
    log = sched.scene_log
    kinds = [e if isinstance(e, str) else e[0] for e in log]
    assert "clock" in kinds, kinds
    n_anim = sum(1 for k in kinds if k in ("dmd", "gif"))
    assert n_anim >= 3, "too few animations: %r" % (kinds,)
    assert "date" in kinds, "date scene never shown: %r" % (kinds,)
    assert "message" in kinds, "message never shown: %r" % (kinds,)
    # animations always separated by a clock-side scene
    prev_anim = None
    for i, k in enumerate(kinds):
        if k in ("dmd", "gif"):
            if prev_anim is not None:
                between = kinds[prev_anim + 1:i]
                assert any(b in ("clock", "date", "message", "weather")
                           for b in between), \
                    "back-to-back animations at %d: %r" % (i, kinds)
            prev_anim = i
    assert driver.frame_count >= 4000, driver.frame_count
    # deterministic with the same seed
    driver2 = matrix.SimDriver(cfg.data,
                               out_dir=os.path.join(TEST_RUN, "sim-out"))
    state2 = control.PlayerState(cfg, lib)
    sched2 = scheduler.Scheduler(cfg, driver2, state2, lib,
                                 rng=random.Random(42), fast=True,
                                 max_frames=4000)
    sched2.run()
    seq1 = [e for e in kinds if e in ("dmd", "gif")]
    seq2 = [e if isinstance(e, str) else e[0] for e in sched2.scene_log]
    seq2 = [e for e in seq2 if e in ("dmd", "gif")]
    assert seq1 == seq2, "seeded runs diverged"
    ok("scheduler sequence (freq 1s, seeded)")


def main():
    tests = [
        test_config_load_merge,
        test_library_flags,
        test_dmd_scene,
        test_gif_scene,
        test_message_tokens_and_scene,
        test_transitions,
        test_control_roundtrip,
        test_scheduler_sequence,
    ]
    for t in tests:
        t()
    print("\nALL %d TESTS PASSED" % len(PASS))


if __name__ == "__main__":
    main()
