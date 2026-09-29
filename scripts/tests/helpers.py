"""Fixture builders shared by the plant tooling tests.

Everything is generated in code (PNG, JPEG, USDZ, GLB) so no binary fixtures
are committed. Importing this module also puts scripts/ on sys.path.
"""

from __future__ import annotations

import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_TRNS_PAYLOAD = {0: b"\x00\x00", 2: b"\x00" * 6, 3: b"\x00"}


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def make_png(width: int, height: int, color_type: int = 2, *, trns: bool = False) -> bytes:
    """A real, decodable 8-bit PNG (IHDR + IDAT + IEND) of the given color type."""
    row = b"\x00" + b"\x80" * (width * _CHANNELS[color_type])
    parts = [PNG_SIGNATURE, png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))]
    if color_type == 3:
        parts.append(png_chunk(b"PLTE", b"\x00\x00\x00"))
    if trns:
        parts.append(png_chunk(b"tRNS", _TRNS_PAYLOAD[color_type]))
    parts.append(png_chunk(b"IDAT", zlib.compress(row * height)))
    parts.append(png_chunk(b"IEND", b""))
    return b"".join(parts)


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def make_jpeg(width: int, height: int, sof_marker: int = 0xC0, *, app0: bool = False) -> bytes:
    """A minimal JPEG header: SOI, optional APP0, SOF, EOI (not decodable)."""
    sof = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    body = jpeg_segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00") if app0 else b""
    return b"\xff\xd8" + body + jpeg_segment(sof_marker, sof) + b"\xff\xd9"


# --------------------------------------------------------------------------
# USDZ
# --------------------------------------------------------------------------

def write_usdz(path: Path, entries: list[tuple[str, bytes]] | None = None,
               compression: int = zipfile.ZIP_STORED) -> Path:
    """Write a zip with the given (name, bytes) entries; stored (uncompressed) by default."""
    if entries is None:
        entries = [("model.usdc", b"PXR-USDC")]
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in entries:
            zf.writestr(name, data, compress_type=compression)
    return path


# --------------------------------------------------------------------------
# GLB
# --------------------------------------------------------------------------

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


def pad4(data: bytes, fill: bytes) -> bytes:
    return data + fill * (-len(data) % 4)


def build_glb(doc: dict[str, Any], bin_data: bytes = b"", *, version: int = 2) -> bytes:
    """Assemble a GLB container: JSON chunk (space padded) and optional BIN chunk (zero padded)."""
    payload = pad4(json.dumps(doc).encode("utf-8"), b" ")
    chunks = struct.pack("<II", len(payload), CHUNK_JSON) + payload
    if bin_data:
        padded = pad4(bin_data, b"\x00")
        chunks += struct.pack("<II", len(padded), CHUNK_BIN) + padded
    return struct.pack("<III", GLB_MAGIC, version, 12 + len(chunks)) + chunks


class GlbBuilder:
    """Small helper for writing glTF documents with buffers, meshes and materials."""

    def __init__(self, generator: str = "unit-test") -> None:
        self.doc: dict[str, Any] = {
            "asset": {"version": "2.0", "generator": generator},
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
            "meshes": [],
            "materials": [],
            "textures": [],
            "images": [],
            "accessors": [],
            "bufferViews": [],
        }
        self.bin = bytearray()

    def add_view(self, data: bytes) -> int:
        self.bin += b"\x00" * (-len(self.bin) % 4)
        self.doc["bufferViews"].append({"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)})
        self.bin += data
        return len(self.doc["bufferViews"]) - 1

    def add_accessor(self, data: bytes, component_type: int, type_: str, count: int,
                     min_: list[float] | None = None, max_: list[float] | None = None) -> int:
        accessor: dict[str, Any] = {"bufferView": self.add_view(data), "componentType": component_type,
                                    "type": type_, "count": count}
        if min_ is not None:
            accessor["min"], accessor["max"] = min_, max_
        self.doc["accessors"].append(accessor)
        return len(self.doc["accessors"]) - 1

    def add_positions(self, points: list[tuple[float, float, float]], *, with_bounds: bool = True) -> int:
        data = b"".join(struct.pack("<3f", *p) for p in points)
        lo = [min(p[i] for p in points) for i in range(3)]
        hi = [max(p[i] for p in points) for i in range(3)]
        return self.add_accessor(data, 5126, "VEC3", len(points), lo if with_bounds else None,
                                 hi if with_bounds else None)

    def add_indices(self, indices: list[int]) -> int:
        return self.add_accessor(struct.pack(f"<{len(indices)}H", *indices), 5123, "SCALAR", len(indices))

    def add_vectors(self, values: list[tuple[float, ...]], type_: str, *, component_type: int = 5126) -> int:
        size = len(values[0])
        fmt = "<%df" % size if component_type == 5126 else "<%dH" % size
        return self.add_accessor(b"".join(struct.pack(fmt, *v) for v in values), component_type, type_,
                                 len(values))

    def add_mesh(self, primitives: list[dict[str, Any]]) -> int:
        self.doc["meshes"].append({"primitives": primitives})
        return len(self.doc["meshes"]) - 1

    def add_node(self, *, root: bool = True, **fields: Any) -> int:
        self.doc["nodes"].append(fields)
        index = len(self.doc["nodes"]) - 1
        if root:
            self.doc["scenes"][0]["nodes"].append(index)
        return index

    def add_image(self, data: bytes, mime: str = "image/png", name: str | None = None) -> int:
        image: dict[str, Any] = {"bufferView": self.add_view(data), "mimeType": mime}
        if name:
            image["name"] = name
        self.doc["images"].append(image)
        return len(self.doc["images"]) - 1

    def add_external_image(self, uri: str) -> int:
        self.doc["images"].append({"uri": uri})
        return len(self.doc["images"]) - 1

    def add_texture(self, image: int) -> int:
        self.doc["textures"].append({"source": image})
        return len(self.doc["textures"]) - 1

    def add_material(self, **fields: Any) -> int:
        self.doc["materials"].append(fields)
        return len(self.doc["materials"]) - 1

    def use_extension(self, name: str, *, required: bool = False) -> None:
        self.doc.setdefault("extensionsUsed", []).append(name)
        if required:
            self.doc.setdefault("extensionsRequired", []).append(name)

    def build(self) -> bytes:
        doc = dict(self.doc)
        if self.bin:
            doc["buffers"] = [{"byteLength": len(self.bin)}]
        return build_glb(doc, bytes(self.bin))


def quad_primitive(b: GlbBuilder, *, indexed: bool = True, material: int | None = None,
                   size: tuple[float, float, float] = (1.0, 1.0, 0.0)) -> dict[str, Any]:
    """A primitive for a flat quad: 4 vertices / 2 triangles indexed, or 6 vertices non-indexed."""
    w, h, d = size
    corners = [(0, 0, 0), (w, 0, 0), (w, h, d), (0, h, d)]
    prim: dict[str, Any] = {}
    if indexed:
        prim["attributes"] = {"POSITION": b.add_positions(corners)}
        prim["indices"] = b.add_indices([0, 1, 2, 0, 2, 3])
    else:
        prim["attributes"] = {"POSITION": b.add_positions([corners[i] for i in (0, 1, 2, 0, 2, 3)])}
    if material is not None:
        prim["material"] = material
    return prim
