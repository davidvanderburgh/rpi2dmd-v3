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
        self.refresh()

    # -- loading ----------------------------------------------------------
    def refresh(self):
        """Re-read index.json (if changed) and rescan the GIF categories."""
        self._load_index()
        self._scan_gifs()
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
        """-> list of (game, name, relative file) tuples."""
        out = []
        for game, anims in self.games().items():
            if not ignore_flags and not self.game_enabled(cfg, game):
                continue
            for entry in anims:
                name = entry.get("name", "")
                if not ignore_flags and not self.anim_enabled(cfg, name):
                    continue
                out.append((game, name, entry.get("file", "")))
        return out

    def enabled_gifs(self, cfg, ignore_flags=False):
        """-> list of (category, filename) tuples."""
        out = []
        for cat, files in self._gifs.items():
            if not ignore_flags and not self.category_enabled(cfg, cat):
                continue
            for f in files:
                out.append((cat, f))
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
    def pick_dmd(self, rng, cfg):
        """-> (game, name, rda_path) among enabled items, or None."""
        items = self.enabled_dmd(cfg, ignore_flags=_show_all(cfg))
        if not items:
            return None
        game, name, rel = rng.choice(items)
        return game, name, os.path.join(paths.dmd_dir(), rel)

    def pick_gif(self, rng, cfg):
        """-> (category, filename, path) among enabled items, or None."""
        items = self.enabled_gifs(cfg, ignore_flags=_show_all(cfg))
        if not items:
            return None
        cat, fname = rng.choice(items)
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
