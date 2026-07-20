"""RGF v2 — pre-decoded GIF frame cache.

Why this exists: Pillow's GIF plugin costs ~90-230ms PER FRAME on the Pi
Zero (measured; the realtime LED thread leaves ~25% of a cache-starved
ARM11), so a 300-frame clip took half a minute to decode and ~40% of the
10k-GIF library was unplayable within any reasonable prefetch window. The
desktop converts each GIF once (tools/build_gif_cache.py) into this
container; the Pi pays one file read plus a few BULK image operations.

Why v2 (single per-clip palette): v1 stored a palette per frame, which
forced per-frame decompress+frombytes+putpalette+convert at render time —
measured p50 35.8ms/frame on the Pi (each PIL op costs ~10ms there), too
slow for 20-50ms holds and the cause of visible stutter on fast clips.
With one clip-wide palette, materialize() rebuilds the whole clip as a
SINGLE tall image strip (one frombytes + one putpalette + one convert,
all C-speed over the full pixel run) and hands out per-frame crop views:
~2-4s for an 1,800-frame clip in the prefetch worker, and the render
loop receives ready RGB frames exactly like the classic decode pipeline.

Layout (little-endian):

    offset  size  field
    0       4     magic b"RGF2"
    4       4     uint32 header length N
    8       N     UTF-8 JSON header
    8+N     ...   num_frames chunks, back to back

Header JSON:
    {"version": 2, "width": W, "height": H, "num_frames": F,
     "palette": [768 ints, RGB triples for P indexes 0..255],
     "durations": [ms, ...],          # F entries, already >= MIN_FRAME_MS
     "chunk_lengths": [bytes, ...],   # F entries
     "src_size": <bytes of the source .gif, staleness guard>}

Chunk i = zlib(W*H P-mode pixel bytes). (v1 = magic RGF1, no header
palette, chunk = zlib(768-byte palette + pixels); still readable, but
only iterable per-frame — rebuild the cache for bulk materialize.)

Python 3.7 / Pillow 5.4 compatible; stdlib + PIL only.
"""

import json
import os
import struct
import zlib

MAGIC_V1 = b"RGF1"
MAGIC_V2 = b"RGF2"
PALETTE_BYTES = 768

# Bulk materialization cap: a 128x32 RGB frame is ~12KB, so 2500 frames
# ~= 30MB — the same ceiling the prefetch frame budget already enforces.
# Longer clips (3 in the library) stay lazy.
MATERIALIZE_MAX_FRAMES = 2500


def cache_path(category, filename):
    """Cache location for a library GIF (sidecar tree, .gif kept intact)."""
    from . import paths
    return os.path.join(paths.media_root(), "gif-cache", category,
                        filename + ".rgf")


