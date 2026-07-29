"""Content library: RDA animation index + GIF category scan.

The DMD side is driven by /media/usb/dmd/index.json (written by the
converter); the GIF side is a directory scan of /media/usb/gif/<Category>.
Enable/disable flags live in the config document:

- cfg["dmd"]["games"]: game -> bool, missing = enabled
- cfg["dmd"]["disabled_animations"]: list of animation names
- cfg["gif"]["categories"]: category -> bool, missing = enabled
- playback.content_filter == "show_all" makes the pick functions ignore
  all of the above.

Python 3.7 compatible; stdlib only.
"""

import json
import os

from . import paths


def _show_all(cfg):
    return cfg.get("playback.content_filter", "enabled_only") == "show_all"


class Library(object):
    """Cached view of the on-disk content (index.json by mtime, GIF scan
    refreshed on refresh())."""

    def __init__(self):
        self._index = {}
        self._index_mtime = None
        self._gifs = {}
        # Building the enabled-items list walks every animation/GIF (10k+
        # items) in pure Python — ~1-3s per pick on the Pi Zero, which
        # showed as the panel hanging on the last animation frame before
        # every clock. Cache per flag-state; treat results as read-only.
        self._enabled_cache = {}
        # kind -> (items list the bag indexes into, shuffled index bag)
        self._bags = {}
        self.refresh()

    # -- loading ----------------------------------------------------------
    def refresh(self):
        """Re-read index.json (if changed) and rescan the GIF categories."""
        self._load_index()
        self._scan_gifs()
        self._enabled_cache.clear()
        self._bags.clear()
        return self

    def _index_path(self):
        return os.path.join(paths.dmd_dir(), "index.json")

    def _load_index(self):
        path = self._index_path()
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            self._index = {}
            self._index_mtime = None
            return
        if self._index_mtime == mtime and self._index:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._index = json.load(f)
            self._index_mtime = mtime
        except (ValueError, OSError):
            self._index = {}
            self._index_mtime = None
        self._enabled_cache.clear()   # index changed under us

    def _scan_gifs(self):
        root = paths.gif_dir()
        cats = {}
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            entries = []
        for entry in entries:
            sub = os.path.join(root, entry)
            if not os.path.isdir(sub):
                continue
            try:
                files = sorted(f for f in os.listdir(sub)
                               if f.lower().endswith(".gif"))
            except OSError:
                files = []
            if files:
                cats[entry] = files
        self._gifs = cats

    # -- raw views --------------------------------------------------------
    def games(self):
        """dict game -> list of animation entries from index.json."""
        self._load_index()
        return self._index.get("games", {})

    def gif_categories(self):
        """dict category -> sorted list of .gif filenames."""
        return self._gifs

    # -- enable flags -----------------------------------------------------
    def game_enabled(self, cfg, game):
        flags = cfg.get("dmd.games", {}) or {}
        return bool(flags.get(game, True))

    def anim_enabled(self, cfg, name):
        disabled = cfg.get("dmd.disabled_animations", []) or []
        return name not in disabled

    def category_enabled(self, cfg, category):
        flags = cfg.get("gif.categories", {}) or {}
        return bool(flags.get(category, True))

    def enabled_dmd(self, cfg, ignore_flags=False):
        """-> list of (game, name, relative file) tuples. Cached — treat as
        read-only."""
        games = self.games()   # may reload the index and clear the cache
        flags = cfg.get("dmd.games", {}) or {}
        disabled = cfg.get("dmd.disabled_animations", []) or []
        key = ("dmd", ignore_flags, tuple(sorted(flags.items())),
               tuple(sorted(disabled)))
        hit = self._enabled_cache.get(key)
        if hit is not None:
            return hit
        out = []
        for game, anims in games.items():
            if not ignore_flags and not self.game_enabled(cfg, game):
                continue
            for entry in anims:
                name = entry.get("name", "")
                if not ignore_flags and not self.anim_enabled(cfg, name):
                    continue
                out.append((game, name, entry.get("file", "")))
        self._enabled_cache[key] = out
        return out

    def enabled_gifs(self, cfg, ignore_flags=False):
        """-> list of (category, filename) tuples. Cached — treat as
        read-only."""
        flags = cfg.get("gif.categories", {}) or {}
        key = ("gif", ignore_flags, tuple(sorted(flags.items())))
        hit = self._enabled_cache.get(key)
        if hit is not None:
            return hit
        out = []
        for cat, files in self._gifs.items():
            if not ignore_flags and not self.category_enabled(cfg, cat):
                continue
            for f in files:
                out.append((cat, f))
        self._enabled_cache[key] = out
        return out

    def enabled_all(self, cfg, ignore_flags=False):
        """-> combined list over both kinds: ('dmd', game, name, rel) and
        ('gif', category, filename) tuples. Cached — treat as read-only;
        rebuilt whenever either per-kind list rebuilds (identity check),
        so the setlist bag over it resets exactly when they change."""
        dmd = self.enabled_dmd(cfg, ignore_flags)
        gifs = self.enabled_gifs(cfg, ignore_flags)
        key = ("all", ignore_flags)
        hit = self._enabled_cache.get(key)
        if hit is not None and hit[0] is dmd and hit[1] is gifs:
            return hit[2]
        out = [("dmd",) + t for t in dmd] + [("gif",) + t for t in gifs]
        self._enabled_cache[key] = (dmd, gifs, out)
        return out

    # -- status counts ----------------------------------------------------
    def counts(self, cfg):
        total_dmd = sum(len(v) for v in self.games().values())
        total_gif = sum(len(v) for v in self._gifs.values())
        return {
            "dmd_animations": total_dmd,
            "dmd_enabled": len(self.enabled_dmd(cfg)),
            "gif_files": total_gif,
            "gif_enabled": len(self.enabled_gifs(cfg)),
        }

    # -- picking ----------------------------------------------------------
    def _draw(self, kind, items, rng):
        """No-repeat shuffle bag: every item in `items` is drawn exactly
        once, in random order, before any repeats — one random setlist
        after another, forever. The bag is tied to the cached enabled
        list by identity: when flags/index change the cache rebuilds,
        the identity check fails, and a fresh setlist starts (the old
        one may reference items that no longer exist)."""
        held = self._bags.get(kind)
        if held is None or held[0] is not items or not held[1]:
            order = list(range(len(items)))
            rng.shuffle(order)
            held = (items, order)
            self._bags[kind] = held
        return items[held[1].pop()]

    def pick_any(self, rng, cfg):
        """-> ('dmd'|'gif', item tuple) from ONE setlist over the whole
        enabled library: DMD animations and GIF clips interleave at
        their natural proportions, every item exactly once per pass."""
        items = self.enabled_all(cfg, ignore_flags=_show_all(cfg))
        if not items:
            return None
        it = self._draw("all", items, rng)
        if it[0] == "dmd":
            _, game, name, rel = it
            return "dmd", (game, name,
                           os.path.join(paths.dmd_dir(), rel))
        _, cat, fname = it
        return "gif", (cat, fname,
                       os.path.join(paths.gif_dir(), cat, fname))

    def pick_dmd(self, rng, cfg):
        """-> (game, name, rda_path) among enabled items, or None."""
        items = self.enabled_dmd(cfg, ignore_flags=_show_all(cfg))
        if not items:
            return None
        game, name, rel = self._draw("dmd", items, rng)
        return game, name, os.path.join(paths.dmd_dir(), rel)

    def pick_gif(self, rng, cfg):
        """-> (category, filename, path) among enabled items, or None."""
        items = self.enabled_gifs(cfg, ignore_flags=_show_all(cfg))
        if not items:
            return None
        cat, fname = self._draw("gif", items, rng)
        return cat, fname, os.path.join(paths.gif_dir(), cat, fname)

    # -- lookup -----------------------------------------------------------
    def get_dmd(self, game, name):
        """-> (index entry, rda_path) or None."""
        for entry in self.games().get(game, []):
            if entry.get("name") == name:
                rel = entry.get("file", "")
                return entry, os.path.join(paths.dmd_dir(), rel)
        return None

    def gif_path(self, category, filename):
        """-> full path or None if the file is not in the scanned library."""
        if filename in self._gifs.get(category, []):
            return os.path.join(paths.gif_dir(), category, filename)
        return None

    # -- web UI listings ----------------------------------------------------
    def dmd_index(self, cfg):
        """JSON-able game list with per-item enabled flags for the web UI."""
        out = []
        for game in sorted(self.games()):
            genabled = self.game_enabled(cfg, game)
            anims = []
            for entry in self.games()[game]:
                name = entry.get("name", "")
                anims.append({
                    "name": name,
                    "frames": entry.get("frames", 0),
                    "duration_ms": entry.get("duration_ms", 0),
                    "clock_type": entry.get("clock_type", ""),
                    "enabled": genabled and self.anim_enabled(cfg, name),
                })
            out.append({"game": game, "enabled": genabled,
                        "count": len(anims), "animations": anims})
        return out

    def gif_index(self, cfg):
        """JSON-able category list for the web UI."""
        out = []
        for cat in sorted(self._gifs):
            out.append({"category": cat,
                        "enabled": self.category_enabled(cfg, cat),
                        "count": len(self._gifs[cat]),
                        "files": list(self._gifs[cat])})
        return out
