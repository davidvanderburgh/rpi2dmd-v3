"""Content-pack discovery and extraction for the RPI2DMD v3 builder.

The builder never ships copyrighted content. Users place the source
artifacts they own in an inputs directory; this module identifies each
one and normalizes it into the three things the image build consumes:

  base-image     the RPI2DMD v2 SD image (operating system base +
                 stock media partition) — required
  rundmd-image   a Run-DMD SD dump (e.g. B237) — optional; becomes the
                 DMD animation library via tools/extract_b237.py
  gif-pack       any number of GIF collections — optional; merged into
                 the image's GIF library by ascending priority
                 (higher priority wins on filename collisions)

Recognition is descriptor-driven (builder/packs/*.json) with a content
sniffer fallback, so unknown inputs still work when they look like one
of the kinds above. Descriptor schema (all fields except id/kind
optional):

  {
    "id": "gifs-ultimate10k",
    "title": "ULTIMATE GIFS DLC 10K (RpiTeaM)",
    "kind": "gif-pack",                  # base-image|rundmd-image|gif-pack
    "match": ["*ULTIMATE GIFS DLC_10K*"],  # case-insensitive fnmatch
    "password": "filename_token",        # literal, or the special value
                                         # "filename_token" = last
                                         # whitespace token of the stem
    "priority": 20                       # gif-pack merge order
  }

Python 3.10+; stdlib only.
"""

import fnmatch
import json
import os
import shutil
import zipfile

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
GENERIC_GIF_PRIORITY = 10


class Pack:
    def __init__(self, kind, title, path, priority=10, password=None,
                 pack_id=None):
        self.kind = kind
        self.title = title
        self.path = path
        self.priority = priority
        self.password = password
        self.id = pack_id or title

    def __repr__(self):
        return "<Pack %s %r %s>" % (self.kind, self.title,
                                    os.path.basename(self.path))


def load_descriptors():
    out = []
    if not os.path.isdir(PACKS_DIR):
        return out
    for f in sorted(os.listdir(PACKS_DIR)):
        if f.endswith(".json"):
            # utf-8-sig: tolerate the BOM Windows editors like to add
            with open(os.path.join(PACKS_DIR, f),
                      encoding="utf-8-sig") as fh:
                out.append(json.load(fh))
    return out


def _resolve_password(desc, filename):
    pw = desc.get("password")
    if pw == "filename_token":
        stem = os.path.splitext(filename)[0]
        return stem.split()[-1] if stem.split() else None
    return pw


def _zip_names(path):
    try:
        with zipfile.ZipFile(path) as z:
            return z.namelist()
    except (zipfile.BadZipFile, OSError):
        return None