def write_rgf(path, frames, src_size=0, level=9):
    """frames: iterable of (PIL Image, duration_ms). Desktop-side.

    All frames are quantized against ONE clip-wide adaptive palette (built
    from a sampled strip of the clip) so the reader can bulk-convert."""
    from PIL import Image

    frames = [(img, int(dur)) for img, dur in frames]
    if not frames:
        raise ValueError("no frames")
    w, h = frames[0][0].size
    for img, _ in frames:
        if img.size != (w, h):
            raise ValueError("frame size changed mid-clip")

    # Build the shared palette from up to 64 sampled frames stacked into
    # one strip, then remap every frame against it (no dither: pixel art).
    step = max(1, len(frames) // 64)
    sample = [img.convert("RGB") for img, _ in frames[::step]][:64]
    strip = Image.new("RGB", (w, h * len(sample)))
    for i, img in enumerate(sample):
        strip.paste(img, (0, i * h))
    palimg = strip.convert("P", palette=Image.ADAPTIVE, colors=256)
    pal = palimg.getpalette() or []
    pal = (pal + [0] * PALETTE_BYTES)[:PALETTE_BYTES]

    durations = []
    chunks = []
    for img, dur in frames:
        q = img.convert("RGB").quantize(palette=palimg, dither=0)
        chunks.append(zlib.compress(q.tobytes(), level))
        durations.append(dur)

    header = json.dumps({
        "version": 2, "width": w, "height": h,
        "num_frames": len(chunks),
        "palette": pal,
        "durations": durations,
        "chunk_lengths": [len(c) for c in chunks],
        "src_size": int(src_size),
    }).encode("utf-8")

    tmp = path + ".tmp"
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(tmp, "wb") as f:
        f.write(MAGIC_V2)
        f.write(struct.pack("<I", len(header)))
        f.write(header)
        for c in chunks:
            f.write(c)
    os.replace(tmp, path)


class RgfClip(object):
    """Loaded clip; frames stay zlib-compressed in RAM (~file size).

    Two consumption modes:
    - materialize(): v2 clips up to MATERIALIZE_MAX_FRAMES become a list
      of (RGB Image, duration_ms) via bulk C-speed conversion — what the
      prefetch worker hands to the render loop.
    - iteration: lazy per-frame decode (both versions) — fallback for
      very long clips and the user-queued play path. Costs ~36ms/frame
      on the Pi, fine for >=60ms holds only.

    Duck-types the prefetcher's GIF payload: len() feeds the frame budget
    and iteration yields (PIL Image, duration_ms) like a decoded list.
    """

    def __init__(self, path):
        with open(path, "rb") as f:
            blob = f.read()
        magic = blob[:4]
        if magic == MAGIC_V2:
            self.version = 2
        elif magic == MAGIC_V1:
            self.version = 1
        else:
            raise ValueError("not an RGF file: %s" % path)
        (hlen,) = struct.unpack("<I", blob[4:8])
        h = json.loads(blob[8:8 + hlen].decode("utf-8"))
        if h.get("version") != self.version:
            raise ValueError("RGF version mismatch in %s" % path)
        self.width = int(h["width"])
        self.height = int(h["height"])
        self.durations = [int(d) for d in h["durations"]]
        self.src_size = int(h.get("src_size", 0))
        pal = h.get("palette") or []
        self.palette = (list(pal) + [0] * PALETTE_BYTES)[:PALETTE_BYTES] \
            if self.version == 2 else None
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

    def _raw(self, i):
        pos, n = self._offsets[i]
        return zlib.decompress(self._blob[pos:pos + n])

    def frame(self, i):
        from PIL import Image
        raw = self._raw(i)
        if self.version == 2:
            img = Image.frombytes("P", (self.width, self.height), raw)
            img.putpalette(self.palette)
        else:
            img = Image.frombytes(
                "P", (self.width, self.height), raw[PALETTE_BYTES:])
            img.putpalette(bytearray(raw[:PALETTE_BYTES]))
        return img

    def __iter__(self):
        for i in range(len(self._offsets)):
            yield self.frame(i), self.durations[i]

    def materialize(self, pace_every=0, pace_s=0.0):
        """-> list of (RGB Image, duration_ms) via bulk conversion, or
        None when this clip cannot be bulk-converted (v1, or too long).

        pace_every/pace_s: voluntary sleeps between decompress batches so
        the worker never monopolizes the GIL for long.
        """
        if self.version != 2 or len(self) > MATERIALIZE_MAX_FRAMES:
            return None
        import time
        from PIL import Image

        n = len(self)
        parts = []
        for i in range(n):
            parts.append(self._raw(i))
            if pace_every and (i + 1) % pace_every == 0:
                time.sleep(pace_s)
        strip = Image.frombytes("P", (self.width, self.height * n),
                                b"".join(parts))
        del parts
        strip.putpalette(self.palette)
        strip = strip.convert("RGB")   # ONE conversion for the whole clip
        out = []
        for i in range(n):
            out.append((strip.crop((0, i * self.height, self.width,
                                    (i + 1) * self.height)),
                        self.durations[i]))
        return out
