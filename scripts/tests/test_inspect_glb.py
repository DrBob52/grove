"""Tests for scripts/inspect_glb.py."""

import base64
import contextlib
import io
import json
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import helpers
import inspect_glb
from helpers import GlbBuilder, build_glb, make_jpeg, make_png, quad_primitive

SCRIPT = helpers.SCRIPTS_DIR / "inspect_glb.py"
ALL_CODES = {
    "external-texture", "gpu-instancing", "compressed-geometry", "alpha-texture-on-opaque",
    "blend-alpha", "single-sided-cutout", "texture-over-1k", "base-not-at-origin",
    "alpha-cutout", "texture-memory",
}


def quad_builder(**node: Any) -> GlbBuilder:
    """A one-quad scene (2 triangles, 4 vertices, 1 x 1 m, sitting on Y=0)."""
    b = GlbBuilder()
    b.add_node(mesh=b.add_mesh([quad_primitive(b)]), **node)
    return b


def material_builder(*, image: bytes | None = None, mime: str = "image/png", **material: Any) -> GlbBuilder:
    """A quad with one material; `image` (if given) becomes its baseColor texture."""
    b = GlbBuilder()
    if image is not None:
        material["pbrMetallicRoughness"] = {"baseColorTexture": {"index": b.add_texture(b.add_image(image, mime))}}
    mat = b.add_material(**material)
    b.add_node(mesh=b.add_mesh([quad_primitive(b, material=mat)]))
    return b


def instanced_builder(translations: list[tuple[float, float, float]], **vectors: Any) -> GlbBuilder:
    """A quad drawn once per translation via EXT_mesh_gpu_instancing."""
    b = GlbBuilder()
    attributes = {"TRANSLATION": b.add_vectors(translations, "VEC3")}
    for name, (values, type_) in vectors.items():
        attributes[name] = b.add_vectors(values, type_)
    b.add_node(mesh=b.add_mesh([quad_primitive(b)]),
               extensions={"EXT_mesh_gpu_instancing": {"attributes": attributes}})
    b.use_extension("EXT_mesh_gpu_instancing")
    return b


class InspectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, data: bytes, name: str = "plant.glb") -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def inspect(self, source: "GlbBuilder | bytes") -> dict[str, Any]:
        data = source.build() if isinstance(source, GlbBuilder) else source
        return inspect_glb.inspect_file(self.write(data))

    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inspect_glb.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def codes(self, report: dict[str, Any]) -> set[str]:
        return {f["code"] for f in report["findings"]}

    def finding(self, report: dict[str, Any], code: str) -> dict[str, str]:
        found = [f for f in report["findings"] if f["code"] == code]
        self.assertEqual(len(found), 1, f"expected one {code} finding in {report['findings']}")
        return found[0]

    def assertBounds(self, report: dict[str, Any], lo: list[float], hi: list[float]) -> None:
        bounds = report["bounds"]
        self.assertIsNotNone(bounds)
        for got, want in zip(bounds["min"] + bounds["max"], lo + hi):
            self.assertAlmostEqual(got, want, places=5)


