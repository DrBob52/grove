#!/usr/bin/env python3
"""Generate the placeholder plant used to test the pipeline before real plants exist.

Usage: python3 scripts/make_placeholder_plant.py [--root PATH]

Writes Assets/Plants/source/placeholder-a-01.glb (a tapered stem with leaf cards)
and App/Resources/Plants/placeholder-a-01-thumb.png (a software-rendered picture).
Standard library only. The output is identical on every run on the same machine.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

PLANT_ID = "placeholder-a-01"
GLB_PATH = Path("Assets") / "Plants" / "source" / f"{PLANT_ID}.glb"
THUMB_PATH = Path("App") / "Resources" / "Plants" / f"{PLANT_ID}-thumb.png"
SEED = 20260929

STEM_HEIGHT = 0.35
STEM_RADII = (0.013, 0.006)  # at the base and at the top
STEM_SIDES = 10
STEM_RINGS = 6
PETIOLE_SIDES = 4
LEAF_COUNT = 16
GOLDEN_ANGLE = math.radians(137.50776)
TEXTURE_SIZE = 256
THUMB_SIZE = 512
THUMB_SAMPLES = 2  # supersampling per side, for smooth edges
THUMB_AZIMUTH = math.radians(32)
THUMB_ELEVATION = math.radians(22)

Vec = tuple[float, float, float]


# --------------------------------------------------------------------------
# Small vector helpers
# --------------------------------------------------------------------------

def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit(a: Vec) -> Vec:
    return mul(a, 1.0 / math.sqrt(dot(a, a)))


def jitter(rng: random.Random, amount: float) -> float:
    """A value in [-amount, amount]. Only random() is used because its sequence never changes."""
    return (rng.random() * 2.0 - 1.0) * amount


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

@dataclass
class Mesh:
    """Indexed triangles with normals and UVs."""

    positions: list[Vec] = field(default_factory=list)
    normals: list[Vec] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)

    def vertex(self, position: Vec, normal: Vec, uv: tuple[float, float]) -> int:
        self.positions.append(position)
        self.normals.append(normal)
        self.uvs.append(uv)
        return len(self.positions) - 1

    def triangle(self, a: int, b: int, c: int) -> None:
        self.indices += [a, b, c]


def add_tube(mesh: Mesh, start: Vec, end: Vec, radius0: float, radius1: float, sides: int,
             rings: int, v_scale: float, cap_end: bool = False) -> None:
    """A tapered tube from start to end with outward normals; optionally closed at the end."""
    axis = sub(end, start)
    length = math.sqrt(dot(axis, axis))
    a = mul(axis, 1.0 / length)
    e1 = unit(cross((1.0, 0.0, 0.0) if abs(a[1]) > 0.9 else (0.0, 1.0, 0.0), a))
    e2 = cross(a, e1)  # (e1, e2, a) is right-handed, so the winding below faces outward
    slope = (radius1 - radius0) / length
    first = len(mesh.positions)
    for ring in range(rings + 1):
        t = ring / rings
        center = add(start, mul(axis, t))
        radius = radius0 + (radius1 - radius0) * t
        for side in range(sides + 1):  # the first and last vertex share a position but not a UV
            angle = 2.0 * math.pi * side / sides
            radial = add(mul(e1, math.cos(angle)), mul(e2, math.sin(angle)))
            mesh.vertex(add(center, mul(radial, radius)), unit(sub(radial, mul(a, slope))),
                        (side / sides, t * length * v_scale))
    row = sides + 1
    for ring in range(rings):
        for side in range(sides):
            p = first + ring * row + side
            mesh.triangle(p, p + 1, p + row + 1)
            mesh.triangle(p, p + row + 1, p + row)
    if cap_end:
        top = first + rings * row
        middle = mesh.vertex(end, a, (0.5, 0.5))
        edge = [mesh.vertex(mesh.positions[top + side], a,
                            (0.5 + 0.5 * math.cos(2.0 * math.pi * side / sides),
                             0.5 + 0.5 * math.sin(2.0 * math.pi * side / sides))) for side in range(sides + 1)]
        for side in range(sides):
            mesh.triangle(middle, edge[side], edge[side + 1])


def add_leaf_card(mesh: Mesh, origin: Vec, side: Vec, along: Vec, width: float, length: float) -> None:
    """A flat quad whose bottom edge is centered on origin. The texture's leaf base is at the bottom."""
    normal = unit(cross(side, along))
    half = mul(side, width / 2.0)
    reach = mul(along, length)
    corners = [sub(origin, half), add(origin, half), add(add(origin, half), reach), add(sub(origin, half), reach)]
    first = [mesh.vertex(c, normal, uv) for c, uv in zip(corners, [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)])]
    mesh.triangle(first[0], first[1], first[2])
    mesh.triangle(first[0], first[2], first[3])


