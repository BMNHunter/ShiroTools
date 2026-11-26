"""Read image dimensions from file headers without decoding pixels.

Lets the texture tools report sizes for images that are not loaded, without
loading them (which costs RAM/VRAM and time). Unknown formats return None.
"""

import os
import struct

_cache = {}  # path -> (mtime, size, dims)


def _png(f):
    head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def _jpeg(f):
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        byte = f.read(1)
        while byte and byte != b"\xff":
            byte = f.read(1)
        while byte == b"\xff":
            byte = f.read(1)
        if not byte:
            return None
        marker = byte[0]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        raw = f.read(2)
        if len(raw) < 2:
            return None
        length = struct.unpack(">H", raw)[0]
        # SOF0..SOF15 carry the frame size, except DHT/JPG/DAC
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            data = f.read(5)
            h, w = struct.unpack(">HH", data[1:5])
            return w, h
        f.seek(length - 2, 1)


def _tga(f):
    head = f.read(18)
    if len(head) < 18 or head[2] not in (1, 2, 3, 9, 10, 11):
        return None
    return struct.unpack("<HH", head[12:16])


def _bmp(f):
    head = f.read(26)
    if head[:2] != b"BM":
        return None
    w, h = struct.unpack("<ii", head[18:26])
    return abs(w), abs(h)


def _exr(f):
    if f.read(4) != b"\x76\x2f\x31\x01":
        return None
    f.read(4)  # version / flags
    while True:
        name = _cstr(f)
        if not name:
            return None
        _type = _cstr(f)
        size = struct.unpack("<i", f.read(4))[0]
        if name == "dataWindow":
            xmin, ymin, xmax, ymax = struct.unpack("<iiii", f.read(16))
            return xmax - xmin + 1, ymax - ymin + 1
        f.seek(size, 1)


def _cstr(f):
    out = bytearray()
    while True:
        c = f.read(1)
        if not c or c == b"\x00":
            return out.decode("latin-1")
        out += c
        if len(out) > 256:
            return ""


def _hdr(f):
    if not f.read(2) == b"#?":
        return None
    for _ in range(64):
        line = f.readline().strip()
        parts = line.split()
        if len(parts) == 4 and parts[0] in (b"-Y", b"+Y") and parts[2] in (b"+X", b"-X"):
            return int(parts[3]), int(parts[1])
    return None


def _tiff(f):
    order = f.read(2)
    if order not in (b"II", b"MM"):
        return None
    e = "<" if order == b"II" else ">"
    if struct.unpack(e + "H", f.read(2))[0] != 42:
        return None
    offset = struct.unpack(e + "I", f.read(4))[0]
    f.seek(offset)
    count = struct.unpack(e + "H", f.read(2))[0]
    w = h = None
    for _ in range(count):
        tag, typ, _n = struct.unpack(e + "HHI", f.read(8))
        raw = f.read(4)
        val = struct.unpack(e + ("H" if typ == 3 else "I"), raw[:2] if typ == 3 else raw)[0]
        if tag == 256:
            w = val
        elif tag == 257:
            h = val
    return (w, h) if w and h else None


def _webp(f):
    head = f.read(30)
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    chunk = head[12:16]
    if chunk == b"VP8X":
        w = 1 + int.from_bytes(head[24:27], "little")
        h = 1 + int.from_bytes(head[27:30], "little")
        return w, h
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", head[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        b = head[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return w, h
    return None


_READERS = {
    ".png": _png, ".jpg": _jpeg, ".jpeg": _jpeg, ".tga": _tga, ".bmp": _bmp,
    ".exr": _exr, ".hdr": _hdr, ".tif": _tiff, ".tiff": _tiff, ".webp": _webp,
}


def read_size(path):
    """(width, height) from the file header, or None. Cached by mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    reader = _READERS.get(os.path.splitext(path)[1].lower())
    dims = None
    if reader is not None:
        try:
            with open(path, "rb") as f:
                dims = reader(f)
        except (OSError, struct.error, ValueError, IndexError):
            dims = None
    if dims is not None:
        dims = (int(dims[0]), int(dims[1]))
    _cache[path] = (st.st_mtime, st.st_size, dims)
    return dims


def is_high_bitdepth(path):
    """True for files Blender loads into a float buffer (EXR, HDR, 16-bit PNG)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".exr", ".hdr"):
        return True
    if ext == ".png":
        try:
            with open(path, "rb") as f:
                head = f.read(25)
            return len(head) == 25 and head[12:16] == b"IHDR" and head[24] == 16
        except OSError:
            return False
    return False


def read_size_bytes(data, ext):
    """Same as read_size but for in-memory data (packed images)."""
    import io
    reader = _READERS.get(ext.lower())
    if reader is None or not data:
        return None
    try:
        dims = reader(io.BytesIO(data))
    except (struct.error, ValueError, IndexError):
        return None
    return (int(dims[0]), int(dims[1])) if dims else None


def clear_cache():
    _cache.clear()
