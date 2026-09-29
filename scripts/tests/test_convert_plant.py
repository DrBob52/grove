"""Tests for scripts/convert_plant.py.

Most tests run everywhere: the check logic works on plain data, and the command line
flow is tested with the converter and the USD checks replaced. The last class does real
conversions and is skipped unless usd-core and numpy are installed.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

import helpers  # first: it puts scripts/ on sys.path
import check_plant_assets
import convert_plant as cp
from convert_plant import Check, MaterialFacts, Outcome, PackageInfo, TextureFacts, UsdFacts
from helpers import GlbBuilder, make_png, write_usdz

SCRIPT = helpers.SCRIPTS_DIR / "convert_plant.py"
HAVE_USD = not cp.missing_packages()


def statuses(checks: list[Check]) -> list[str]:
    return [c.status for c in checks]


def usd_facts(**changes: Any) -> UsdFacts:
    """A healthy plant scene; keyword arguments replace fields."""
    fields: dict[str, Any] = {
        "up_axis": "Y", "meters_per_unit": 1.0, "bounds": (0.0, 0.45), "triangles": 290, "textures": [],
        "materials": {"Bark": MaterialFacts(True, None, False), "Leaf": MaterialFacts(True, 0.5, True)},
    }
    fields.update(changes)
    return UsdFacts(**fields)


def glb_material(name: str | None, mode: str, index: int = 1) -> dict[str, Any]:
    """The dict the inspector reports for one material."""
    return {"index": index, "name": name, "alphaMode": mode, "alphaCutoff": 0.5 if mode == "MASK" else None,
            "doubleSided": True, "textures": {}}


class NamingTests(unittest.TestCase):
    def test_usd_material_names_follow_usdzconvert(self) -> None:
        cases = [("Leaf", 1, "Leaf"), ("Leaf Material", 1, "Leaf_Material"), ("leaf.001", 2, "leaf_001"),
                 ("3d-leaf", 0, "_3d_leaf"), (None, 4, "material_4"), ("", 0, "material_0")]
        for name, index, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(cp.usd_material_name(name, index), expected)

    def test_check_lines_show_their_status_name_and_reason(self) -> None:
        self.assertEqual(str(Check("PASS", "Up axis", "Y")), "PASS  Up axis: Y")
        self.assertEqual(str(Check("FAIL", "Units", "wrong")), "FAIL  Units: wrong")


class StageCheckTests(unittest.TestCase):
    def test_y_up_and_meters_pass(self) -> None:
        self.assertEqual(statuses(cp.check_stage(usd_facts())), ["PASS", "PASS"])

    def test_z_up_fails(self) -> None:
        checks = cp.check_stage(usd_facts(up_axis="Z"))
        self.assertEqual(statuses(checks), ["FAIL", "PASS"])
        self.assertIn("Z-up", checks[0].detail)

    def test_centimeters_fail(self) -> None:
        checks = cp.check_stage(usd_facts(meters_per_unit=0.01))
        self.assertEqual(statuses(checks), ["PASS", "FAIL"])
        self.assertIn("0.01", checks[1].detail)


class BoundsCheckTests(unittest.TestCase):
    def check(self, bounds: tuple[float, float] | None, glb_height: float | None) -> list[Check]:
        return cp.check_bounds(usd_facts(bounds=bounds), glb_height)

    def test_base_at_zero_and_matching_height_pass(self) -> None:
        self.assertEqual(statuses(self.check((0.0, 0.45), 0.45)), ["PASS", "PASS"])

    def test_base_within_five_millimeters_passes(self) -> None:
        self.assertEqual(statuses(self.check((-0.005, 0.45), 0.45))[0], "PASS")
        self.assertEqual(statuses(self.check((0.0049, 0.45), 0.45))[0], "PASS")

    def test_floating_or_sunken_base_fails(self) -> None:
        for min_y in (0.006, -0.02, 0.3):
            with self.subTest(min_y=min_y):
                checks = self.check((min_y, 0.45), 0.45)
                self.assertEqual(statuses(checks), ["FAIL", "PASS"])
                self.assertIn("float or sink", checks[0].detail)

    def test_height_within_two_percent_passes(self) -> None:
        self.assertEqual(statuses(self.check((0.0, 0.4589), 0.45))[1], "PASS")
        self.assertEqual(statuses(self.check((0.0, 0.4411), 0.45))[1], "PASS")

    def test_height_more_than_two_percent_off_fails(self) -> None:
        for height in (0.4591, 0.4409, 0.0045, 45.0):
            with self.subTest(height=height):
                self.assertEqual(statuses(self.check((0.0, height), 0.45))[1], "FAIL")

    def test_missing_glb_height_is_a_warning_not_a_failure(self) -> None:
        self.assertEqual(statuses(self.check((0.0, 0.45), None)), ["PASS", "WARN"])

    def test_no_geometry_fails(self) -> None:
        self.assertEqual(statuses(self.check(None, 0.45)), ["FAIL"])


class LeafMaterialCheckTests(unittest.TestCase):
    def check(self, glb_materials: list[dict[str, Any]], **changes: Any) -> list[Check]:
        return cp.check_leaf_materials(glb_materials, usd_facts(**changes))

    def test_no_transparent_materials_means_nothing_to_check(self) -> None:
        checks = self.check([glb_material("Bark", "OPAQUE")])
        self.assertEqual(statuses(checks), ["PASS"])

    def test_mask_with_threshold_and_connected_opacity_passes(self) -> None:
        checks = self.check([glb_material("Leaf", "MASK")])
        self.assertEqual(statuses(checks), ["PASS"])
        self.assertIn("opacityThreshold is 0.5", checks[0].detail)

    def test_mask_without_threshold_fails(self) -> None:
        for threshold in (None, 0.0):
            with self.subTest(threshold=threshold):
                leaf = {"Leaf": MaterialFacts(True, threshold, True)}
                checks = self.check([glb_material("Leaf", "MASK")], materials=leaf)
                self.assertEqual(statuses(checks), ["FAIL"])
                self.assertIn("opacityThreshold", checks[0].detail)

    def test_mask_with_unconnected_opacity_fails(self) -> None:
        leaf = {"Leaf": MaterialFacts(True, 0.5, False)}
        checks = self.check([glb_material("Leaf", "MASK")], materials=leaf)
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("not connected", checks[0].detail)

    def test_mask_missing_both_lists_both_problems(self) -> None:
        checks = self.check([glb_material("Leaf", "MASK")], materials={"Leaf": MaterialFacts(True, None, False)})
        self.assertIn("opacityThreshold", checks[0].detail)
        self.assertIn("not connected", checks[0].detail)

    def test_blend_with_connected_opacity_passes_with_a_flicker_warning(self) -> None:
        leaf = {"Leaf": MaterialFacts(True, None, True)}
        checks = self.check([glb_material("Leaf", "BLEND")], materials=leaf)
        self.assertEqual(statuses(checks), ["PASS", "WARN"])
        self.assertIn("flicker", checks[1].detail)

    def test_blend_does_not_need_a_threshold_but_needs_opacity(self) -> None:
        leaf = {"Leaf": MaterialFacts(True, None, False)}
        checks = self.check([glb_material("Leaf", "BLEND")], materials=leaf)
        self.assertEqual(statuses(checks), ["FAIL"])

    def test_material_is_matched_by_usd_name(self) -> None:
        materials = {"Leaf_Material": MaterialFacts(True, 0.5, True), "material_3": MaterialFacts(True, 0.5, True)}
        glb = [glb_material("Leaf Material", "MASK", 1), glb_material(None, "MASK", 3)]
        self.assertEqual(statuses(self.check(glb, materials=materials)), ["PASS", "PASS"])

    def test_material_missing_from_the_usd_fails_and_lists_what_exists(self) -> None:
        checks = self.check([glb_material("Fern", "MASK")])
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("Bark, Leaf", checks[0].detail)

    def test_material_without_a_preview_surface_fails(self) -> None:
        checks = self.check([glb_material("Leaf", "MASK")], materials={"Leaf": MaterialFacts(False, None, False)})
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("UsdPreviewSurface", checks[0].detail)

    def test_every_transparent_material_is_checked(self) -> None:
        materials = {"A": MaterialFacts(True, 0.5, True), "B": MaterialFacts(True, 0.5, False)}
        glb = [glb_material("A", "MASK", 0), glb_material("B", "MASK", 1), glb_material("C", "OPAQUE", 2)]
        self.assertEqual(statuses(self.check(glb, materials=materials)), ["PASS", "FAIL"])


class TextureCheckTests(unittest.TestCase):
    PACKAGE = PackageInfo(1000, ["plant.usdc", "0/leaf.png"], [("0/leaf.png", 256)])

    def check(self, *textures: TextureFacts) -> list[Check]:
        return cp.check_textures(usd_facts(textures=list(textures)), self.PACKAGE)

    def test_no_textures_pass(self) -> None:
        self.assertEqual(statuses(self.check()), ["PASS"])

    def test_texture_resolving_inside_the_package_passes(self) -> None:
        good = TextureFacts("Leaf", "0/leaf.png", "/tmp/plant.usdz[0/leaf.png]")
        self.assertEqual(statuses(self.check(good, good)), ["PASS"])

    def test_dot_slash_paths_inside_the_package_pass(self) -> None:
        self.assertEqual(statuses(self.check(TextureFacts("Leaf", "./0/leaf.png", "/tmp/a.usdz[./0/leaf.png]"))),
                         ["PASS"])

    def test_bad_links_fail_one_line_each(self) -> None:
        cases = {
            "no image file": TextureFacts("Leaf", "", ""),
            "cannot find": TextureFacts("Leaf", "gone.png", ""),
            "outside the package": TextureFacts("Leaf", "/Users/me/leaf.png", "/Users/me/leaf.png"),
            "not in the package": TextureFacts("Leaf", "0/other.png", "/tmp/plant.usdz[0/other.png]"),
        }
        for words, texture in cases.items():
            with self.subTest(words):
                checks = self.check(texture)
                self.assertEqual(statuses(checks), ["FAIL"])
                self.assertIn(words, checks[0].detail)
        self.assertEqual(statuses(self.check(*cases.values())), ["FAIL"] * 4)


class TriangleCheckTests(unittest.TestCase):
    def test_equal_counts_pass(self) -> None:
        checks = cp.check_triangles(290, 290)
        self.assertEqual(statuses(checks), ["PASS"])
        self.assertIn("290 triangles in the USDZ, 290 in the GLB", checks[0].detail)

    def test_within_one_percent_passes(self) -> None:
        self.assertEqual(statuses(cp.check_triangles(1010, 1000)), ["PASS"])
        self.assertEqual(statuses(cp.check_triangles(990, 1000)), ["PASS"])

    def test_more_than_one_percent_apart_fails_and_reports_both_counts(self) -> None:
        for usd in (1011, 989, 500):
            with self.subTest(usd=usd):
                checks = cp.check_triangles(usd, 1000)
                self.assertEqual(statuses(checks), ["FAIL"])
                self.assertIn(f"{usd:,} triangles in the USDZ, 1,000 in the GLB", checks[0].detail)

    def test_empty_usd_fails(self) -> None:
        self.assertEqual(statuses(cp.check_triangles(0, 100)), ["FAIL"])
        self.assertEqual(statuses(cp.check_triangles(0, 0)), ["FAIL"])


class PackagingCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def package(self, entries: list[tuple[str, bytes]] | None = None,
                compression: int = zipfile.ZIP_STORED) -> Path:
        return write_usdz(self.dir / "plant.usdz", entries, compression)

    def check(self, path: Path) -> list[Check]:
        return cp.check_packaging(path, cp.read_package(path))

    def test_a_good_package_passes_every_rule(self) -> None:
        path = self.package([("plant.usdc", b"PXR-USDC"), ("0/leaf.png", make_png(256, 256))])
        checks = self.check(path)
        self.assertEqual(statuses(checks), ["PASS"] * 4)
        self.assertEqual([c.name for c in checks],
                         ["Stored entries", "USD file first", "Size budget", "Texture budget"])

    def test_read_package_lists_entries_and_texture_sizes(self) -> None:
        path = self.package([("plant.usdc", b"PXR-USDC"), ("0/a.png", make_png(64, 32)),
                             ("0/b.png", make_png(16, 128, 6))])
        package = cp.read_package(path)
        assert package is not None
        self.assertEqual(package.names, ["plant.usdc", "0/a.png", "0/b.png"])
        self.assertEqual(package.textures, [("0/a.png", 64), ("0/b.png", 128)])
        self.assertEqual(package.size_bytes, path.stat().st_size)

    def test_compressed_entries_fail(self) -> None:
        checks = self.check(self.package(compression=zipfile.ZIP_DEFLATED))
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("uncompressed", checks[0].detail)

    def test_usd_file_not_first_fails(self) -> None:
        checks = self.check(self.package([("0/leaf.png", make_png(8, 8)), ("plant.usdc", b"x")]))
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("first entry", checks[0].detail)

    def test_texture_over_1024_fails(self) -> None:
        checks = self.check(self.package([("plant.usdc", b"x"), ("0/big.png", make_png(2048, 512))]))
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("2048x512", checks[0].detail)

    def test_texture_of_exactly_1024_passes(self) -> None:
        checks = self.check(self.package([("plant.usdc", b"x"), ("0/ok.png", make_png(1024, 1024))]))
        self.assertEqual(statuses(checks), ["PASS"] * 4)

    def test_oversized_package_fails(self) -> None:
        path = self.package()
        with mock.patch.object(check_plant_assets, "MAX_USDZ_BYTES", 100):
            checks = self.check(path)
        self.assertEqual(statuses(checks), ["FAIL"])
        self.assertIn("over the", checks[0].detail)

    def test_unreadable_texture_size_warns_but_does_not_fail(self) -> None:
        checks = self.check(self.package([("plant.usdc", b"x"), ("0/odd.png", b"not a png")]))
        self.assertEqual(statuses(checks), ["WARN"] + ["PASS"] * 4)

    def test_not_a_zip_fails(self) -> None:
        path = self.dir / "plant.usdz"
        path.write_bytes(b"this is not a zip")
        self.assertIsNone(cp.read_package(path))
        self.assertEqual(statuses(self.check(path)), ["FAIL"])

    def test_missing_file_fails(self) -> None:
        self.assertEqual(statuses(self.check(self.dir / "gone.usdz")), ["FAIL"])


class SummaryTests(unittest.TestCase):
    def test_summary_reports_size_triangles_textures_and_materials(self) -> None:
        package = PackageInfo(2_345_678, ["a.usdc"], [("0/a.png", 1024), ("0/b.png", 256)])
        lines = cp.summary_lines(package, usd_facts(triangles=12345))
        self.assertEqual(lines, ["File size:  2.35 MB (limit 8 MB)", "Triangles:  12,345",
                                 "Textures:   2 (largest 1024 px)", "Materials:  2"])

    def test_summary_without_textures(self) -> None:
        lines = cp.summary_lines(PackageInfo(1000, ["a.usdc"]), usd_facts(materials={}))
        self.assertEqual(lines[2:], ["Textures:   0", "Materials:  0"])


class FlowTestCase(unittest.TestCase):
    """Runs cp.main in a temporary repo root with the USD packages reported as present."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write_glb(self, plant_id: str, data: bytes) -> Path:
        path = self.root / "Assets" / "Plants" / "source" / f"{plant_id}.glb"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_main(self, *argv: str, missing: tuple[str, ...] = ()) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.object(cp, "missing_packages", return_value=list(missing)),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            code = cp.main([*argv, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()


def simple_glb(**material: Any) -> bytes:
    """A one-quad GLB with one material and no textures."""
    b = GlbBuilder()
    mat = b.add_material(**material)
    b.add_node(mesh=b.add_mesh([helpers.quad_primitive(b, material=mat, size=(0.2, 0.4, 0.0))]))
    return b.build()


def fake_convert(glb: Path, usdz: Path) -> list[str]:
    usdz.parent.mkdir(parents=True, exist_ok=True)
    usdz.write_bytes(b"fake usdz")
    return []


def passing_outcome(height: float | None = 0.4467, *checks: Check) -> Outcome:
    return Outcome(list(checks) or [Check("PASS", "Up axis", "Y")], ["Triangles:  4"], height)


class InputTests(FlowTestCase):
    def test_invalid_ids_exit_1_before_the_setup_check(self) -> None:
        for bad in ("fern", "Fern-a-01", "fern-a-1", "fern_a_01", "fern-a-01.glb", "../evil-01", ""):
            with self.subTest(bad):
                code, _out, err = self.run_main(bad, missing=("usd-core",))
                self.assertEqual(code, 1)
                self.assertIn("not a valid plant ID", err)

    def test_id_rule_is_the_one_check_plant_assets_uses(self) -> None:
        self.assertIs(cp.ID_PATTERN, check_plant_assets.ID_PATTERN)

    def test_missing_converter_packages_print_the_setup_commands_and_exit_2(self) -> None:
        code, out, err = self.run_main("fern-a-01", missing=("usd-core (pxr)", "numpy"))
        self.assertEqual(code, 2)
        for text in ("cannot import: usd-core (pxr), numpy", "python3 -m venv .venv",
                     ".venv/bin/pip install -r scripts/requirements-convert.txt", "source .venv/bin/activate"):
            self.assertIn(text, err)
        self.assertEqual(out, "")

    def test_setup_help_points_at_an_existing_venv(self) -> None:
        (self.root / ".venv").mkdir()
        code, _out, err = self.run_main("fern-a-01", missing=("usd-core",))
        self.assertEqual(code, 2)
        self.assertIn("A .venv folder already exists", err)
        self.assertIn("source .venv/bin/activate", err)
        self.assertIn(".venv/bin/pip install -r scripts/requirements-convert.txt", err)

    def test_missing_glb_exits_1_and_names_the_file(self) -> None:
        code, _out, err = self.run_main("fern-a-01")
        self.assertEqual(code, 1)
        self.assertIn("Assets/Plants/source/fern-a-01.glb does not exist", err)

    def test_file_that_is_not_a_glb_exits_1(self) -> None:
        self.write_glb("fern-a-01", b"definitely not a glb")
        code, _out, err = self.run_main("fern-a-01")
        self.assertEqual(code, 1)
        self.assertIn("not a usable GLB", err)


class InspectorStepTests(FlowTestCase):
    def test_inspector_errors_print_findings_and_stop_before_converting(self) -> None:
        b = GlbBuilder()
        image = b.add_external_image("leaf.png")
        mat = b.add_material(pbrMetallicRoughness={"baseColorTexture": {"index": b.add_texture(image)}})
        b.add_node(mesh=b.add_mesh([helpers.quad_primitive(b, material=mat)]))
        self.write_glb("fern-a-01", b.build())
        with mock.patch.object(cp, "run_converter") as converter:
            code, out, err = self.run_main("fern-a-01")
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] external-texture", out)
        self.assertIn("inspector found 1 error(s)", err)
        converter.assert_not_called()

    def test_inspector_warnings_print_and_the_run_continues(self) -> None:
        self.write_glb("fern-a-01", simple_glb(alphaMode="BLEND", doubleSided=True))
        with mock.patch.object(cp, "run_converter", side_effect=cp.ConvertError("stop here")) as converter:
            code, out, err = self.run_main("fern-a-01")
        self.assertIn("[WARN] blend-alpha", out)
        converter.assert_called_once()
        self.assertEqual(code, 1)
        self.assertIn("stop here", err)


class ConverterStepTests(FlowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_glb("fern-a-01", simple_glb(doubleSided=True))

    def run_with_fake(self, result: subprocess.CompletedProcess[str]) -> tuple[int, str, str, mock.Mock]:
        with mock.patch.object(cp.subprocess, "run", return_value=result) as run:
            code, out, err = self.run_main("fern-a-01")
        return code, out, err, run

    def test_converter_failure_prints_both_streams_without_color_codes(self) -> None:
        result = subprocess.CompletedProcess([], 2, stdout="  \x1b[91mError: can't load the input file.\x1b[0m\n",
                                              stderr="Traceback: boom\n")
        code, _out, err, _run = self.run_with_fake(result)
        self.assertEqual(code, 1)
        self.assertIn("exit code 2", err)
        self.assertIn("Traceback: boom", err)
        self.assertIn("Error: can't load the input file.", err)
        self.assertNotIn("\x1b", err)

    def test_converter_warnings_are_shown_when_it_succeeds(self) -> None:
        def write_output(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            fake_convert(Path(command[-2]), Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stderr="", stdout=(
                "Input file: x.glb\n  \x1b[93mWarning: texture 'a.png' was not found.\x1b[0m\n  Node: plant\n"))

        with (mock.patch.object(cp.subprocess, "run", side_effect=write_output),
              mock.patch.object(cp, "run_checks", return_value=passing_outcome())):
            code, out, _err = self.run_main("fern-a-01")
        self.assertEqual(code, 0)
        self.assertIn("usdzconvert: Warning: texture 'a.png' was not found.", out)
        self.assertNotIn("Node: plant", out)

    def test_success_without_an_output_file_fails(self) -> None:
        code, _out, err, _run = self.run_with_fake(subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        self.assertEqual(code, 1)
        self.assertIn("wrote no output file", err)

    def test_a_stale_usdz_is_never_mistaken_for_a_fresh_one(self) -> None:
        stale = self.root / "Assets" / "Plants" / "usdz" / "fern-a-01.usdz"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        code, _out, err, _run = self.run_with_fake(subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        self.assertEqual(code, 1)
        self.assertFalse(stale.exists())

    def test_it_runs_the_vendored_converter_with_this_python(self) -> None:
        _code, _out, _err, run = self.run_with_fake(subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        script = Path(command[command.index("-B") + 1])
        self.assertEqual(script, helpers.SCRIPTS_DIR / "vendor" / "usdzconvert" / "usdzconvert")
        self.assertTrue(script.is_file())
        self.assertEqual(command[-2:], [str(self.root / "Assets" / "Plants" / "source" / "fern-a-01.glb"),
                                        str(self.root / "Assets" / "Plants" / "usdz" / "fern-a-01.usdz")])

    def test_timeout_is_reported_as_a_failure(self) -> None:
        with mock.patch.object(cp.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            code, _out, err = self.run_main("fern-a-01")
        self.assertEqual(code, 1)
        self.assertIn("could not run usdzconvert", err)


class OutcomeTests(FlowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_glb("fern-a-01", simple_glb(doubleSided=True))
        self.installed = self.root / "App" / "Resources" / "Plants" / "fern-a-01.usdz"

    def run_with(self, outcome: Outcome, *argv: str) -> tuple[int, str, str]:
        with (mock.patch.object(cp, "run_converter", side_effect=fake_convert),
              mock.patch.object(cp, "run_checks", return_value=outcome)):
            return self.run_main("fern-a-01", *argv)

    def test_passing_checks_without_install_stage_only(self) -> None:
        code, out, _err = self.run_with(passing_outcome())
        self.assertEqual(code, 0)
        self.assertIn("PASS  Up axis: Y", out)
        self.assertIn("All checks passed", out)
        self.assertIn("--install", out)
        self.assertTrue((self.root / "Assets" / "Plants" / "usdz" / "fern-a-01.usdz").is_file())
        self.assertFalse(self.installed.exists())

    def test_install_copies_the_usdz_and_prints_the_manifest_entry(self) -> None:
        code, out, _err = self.run_with(passing_outcome(0.4467), "--install")
        self.assertEqual(code, 0)
        self.assertEqual(self.installed.read_bytes(), b"fake usdz")
        line = next(line for line in out.splitlines() if line.strip().startswith('{"id"'))
        self.assertEqual(json.loads(line), {"id": "fern-a-01", "displayName": "TODO", "category": "fern",
                                            "heightMeters": 0.45, "thumbnail": "fern-a-01-thumb",
                                            "model": "fern-a-01"})

    def test_install_prints_the_remaining_manual_steps(self) -> None:
        _code, out, _err = self.run_with(passing_outcome(), "--install")
        for text in ("App/Resources/Plants/fern-a-01-thumb.png", "App/Resources/Plants/plants.json",
                     "python3 scripts/check_plant_assets.py", "Assets/Plants/LICENSE-RECORD.md", "xcodegen"):
            self.assertIn(text, out)

    def test_height_is_rounded_to_two_decimals_and_category_is_the_first_segment(self) -> None:
        self.write_glb("succulent-b-02", simple_glb(doubleSided=True))
        with (mock.patch.object(cp, "run_converter", side_effect=fake_convert),
              mock.patch.object(cp, "run_checks", return_value=passing_outcome(0.6249))):
            _code, out, _err = self.run_main("succulent-b-02", "--install")
        entry = json.loads(next(line for line in out.splitlines() if line.strip().startswith('{"id"')))
        self.assertEqual((entry["category"], entry["heightMeters"]), ("succulent", 0.62))

    def test_any_failed_check_exits_1_and_installs_nothing(self) -> None:
        outcome = passing_outcome(0.4, Check("PASS", "Up axis", "Y"), Check("FAIL", "Base at zero", "floats"))
        code, out, _err = self.run_with(outcome, "--install")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  Base at zero: floats", out)
        self.assertIn("1 check(s) failed", out)
        self.assertFalse(self.installed.exists())
        self.assertNotIn('{"id"', out)

    def test_warnings_do_not_stop_an_install(self) -> None:
        code, out, _err = self.run_with(passing_outcome(0.4, Check("WARN", "Leaf", "may flicker")), "--install")
        self.assertEqual(code, 0)
        self.assertIn("WARN  Leaf: may flicker", out)
        self.assertTrue(self.installed.is_file())

    def test_checks_that_could_not_run_still_fail_the_plant(self) -> None:
        with (mock.patch.object(cp, "run_converter", side_effect=fake_convert),
              mock.patch.object(cp, "run_checks", side_effect=cp.ConvertError("the USD could not be opened"))):
            code, _out, err = self.run_main("fern-a-01")
        self.assertEqual(code, 1)
        self.assertIn("could not be opened", err)


class CommandLineTests(unittest.TestCase):
    def test_help_explains_the_tool_and_works_without_usd_installed(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("plant-id", "--install", "--root", "Assets/Plants/source/<plant-id>.glb",
                     "App/Resources/Plants/<plant-id>.usdz", "Y-up", "source .venv/bin/activate", "Exit status"):
            self.assertIn(text, result.stdout)

    def test_script_is_an_executable_python_script(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertTrue(SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3"))

    def test_running_it_with_a_bad_id_exits_1(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "Not A Plant"], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a valid plant ID", result.stderr)

    def test_vendored_converter_and_its_license_are_present(self) -> None:
        vendor = helpers.SCRIPTS_DIR / "vendor" / "usdzconvert"
        self.assertTrue((vendor / "usdzconvert").is_file())
        self.assertIn("Permission is hereby granted", (vendor / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("f32c067", (vendor / "VENDORED.md").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Real conversions
# --------------------------------------------------------------------------

def card(b: GlbBuilder, material: int, width: float, y0: float, x: float = 0.0) -> dict[str, Any]:
    """A 0.4 m tall upright quad with normals and UVs, its bottom edge at y0."""
    positions = [(x - width / 2, y0, 0.0), (x + width / 2, y0, 0.0), (x + width / 2, y0 + 0.4, 0.0),
                 (x - width / 2, y0 + 0.4, 0.0)]
    return {"attributes": {"POSITION": b.add_positions(positions),
                           "NORMAL": b.add_vectors([(0.0, 0.0, 1.0)] * 4, "VEC3"),
                           "TEXCOORD_0": b.add_vectors([(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)], "VEC2")},
            "indices": b.add_indices([0, 1, 2, 0, 2, 3]), "material": material}


def plant_glb(leaves: list[dict[str, Any]], *, lift: float = 0.0, texture: bool = True) -> bytes:
    """A bark strip plus one leaf card per entry of `leaves` (material fields); 0.5 m tall above `lift`."""
    b = GlbBuilder()
    bark = b.add_material(name="Bark", pbrMetallicRoughness={"baseColorFactor": [0.35, 0.25, 0.15, 1.0],
                                                             "metallicFactor": 0.0, "roughnessFactor": 0.9})
    b.add_node(name="Stem", mesh=b.add_mesh([card(b, bark, 0.04, lift)]))
    image = b.add_texture(b.add_image(make_png(64, 64, 6))) if texture else None
    for number, fields in enumerate(leaves):
        pbr: dict[str, Any] = {"metallicFactor": 0.0, "roughnessFactor": 0.7}
        if image is not None:
            pbr["baseColorTexture"] = {"index": image}
        material = b.add_material(pbrMetallicRoughness=pbr, doubleSided=True, **fields)
        b.add_node(name=f"Leaf{number}", mesh=b.add_mesh([card(b, material, 0.2, lift + 0.1, 0.12 * number)]))
    return b.build()


@unittest.skipUnless(HAVE_USD, "needs usd-core and numpy: pip install -r scripts/requirements-convert.txt")
class RealConversionTests(FlowTestCase):
    """Runs the vendored usdzconvert for real, then the real checks."""

    def convert(self, glb: bytes, *argv: str, plant_id: str = "fern-a-01") -> tuple[int, str, str]:
        self.write_glb(plant_id, glb)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cp.main([plant_id, "--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def staged(self, plant_id: str = "fern-a-01") -> Path:
        return self.root / "Assets" / "Plants" / "usdz" / f"{plant_id}.usdz"

    def installed(self, plant_id: str = "fern-a-01") -> Path:
        return self.root / "App" / "Resources" / "Plants" / f"{plant_id}.usdz"

    def test_mask_leaf_converts_passes_every_check_and_installs(self) -> None:
        glb = plant_glb([{"name": "Leaf", "alphaMode": "MASK", "alphaCutoff": 0.4}])
        code, out, err = self.convert(glb)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("FAIL", out)
        self.assertIn("PASS  Leaf material 'Leaf': MASK: opacity comes from the texture's alpha and "
                      "opacityThreshold is 0.4", out)
        self.assertIn("PASS  Triangles: 4 triangles in the USDZ, 4 in the GLB", out)
        self.assertIn("Textures:   1 (largest 64 px)", out)
        self.assertTrue(self.staged().is_file())
        self.assertFalse(self.installed().exists())

        code, out, err = self.convert(glb, "--install")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.installed().read_bytes(), self.staged().read_bytes())
        self.assertIn('"heightMeters": 0.5', out)

    def test_facts_read_from_the_converted_usdz(self) -> None:
        self.convert(plant_glb([{"name": "Leaf", "alphaMode": "MASK", "alphaCutoff": 0.4}]))
        facts = cp.read_usd_facts(self.staged())
        self.assertEqual((facts.up_axis, facts.meters_per_unit, facts.triangles), ("Y", 1.0, 4))
        assert facts.bounds is not None
        self.assertAlmostEqual(facts.bounds[0], 0.0, places=5)
        self.assertAlmostEqual(facts.bounds[1], 0.5, places=5)
        self.assertEqual(set(facts.materials), {"Bark", "Leaf"})
        leaf = facts.materials["Leaf"]
        self.assertTrue(leaf.has_preview_surface and leaf.opacity_connected)
        self.assertAlmostEqual(leaf.opacity_threshold or 0.0, 0.4, places=5)
        self.assertFalse(facts.materials["Bark"].opacity_connected)
        self.assertEqual(len(facts.textures), 1)
        self.assertTrue(facts.textures[0].resolved_path.endswith(".usdz[0/texgen_0.png]"))

    def test_blend_leaf_passes_with_a_flicker_warning(self) -> None:
        code, out, err = self.convert(plant_glb([{"name": "Leaf", "alphaMode": "BLEND"}]))
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS  Leaf material 'Leaf': BLEND", out)
        self.assertRegex(out, r"WARN  Leaf material 'Leaf': blended leaves may flicker")

    def test_unnamed_and_oddly_named_materials_are_found_by_their_usd_names(self) -> None:
        glb = plant_glb([{"alphaMode": "MASK"}, {"name": "Leaf Material", "alphaMode": "MASK"}])
        code, out, err = self.convert(glb)
        self.assertEqual(code, 0, out + err)
        self.assertIn("PASS  Leaf material 'material_1'", out)
        self.assertIn("PASS  Leaf material 'Leaf_Material'", out)

    def test_mask_material_without_a_texture_fails_and_is_not_installed(self) -> None:
        code, out, _err = self.convert(plant_glb([{"name": "Leaf", "alphaMode": "MASK"}], texture=False),
                                       "--install")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL  Leaf material 'Leaf': MASK: .*not connected")
        self.assertIn("1 check(s) failed", out)
        self.assertFalse(self.installed().exists())

    def test_floating_plant_fails_the_base_check(self) -> None:
        code, out, _err = self.convert(plant_glb([{"name": "Leaf", "alphaMode": "MASK"}], lift=0.2), "--install")
        self.assertEqual(code, 1)
        self.assertIn("WARN", out)  # the inspector's base-not-at-origin warning was shown first
        self.assertRegex(out, r"FAIL  Base at zero: the lowest point is at Y = 0\.2000 m")
        self.assertIn("PASS  Height", out)
        self.assertFalse(self.installed().exists())

    def test_external_texture_is_stopped_by_the_inspector(self) -> None:
        b = GlbBuilder()
        image = b.add_external_image("leaf.png")
        mat = b.add_material(name="Leaf", alphaMode="MASK", doubleSided=True,
                             pbrMetallicRoughness={"baseColorTexture": {"index": b.add_texture(image)}})
        b.add_node(mesh=b.add_mesh([card(b, mat, 0.2, 0.0)]))
        code, out, err = self.convert(b.build())
        self.assertEqual(code, 1)
        self.assertIn("[ERROR] external-texture", out)
        self.assertFalse(self.staged().exists())

    def test_converting_again_replaces_the_staged_file(self) -> None:
        glb = plant_glb([{"name": "Leaf", "alphaMode": "MASK"}])
        self.assertEqual(self.convert(glb)[0], 0)
        self.assertEqual(self.convert(glb)[0], 0)

    def test_a_broken_scene_the_inspector_accepts_is_reported_by_the_converter(self) -> None:
        # A valid GLB whose mesh has no POSITION: the inspector passes it, usdzconvert cannot use it.
        b = GlbBuilder()
        b.add_node(mesh=b.add_mesh([{"attributes": {}}]))
        code, out, err = self.convert(b.build())
        self.assertEqual(code, 1, out + err)
        self.assertFalse(self.installed().exists())


if __name__ == "__main__":
    unittest.main()