class ContainerTests(InspectTestCase):
    def test_reads_file_facts(self) -> None:
        data = quad_builder().build()
        report = self.inspect(data)
        self.assertEqual(report["file"], {"sizeBytes": len(data), "generator": "unit-test", "assetVersion": "2.0"})

    def test_parse_glb_returns_json_and_bin(self) -> None:
        doc, bin_data = inspect_glb.parse_glb(build_glb({"asset": {"version": "2.0"}}, b"\x01\x02\x03"))
        self.assertEqual(doc["asset"]["version"], "2.0")
        self.assertEqual(bin_data, b"\x01\x02\x03\x00")  # padded to 4 bytes

    def test_glb_without_bin_chunk(self) -> None:
        doc, bin_data = inspect_glb.parse_glb(build_glb({"asset": {"version": "2.0"}}))
        self.assertIsNone(bin_data)
        self.assertEqual(self.inspect(build_glb({"asset": {"version": "2.0"}}))["geometry"]["triangleCount"], 0)

    def test_json_chunk_padded_with_nul_bytes(self) -> None:
        payload = json.dumps({"asset": {"version": "2.0"}}).encode()
        payload += b"\x00" * (-len(payload) % 4 or 4)
        chunk = struct.pack("<II", len(payload), helpers.CHUNK_JSON) + payload
        data = struct.pack("<III", helpers.GLB_MAGIC, 2, 12 + len(chunk)) + chunk
        self.assertEqual(inspect_glb.parse_glb(data)[0]["asset"]["version"], "2.0")

    def test_missing_generator_is_none(self) -> None:
        self.assertIsNone(self.inspect(build_glb({"asset": {"version": "2.0"}}))["file"]["generator"])

    def test_rejects_non_glb(self) -> None:
        for data in (b"hello, this is definitely not a glb file", b"", b"glTF"):
            with self.subTest(data=data[:10]):
                with self.assertRaises(inspect_glb.GlbError):
                    inspect_glb.parse_glb(data)

    def test_short_input_messages(self) -> None:
        with self.assertRaisesRegex(inspect_glb.GlbError, "does not start with"):
            inspect_glb.parse_glb(b"hello")
        with self.assertRaisesRegex(inspect_glb.GlbError, "too short"):
            inspect_glb.parse_glb(b"glTF\x02\x00")

    def test_gltf_text_file_gets_a_hint(self) -> None:
        with self.assertRaisesRegex(inspect_glb.GlbError, r"\.gltf text file"):
            inspect_glb.parse_glb(b'{"asset": {"version": "2.0"}}')

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaisesRegex(inspect_glb.GlbError, "version 1"):
            inspect_glb.parse_glb(build_glb({"asset": {"version": "2.0"}}, version=1))

    def test_rejects_truncated_file(self) -> None:
        data = quad_builder().build()
        with self.assertRaisesRegex(inspect_glb.GlbError, "truncated"):
            inspect_glb.parse_glb(data[:-10])

    def test_rejects_invalid_json_chunk(self) -> None:
        payload = b"{not json!!}  "[:12]
        chunk = struct.pack("<II", len(payload), helpers.CHUNK_JSON) + payload
        data = struct.pack("<III", helpers.GLB_MAGIC, 2, 12 + len(chunk)) + chunk
        with self.assertRaisesRegex(inspect_glb.GlbError, "JSON chunk"):
            inspect_glb.parse_glb(data)

    def test_rejects_missing_json_chunk(self) -> None:
        chunk = struct.pack("<II", 4, helpers.CHUNK_BIN) + b"\x00" * 4
        data = struct.pack("<III", helpers.GLB_MAGIC, 2, 12 + len(chunk)) + chunk
        with self.assertRaisesRegex(inspect_glb.GlbError, "no JSON chunk"):
            inspect_glb.parse_glb(data)

    def test_main_reports_bad_file_cleanly(self) -> None:
        path = self.write(b"plainly not a glb")
        code, out, err = self.run_main(str(path))
        self.assertEqual(code, 1)
        self.assertIn("not a GLB file", err)
        self.assertNotIn("Traceback", err)

    def test_main_reports_wrong_version_cleanly(self) -> None:
        path = self.write(build_glb({"asset": {"version": "2.0"}}, version=1))
        code, _, err = self.run_main(str(path))
        self.assertEqual(code, 1)
        self.assertIn("unsupported GLB version 1", err)

    def test_main_reports_missing_file_cleanly(self) -> None:
        code, _, err = self.run_main(str(self.tmp / "nope.glb"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read file", err)

    def test_malformed_references_are_reported_not_raised(self) -> None:
        b = GlbBuilder()
        b.add_node(mesh=7)
        with self.assertRaisesRegex(inspect_glb.GlbError, r"meshes\[7\]"):
            self.inspect(b)

    def test_hierarchy_loop_is_reported(self) -> None:
        b = GlbBuilder()
        b.add_node(children=[0])
        with self.assertRaisesRegex(inspect_glb.GlbError, "reachable twice"):
            self.inspect(b)

    def test_wrong_typed_json_is_reported_not_raised(self) -> None:
        with self.assertRaises(inspect_glb.GlbError):
            self.inspect(build_glb({"asset": {"version": "2.0"}, "scenes": [{"nodes": [0]}],
                                    "nodes": [{"translation": "up"}]}))


class GeometryTests(InspectTestCase):
    def test_indexed_triangles(self) -> None:
        geo = self.inspect(quad_builder())["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"]), (2, 4))
        self.assertEqual((geo["meshCount"], geo["nodeCount"]), (1, 1))

    def test_non_indexed_triangles(self) -> None:
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([quad_primitive(b, indexed=False)]))
        geo = self.inspect(b)["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"]), (2, 6))

    def test_one_mesh_on_two_nodes_counts_twice(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b)])
        b.add_node(mesh=mesh)
        b.add_node(mesh=mesh, translation=[2, 0, 0])
        geo = self.inspect(b)["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"], geo["meshCount"]), (4, 8, 1))
        self.assertEqual(geo["meshNodeInstances"], 2)

    def test_multiple_primitives_are_summed(self) -> None:
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([quad_primitive(b), quad_primitive(b, indexed=False)]))
        geo = self.inspect(b)["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"]), (4, 10))

    def test_gpu_instancing_multiplies_by_instance_count(self) -> None:
        geo = self.inspect(instanced_builder([(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0)]))["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"]), (10, 20))

    def test_gpu_instancing_count_uses_any_attribute(self) -> None:
        b = GlbBuilder()
        scale = b.add_vectors([(1, 1, 1)] * 3, "VEC3")
        b.add_node(mesh=b.add_mesh([quad_primitive(b)]),
                   extensions={"EXT_mesh_gpu_instancing": {"attributes": {"SCALE": scale}}})
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 6)

    def test_gpu_instancing_on_a_shared_mesh(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b)])
        translation = b.add_vectors([(0, 0, 0)] * 4, "VEC3")
        b.add_node(mesh=mesh, extensions={"EXT_mesh_gpu_instancing": {"attributes": {"TRANSLATION": translation}}})
        b.add_node(mesh=mesh)
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 10)

    def test_primitive_modes(self) -> None:
        cases = {4: 2, 5: 4, 6: 4, 0: 0, 1: 0, 2: 0, 3: 0}  # 6 indices per primitive
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                b = GlbBuilder()
                positions = b.add_positions([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
                indices = b.add_indices([0, 1, 2, 0, 2, 3])
                b.add_node(mesh=b.add_mesh([{"attributes": {"POSITION": positions}, "indices": indices,
                                             "mode": mode}]))
                self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], expected)

    def test_strip_and_fan_without_indices_use_position_count(self) -> None:
        for mode in (5, 6):
            with self.subTest(mode=mode):
                b = GlbBuilder()
                positions = b.add_positions([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 2, 0)])
                b.add_node(mesh=b.add_mesh([{"attributes": {"POSITION": positions}, "mode": mode}]))
                self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 3)

    def test_nodes_without_meshes_are_ignored(self) -> None:
        b = GlbBuilder()
        b.add_node()
        b.add_node(mesh=b.add_mesh([quad_primitive(b)]))
        geo = self.inspect(b)["geometry"]
        self.assertEqual((geo["nodeCount"], geo["triangleCount"]), (2, 2))

    def test_child_nodes_are_counted(self) -> None:
        b = GlbBuilder()
        child = b.add_node(root=False, mesh=b.add_mesh([quad_primitive(b)]))
        b.add_node(children=[child])
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 2)

    def test_only_default_scene_is_counted(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b)])
        b.add_node(mesh=mesh)  # scene 0 holds one quad
        second = b.add_node(root=False, mesh=mesh)
        third = b.add_node(root=False, mesh=mesh)
        b.doc["scenes"].append({"nodes": [second, third]})  # scene 1 holds two
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 2)
        b.doc["scene"] = 1
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 4)

    def test_scene_defaults_to_zero_when_scene_key_missing(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b)])
        b.add_node(mesh=mesh)
        other = b.add_node(root=False, mesh=mesh)
        b.doc["scenes"].append({"nodes": [other]})
        del b.doc["scene"]
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 2)

    def test_no_scenes_uses_all_root_nodes(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b)])
        child = b.add_node(root=False, mesh=mesh)
        b.add_node(root=False, mesh=mesh, children=[child])  # root A with child
        b.add_node(root=False, mesh=mesh)  # root B
        del b.doc["scenes"], b.doc["scene"]
        self.assertEqual(self.inspect(b)["geometry"]["triangleCount"], 6)

    def test_vertex_count_without_position_is_zero(self) -> None:
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([{"attributes": {}}]))
        geo = self.inspect(b)["geometry"]
        self.assertEqual((geo["triangleCount"], geo["vertexCount"]), (0, 0))