def stem_radius(height: float) -> float:
    return STEM_RADII[0] + (STEM_RADII[1] - STEM_RADII[0]) * height / STEM_HEIGHT


def add_leaf(bark: Mesh, leaf: Mesh, rng: random.Random, index: int) -> None:
    """One leaf on a short petiole, spiralling up the stem. Leaves higher up point more upward."""
    f = index / (LEAF_COUNT - 1)
    top = index == LEAF_COUNT - 1
    height = 0.07 + (STEM_HEIGHT - 0.075) * f
    yaw = index * GOLDEN_ANGLE + jitter(rng, 0.12)
    pitch = math.radians(20.0 + 48.0 * f + (14.0 if top else 0.0)) + jitter(rng, 0.1)
    roll = jitter(rng, 0.4)
    length = 0.108 - 0.028 * f + jitter(rng, 0.006)
    petiole = 0.022 + 0.008 * (1.0 - f)

    outward = (math.cos(yaw), 0.0, math.sin(yaw))
    inside = stem_radius(height) * 0.5
    start = add(mul(outward, inside), (0.0, height, 0.0))
    petiole_pitch = pitch * 0.6
    direction = unit(add(mul(outward, math.cos(petiole_pitch)), (0.0, math.sin(petiole_pitch), 0.0)))
    end = add(start, mul(direction, inside + petiole))
    add_tube(bark, start, end, 0.0028, 0.0018, PETIOLE_SIDES, 1, 4.0)

    along = add(mul(outward, math.cos(pitch)), (0.0, math.sin(pitch), 0.0))
    flat = (-math.sin(yaw), 0.0, math.cos(yaw))  # horizontal, across the leaf
    side = add(mul(flat, math.cos(roll)), mul(cross(along, flat), math.sin(roll)))
    add_leaf_card(leaf, end, side, along, length * 0.55, length)


def build_meshes(rng: random.Random) -> tuple[Mesh, Mesh]:
    """The bark mesh (stem and petioles) and the leaf mesh (cards)."""
    bark, leaf = Mesh(), Mesh()
    add_tube(bark, (0.0, 0.0, 0.0), (0.0, STEM_HEIGHT, 0.0), STEM_RADII[0], STEM_RADII[1], STEM_SIDES,
             STEM_RINGS, 3.0 / STEM_HEIGHT, cap_end=True)
    for index in range(LEAF_COUNT):
        add_leaf(bark, leaf, rng, index)
    return bark, leaf


# --------------------------------------------------------------------------
# Textures and PNG
# --------------------------------------------------------------------------

@dataclass
class Image:
    """8-bit pixels, row by row, top row first."""

    width: int
    height: int
    channels: int  # 3 for RGB, 4 for RGBA
    pixels: bytes

    def sample(self, u: float, v: float, wrap: bool) -> tuple[int, ...]:
        x, y = int(math.floor(u * self.width)), int(math.floor(v * self.height))
        if wrap:
            x, y = x % self.width, y % self.height
        else:
            x, y = min(max(x, 0), self.width - 1), min(max(y, 0), self.height - 1)
        start = (y * self.width + x) * self.channels
        return tuple(self.pixels[start:start + self.channels])


def clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(value))))


def bark_texture(rng: random.Random) -> Image:
    """256x256 RGB brown bark: wavy vertical streaks and cracks. It tiles on both axes."""
    size = TEXTURE_SIZE
    cracks = [(rng.random() * size, 1.0 + rng.random() * 2.0, rng.random() * math.tau, 2 + int(rng.random() * 3))
              for _ in range(14)]
    pixels = bytearray()
    for y in range(size):
        wobble = 7.0 * math.sin(math.tau * y / size)
        breaks = [0.5 + 0.5 * math.sin(math.tau * rate * y / size + phase) for _c, _w, phase, rate in cracks]
        for x in range(size):
            shifted = x + wobble
            streak = (0.5 * math.sin(math.tau * 3 * shifted / size + 0.7)
                      + 0.3 * math.sin(math.tau * 7 * shifted / size + 2.1)
                      + 0.2 * math.sin(math.tau * 17 * shifted / size + 4.0))
            dark = 0.0
            for (center, width, _phase, _rate), broken in zip(cracks, breaks):
                gap = abs((shifted - center + size / 2) % size - size / 2)
                if gap < width:
                    dark = max(dark, (1.0 - gap / width) * broken)
            grain = jitter(rng, 4.0)
            pixels += bytes((clamp_byte(104 + 24 * streak - 52 * dark + grain),
                             clamp_byte(76 + 18 * streak - 40 * dark + grain),
                             clamp_byte(52 + 12 * streak - 28 * dark + grain)))
    return Image(size, size, 3, bytes(pixels))


