"""Verify: enabled-content lists are cached (picks were rebuilding a 10k
item list per call on the Pi), and the cache invalidates on flag changes
and refresh()."""
import os
import sys

os.environ.setdefault("RPI2DMD_MEDIA", r"C:\tmp\rpi2dmd-v3-work\testmedia")
os.environ.setdefault("RPI2DMD_RUN", r"C:\tmp\rpi2dmd-v3-work\run-libcache")
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

g1 = lib.enabled_gifs(cfg)
g2 = lib.enabled_gifs(cfg)
check("gif list cached (same object)", g1 is g2)
check("gif list non-empty", len(g1) > 0, len(g1))

d1 = lib.enabled_dmd(cfg)
d2 = lib.enabled_dmd(cfg)
check("dmd list cached (same object)", d1 is d2)

# flag change -> different key -> rebuilt and actually filtered
cats = sorted(set(c for c, _ in g1))
cfg.data["gif"]["categories"] = {cats[0]: False}
g3 = lib.enabled_gifs(cfg)
check("flag change invalidates", g3 is not g1)
check("disabled category filtered out",
      all(c != cats[0] for c, _ in g3), cats[0])

# same flags again -> cache hit for the new key
g4 = lib.enabled_gifs(cfg)
check("new flag state cached too", g4 is g3)

# refresh clears everything
lib.refresh()
g5 = lib.enabled_gifs(cfg)
check("refresh() rebuilds", g5 is not g3)
check("rebuild equals old content", g5 == g3)

# ignore_flags variants are cached separately
a = lib.enabled_gifs(cfg, ignore_flags=True)
b = lib.enabled_gifs(cfg, ignore_flags=False)
check("ignore_flags cached separately", a is not b and len(a) >= len(b))

print()
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("LIBRARY CACHE TESTS PASSED")
