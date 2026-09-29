"""Tests for scripts/make_placeholder_plant.py (standard library only)."""

import contextlib
import io
import math
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from typing import Any
from unittest import mock

import helpers
import inspect_glb
import make_placeholder_plant as make

REPO_ROOT = helpers.SCRIPTS_DIR.parent
_generated: list[tuple[bytes, bytes]] = []


def generated() -> tuple[bytes, bytes]:
    """The plant from make.generate(), built once and shared by every test class."""
    if not _generated:
        _generated.append(make.generate())
    return _generated[0]


def decode_png(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Decode the PNGs this generator writes (8-bit RGB/RGBA, filters None and Up) into rows of bytes."""
    assert data[:8] == helpers.PNG_SIGNATURE
    pos, idat, width, height, color_type = 8, b"", 0, 0, 0
    while pos < len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        payload = data[pos + 8:pos + 8 + length]
        assert struct.unpack_from(">I", data, pos + 8 + length)[0] == zlib.crc32(kind + payload) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height, depth, color_type = struct.unpack_from(">IIBB", payload)
            assert depth == 8
        elif kind == b"IDAT":
            idat += payload
        pos += 12 + length
    channels = {2: 3, 6: 4}[color_type]
    raw, stride = zlib.decompress(idat), width * channels
    rows, previous = [], bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        filter_type, row = raw[start], raw[start + 1:start + 1 + stride]
        assert filter_type in (0, 2)
        row = bytes((a + b) & 0xFF for a, b in zip(row, previous)) if filter_type == 2 else bytes(row)
        rows.append(row)
        previous = row
    return width, height, channels, rows


def pixel(rows: list[bytes], channels: int, x: int, y: int) -> tuple[int, ...]:
    return tuple(rows[y][x * channels:(x + 1) * channels])


class PlaceholderTestCase(unittest.TestCase):
    glb: bytes
    thumbnail: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.glb, cls.thumbnail = generated()
        cls.doc, cls.bin = inspect_glb.parse_glb(cls.glb)

    def image_rows(self, image_index: int) -> tuple[int, int, int, list[bytes]]:
        view = self.doc["bufferViews"][self.doc["images"][image_index]["bufferView"]]
        return decode_png(self.bin[view["byteOffset"]:view["byteOffset"] + view["byteLength"]])

    def mesh_arrays(self, mesh_index: int) -> tuple[list[Any], list[Any], list[int]]:
        primitive = self.doc["meshes"][mesh_index]["primitives"][0]
        positions = inspect_glb.read_float_vectors(self.doc, self.bin, primitive["attributes"]["POSITION"], 3)
        normals = inspect_glb.read_float_vectors(self.doc, self.bin, primitive["attributes"]["NORMAL"], 3)
        accessor = self.doc["accessors"][primitive["indices"]]
        view = self.doc["bufferViews"][accessor["bufferView"]]
        data = self.bin[view["byteOffset"]:view["byteOffset"] + view["byteLength"]]
        assert positions is not None and normals is not None
        return positions, normals, list(struct.unpack(f"<{accessor['count']}H", data))


class GeneratorDeterminismTests(PlaceholderTestCase):
    def test_two_runs_produce_identical_bytes(self) -> None:
        glb, thumbnail = make.generate()
        self.assertEqual(glb, self.glb)
        self.assertEqual(thumbnail, self.thumbnail)

    def test_main_writes_both_files_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(make, "generate", return_value=(b"glb", b"png")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(make.main(["--root", tmp]), 0)
            self.assertEqual((Path(tmp) / make.GLB_PATH).read_bytes(), b"glb")
            self.assertEqual((Path(tmp) / make.THUMB_PATH).read_bytes(), b"png")


class GlbTests(PlaceholderTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "placeholder-a-01.glb"
        path.write_bytes(self.glb)
        self.report = inspect_glb.inspect_file(path)

    def test_inspector_reports_only_info_findings(self) -> None:
        self.assertEqual({f["severity"] for f in self.report["findings"]}, {"INFO"})
        self.assertIn("alpha-cutout", {f["code"] for f in self.report["findings"]})

    def test_base_is_at_the_origin_and_plant_is_centered(self) -> None:
        bounds = self.report["bounds"]
        self.assertAlmostEqual(bounds["minY"], 0.0, places=6)
        for axis in (0, 2):
            self.assertLess(abs(bounds["min"][axis] + bounds["max"][axis]) / 2, 0.03)

    def test_height_is_about_forty_five_centimeters(self) -> None:
        self.assertTrue(0.40 <= self.report["bounds"]["heightMeters"] <= 0.50, self.report["bounds"])

    def test_leaf_material_is_a_double_sided_cutout_with_an_alpha_texture(self) -> None:
        leaf = next(m for m in self.report["materials"] if m["name"] == "Leaf")
        self.assertEqual((leaf["alphaMode"], leaf["alphaCutoff"], leaf["doubleSided"]), ("MASK", 0.5, True))
        image = self.report["images"][leaf["textures"]["baseColor"]["image"]]
        self.assertTrue(image["hasAlpha"])
        self.assertEqual((image["width"], image["height"], image["source"]), (256, 256, "embedded"))
        material = self.doc["materials"][1]["pbrMetallicRoughness"]
        self.assertEqual((material["metallicFactor"], material["roughnessFactor"]), (0.0, 0.7))

    def test_bark_material_is_an_opaque_single_sided_rgb_texture(self) -> None:
        bark = next(m for m in self.report["materials"] if m["name"] == "Bark")
        self.assertEqual((bark["alphaMode"], bark["doubleSided"]), ("OPAQUE", False))
        image = self.report["images"][bark["textures"]["baseColor"]["image"]]
        self.assertFalse(image["hasAlpha"])
        self.assertEqual((image["width"], image["height"]), (256, 256))
        self.assertEqual(self.image_rows(0)[2], 3)
        material = self.doc["materials"][0]["pbrMetallicRoughness"]
        self.assertEqual((material["metallicFactor"], material["roughnessFactor"]), (0.0, 0.9))

    def test_textures_are_embedded_and_there_are_no_external_files(self) -> None:
        self.assertEqual({image["source"] for image in self.report["images"]}, {"embedded"})
        self.assertEqual(self.report["extensions"], {"used": [], "required": []})

    def test_leaf_cards_are_quads_with_normals_and_uvs(self) -> None:
        primitive = self.doc["meshes"][1]["primitives"][0]
        self.assertEqual(set(primitive["attributes"]), {"POSITION", "NORMAL", "TEXCOORD_0"})
        cards = self.doc["accessors"][primitive["attributes"]["POSITION"]]["count"] / 4
        self.assertEqual(cards, int(cards))
        self.assertTrue(14 <= cards <= 20, cards)

    def test_stem_has_normals_and_uvs_and_a_sensible_side_count(self) -> None:
        primitive = self.doc["meshes"][0]["primitives"][0]
        self.assertEqual(set(primitive["attributes"]), {"POSITION", "NORMAL", "TEXCOORD_0"})
        self.assertTrue(8 <= make.STEM_SIDES <= 12)
        self.assertTrue(0.30 <= make.STEM_HEIGHT <= 0.40)
        self.assertGreater(make.STEM_RADII[0], make.STEM_RADII[1])  # tapered

    def test_normals_are_unit_length_and_agree_with_the_triangle_winding(self) -> None:
        for mesh_index in (0, 1):
            positions, normals, indices = self.mesh_arrays(mesh_index)
            for n in normals:
                self.assertAlmostEqual(math.sqrt(sum(c * c for c in n)), 1.0, places=5)
            for t in range(0, len(indices), 3):
                a, b, c = (positions[i] for i in indices[t:t + 3])
                face = make.cross(make.sub(b, a), make.sub(c, a))
                average = [sum(normals[i][k] for i in indices[t:t + 3]) for k in range(3)]
                self.assertGreater(make.dot(face, tuple(average)), 0.0, f"mesh {mesh_index} triangle {t // 3}")

    def test_leaves_are_spread_up_the_stem_at_different_angles(self) -> None:
        positions, _normals, indices = self.mesh_arrays(1)
        origins = [positions[indices[t]] for t in range(0, len(indices), 6)]
        heights = [o[1] for o in origins]
        self.assertGreater(max(heights) - min(heights), 0.15)
        angles = {round(math.degrees(math.atan2(o[2], o[0])) / 30) for o in origins}
        self.assertGreater(len(angles), 6)


class TextureTests(PlaceholderTestCase):
    def test_leaf_alpha_is_a_leaf_shape_with_transparent_surroundings(self) -> None:
        width, height, channels, rows = self.image_rows(1)
        self.assertEqual((width, height, channels), (256, 256, 4))
        for x, y in ((0, 0), (255, 0), (0, 255), (255, 255), (10, 128), (245, 128)):
            self.assertEqual(pixel(rows, 4, x, y)[3], 0, (x, y))
        self.assertEqual(pixel(rows, 4, 128, 130)[3], 255)
        alphas = [row[i] for row in rows for i in range(3, len(row), 4)]
        self.assertTrue(0.25 < alphas.count(0) / len(alphas) < 0.55)
        self.assertGreater(len(set(alphas)), 2)  # the outline is anti-aliased, not just 0 and 255

    def test_leaf_has_a_lighter_midrib_and_a_color_gradient(self) -> None:
        _w, _h, _c, rows = self.image_rows(1)
        rib, blade = pixel(rows, 4, 128, 120), pixel(rows, 4, 150, 120)
        self.assertGreater(sum(rib[:3]), sum(blade[:3]) + 40)
        low, high = pixel(rows, 4, 100, 200), pixel(rows, 4, 100, 70)
        self.assertNotEqual(low[:3], high[:3])

    def test_bark_is_opaque_brown(self) -> None:
        width, height, channels, rows = self.image_rows(0)
        self.assertEqual((width, height, channels), (256, 256, 3))
        red = sum(row[i] for row in rows for i in range(0, len(row), 3)) / (256 * 256)
        blue = sum(row[i] for row in rows for i in range(2, len(row), 3)) / (256 * 256)
        self.assertGreater(red, blue + 30)

    def test_bark_tiles_without_a_visible_seam(self) -> None:
        _w, _h, _c, rows = self.image_rows(0)
        seam = sum(abs(a - b) for row in rows for a, b in zip(row[-3:], row[:3])) / (256 * 3)
        inside = sum(abs(a - b) for row in rows for a, b in zip(row[126:129], row[129:132])) / (256 * 3)
        self.assertLess(seam, inside * 2 + 4)


class ThumbnailTests(PlaceholderTestCase):
    def test_thumbnail_is_a_512_square_rgba_png_with_a_transparent_background(self) -> None:
        width, height, channels, rows = decode_png(self.thumbnail)
        self.assertEqual((width, height, channels), (512, 512, 4))
        for x, y in ((0, 0), (511, 0), (0, 511), (511, 511), (20, 256)):
            self.assertEqual(pixel(rows, 4, x, y), (0, 0, 0, 0), (x, y))

    def test_thumbnail_shows_a_green_plant_on_a_brown_stem(self) -> None:
        _w, _h, _c, rows = decode_png(self.thumbnail)
        opaque = [pixel(rows, 4, x, y) for y in range(512) for x in range(512) if rows[y][x * 4 + 3] == 255]
        self.assertTrue(0.04 < len(opaque) / (512 * 512) < 0.4, len(opaque))
        greens = sum(1 for r, g, b, _a in opaque if g > r + 20 and g > b + 20)
        browns = sum(1 for r, g, b, _a in opaque if r > g > b and r - b > 20)
        self.assertGreater(greens, len(opaque) * 0.3)
        self.assertGreater(browns, 200)

    def test_leaf_cutouts_leave_the_background_visible_inside_the_plant(self) -> None:
        _w, _h, _c, rows = decode_png(self.thumbnail)
        ys = [y for y in range(512) if any(rows[y][x * 4 + 3] for x in range(512))]
        xs = [x for x in range(512) if any(rows[y][x * 4 + 3] for y in range(512))]
        self.assertGreater(min(xs), 10)  # the plant fits inside the frame
        self.assertLess(max(xs), 502)
        self.assertGreater(min(ys), 10)
        self.assertLess(max(ys), 502)
        covered = sum(1 for y in range(min(ys), max(ys)) for x in range(min(xs), max(xs)) if rows[y][x * 4 + 3])
        self.assertLess(covered / ((max(xs) - min(xs)) * (max(ys) - min(ys))), 0.5)


class PngTests(unittest.TestCase):
    def test_encoded_png_decodes_to_the_same_pixels(self) -> None:
        pixels = bytes((x * 7 + y * 3 + c) & 0xFF for y in range(9) for x in range(11) for c in range(4))
        width, height, channels, rows = decode_png(make.encode_png(make.Image(11, 9, 4, pixels)))
        self.assertEqual((width, height, channels), (11, 9, 4))
        self.assertEqual(b"".join(rows), pixels)


class CommandLineTests(unittest.TestCase):
    def test_script_is_executable_and_names_its_outputs(self) -> None:
        path = helpers.SCRIPTS_DIR / "make_placeholder_plant.py"
        self.assertTrue(os.access(path, os.X_OK))
        self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3"))
        self.assertEqual(make.PLANT_ID, "placeholder-a-01")


if __name__ == "__main__":
    unittest.main()