class BoundsTests(InspectTestCase):
    def cube_builder(self, **node: Any) -> GlbBuilder:
        """One primitive spanning x 0..1, y 0..2, z 0..3."""
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([quad_primitive(b, size=(1, 2, 3))]), **node)
        return b

    def test_untransformed_bounds_and_dimensions(self) -> None:
        report = self.inspect(self.cube_builder())
        self.assertBounds(report, [0, 0, 0], [1, 2, 3])
        bounds = report["bounds"]
        self.assertEqual((bounds["widthMeters"], bounds["heightMeters"], bounds["depthMeters"]), (1.0, 2.0, 3.0))
        self.assertEqual(bounds["minY"], 0.0)
        self.assertFalse(bounds["excludesInstances"])

    def test_translation_and_scale(self) -> None:
        report = self.inspect(self.cube_builder(translation=[10, 1, 0], scale=[0.5, 0.5, 0.5]))
        self.assertBounds(report, [10, 1, 0], [10.5, 2, 1.5])
        bounds = report["bounds"]
        self.assertEqual((bounds["widthMeters"], bounds["heightMeters"], bounds["depthMeters"]), (0.5, 1.0, 1.5))
        self.assertEqual(bounds["minY"], 1.0)

    def test_negative_scale_flips_bounds(self) -> None:
        self.assertBounds(self.inspect(self.cube_builder(scale=[-1, 1, 1])), [-1, 0, 0], [0, 2, 3])

    def test_parent_and_child_transforms_compose(self) -> None:
        b = GlbBuilder()
        child = b.add_node(root=False, mesh=b.add_mesh([quad_primitive(b, size=(1, 2, 3))]), scale=[2, 2, 2])
        b.add_node(translation=[1, 0, 0], children=[child])
        self.assertBounds(self.inspect(b), [1, 0, 0], [3, 4, 6])

    def test_parent_rotation_applies_to_child_translation(self) -> None:
        s = 0.7071067811865476
        b = GlbBuilder()
        child = b.add_node(root=False, mesh=b.add_mesh([quad_primitive(b, size=(1, 1, 0))]),
                           translation=[10, 0, 0])
        b.add_node(rotation=[0, 0, s, s], children=[child])
        self.assertBounds(self.inspect(b), [-1, 10, 0], [0, 11, 0])

    def test_matrix_is_column_major(self) -> None:
        matrix = [2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 2, 0, 5, 6, 7, 1]
        self.assertBounds(self.inspect(self.cube_builder(matrix=matrix)), [5, 6, 7], [7, 10, 13])

    def test_matrix_rotation(self) -> None:
        matrix = [0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]  # +90 degrees about Z
        self.assertBounds(self.inspect(self.cube_builder(matrix=matrix)), [-2, 0, 0], [0, 1, 3])

    def test_quaternion_rotation(self) -> None:
        s = 0.7071067811865476
        self.assertBounds(self.inspect(self.cube_builder(rotation=[0, 0, s, s])), [-2, 0, 0], [0, 1, 3])

    def test_trs_order_is_translate_rotate_scale(self) -> None:
        s = 0.7071067811865476
        report = self.inspect(self.cube_builder(translation=[5, 0, 0], rotation=[0, 0, s, s], scale=[1, 3, 1]))
        self.assertBounds(report, [-1, 0, 0], [5, 1, 3])

    def test_bounds_aggregate_across_nodes(self) -> None:
        b = GlbBuilder()
        mesh = b.add_mesh([quad_primitive(b, size=(1, 1, 1))])
        b.add_node(mesh=mesh)
        b.add_node(mesh=mesh, translation=[0, -3, 4])
        self.assertBounds(self.inspect(b), [0, -3, 0], [1, 1, 5])

    def test_gpu_instances_included_in_bounds(self) -> None:
        report = self.inspect(instanced_builder([(0, 0, 0), (5, 0, 0), (0, 3, 0)]))
        self.assertBounds(report, [0, 0, 0], [6, 4, 0])
        self.assertFalse(report["bounds"]["excludesInstances"])

    def test_gpu_instance_scale_and_rotation_included(self) -> None:
        s = 0.7071067811865476
        report = self.inspect(instanced_builder(
            [(0, 0, 0), (5, 0, 0)],
            SCALE=([(1, 1, 1), (2, 2, 2)], "VEC3"),
            ROTATION=([(0, 0, 0, 1), (0, 0, s, s)], "VEC4")))
        # Second instance: rotate 90 degrees about Z, scale 2, move +5 in X -> x 3..5, y 0..2.
        self.assertBounds(report, [0, 0, 0], [5, 2, 0])

    def test_gpu_instances_combine_with_node_transform(self) -> None:
        b = instanced_builder([(0, 0, 0), (5, 0, 0)])
        b.doc["nodes"][0]["translation"] = [0, 10, 0]
        self.assertBounds(self.inspect(b), [0, 10, 0], [6, 11, 0])

    def test_unreadable_instances_are_excluded_and_noted(self) -> None:
        b = GlbBuilder()
        translation = b.add_vectors([(0, 0, 0), (50, 0, 0), (0, 50, 0)], "VEC3", component_type=5123)
        b.add_node(mesh=b.add_mesh([quad_primitive(b)]),
                   extensions={"EXT_mesh_gpu_instancing": {"attributes": {"TRANSLATION": translation}}})
        report = self.inspect(b)
        self.assertBounds(report, [0, 0, 0], [1, 1, 0])
        self.assertTrue(report["bounds"]["excludesInstances"])
        self.assertTrue(any("EXT_mesh_gpu_instancing" in n for n in report["bounds"]["notes"]))
        self.assertEqual(report["geometry"]["triangleCount"], 6)

    def test_bounds_missing_when_no_position_min_max(self) -> None:
        b = GlbBuilder()
        positions = b.add_positions([(0, 0, 0), (1, 1, 1), (2, 0, 2)], with_bounds=False)
        b.add_node(mesh=b.add_mesh([{"attributes": {"POSITION": positions}}]))
        report = self.inspect(b)
        self.assertIsNone(report["bounds"])
        self.assertNotIn("base-not-at-origin", self.codes(report))

    def test_primitive_without_min_max_is_noted(self) -> None:
        b = GlbBuilder()
        positions = b.add_positions([(0, 0, 0), (1, 1, 1), (2, 0, 2)], with_bounds=False)
        b.add_node(mesh=b.add_mesh([quad_primitive(b), {"attributes": {"POSITION": positions}}]))
        report = self.inspect(b)
        self.assertBounds(report, [0, 0, 0], [1, 1, 0])
        self.assertTrue(any("no POSITION min/max" in n for n in report["bounds"]["notes"]))

    def test_no_meshes_no_bounds(self) -> None:
        b = GlbBuilder()
        b.add_node()
        self.assertIsNone(self.inspect(b)["bounds"])


