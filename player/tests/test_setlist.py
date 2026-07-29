"""Verify the no-repeat shuffle-bag picker: every enabled item plays
exactly once per setlist, then a fresh shuffled setlist starts — forever.
The bag survives interleaved picks of the other kind and resets when the
enabled list changes (flags/refresh)."""
import os
import random
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-setlist")
sys.path.insert(0, r"C:\Users\david\Documents\development\RPI2DMD\rpi2dmd-v3\player")

from rpi2dmd import config, library  # noqa

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " | %s" % (extra,)))
    if not cond:
        fails.append(name)


cfg = config.Config()
cfg.data = config.deep_merge(config.DEFAULTS, {})
lib = library.Library()
rng = random.Random(1234)

gifs = lib.enabled_gifs(cfg)
dmds = lib.enabled_dmd(cfg)
check("test library has content", len(gifs) > 1 and len(dmds) > 1,
      (len(gifs), len(dmds)))

# one full setlist covers every enabled gif exactly once
n = len(gifs)
drawn = [lib.pick_gif(rng, cfg)[:2] for _ in range(n)]
check("setlist has no repeats", len(set(drawn)) == n,
      "%d unique of %d" % (len(set(drawn)), n))
check("setlist covers every enabled item", set(drawn) == set(gifs))

# the next setlist starts automatically and covers everything again
drawn2 = [lib.pick_gif(rng, cfg)[:2] for _ in range(n)]
check("next setlist also covers everything", set(drawn2) == set(gifs))

# dmd picks interleaved with gif picks keep their own independent bag
m = len(dmds)
seen_dmd = set()
for i in range(m):
    g, name, _ = lib.pick_dmd(rng, cfg)
    seen_dmd.add((g, name))
    lib.pick_gif(rng, cfg)      # interleave: must not disturb the dmd bag
check("dmd setlist unaffected by interleaved gif picks",
      len(seen_dmd) == m and seen_dmd == set((g, n_) for g, n_, _ in dmds))

# refresh (config reload) starts a fresh, still-complete setlist
lib.refresh()
drawn3 = [lib.pick_gif(rng, cfg)[:2] for _ in range(len(lib.enabled_gifs(cfg)))]
check("post-refresh setlist still complete",
      set(drawn3) == set(lib.enabled_gifs(cfg)))

# a flag change mid-setlist swaps to a bag over the new enabled list
cats = sorted(set(c for c, _ in gifs))
if len(cats) > 1:
    cfg.data["gif"]["categories"] = {cats[0]: False}
    filtered = lib.enabled_gifs(cfg)
    got = [lib.pick_gif(rng, cfg)[:2] for _ in range(len(filtered))]
    check("flag change: setlist over the filtered list only",
          set(got) == set(filtered))
else:
    print("SKIP flag-change case (single category in test media)")

# pick_any: ONE setlist over both kinds at their natural proportions
cfg.data["gif"]["categories"] = {}
all_items = lib.enabled_all(cfg)
check("combined list is dmd+gif", len(all_items) ==
      len(lib.enabled_dmd(cfg)) + len(lib.enabled_gifs(cfg)),
      len(all_items))
combo = [lib.pick_any(rng, cfg) for _ in range(len(all_items))]
keys = set((kind, item[0], item[1]) for kind, item in combo)
want = set(("dmd", g, n_) for g, n_, _ in lib.enabled_dmd(cfg)) | \
       set(("gif", c, f) for c, f in lib.enabled_gifs(cfg))
check("one combined setlist covers the whole library exactly once",
      len(keys) == len(all_items) and keys == want,
      "%d unique of %d" % (len(keys), len(all_items)))
check("both kinds appear in the combined setlist",
      len(set(k[0] for k in keys)) == 2)

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("SETLIST TESTS PASSED")
