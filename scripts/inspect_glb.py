#!/usr/bin/env python3
"""Inspect GLB files before converting them to USDZ and explain the risks.

Usage: python3 scripts/inspect_glb.py FILE.glb [FILE2.glb ...] [--json]

Prints size, geometry, bounds, materials and textures, then a list of findings
in plain English. With --json, prints a list with one dict per file instead.
Exits 1 if any file could not be read or has an ERROR finding.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import struct
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

from imageinfo import read_image_info

GLB_MAGIC = 0x46546C67  # "glTF"
CHUNK_JSON = 0x4E4F534A  # "JSON"
CHUNK_BIN = 0x004E4942  # "BIN\0"
GLB_HEADER = struct.Struct("<III")
CHUNK_HEADER = struct.Struct("<II")

MAX_TEXTURE_PX = 1024
ORIGIN_TOLERANCE_M = 0.005  # base may sit this far from Y=0 ...
ORIGIN_TOLERANCE_FRACTION = 0.01  # ... plus this fraction of the plant's height
MIPMAP_FACTOR = 4 / 3
FLOAT_COMPONENT_TYPE = 5126
MB = 1_000_000

EXT_INSTANCING = "EXT_mesh_gpu_instancing"
COMPRESSION_EXTENSIONS = (
    "KHR_draco_mesh_compression",
    "EXT_meshopt_compression",
    "KHR_texture_basisu",
    "KHR_mesh_quantization",
)
TEXTURE_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("baseColor", ("pbrMetallicRoughness", "baseColorTexture")),
    ("metallicRoughness", ("pbrMetallicRoughness", "metallicRoughnessTexture")),
    ("normal", ("normalTexture",)),
    ("occlusion", ("occlusionTexture",)),
    ("emissive", ("emissiveTexture",)),
)
SEVERITY_ORDER = {"ERROR": 0, "WARN": 1, "INFO": 2}

Matrix = list[float]  # 4x4, column-major like glTF
IDENTITY: Matrix = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
Box = tuple[list[float], list[float]]


class GlbError(Exception):
    """Input that is not a usable GLB version 2 file."""


# --------------------------------------------------------------------------
# GLB container and glTF reference helpers
# --------------------------------------------------------------------------

def parse_glb(data: bytes) -> tuple[dict[str, Any], bytes | None]:
    """Split a GLB v2 container into its glTF JSON document and BIN chunk."""
    if len(data) < 4 or struct.unpack_from("<I", data)[0] != GLB_MAGIC:
        hint = " If this is a .gltf text file, export it as .glb instead." if data[:1] == b"{" else ""
        raise GlbError("not a GLB file (it does not start with the 'glTF' marker)." + hint)
    if len(data) < GLB_HEADER.size:
        raise GlbError("not a valid GLB file (too short to contain a GLB header)")
    _magic, version, length = GLB_HEADER.unpack_from(data)
    if version != 2:
        raise GlbError(f"unsupported GLB version {version} (only version 2 is supported)")
    if length < GLB_HEADER.size or length > len(data):
        raise GlbError(f"file is truncated or corrupt (header says {length} bytes, file has {len(data)})")

    json_bytes: bytes | None = None
    bin_bytes: bytes | None = None
    offset = GLB_HEADER.size
    while offset + CHUNK_HEADER.size <= length:
        chunk_length, chunk_type = CHUNK_HEADER.unpack_from(data, offset)
        start = offset + CHUNK_HEADER.size
        stop = start + chunk_length
        if stop > length:
            raise GlbError("a chunk runs past the end of the file (file is truncated or corrupt)")
        if chunk_type == CHUNK_JSON and json_bytes is None:
            json_bytes = data[start:stop]
        elif chunk_type == CHUNK_BIN and bin_bytes is None:
            bin_bytes = data[start:stop]
        offset = stop
    if json_bytes is None:
        raise GlbError("no JSON chunk found, so this is not a valid GLB file")
    try:
        doc = json.loads(json_bytes.decode("utf-8").rstrip("\x00 \t\r\n"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GlbError(f"the JSON chunk is not valid: {exc}") from exc
    if not isinstance(doc, dict):
        raise GlbError("the JSON chunk is not a glTF object")
    return doc, bin_bytes


def ref(doc: dict[str, Any], key: str, index: Any) -> dict[str, Any]:
    """Look up doc[key][index] as an object; a bad reference is a GlbError."""
    items = doc.get(key)
    if (not isinstance(items, list) or isinstance(index, bool) or not isinstance(index, int)
            or not 0 <= index < len(items)):
        raise GlbError(f"broken reference: {key}[{index}] does not exist")
    item = items[index]
    if not isinstance(item, dict):
        raise GlbError(f"{key}[{index}] is not an object")
    return item


def accessor_count(doc: dict[str, Any], index: Any) -> int:
    count = ref(doc, "accessors", index).get("count", 0)
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def str_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def numbers(value: Any, length: int, what: str) -> list[float]:
    """A list of exactly `length` numbers, or a GlbError naming `what`."""
    if (not isinstance(value, list) or len(value) != length
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value)):
        raise GlbError(f"node {what} must be a list of {length} numbers")
    return [float(v) for v in value]


def buffer_slice(doc: dict[str, Any], bin_data: bytes | None, view: dict[str, Any]) -> bytes | None:
    """Bytes of a bufferView stored in the GLB's BIN chunk, else None."""
    if bin_data is None or view.get("buffer") != 0:
        return None
    buffers = doc.get("buffers")
    if isinstance(buffers, list) and buffers and isinstance(buffers[0], dict) and "uri" in buffers[0]:
        return None  # buffer 0 lives in a separate file
    start, length = view.get("byteOffset", 0), view.get("byteLength", 0)
    if (not isinstance(start, int) or not isinstance(length, int) or start < 0 or length < 0
            or start + length > len(bin_data)):
        return None
    return bin_data[start:start + length]