class MaterialAndImageTests(InspectTestCase):
    def test_material_defaults(self) -> None:
        mat = self.inspect(material_builder())["materials"][0]
        self.assertEqual((mat["alphaMode"], mat["alphaCutoff"], mat["doubleSided"], mat["textures"]),
                         ("OPAQUE", None, False, {}))

    def test_mask_material_defaults_cutoff_to_half(self) -> None:
        mat = self.inspect(material_builder(name="leaf", alphaMode="MASK", doubleSided=True))["materials"][0]
        self.assertEqual((mat["name"], mat["alphaMode"], mat["alphaCutoff"], mat["doubleSided"]),
                         ("leaf", "MASK", 0.5, True))

    def test_explicit_alpha_cutoff(self) -> None:
        mat = self.inspect(material_builder(alphaMode="MASK", alphaCutoff=0.3))["materials"][0]
        self.assertEqual(mat["alphaCutoff"], 0.3)

    def test_cutoff_ignored_unless_mask(self) -> None:
        mat = self.inspect(material_builder(alphaMode="BLEND", alphaCutoff=0.3))["materials"][0]
        self.assertIsNone(mat["alphaCutoff"])

    def test_texture_slots_map_to_images(self) -> None:
        b = GlbBuilder()
        images = [b.add_image(make_png(4 + i, 4), name=f"img{i}") for i in range(5)]
        tex = [b.add_texture(i) for i in images]
        mat = b.add_material(
            pbrMetallicRoughness={"baseColorTexture": {"index": tex[0]},
                                  "metallicRoughnessTexture": {"index": tex[1]}},
            normalTexture={"index": tex[2]}, occlusionTexture={"index": tex[3]},
            emissiveTexture={"index": tex[4]})
        b.add_node(mesh=b.add_mesh([quad_primitive(b, material=mat)]))
        slots = self.inspect(b)["materials"][0]["textures"]
        self.assertEqual({k: v["image"] for k, v in slots.items()},
                         {"baseColor": 0, "metallicRoughness": 1, "normal": 2, "occlusion": 3, "emissive": 4})

    def test_two_slots_may_share_one_image(self) -> None:
        b = GlbBuilder()
        tex = b.add_texture(b.add_image(make_png(4, 4)))
        b.add_material(pbrMetallicRoughness={"baseColorTexture": {"index": tex}}, emissiveTexture={"index": tex})
        slots = self.inspect(b)["materials"][0]["textures"]
        self.assertEqual((slots["baseColor"]["image"], slots["emissive"]["image"]), (0, 0))

    def test_texture_source_from_extension(self) -> None:
        b = GlbBuilder()
        image = b.add_image(make_png(4, 4))
        b.doc["textures"].append({"extensions": {"KHR_texture_basisu": {"source": image}}})
        b.add_material(pbrMetallicRoughness={"baseColorTexture": {"index": 0}})
        self.assertEqual(self.inspect(b)["materials"][0]["textures"]["baseColor"]["image"], 0)

    def test_embedded_png_details(self) -> None:
        data = make_png(32, 16, 6)
        b = GlbBuilder()
        b.add_image(data, name="leaf")
        img = self.inspect(b)["images"][0]
        self.assertEqual((img["index"], img["name"], img["mimeType"], img["width"], img["height"]),
                         (0, "leaf", "image/png", 32, 16))
        self.assertEqual((img["hasAlpha"], img["bytes"], img["source"]), (True, len(data), "embedded"))

    def test_png_without_alpha(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(8, 8, 2))
        self.assertFalse(self.inspect(b)["images"][0]["hasAlpha"])

    def test_png_with_trns_has_alpha(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(8, 8, 2, trns=True))
        self.assertTrue(self.inspect(b)["images"][0]["hasAlpha"])

    def test_embedded_jpeg(self) -> None:
        b = GlbBuilder()
        b.add_image(make_jpeg(640, 480), "image/jpeg")
        img = self.inspect(b)["images"][0]
        self.assertEqual((img["mimeType"], img["width"], img["height"], img["hasAlpha"]),
                         ("image/jpeg", 640, 480, False))

    def test_mime_type_is_sniffed_when_missing(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(4, 4))
        del b.doc["images"][0]["mimeType"]
        self.assertEqual(self.inspect(b)["images"][0]["mimeType"], "image/png")

    def test_data_uri_image(self) -> None:
        data = make_png(12, 6, 6)
        b = GlbBuilder()
        b.doc["images"].append({"uri": "data:image/png;base64," + base64.b64encode(data).decode()})
        img = self.inspect(b)["images"][0]
        self.assertEqual((img["source"], img["width"], img["height"], img["hasAlpha"], img["bytes"]),
                         ("data-uri", 12, 6, True, len(data)))

    def test_bad_data_uri_is_unmeasured_not_a_crash(self) -> None:
        b = GlbBuilder()
        b.doc["images"].append({"uri": "data:image/png;base64,@@@not-base64@@@"})
        img = self.inspect(b)["images"][0]
        self.assertEqual((img["source"], img["width"], img["bytes"]), ("data-uri", None, None))

    def test_external_image(self) -> None:
        b = GlbBuilder()
        b.add_external_image("textures/leaf.png")
        img = self.inspect(b)["images"][0]
        self.assertEqual((img["source"], img["uri"], img["width"], img["bytes"]),
                         ("external", "textures/leaf.png", None, None))

    def test_texture_memory_estimate(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(16, 16))
        b.add_image(make_png(8, 8), "image/png")
        memory = self.inspect(b)["textureMemory"]
        self.assertEqual(memory["bytes"], (16 * 16 + 8 * 8) * 4)
        self.assertEqual(memory["measuredImages"], 2)

    def test_texture_memory_in_megabytes(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(1000, 1000))
        memory = self.inspect(b)["textureMemory"]
        self.assertEqual(memory["megabytes"], 4.0)
        self.assertAlmostEqual(memory["withMipmapsMegabytes"], 5.33, places=2)

    def test_texture_memory_skips_unmeasurable_images(self) -> None:
        b = GlbBuilder()
        b.add_image(make_png(10, 10))
        b.add_external_image("x.png")
        memory = self.inspect(b)["textureMemory"]
        self.assertEqual((memory["bytes"], memory["unmeasuredImages"]), (400, 1))

    def test_extensions_listed(self) -> None:
        b = quad_builder()
        b.use_extension("KHR_materials_unlit")
        b.use_extension("KHR_texture_transform", required=True)
        ext = self.inspect(b)["extensions"]
        self.assertEqual(ext["used"], ["KHR_materials_unlit", "KHR_texture_transform"])
        self.assertEqual(ext["required"], ["KHR_texture_transform"])


