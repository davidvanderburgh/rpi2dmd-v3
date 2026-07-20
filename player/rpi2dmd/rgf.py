"""RGF v1 — pre-decoded GIF frame cache.

Why this exists: Pillow's GIF plugin costs ~90-230ms PER FRAME on the Pi
Zero (measured; the realtime LED thread leaves ~25% of a cache-starved
ARM11), so a 300-frame clip takes half a minute to decode and ~40% of the
10k-GIF library was unplayable within any reasonable prefetch window. The
desktop converts each heavy GIF once (tools/build_gif_cache.py) into this
container; the Pi then pays one file read up front and ~2 cheap C calls
per frame at render time (zlib.decompress + Image.frombytes — the same
ops the RDA path proves hold 30fps on this hardware).

Layout (little-endian):

    offset  size  field
    0       4     magic b"RGF1"
    4       4     uint32 header length N
    8       N     UTF-8 JSON header
    8+N     ...   num_frames chunks, back to back

Header JSON:
    {"version": 1, "width": W, "height": H, "num_frames": F,
     "durations": [ms, ...],          # F entries, already >= MIN_FRAME_MS
     "chunk_lengths": [bytes, ...],   # F entries
     "src_size": <bytes of the source .gif, staleness guard>}

Chunk i = zlib(768-byte RGB palette + W*H P-mode pixel bytes).

Python 3.7 / Pillow 5.4 compatible; stdlib + PIL only.
"""

import json
import os
import struct
import zlib

MAGIC = b"RGF1"
PALETTE_BYTES = 768


def cache_path(category, filename):
    """Cache location for a library GIF (sidecar tree, .gif kept intact)."""
    from . import paths
    return os.path.join(paths.media_root(), "gif-cache", category,
                        filename + ".rgf")


def write_rgf(path, frames, src_size=0, level=9):
    """frames: iterable of (PIL RGB/P Image, duration_ms). Desktop-side."""
    from PIL import Image

    durations = []
    chunks = []
    w = h = None
    for img, dur in frames:
        if w is None:
            w, h = img.size
        if img.size != (w, h):
            raise ValueError("frame size changed mid-clip")
        if img.mode != "P":
            img = img.convert("P", palette=Image.ADAPTIVE, colors=256)
        pal = img.getpalette() or []
        pal = (pal + [0] * PALETTE_BYTES)[:PALETTE_BYTES]
        raw = bytes(bytearray(pal)) + img.tobytes()
        chunks.append(zlib.compress(raw, level))
        durations.append(int(dur))
    if not chunks:
        raise ValueError("no frames")

    header = json.dumps({
        "version": 1, "width": w, "height": h,
        "num_frames": len(chunks),
        "durations": durations,
        "chunk_lengths": [len(c) for c in chunks],
        "src_size": int(src_size),
    }).encode("utf-8")

    tmp = path + ".tmp"
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", len(header)))
        f.write(header)
        for c in chunks:
            f.write(c)
    os.replace(tmp, path)


class RgfClip(object):
    """Loaded clip; frames stay zlib-compressed in RAM (~file size) and
    decompress lazily per yield. Iterable multiple times; supports len().

    Duck-types the prefetcher's GIF payload: len() feeds the frame budget
    and iteration yields (PIL Image, duration_ms) like a decoded list.
    """

    def __init__(self, path):
        with open(path, "rb") as f:
            blob = f.read()
        if blob[:4] != MAGIC:
            raise ValueError("not an RGF file: %s" % path)
        (hlen,) = struct.unpack("<I", blob[4:8])
        h = json.loads(blob[8:8 + hlen].decode("utf-8"))
        if h.get("version") != 1:
            raise ValueError("unsupported RGF version %r" % h.get("version"))
        self.width = int(h["width"])
        self.height = int(h["height"])
        self.durations = [int(d) for d in h["durations"]]
        self.src_size = int(h.get("src_size", 0))
        lengths = [int(n) for n in h["chunk_lengths"]]
        if len(lengths) != len(self.durations):
            raise ValueError("chunk/duration count mismatch")
        self._offsets = []
        pos = 8 + hlen
        for n in lengths:
            self._offsets.append((pos, n))
            pos += n
        if pos > len(blob):
            raise ValueError("truncated RGF file")
        self._blob = blob
        self.total_ms = sum(self.durations)

    def __len__(self):
        return len(self._offsets)

    def frame(self, i):
        from PIL import Image
        pos, n = self._offsets[i]
        raw = zlib.decompress(self._blob[pos:pos + n])
        img = Image.frombytes(
            "P", (self.width, self.height), raw[PALETTE_BYTES:])
        img.putpalette(bytearray(raw[:PALETTE_BYTES]))
        return img

    def __iter__(self):
        for i in range(len(self._offsets)):
            yield self.frame(i), self.durations[i]
