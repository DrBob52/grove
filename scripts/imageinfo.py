"""Tiny PNG/JPEG header readers shared by the plant asset tooling.

Standard library only. These read just enough of an image to report its size
(and whether a PNG can be transparent); they never decode pixels.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOI = b"\xff\xd8"

_PNG_MAX_DIMENSION = 0x7FFFFFFF
_PNG_ALPHA_COLOR_TYPES = frozenset({4, 6})  # grey+alpha, RGBA
# SOF0-SOF15, except DHT (C4), JPG (C8) and DAC (CC), which are not frame headers.
_JPEG_SOF_MARKERS = frozenset(m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC))
_JPEG_STANDALONE_MARKERS = frozenset({0x00, 0x01} | set(range(0xD0, 0xD9)))  # no length field
_JPEG_EOI = 0xD9
_JPEG_SOS = 0xDA


@dataclass(frozen=True)
class ImageInfo:
    """What we can learn about an image from its header alone."""

    kind: str  # "png" or "jpeg"
    width: int
    height: int
    has_alpha: bool  # always False for JPEG


def has_png_signature(data: bytes) -> bool:
    """True if data starts with the 8-byte PNG signature."""
    return data[:8] == PNG_SIGNATURE


def png_has_trns(data: bytes) -> bool:
    """True if a tRNS chunk appears before the pixel data (PNG transparency)."""
    offset = 8
    while offset + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, offset)
        if kind == b"tRNS":
            return True
        if kind in (b"IDAT", b"IEND"):
            return False
        offset += 12 + length  # length + type + payload + CRC
    return False


def png_info(data: bytes) -> ImageInfo | None:
    """Read size and alpha from a PNG's IHDR (plus tRNS); None if unreadable."""
    if not has_png_signature(data) or len(data) < 26:
        return None
    length, kind = struct.unpack_from(">I4s", data, 8)
    if kind != b"IHDR" or length < 13:
        return None
    width, height = struct.unpack_from(">II", data, 16)
    color_type = data[25]
    if not (0 < width <= _PNG_MAX_DIMENSION and 0 < height <= _PNG_MAX_DIMENSION):
        return None
    has_alpha = color_type in _PNG_ALPHA_COLOR_TYPES or png_has_trns(data)
    return ImageInfo("png", width, height, has_alpha)


def jpeg_info(data: bytes) -> ImageInfo | None:
    """Read size from a JPEG by scanning markers for a frame header (SOF)."""
    if data[:2] != JPEG_SOI:
        return None
    size = len(data)
    pos = 2
    while True:
        pos = data.find(b"\xff", pos)
        if pos < 0:
            return None
        while pos < size and data[pos] == 0xFF:  # skip 0xFF fill bytes
            pos += 1
        if pos >= size:
            return None
        marker = data[pos]
        pos += 1
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if marker in (_JPEG_EOI, _JPEG_SOS):  # pixel data or end, and no SOF seen
            return None
        if pos + 2 > size:
            return None
        (segment_length,) = struct.unpack_from(">H", data, pos)
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 7 or pos + 7 > size:
                return None
            height, width = struct.unpack_from(">HH", data, pos + 3)
            if width == 0 or height == 0:
                return None
            return ImageInfo("jpeg", width, height, False)
        pos += segment_length


def read_image_info(data: bytes) -> ImageInfo | None:
    """Sniff PNG or JPEG from the leading bytes and read its header."""
    if has_png_signature(data):
        return png_info(data)
    if data[:2] == JPEG_SOI:
        return jpeg_info(data)
    return None