class FindingTests(InspectTestCase):
    def test_external_texture_is_an_error(self) -> None:
        b = GlbBuilder()
        b.add_external_image("leaf.png")
        f = self.finding(self.inspect(b), "external-texture")
        self.assertEqual(f["severity"], "ERROR")
        self.assertIn("leaf.png", f["message"])

    def test_embedded_textures_do_not_trigger_external_texture(self) -> None:
        self.assertNotIn("external-texture", self.codes(self.inspect(material_builder(image=make_png(8, 8)))))

    def test_gpu_instancing_warns(self) -> None:
        f = self.finding(self.inspect(instanced_builder([(0, 0, 0), (1, 0, 0)])), "gpu-instancing")
        self.assertEqual(f["severity"], "WARN")
        self.assertIn("Normal GLB", f["message"])
        self.assertIn("compatibility", f["message"])
        self.assertIn("EXT_mesh_gpu_instancing", f["message"])

    def test_gpu_instancing_detected_even_if_not_declared(self) -> None:
        b = instanced_builder([(0, 0, 0)])
        del b.doc["extensionsUsed"]
        self.assertIn("gpu-instancing", self.codes(self.inspect(b)))

    def test_gpu_instancing_detected_from_required_list_alone(self) -> None:
        b = quad_builder()
        b.doc["extensionsRequired"] = ["EXT_mesh_gpu_instancing"]
        self.assertIn("gpu-instancing", self.codes(self.inspect(b)))

    def test_no_instancing_no_warning(self) -> None:
        self.assertNotIn("gpu-instancing", self.codes(self.inspect(quad_builder())))

    def test_compressed_geometry_for_each_extension(self) -> None:
        for name in ("KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_texture_basisu",
                     "KHR_mesh_quantization"):
            with self.subTest(extension=name):
                b = quad_builder()
                b.use_extension(name)
                f = self.finding(self.inspect(b), "compressed-geometry")
                self.assertEqual(f["severity"], "WARN")
                self.assertIn(name, f["message"])

    def test_compressed_geometry_lists_every_extension_found(self) -> None:
        b = quad_builder()
        b.use_extension("KHR_draco_mesh_compression", required=True)
        b.use_extension("EXT_meshopt_compression")
        message = self.finding(self.inspect(b), "compressed-geometry")["message"]
        self.assertIn("KHR_draco_mesh_compression", message)
        self.assertIn("EXT_meshopt_compression", message)

    def test_unrelated_extensions_do_not_trigger_compressed_geometry(self) -> None:
        b = quad_builder()
        b.use_extension("KHR_materials_unlit")
        self.assertNotIn("compressed-geometry", self.codes(self.inspect(b)))

    def test_alpha_texture_on_opaque(self) -> None:
        f = self.finding(self.inspect(material_builder(image=make_png(8, 8, 6))), "alpha-texture-on-opaque")
        self.assertEqual(f["severity"], "WARN")
        self.assertIn("rectangles", f["message"])

    def test_alpha_texture_on_opaque_when_mode_omitted_or_explicit(self) -> None:
        for extra in ({}, {"alphaMode": "OPAQUE"}):
            with self.subTest(extra=extra):
                report = self.inspect(material_builder(image=make_png(8, 8, 4), **extra))
                self.assertIn("alpha-texture-on-opaque", self.codes(report))

    def test_alpha_texture_via_trns_chunk_counts(self) -> None:
        report = self.inspect(material_builder(image=make_png(8, 8, 2, trns=True)))
        self.assertIn("alpha-texture-on-opaque", self.codes(report))

    def test_no_alpha_texture_warning_when_safe(self) -> None:
        cases = {
            "rgb png": material_builder(image=make_png(8, 8, 2)),
            "jpeg": material_builder(image=make_jpeg(8, 8), mime="image/jpeg"),
            "mask": material_builder(image=make_png(8, 8, 6), alphaMode="MASK"),
            "blend": material_builder(image=make_png(8, 8, 6), alphaMode="BLEND"),
            "no texture": material_builder(),
        }
        for label, builder in cases.items():
            with self.subTest(case=label):
                self.assertNotIn("alpha-texture-on-opaque", self.codes(self.inspect(builder)))

    def test_alpha_in_non_base_color_slot_is_fine(self) -> None:
        b = GlbBuilder()
        tex = b.add_texture(b.add_image(make_png(8, 8, 6)))
        b.add_material(emissiveTexture={"index": tex})
        self.assertNotIn("alpha-texture-on-opaque", self.codes(self.inspect(b)))

    def test_blend_alpha(self) -> None:
        report = self.inspect(material_builder(alphaMode="BLEND", doubleSided=True))
        f = self.finding(report, "blend-alpha")
        self.assertEqual(f["severity"], "WARN")
        self.assertIn("MASK", f["message"])
        self.assertNotIn("single-sided-cutout", self.codes(report))

    def test_single_sided_cutout(self) -> None:
        for mode in ("MASK", "BLEND"):
            with self.subTest(mode=mode):
                f = self.finding(self.inspect(material_builder(alphaMode=mode)), "single-sided-cutout")
                self.assertEqual(f["severity"], "WARN")

    def test_no_single_sided_warning_when_double_sided_or_opaque(self) -> None:
        for extra in ({"alphaMode": "MASK", "doubleSided": True}, {"alphaMode": "OPAQUE"}, {}):
            with self.subTest(extra=extra):
                self.assertNotIn("single-sided-cutout", self.codes(self.inspect(material_builder(**extra))))

    def test_texture_over_1k(self) -> None:
        for data, mime in ((make_png(1025, 4), "image/png"), (make_png(4, 2048), "image/png"),
                           (make_jpeg(3000, 10), "image/jpeg")):
            with self.subTest(mime=mime):
                f = self.finding(self.inspect(material_builder(image=data, mime=mime)), "texture-over-1k")
                self.assertEqual(f["severity"], "WARN")

    def test_texture_at_1024_is_fine(self) -> None:
        report = self.inspect(material_builder(image=make_png(1024, 4)))
        self.assertNotIn("texture-over-1k", self.codes(report))

    def test_base_not_at_origin(self) -> None:
        for y in (0.5, -0.02, 0.016, -3.0):
            with self.subTest(y=y):
                f = self.finding(self.inspect(quad_builder(translation=[0, y, 0])), "base-not-at-origin")
                self.assertEqual(f["severity"], "WARN")

    def test_base_at_origin_within_tolerance(self) -> None:
        # Height is 1 m, so the tolerance is 0.005 + 1% of 1 = 0.015 m.
        for y in (0.0, 0.005, -0.01, 0.014):
            with self.subTest(y=y):
                report = self.inspect(quad_builder(translation=[0, y, 0]))
                self.assertNotIn("base-not-at-origin", self.codes(report))

    def test_base_tolerance_scales_with_height(self) -> None:
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([quad_primitive(b, size=(1, 10, 0))]), translation=[0, 0.1, 0])
        self.assertNotIn("base-not-at-origin", self.codes(self.inspect(b)))  # 0.1 <= 0.005 + 0.1
        b.doc["nodes"][0]["translation"] = [0, 0.11, 0]
        self.assertIn("base-not-at-origin", self.codes(self.inspect(b)))

    def test_alpha_cutout_info(self) -> None:
        f = self.finding(self.inspect(material_builder(alphaMode="MASK", doubleSided=True)), "alpha-cutout")
        self.assertEqual(f["severity"], "INFO")
        self.assertIn("rectangles", f["message"])

    def test_texture_memory_info_always_present(self) -> None:
        for builder in (quad_builder(), material_builder(image=make_png(64, 64))):
            report = self.inspect(builder)
            f = self.finding(report, "texture-memory")
            self.assertEqual(f["severity"], "INFO")
            self.assertIn("MB", f["message"])

    def test_texture_memory_message_mentions_mipmaps(self) -> None:
        message = self.finding(self.inspect(material_builder(image=make_png(64, 64))), "texture-memory")["message"]
        self.assertIn("third", message)

    def test_clean_plant_has_only_the_memory_info(self) -> None:
        report = self.inspect(material_builder(image=make_png(64, 64, 2), alphaMode="OPAQUE"))
        self.assertEqual(self.codes(report), {"texture-memory"})

    def test_every_code_and_severity(self) -> None:
        b = GlbBuilder()
        alpha_tex = b.add_texture(b.add_image(make_png(1025, 4, 6)))
        b.add_external_image("leaf.png")
        opaque = b.add_material(pbrMetallicRoughness={"baseColorTexture": {"index": alpha_tex}})
        blend = b.add_material(alphaMode="BLEND")
        mask = b.add_material(alphaMode="MASK")
        translation = b.add_vectors([(0, 0, 0), (1, 0, 0)], "VEC3")
        mesh = b.add_mesh([quad_primitive(b, material=m) for m in (opaque, blend, mask)])
        b.add_node(mesh=mesh, translation=[0, 2, 0],
                   extensions={"EXT_mesh_gpu_instancing": {"attributes": {"TRANSLATION": translation}}})
        b.use_extension("EXT_mesh_gpu_instancing")
        b.use_extension("KHR_draco_mesh_compression")
        report = self.inspect(b)
        severities = {f["code"]: f["severity"] for f in report["findings"]}
        self.assertEqual(severities, {
            "external-texture": "ERROR", "gpu-instancing": "WARN", "compressed-geometry": "WARN",
            "alpha-texture-on-opaque": "WARN", "blend-alpha": "WARN", "single-sided-cutout": "WARN",
            "texture-over-1k": "WARN", "base-not-at-origin": "WARN", "alpha-cutout": "INFO",
            "texture-memory": "INFO",
        })
        self.assertEqual(set(severities), ALL_CODES)

    def test_findings_are_sorted_errors_first(self) -> None:
        b = GlbBuilder()
        b.add_external_image("leaf.png")
        b.add_material(alphaMode="BLEND")
        order = [f["severity"] for f in self.inspect(b)["findings"]]
        self.assertEqual(order, sorted(order, key=["ERROR", "WARN", "INFO"].index))
        self.assertEqual(order[0], "ERROR")
        self.assertEqual(order[-1], "INFO")

    def test_finding_messages_are_plain_sentences(self) -> None:
        report = self.inspect(instanced_builder([(0, 0, 0)]))
        for f in report["findings"]:
            self.assertEqual(set(f), {"code", "severity", "message"})
            self.assertGreater(len(f["message"]), 20)