def _sniff_img(path):
    """.img file -> 'rundmd-image' | 'base-image' | None."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    if head[:3] == b"DGD":
        return "rundmd-image"
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        return "base-image"
    return None


def _dir_gif_root(path):
    """Directory containing categories of GIFs -> the gif root, or None."""
    for cand in (os.path.join(path, "gif"), path):
        if not os.path.isdir(cand):
            continue
        for entry in os.listdir(cand):
            sub = os.path.join(cand, entry)
            if os.path.isdir(sub) and any(
                    f.lower().endswith(".gif")
                    for f in os.listdir(sub)[:200]):
                return cand
    return None


def identify(entry_path):
    """One inputs-dir entry -> Pack or None (unrecognized)."""
    name = os.path.basename(entry_path)
    for desc in load_descriptors():
        for pat in desc.get("match", []):
            if fnmatch.fnmatch(name.lower(), pat.lower()):
                return Pack(desc["kind"], desc.get("title", desc["id"]),
                            entry_path,
                            priority=desc.get("priority", 10),
                            password=_resolve_password(desc, name),
                            pack_id=desc["id"])
    # content sniffing fallback
    if os.path.isdir(entry_path):
        if _dir_gif_root(entry_path):
            return Pack("gif-pack", name, entry_path,
                        priority=GENERIC_GIF_PRIORITY)
        return None
    low = name.lower()
    if low.endswith(".img"):
        kind = _sniff_img(entry_path)
        return Pack(kind, name, entry_path) if kind else None
    if low.endswith(".zip"):
        names = _zip_names(entry_path)
        if names is None:
            return None
        if any(n.lower().endswith(".img") for n in names):
            return Pack("rundmd-image", name, entry_path)
        if any(n.lower().endswith(".gif") for n in names):
            return Pack("gif-pack", name, entry_path,
                        priority=GENERIC_GIF_PRIORITY)
    return None


def scan_inputs(inputs_dir):
    """-> (packs, unrecognized_names)."""
    packs, unknown = [], []
    for name in sorted(os.listdir(inputs_dir)):
        p = os.path.join(inputs_dir, name)
        pack = identify(p)
        if pack is None:
            unknown.append(name)
        elif pack.kind is None:
            unknown.append(name)
        else:
            packs.append(pack)
    return packs, unknown


# ---------------------------------------------------------------------------
# Extraction / normalization
# ---------------------------------------------------------------------------

def extract_image(pack, work_dir):
    """base-image / rundmd-image pack -> path to a .img file."""
    if pack.path.lower().endswith(".img"):
        return pack.path
    out_dir = os.path.join(work_dir, "img-" + _safe(pack.id))
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(pack.path) as z:
        if pack.password:
            z.setpassword(pack.password.encode("utf-8"))
        member = next(n for n in z.namelist()
                      if n.lower().endswith(".img"))
        target = os.path.join(out_dir, os.path.basename(member))
        if not os.path.exists(target):
            print("  extracting %s (%.1f GB) ..."
                  % (os.path.basename(member),
                     z.getinfo(member).file_size / 1e9))
            with z.open(member) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.replace(target + ".tmp", target)
        return target


def extract_gif_pack(pack, work_dir):
    """gif-pack -> directory laid out as <root>/<Category>/*.gif."""
    if os.path.isdir(pack.path):
        return _dir_gif_root(pack.path)
    out_dir = os.path.join(work_dir, "gifs-" + _safe(pack.id))
    done_marker = os.path.join(out_dir, ".extracted")
    if os.path.exists(done_marker):
        return out_dir
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with zipfile.ZipFile(pack.path) as z:
        if pack.password:
            z.setpassword(pack.password.encode("utf-8"))
        for info in z.infolist():
            nm = info.filename
            if not nm.lower().endswith(".gif"):
                continue
            parts = nm.replace("\\", "/").split("/")
            # take Category/file.gif relative to a 'gif' path component
            # when present, else the last two components
            if "gif" in parts[:-2]:
                rel = parts[parts.index("gif") + 1:]
            else:
                rel = parts[-2:]
            if len(rel) != 2 or not rel[0]:
                continue
            cat, fname = rel
            cdir = os.path.join(out_dir, _safe(cat))
            os.makedirs(cdir, exist_ok=True)
            with z.open(info) as src, \
                    open(os.path.join(cdir, fname), "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 256)
            n += 1
    with open(done_marker, "w") as f:
        f.write("%d\n" % n)
    print("  %s: %d gifs" % (pack.title, n))
    return out_dir


def merge_gif_trees(trees, out_dir):
    """[(priority, root)] -> merged out_dir; higher priority wins.
    -> number of files in the merged tree."""
    os.makedirs(out_dir, exist_ok=True)
    for _prio, root in sorted(trees, key=lambda t: t[0]):
        for cat in sorted(os.listdir(root)):
            cdir = os.path.join(root, cat)
            if not os.path.isdir(cdir):
                continue
            odir = os.path.join(out_dir, cat)
            os.makedirs(odir, exist_ok=True)
            for f in os.listdir(cdir):
                if f.lower().endswith(".gif"):
                    shutil.copy2(os.path.join(cdir, f),
                                 os.path.join(odir, f))
    return sum(len([f for f in files if f.lower().endswith(".gif")])
               for _, _, files in os.walk(out_dir))


def _safe(s):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)