def leaf_half_width(t: float) -> float:
    """Half the leaf's width (as a fraction of the card) at t, 0 at the base and 1 at the tip."""
    return 0.47 * math.sin(math.pi * t ** 0.85) ** 0.75 * (1.0 - 0.3 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def leaf_texture() -> Image:
    """256x256 RGBA leaf: a pointed oval with a lighter midrib and side veins, fully transparent outside."""
    size = TEXTURE_SIZE
    pixels = bytearray()
    for y in range(size):
        t = 1.0 - (y + 0.5) / size  # 0 at the base (bottom), 1 at the tip (top)
        for x in range(size):
            across = abs((x + 0.5) / size - 0.5)
            edge = (leaf_half_width(t) - across) * size  # pixels inside the outline
            alpha = max(0.0, min(1.0, edge + 0.5))
            rib = max(0.0, 1.0 - across / (0.012 + 0.010 * (1.0 - t)))
            vein = max(0.0, 1.0 - abs(((t * 9.0 - across * 5.5) % 1.0) - 0.5) * 2.0 / 0.16) * 0.5 * (across > 0.02)
            light = max(rib, vein * 0.6)
            shade = 0.5 * t + 0.25 * (across / 0.47)
            red, green, blue = lerp(40, 92, shade), lerp(102, 156, shade), lerp(48, 58, shade)
            pixels += bytes((clamp_byte(lerp(red, 196, light)), clamp_byte(lerp(green, 222, light)),
                             clamp_byte(lerp(blue, 128, light)), clamp_byte(alpha * 255)))
    return Image(size, size, 4, bytes(pixels))


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def encode_png(image: Image) -> bytes:
    """An 8-bit RGB or RGBA PNG. Every row after the first uses the Up filter, which suits smooth images."""
    stride = image.width * image.channels
    raw = bytearray()
    previous = bytes(stride)
    for y in range(image.height):
        row = image.pixels[y * stride:(y + 1) * stride]
        raw.append(2 if y else 0)
        raw += bytes((a - b) & 0xFF for a, b in zip(row, previous)) if y else row
        previous = row
    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 6 if image.channels == 4 else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + png_chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# GLB
# --------------------------------------------------------------------------

GLB_MAGIC, JSON_CHUNK, BIN_CHUNK = 0x46546C67, 0x4E4F534A, 0x004E4942
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
FLOAT, UNSIGNED_SHORT = 5126, 5123


def float32(value: float) -> float:
    """value rounded to the float32 that will be stored, so min/max match the data exactly."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


class BinaryBuffer:
    """Collects the GLB's binary chunk together with its bufferViews and accessors."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.views: list[dict[str, int]] = []
        self.accessors: list[dict[str, object]] = []

    def view(self, payload: bytes, target: int | None = None) -> int:
        self.data += b"\x00" * (-len(self.data) % 4)
        entry = {"buffer": 0, "byteOffset": len(self.data), "byteLength": len(payload)}
        if target is not None:
            entry["target"] = target
        self.views.append(entry)
        self.data += payload
        return len(self.views) - 1

    def accessor(self, payload: bytes, component: int, kind: str, count: int, target: int,
                 bounds: tuple[list[float], list[float]] | None = None) -> int:
        entry: dict[str, object] = {"bufferView": self.view(payload, target), "componentType": component,
                                    "type": kind, "count": count}
        if bounds:
            entry["min"], entry["max"] = bounds
        self.accessors.append(entry)
        return len(self.accessors) - 1

    def primitive(self, mesh: Mesh, material: int) -> dict[str, object]:
        """Write a mesh's vertex data and return the glTF primitive that uses it."""
        points = [tuple(float32(c) for c in p) for p in mesh.positions]
        bounds = ([min(p[i] for p in points) for i in range(3)], [max(p[i] for p in points) for i in range(3)])
        count = len(points)
        attributes = {
            "POSITION": self.accessor(b"".join(struct.pack("<3f", *p) for p in points), FLOAT, "VEC3", count,
                                      ARRAY_BUFFER, bounds),
            "NORMAL": self.accessor(b"".join(struct.pack("<3f", *n) for n in mesh.normals), FLOAT, "VEC3", count,
                                    ARRAY_BUFFER),
            "TEXCOORD_0": self.accessor(b"".join(struct.pack("<2f", *uv) for uv in mesh.uvs), FLOAT, "VEC2", count,
                                        ARRAY_BUFFER),
        }
        indices = self.accessor(struct.pack(f"<{len(mesh.indices)}H", *mesh.indices), UNSIGNED_SHORT, "SCALAR",
                                len(mesh.indices), ELEMENT_ARRAY_BUFFER)
        return {"attributes": attributes, "indices": indices, "material": material, "mode": 4}


def build_glb(bark: Mesh, leaf: Mesh, bark_image: Image, leaf_image: Image) -> bytes:
    """Assemble the GLB: a Stem node with the Bark material and a Leaves node with the Leaf material."""
    buffer = BinaryBuffer()
    meshes = [{"name": "Stem", "primitives": [buffer.primitive(bark, 0)]},
              {"name": "Leaves", "primitives": [buffer.primitive(leaf, 1)]}]
    images = [{"name": name, "mimeType": "image/png", "bufferView": buffer.view(encode_png(image))}
              for name, image in (("bark", bark_image), ("leaf", leaf_image))]
    document = {
        "asset": {"version": "2.0", "generator": "grove scripts/make_placeholder_plant.py"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [{"name": "Stem", "mesh": 0}, {"name": "Leaves", "mesh": 1}],
        "meshes": meshes,
        "materials": [
            {"name": "Bark", "alphaMode": "OPAQUE", "doubleSided": False,
             "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0.0,
                                      "roughnessFactor": 0.9}},
            {"name": "Leaf", "alphaMode": "MASK", "alphaCutoff": 0.5, "doubleSided": True,
             "pbrMetallicRoughness": {"baseColorTexture": {"index": 1}, "metallicFactor": 0.0,
                                      "roughnessFactor": 0.7}},
        ],
        "textures": [{"sampler": 0, "source": 0}, {"sampler": 1, "source": 1}],
        "images": images,
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497},
                     {"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "accessors": buffer.accessors,
        "bufferViews": buffer.views,
        "buffers": [{"byteLength": len(buffer.data) + (-len(buffer.data) % 4)}],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    binary = bytes(buffer.data) + b"\x00" * (-len(buffer.data) % 4)
    chunks = (struct.pack("<II", len(payload), JSON_CHUNK) + payload
              + struct.pack("<II", len(binary), BIN_CHUNK) + binary)
    return struct.pack("<III", GLB_MAGIC, 2, 12 + len(chunks)) + chunks


# --------------------------------------------------------------------------
# Thumbnail: a tiny software rasterizer
# --------------------------------------------------------------------------

@dataclass
class Part:
    """A mesh with its texture and how it is drawn."""

    mesh: Mesh
    texture: Image
    double_sided: bool
    cutout: bool  # skip pixels whose texture alpha is below 0.5, like alphaMode MASK
    wrap: bool


class Canvas:
    """Supersampled RGB and coverage buffers that are averaged down to RGBA at the end."""

    def __init__(self, size: int, samples: int) -> None:
        self.size, self.samples = size, samples
        self.width = size * samples
        self.rgb = bytearray(self.width * self.width * 3)
        self.covered = bytearray(self.width * self.width)

    def set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        index = y * self.width + x
        self.covered[index] = 1
        self.rgb[index * 3:index * 3 + 3] = bytes(color)

    def to_image(self) -> Image:
        s, out = self.samples, bytearray()
        for oy in range(self.size):
            for ox in range(self.size):
                count = red = green = blue = 0
                for sy in range(oy * s, oy * s + s):
                    for sx in range(ox * s, ox * s + s):
                        index = sy * self.width + sx
                        if self.covered[index]:
                            count += 1
                            red += self.rgb[index * 3]
                            green += self.rgb[index * 3 + 1]
                            blue += self.rgb[index * 3 + 2]
                if count:
                    out += bytes((red // count, green // count, blue // count, count * 255 // (s * s)))
                else:
                    out += b"\x00\x00\x00\x00"
        return Image(self.size, self.size, 4, bytes(out))


def camera_basis() -> tuple[Vec, Vec, Vec]:
    """(right, up, toward-camera) for a 3/4 view from above and to the side."""
    toward = (math.sin(THUMB_AZIMUTH) * math.cos(THUMB_ELEVATION), math.sin(THUMB_ELEVATION),
              math.cos(THUMB_AZIMUTH) * math.cos(THUMB_ELEVATION))
    right = unit(cross((0.0, 1.0, 0.0), toward))
    return right, cross(toward, right), toward


def shade(color: tuple[int, ...], normal: Vec, toward: Vec, light: Vec) -> tuple[int, int, int]:
    """Lambert shading with a little ambient light; the normal is flipped to face the camera."""
    if dot(normal, toward) < 0:
        normal = mul(normal, -1.0)
    level = 0.45 + 0.75 * max(0.0, dot(normal, light))
    return clamp_byte(color[0] * level), clamp_byte(color[1] * level), clamp_byte(color[2] * level)


def draw_triangle(canvas: Canvas, part: Part, corners: list[int], screen: list[tuple[float, float]],
                  light: Vec, toward: Vec) -> None:
    """Fill one triangle, interpolating UVs and normals from its corners."""
    (ax, ay), (bx, by), (cx, cy) = screen
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if area == 0:
        return
    (u0, v0), (u1, v1), (u2, v2) = (part.mesh.uvs[i] for i in corners)
    n0, n1, n2 = (part.mesh.normals[i] for i in corners)
    limit = canvas.width - 1
    x0, x1 = max(int(min(ax, bx, cx)), 0), min(int(max(ax, bx, cx)) + 1, limit)
    y0, y1 = max(int(min(ay, by, cy)), 0), min(int(max(ay, by, cy)) + 1, limit)
    for y in range(y0, y1 + 1):
        py = y + 0.5
        for x in range(x0, x1 + 1):
            px = x + 0.5
            w0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / area
            w1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / area
            w2 = 1.0 - w0 - w1
            if w0 < 0 or w1 < 0 or w2 < 0:
                continue
            texel = part.texture.sample(w0 * u0 + w1 * u1 + w2 * u2, w0 * v0 + w1 * v1 + w2 * v2, part.wrap)
            if part.cutout and texel[3] < 128:
                continue
            normal = unit((w0 * n0[0] + w1 * n1[0] + w2 * n2[0], w0 * n0[1] + w1 * n1[1] + w2 * n2[1],
                           w0 * n0[2] + w1 * n1[2] + w2 * n2[2]))
            canvas.set(x, y, shade(texel, normal, toward, light))


def render_thumbnail(parts: list[Part]) -> bytes:
    """Draw the plant far triangles first (painter's algorithm) into a transparent 512x512 PNG."""
    right, up, toward = camera_basis()
    light = unit((-0.45, 0.8, 0.5))
    points = [p for part in parts for p in part.mesh.positions]
    xs, ys = [dot(p, right) for p in points], [dot(p, up) for p in points]
    canvas = Canvas(THUMB_SIZE, THUMB_SAMPLES)
    scale = 0.9 * canvas.width / max(max(xs) - min(xs), max(ys) - min(ys))
    mid_x, mid_y = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def to_screen(p: Vec) -> tuple[float, float]:
        return (canvas.width / 2 + (dot(p, right) - mid_x) * scale, canvas.width / 2 - (dot(p, up) - mid_y) * scale)

    faces = []
    for number, part in enumerate(parts):
        idx = part.mesh.indices
        for t in range(0, len(idx), 3):
            corners = idx[t:t + 3]
            pts = [part.mesh.positions[i] for i in corners]
            depth = sum(dot(p, toward) for p in pts) / 3.0
            facing = dot(cross(sub(pts[1], pts[0]), sub(pts[2], pts[0])), toward) > 0
            if facing or part.double_sided:
                faces.append((depth, number, t, corners))
    for _depth, number, _t, corners in sorted(faces, key=lambda f: f[:3]):
        part = parts[number]
        draw_triangle(canvas, part, corners, [to_screen(part.mesh.positions[i]) for i in corners], light, toward)
    return encode_png(canvas.to_image())


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

def generate() -> tuple[bytes, bytes]:
    """Build the plant and return (GLB bytes, thumbnail PNG bytes)."""
    rng = random.Random(SEED)
    bark_image, leaf_image = bark_texture(rng), leaf_texture()
    bark, leaf = build_meshes(rng)
    glb = build_glb(bark, leaf, bark_image, leaf_image)
    thumbnail = render_thumbnail([Part(bark, bark_image, False, False, True),
                                  Part(leaf, leaf_image, True, True, False)])
    return glb, thumbnail


def default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Generate the placeholder plant: a GLB and a thumbnail PNG.")
    parser.add_argument("--root", type=Path, default=default_root(),
                        help="repo root to write into (default: the parent of scripts/)")
    args = parser.parse_args(argv)
    glb, thumbnail = generate()
    for relative, data in ((GLB_PATH, glb), (THUMB_PATH, thumbnail)):
        path = args.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"Wrote {relative} ({len(data):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