class OutputTests(InspectTestCase):
    def test_text_output_has_sections_and_findings(self) -> None:
        path = self.write(instanced_builder([(0, 0, 0), (1, 0, 0)]).build())
        code, out, _ = self.run_main(str(path))
        self.assertEqual(code, 0)
        for heading in ("File", "Extensions", "Geometry", "Bounds (meters)", "Materials", "Images",
                        "Texture memory estimate", "Findings"):
            self.assertRegex(out, rf"(?m)^{re.escape(heading)}$")
        self.assertIn("[WARN] gpu-instancing:", out)
        self.assertIn("[INFO] texture-memory:", out)
        self.assertIn("Width (X)", out)
        self.assertIn("Height (Y)", out)
        self.assertIn("Depth (Z)", out)
        self.assertIn("EXT_mesh_gpu_instancing", out)

    def test_text_output_describes_materials_and_images(self) -> None:
        path = self.write(material_builder(image=make_png(32, 16, 6), name="leaf", alphaMode="MASK",
                                           doubleSided=True).build())
        _, out, _ = self.run_main(str(path))
        self.assertIn("alphaMode: MASK (cutoff 0.5)", out)
        self.assertIn("baseColor texture -> image #0", out)
        self.assertIn("32x16", out)
        self.assertIn("alpha: yes", out)

    def test_exit_zero_with_only_warnings_and_info(self) -> None:
        path = self.write(material_builder(alphaMode="BLEND").build())
        self.assertEqual(self.run_main(str(path))[0], 0)

    def test_exit_one_on_error_finding(self) -> None:
        b = GlbBuilder()
        b.add_external_image("leaf.png")
        code, out, _ = self.run_main(str(self.write(b.build())))
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] external-texture:", out)

    def test_json_output_is_a_list_of_per_file_dicts(self) -> None:
        path = self.write(material_builder(image=make_png(8, 8, 6), alphaMode="MASK").build())
        code, out, _ = self.run_main(str(path), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        report = data[0]
        for key in ("path", "file", "extensions", "geometry", "bounds", "materials", "images",
                    "textureMemory", "findings"):
            self.assertIn(key, report)
        self.assertEqual(report["path"], str(path))
        self.assertEqual({f["code"] for f in report["findings"]} >= {"alpha-cutout", "texture-memory"}, True)
        for f in report["findings"]:
            self.assertEqual(set(f), {"code", "severity", "message"})

    def test_json_is_strict_json(self) -> None:
        path = self.write(quad_builder().build())
        _, out, _ = self.run_main(str(path), "--json")
        json.loads(out, parse_constant=lambda name: self.fail(f"non-standard JSON constant {name}"))

    def test_multiple_files(self) -> None:
        first = self.write(quad_builder().build(), "a.glb")
        second = self.write(material_builder(alphaMode="BLEND").build(), "b.glb")
        code, out, _ = self.run_main(str(first), str(second), "--json")
        self.assertEqual(code, 0)
        self.assertEqual([r["path"] for r in json.loads(out)], [str(first), str(second)])
        _, text, _ = self.run_main(str(first), str(second))
        self.assertIn(f"=== {first} ===", text)
        self.assertIn(f"=== {second} ===", text)

    def test_bad_file_among_good_ones(self) -> None:
        good = self.write(quad_builder().build(), "good.glb")
        bad = self.write(b"not a glb at all, sorry", "bad.glb")
        code, out, err = self.run_main(str(good), str(bad))
        self.assertEqual(code, 1)
        self.assertIn("good.glb", out)
        self.assertIn("bad.glb", err)
        code, out, _ = self.run_main(str(good), str(bad), "--json")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("findings", data[0])
        self.assertEqual(set(data[1]), {"path", "error"})

    def test_script_runs_as_a_program(self) -> None:
        path = self.write(quad_builder().build())
        result = subprocess.run([sys.executable, str(SCRIPT), str(path), "--json"], capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]["geometry"]["triangleCount"], 2)
        self.assertEqual(SCRIPT.read_text().splitlines()[0], "#!/usr/bin/env python3")

    def test_script_exit_code_for_bad_input(self) -> None:
        path = self.write(b"garbage")
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a GLB file", result.stderr)


class MatrixMathTests(unittest.TestCase):
    def test_identity_product(self) -> None:
        m = [1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0, 10, 11, 12, 1]
        self.assertEqual(inspect_glb.mat_mul(inspect_glb.IDENTITY, m), [float(v) for v in m])
        self.assertEqual(inspect_glb.mat_mul(m, inspect_glb.IDENTITY), [float(v) for v in m])

    def test_translation_of_child_is_scaled_by_parent(self) -> None:
        parent = inspect_glb.trs_matrix((0, 0, 0), (0, 0, 0, 1), (2, 2, 2))
        child = inspect_glb.trs_matrix((1, 0, 0), (0, 0, 0, 1), (1, 1, 1))
        world = inspect_glb.mat_mul(parent, child)
        self.assertEqual(world[12:15], [2.0, 0.0, 0.0])

    def test_zero_quaternion_falls_back_to_identity_rotation(self) -> None:
        m = inspect_glb.trs_matrix((0, 0, 0), (0, 0, 0, 0), (1, 1, 1))
        self.assertEqual(m, inspect_glb.IDENTITY)


if __name__ == "__main__":
    unittest.main()