# --------------------------------------------------------------------------
# Matrices and bounds
# --------------------------------------------------------------------------

def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    """a * b for column-major 4x4 matrices."""
    return [sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4)) for col in range(4) for row in range(4)]


def trs_matrix(t: Iterable[float], r: Iterable[float], s: Iterable[float]) -> Matrix:
    """Translation * rotation (quaternion x,y,z,w) * scale as a column-major matrix."""
    tx, ty, tz = t
    sx, sy, sz = s
    x, y, z, w = r
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = (x / norm, y / norm, z / norm, w / norm) if norm else (0.0, 0.0, 0.0, 1.0)
    return [
        (1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0.0,
        (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0.0,
        (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0.0,
        tx, ty, tz, 1.0,
    ]


def local_matrix(node: dict[str, Any]) -> Matrix:
    """A node's own transform: `matrix`, else translation/rotation/scale."""
    if "matrix" in node:
        return numbers(node["matrix"], 16, "matrix")
    return trs_matrix(
        numbers(node.get("translation", [0, 0, 0]), 3, "translation"),
        numbers(node.get("rotation", [0, 0, 0, 1]), 4, "rotation"),
        numbers(node.get("scale", [1, 1, 1]), 3, "scale"),
    )


def transform_box(m: Matrix, lo: list[float], hi: list[float]) -> Box:
    """World AABB of a box under an affine matrix (same as transforming its 8 corners)."""
    out_lo, out_hi = [], []
    for row in range(3):
        low = high = m[12 + row]
        for k in range(3):
            a, b = m[k * 4 + row] * lo[k], m[k * 4 + row] * hi[k]
            low += min(a, b)
            high += max(a, b)
        out_lo.append(low)
        out_hi.append(high)
    return out_lo, out_hi


def scene_roots(doc: dict[str, Any]) -> list[int]:
    """Root nodes of the default scene, or every parentless node if there are no scenes."""
    scenes = doc.get("scenes")
    if isinstance(scenes, list) and scenes:
        scene = ref(doc, "scenes", doc.get("scene", 0))
        roots = scene.get("nodes", [])
        return list(roots) if isinstance(roots, list) else []
    nodes = doc.get("nodes")
    nodes = nodes if isinstance(nodes, list) else []
    children = {c for n in nodes if isinstance(n, dict) for c in n.get("children", [])}
    return [i for i in range(len(nodes)) if i not in children]


def walk_scene(doc: dict[str, Any]) -> list[tuple[int, Matrix]]:
    """Every node reachable from the default scene, with its world matrix."""
    visits: list[tuple[int, Matrix]] = []
    seen: set[int] = set()
    stack: list[tuple[int, Matrix]] = [(root, IDENTITY) for root in reversed(scene_roots(doc))]
    while stack:
        index, parent = stack.pop()
        node = ref(doc, "nodes", index)
        if index in seen:
            raise GlbError(f"node {index} is reachable twice (loop or shared node in the hierarchy)")
        seen.add(index)
        world = mat_mul(parent, local_matrix(node))
        visits.append((index, world))
        children = node.get("children", [])
        stack.extend((child, world) for child in reversed(children if isinstance(children, list) else []))
    return visits


def instancing_attributes(node: dict[str, Any]) -> dict[str, Any] | None:
    """The EXT_mesh_gpu_instancing attributes on a node, or None if not instanced."""
    extensions = node.get("extensions")
    ext = extensions.get(EXT_INSTANCING) if isinstance(extensions, dict) else None
    if not isinstance(ext, dict):
        return None
    attributes = ext.get("attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def instance_count(doc: dict[str, Any], attributes: dict[str, Any] | None) -> int:
    """Instances drawn for a node: the count of any of its instancing accessors."""
    for index in (attributes or {}).values():
        return accessor_count(doc, index)
    return 1


def read_float_vectors(doc: dict[str, Any], bin_data: bytes | None, index: Any,
                       components: int) -> list[tuple[float, ...]] | None:
    """Read a plain float VECn accessor from the BIN chunk; None if not simple."""
    acc = ref(doc, "accessors", index)
    if (acc.get("componentType") != FLOAT_COMPONENT_TYPE or acc.get("sparse") or acc.get("normalized")
            or acc.get("type") != f"VEC{components}" or acc.get("bufferView") is None):
        return None
    view = ref(doc, "bufferViews", acc["bufferView"])
    data = buffer_slice(doc, bin_data, view)
    if data is None:
        return None
    stride = view.get("byteStride") or components * 4
    base, count = acc.get("byteOffset", 0), acc.get("count", 0)
    if (not isinstance(base, int) or not isinstance(count, int) or not isinstance(stride, int)
            or base < 0 or count < 0 or (count and base + (count - 1) * stride + components * 4 > len(data))):
        return None
    fmt = struct.Struct(f"<{components}f")
    return [fmt.unpack_from(data, base + i * stride) for i in range(count)]


def instance_matrices(doc: dict[str, Any], bin_data: bytes | None,
                      attributes: dict[str, Any]) -> Iterator[Matrix] | None:
    """Per-instance local matrices, or None if the transforms can't be read simply."""
    count = instance_count(doc, attributes)
    vectors: dict[str, list[tuple[float, ...]]] = {}
    for name, size in (("TRANSLATION", 3), ("ROTATION", 4), ("SCALE", 3)):
        if name in attributes:
            values = read_float_vectors(doc, bin_data, attributes[name], size)
            if values is None or len(values) != count:
                return None
            vectors[name] = values
    if not vectors:
        return iter([IDENTITY])
    return (
        trs_matrix(
            vectors["TRANSLATION"][i] if "TRANSLATION" in vectors else (0.0, 0.0, 0.0),
            vectors["ROTATION"][i] if "ROTATION" in vectors else (0.0, 0.0, 0.0, 1.0),
            vectors["SCALE"][i] if "SCALE" in vectors else (1.0, 1.0, 1.0),
        )
        for i in range(count)
    )


def primitive_boxes(doc: dict[str, Any], mesh: dict[str, Any]) -> tuple[list[Box], int]:
    """Local POSITION boxes of a mesh's primitives, and how many had no min/max."""
    boxes: list[Box] = []
    missing = 0
    for prim in mesh.get("primitives", []):
        index = prim.get("attributes", {}).get("POSITION")
        acc = ref(doc, "accessors", index) if index is not None else {}
        lo, hi = acc.get("min"), acc.get("max")
        if isinstance(lo, list) and isinstance(hi, list) and len(lo) >= 3 and len(hi) >= 3:
            boxes.append((numbers(lo[:3], 3, "min"), numbers(hi[:3], 3, "max")))
        else:
            missing += 1
    return boxes, missing


def rounded(value: float) -> float:
    """Round to micrometers and turn -0.0 into 0.0."""
    return round(value, 6) + 0.0


def compute_bounds(doc: dict[str, Any], bin_data: bytes | None,
                   visits: list[tuple[int, Matrix]]) -> dict[str, Any] | None:
    """World-space AABB of everything in the scene, or None if nothing has POSITION min/max."""
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    missing = 0
    excluded_nodes = 0
    for index, world in visits:
        node = doc["nodes"][index]
        if node.get("mesh") is None:
            continue
        boxes, no_minmax = primitive_boxes(doc, ref(doc, "meshes", node["mesh"]))
        missing += no_minmax
        matrices: Iterable[Matrix] = [world]
        attributes = instancing_attributes(node)
        if attributes:
            local = instance_matrices(doc, bin_data, attributes)
            if local is None:
                excluded_nodes += 1
            else:
                matrices = (mat_mul(world, m) for m in local)
        for matrix in matrices:
            for box_lo, box_hi in boxes:
                new_lo, new_hi = transform_box(matrix, box_lo, box_hi)
                lo = [min(a, b) for a, b in zip(lo, new_lo)]
                hi = [max(a, b) for a, b in zip(hi, new_hi)]
    if math.inf in lo:
        return None
    notes = []
    if missing:
        notes.append(f"{missing} mesh primitive(s) have no POSITION min/max and are not included.")
    if excluded_nodes:
        notes.append(f"Bounds exclude {EXT_INSTANCING} instances on {excluded_nodes} node(s) "
                     "because their transforms could not be read.")
    return {
        "min": [rounded(v) for v in lo],
        "max": [rounded(v) for v in hi],
        "widthMeters": rounded(hi[0] - lo[0]),
        "heightMeters": rounded(hi[1] - lo[1]),
        "depthMeters": rounded(hi[2] - lo[2]),
        "minY": rounded(lo[1]),
        "excludesInstances": excluded_nodes > 0,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Geometry, images, materials
# --------------------------------------------------------------------------

def primitive_counts(doc: dict[str, Any], prim: dict[str, Any]) -> tuple[int, int]:
    """(triangles, vertices) for one primitive."""
    position = prim.get("attributes", {}).get("POSITION")
    vertices = accessor_count(doc, position) if position is not None else 0
    n = accessor_count(doc, prim["indices"]) if prim.get("indices") is not None else vertices
    mode = prim.get("mode", 4)
    if mode == 4:
        return n // 3, vertices
    if mode in (5, 6):
        return max(n - 2, 0), vertices
    return 0, vertices


def count_geometry(doc: dict[str, Any], visits: list[tuple[int, Matrix]]) -> dict[str, int]:
    """Triangle and vertex totals over node instances in the scene."""
    triangles = vertices = mesh_nodes = 0
    for index, _world in visits:
        node = doc["nodes"][index]
        if node.get("mesh") is None:
            continue
        mesh_nodes += 1
        attributes = instancing_attributes(node)
        copies = instance_count(doc, attributes) if attributes is not None else 1
        for prim in ref(doc, "meshes", node["mesh"]).get("primitives", []):
            tri, vert = primitive_counts(doc, prim)
            triangles += tri * copies
            vertices += vert * copies
    return {
        "meshCount": len(doc.get("meshes", [])),
        "nodeCount": len(doc.get("nodes", [])),
        "meshNodeInstances": mesh_nodes,
        "triangleCount": triangles,
        "vertexCount": vertices,
    }


def decode_data_uri(uri: str) -> bytes | None:
    """Bytes of a data: URI, or None if it is malformed."""
    header, _, payload = uri[5:].partition(",")
    if ";base64" in header.lower():
        try:
            return base64.b64decode(payload)
        except (binascii.Error, ValueError):
            return None
    return unquote_to_bytes(payload)


def describe_image(doc: dict[str, Any], bin_data: bytes | None, index: int,
                   image: dict[str, Any]) -> dict[str, Any]:
    """Facts about one glTF image: where its bytes live, size, alpha."""
    uri = image.get("uri")
    data: bytes | None = None
    if isinstance(uri, str) and uri[:5].lower() == "data:":
        source, data = "data-uri", decode_data_uri(uri)
    elif isinstance(uri, str):
        source = "external"
    elif image.get("bufferView") is not None:
        source, data = "embedded", buffer_slice(doc, bin_data, ref(doc, "bufferViews", image["bufferView"]))
    else:
        source = "missing"
    info = read_image_info(data) if data is not None else None
    mime = image.get("mimeType")
    if mime is None and info is not None:
        mime = f"image/{info.kind}"
    return {
        "index": index,
        "name": image.get("name"),
        "mimeType": mime,
        "source": source,
        "uri": uri if source == "external" else None,
        "width": info.width if info else None,
        "height": info.height if info else None,
        "hasAlpha": info.has_alpha if info and info.kind == "png" else (False if info else None),
        "bytes": len(data) if data is not None else None,
    }


def texture_image(doc: dict[str, Any], texture_index: Any) -> int | None:
    """Image index a texture points at (also via extensions like KHR_texture_basisu)."""
    texture = ref(doc, "textures", texture_index)
    if texture.get("source") is not None:
        return texture["source"]
    extensions = texture.get("extensions")
    for ext in (extensions.values() if isinstance(extensions, dict) else []):
        if isinstance(ext, dict) and ext.get("source") is not None:
            return ext["source"]
    return None


def describe_material(doc: dict[str, Any], index: int, material: dict[str, Any]) -> dict[str, Any]:
    """Alpha mode, sidedness and texture slots of one material."""
    mode = material.get("alphaMode", "OPAQUE")
    slots: dict[str, dict[str, Any]] = {}
    for slot, path in TEXTURE_SLOTS:
        node: Any = material
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get("index") is not None:
            slots[slot] = {"texture": node["index"], "image": texture_image(doc, node["index"])}
    return {
        "index": index,
        "name": material.get("name"),
        "alphaMode": mode,
        "alphaCutoff": material.get("alphaCutoff", 0.5) if mode == "MASK" else None,
        "doubleSided": bool(material.get("doubleSided", False)),
        "textures": slots,
    }


def texture_memory(images: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate uncompressed GPU memory: width * height * 4 bytes per image."""
    measured = [i for i in images if i["width"] is not None]
    total = sum(i["width"] * i["height"] * 4 for i in measured)
    return {
        "bytes": total,
        "megabytes": round(total / MB, 2),
        "withMipmapsMegabytes": round(total * MIPMAP_FACTOR / MB, 2),
        "measuredImages": len(measured),
        "unmeasuredImages": len(images) - len(measured),
    }


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

def finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def image_label(image: dict[str, Any]) -> str:
    name = f" '{image['name']}'" if image["name"] else ""
    return f"Image #{image['index']}{name}"


def material_label(material: dict[str, Any]) -> str:
    name = f" '{material['name']}'" if material["name"] else ""
    return f"Material #{material['index']}{name}"


def image_findings(images: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for img in images:
        if img["source"] == "external":
            out.append(finding("external-texture", "ERROR",
                f"{image_label(img)} is loaded from a separate file ('{img['uri']}') instead of being "
                "stored inside the GLB. Converters usually can't find it, so the plant would come out "
                "untextured. Re-export as a single GLB with the textures embedded."))
        if img["width"] and max(img["width"], img["height"]) > MAX_TEXTURE_PX:
            out.append(finding("texture-over-1k", "WARN",
                f"{image_label(img)} is {img['width']}x{img['height']} pixels. The limit is "
                f"{MAX_TEXTURE_PX} pixels per side; bigger textures bloat the app and use lots of memory. "
                f"Re-export with textures at {MAX_TEXTURE_PX}x{MAX_TEXTURE_PX} or smaller."))
    return out


def material_findings(materials: list[dict[str, Any]], images: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for mat in materials:
        label, mode = material_label(mat), mat["alphaMode"]
        base = mat["textures"].get("baseColor")
        base_image = images[base["image"]] if base and isinstance(base["image"], int) \
            and 0 <= base["image"] < len(images) else None
        if mode == "OPAQUE" and base_image and base_image["hasAlpha"]:
            out.append(finding("alpha-texture-on-opaque", "WARN",
                f"{label} has a color texture with transparent pixels, but its alpha mode is OPAQUE, so "
                "the transparency is ignored and leaves will render as solid rectangles. Set the alpha "
                "mode to MASK (alpha cutout) when exporting."))
        if mode == "BLEND":
            out.append(finding("blend-alpha", "WARN",
                f"{label} uses alpha mode BLEND. Blended transparency often shows sorting errors or "
                "flicker after conversion (leaves drawing in front of or behind each other wrongly). "
                "Alpha cutout (MASK) is the usual fix."))
        if mode in ("MASK", "BLEND") and not mat["doubleSided"]:
            out.append(finding("single-sided-cutout", "WARN",
                f"{label} uses transparency ({mode}) but is single-sided, so the back of each leaf "
                "disappears when you orbit around the plant. Turn on double-sided for this material."))
        if mode == "MASK":
            out.append(finding("alpha-cutout", "INFO",
                f"{label} uses alpha cutout (MASK, cutoff {mat['alphaCutoff']}). After converting, open "
                "the plant in the app and check that the leaves show gaps around their edges rather "
                "than solid rectangles."))
    return out


def extension_findings(doc: dict[str, Any], used: list[str], required: list[str]) -> list[dict[str, str]]:
    out = []
    declared = set(used) | set(required)
    nodes = doc.get("nodes", [])
    if EXT_INSTANCING in declared or any(isinstance(n, dict) and instancing_attributes(n) is not None
                                         for n in nodes):
        out.append(finding("gpu-instancing", "WARN",
            f"This file uses {EXT_INSTANCING}. That is FABOTANIC's lightweight GLB: each leaf is stored "
            "once and repeated by the GPU. Many converters do not understand it and may drop the "
            "instanced leaves, leaving a bare stem. Re-export from FABOTANIC as a Normal GLB "
            "(compatibility) before converting."))
    compressed = [e for e in COMPRESSION_EXTENSIONS if e in declared]
    if compressed:
        out.append(finding("compressed-geometry", "WARN",
            f"This file uses {', '.join(compressed)}. Compressed or quantized data needs special support, "
            "and many converters cannot read it, so the conversion may fail or come out empty. Re-export "
            "without compression if the conversion fails."))
    return out


def bounds_findings(bounds: dict[str, Any] | None) -> list[dict[str, str]]:
    if bounds is None:
        return []
    height, min_y = bounds["heightMeters"], bounds["minY"]
    if abs(min_y) > ORIGIN_TOLERANCE_M + ORIGIN_TOLERANCE_FRACTION * height:
        return [finding("base-not-at-origin", "WARN",
            f"The lowest point of the plant is at Y = {min_y:.3f} m (the plant is {height:.3f} m tall). "
            "The base should sit at Y = 0, otherwise the plant will float above or sink below the ground "
            "when placed. Move the base to the origin before converting.")]
    return []


def memory_finding(memory: dict[str, Any]) -> dict[str, str]:
    if memory["measuredImages"] == 0 and memory["unmeasuredImages"] == 0:
        return finding("texture-memory", "INFO", "This file has no textures (0.0 MB of texture memory).")
    text = (f"Textures will need about {memory['megabytes']:.2f} MB of memory once loaded (width x height "
            f"x 4 bytes per image). Mipmaps add about a third, so budget roughly "
            f"{memory['withMipmapsMegabytes']:.2f} MB.")
    if memory["unmeasuredImages"]:
        text += f" {memory['unmeasuredImages']} image(s) could not be measured and are not counted."
    return finding("texture-memory", "INFO", text)


def sort_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(findings, key=lambda f: SEVERITY_ORDER[f["severity"]])


# --------------------------------------------------------------------------
# Inspection and output
# --------------------------------------------------------------------------

def analyze(doc: dict[str, Any], bin_data: bytes | None, path: str, size: int) -> dict[str, Any]:
    """Build the full report dict for one parsed GLB."""
    asset = doc.get("asset") if isinstance(doc.get("asset"), dict) else {}
    used, required = str_list(doc.get("extensionsUsed")), str_list(doc.get("extensionsRequired"))
    visits = walk_scene(doc)
    bounds = compute_bounds(doc, bin_data, visits)
    images = [describe_image(doc, bin_data, i, ref(doc, "images", i)) for i in range(len(doc.get("images", [])))]
    materials = [describe_material(doc, i, ref(doc, "materials", i)) for i in range(len(doc.get("materials", [])))]
    memory = texture_memory(images)
    findings = (image_findings(images) + extension_findings(doc, used, required)
                + material_findings(materials, images) + bounds_findings(bounds) + [memory_finding(memory)])
    return {
        "path": path,
        "file": {"sizeBytes": size, "generator": asset.get("generator"), "assetVersion": asset.get("version")},
        "extensions": {"used": used, "required": required},
        "geometry": count_geometry(doc, visits),
        "bounds": bounds,
        "materials": materials,
        "images": images,
        "textureMemory": memory,
        "findings": sort_findings(findings),
    }


def inspect_file(path: Path) -> dict[str, Any]:
    """Read and analyze one GLB file; raises GlbError with a friendly message."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GlbError(f"cannot read file: {exc.strerror or exc}") from exc
    doc, bin_data = parse_glb(data)
    try:
        return analyze(doc, bin_data, str(path), len(data))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, struct.error) as exc:
        raise GlbError(f"the glTF data is malformed ({type(exc).__name__}: {exc})") from exc


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "unknown size"
    if n >= MB:
        return f"{n / MB:.2f} MB"
    return f"{n / 1000:.1f} KB" if n >= 1000 else f"{n} bytes"


def section(title: str, rows: list[str]) -> list[str]:
    return ["", title, "-" * len(title)] + [f"  {row}" for row in rows]


def describe_image_line(img: dict[str, Any]) -> str:
    size = f"{img['width']}x{img['height']}" if img["width"] else "size unknown"
    alpha = {True: "alpha: yes", False: "alpha: no", None: "alpha: unknown"}[img["hasAlpha"]]
    where = {"embedded": "", "data-uri": ", data URI", "external": f", EXTERNAL file '{img['uri']}'",
             "missing": ", no image data"}[img["source"]]
    name = f" {img['name']!r}" if img["name"] else ""
    return f"#{img['index']}{name}  {img['mimeType'] or 'unknown type'}  {size}  {alpha}  {fmt_bytes(img['bytes'])}{where}"


def describe_material_line(mat: dict[str, Any], images: list[dict[str, Any]]) -> list[str]:
    name = f" {mat['name']!r}" if mat["name"] else ""
    mode = mat["alphaMode"] + (f" (cutoff {mat['alphaCutoff']})" if mat["alphaCutoff"] is not None else "")
    lines = [f"#{mat['index']}{name}  alphaMode: {mode}  doubleSided: {'yes' if mat['doubleSided'] else 'no'}"]
    for slot, target in mat["textures"].items():
        image = target["image"]
        label = "no image" if image is None else f"image #{image}"
        if isinstance(image, int) and 0 <= image < len(images) and images[image]["name"]:
            label += f" {images[image]['name']!r}"
        lines.append(f"    {slot} texture -> {label}")
    if not mat["textures"]:
        lines.append("    no textures")
    return lines


def format_report(report: dict[str, Any]) -> str:
    """Render a report dict as readable text."""
    f, ext, geo, bounds = report["file"], report["extensions"], report["geometry"], report["bounds"]
    lines = [f"=== {report['path']} ==="]
    lines += section("File", [
        f"Size:       {fmt_bytes(f['sizeBytes'])}",
        f"Generator:  {f['generator'] or '(not recorded)'}",
        f"glTF asset version: {f['assetVersion'] or '(not recorded)'}",
    ])
    lines += section("Extensions", [
        f"Used:      {', '.join(ext['used']) or '(none)'}",
        f"Required:  {', '.join(ext['required']) or '(none)'}",
    ])
    lines += section("Geometry", [
        f"Meshes: {geo['meshCount']}    Nodes: {geo['nodeCount']}",
        f"Triangles: {geo['triangleCount']:,}    Vertices: {geo['vertexCount']:,}"
        "  (counted per node instance in the default scene)",
    ])
    if bounds:
        rows = [f"Width (X):   {bounds['widthMeters']:.3f} m", f"Height (Y):  {bounds['heightMeters']:.3f} m",
                f"Depth (Z):   {bounds['depthMeters']:.3f} m", f"Lowest point (min Y): {bounds['minY']:.3f} m"]
        rows += [f"Note: {n}" for n in bounds["notes"]]
    else:
        rows = ["Not available (no mesh in the default scene has POSITION min/max)."]
    lines += section("Bounds (meters)", rows)
    mat_rows = [line for m in report["materials"] for line in describe_material_line(m, report["images"])]
    lines += section("Materials", mat_rows or ["(none)"])
    lines += section("Images", [describe_image_line(i) for i in report["images"]] or ["(none)"])
    mem = report["textureMemory"]
    lines += section("Texture memory estimate", [
        f"About {mem['megabytes']:.2f} MB (width x height x 4 bytes per image).",
        f"Mipmaps add about a third: roughly {mem['withMipmapsMegabytes']:.2f} MB in total.",
    ])
    lines += section("Findings", [f"[{x['severity']}] {x['code']}: {x['message']}" for x in report["findings"]])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Inspect GLB files and explain conversion risks in plain English.")
    parser.add_argument("files", nargs="+", metavar="FILE.glb", help="one or more GLB files")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print a JSON list with one dict per file instead of text")
    args = parser.parse_args(argv)

    results: list[dict[str, Any]] = []
    failed = False
    for name in args.files:
        try:
            report = inspect_file(Path(name))
        except GlbError as exc:
            print(f"ERROR {name}: {exc}", file=sys.stderr)
            results.append({"path": name, "error": str(exc)})
            failed = True
            continue
        results.append(report)
        failed = failed or any(x["severity"] == "ERROR" for x in report["findings"])
    if args.as_json:
        print(json.dumps(results, indent=2))
    else:
        print("\n\n".join(format_report(r) for r in results if "error" not in r))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
