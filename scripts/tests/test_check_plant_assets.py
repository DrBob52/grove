"""Tests for scripts/check_plant_assets.py."""

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
from unittest import mock

import helpers
import check_plant_assets as checker

SCRIPT = helpers.SCRIPTS_DIR / "check_plant_assets.py"


def entry(plant_id: str = "fern-a-01", **overrides: object) -> dict:
    """A valid manifest entry, with optional field overrides."""
    data = {
        "id": plant_id,
        "displayName": "Arching Fern",
        "category": "fern",
        "heightMeters": 0.62,
        "thumbnail": f"{plant_id}-thumb",
        "model": plant_id,
    }
    data.update(overrides)
    return data


class CheckerTestCase(unittest.TestCase):
    """Builds a throwaway repo root with App/Resources/Plants/."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plants = self.root / "App" / "Resources" / "Plants"
        self.plants.mkdir(parents=True)

    def write_manifest(self, data: object) -> None:
        (self.plants / "plants.json").write_text(json.dumps(data), encoding="utf-8")

    def add_files(self, plant_id: str, entries: list | None = None, thumb: tuple = (64, 64)) -> None:
        helpers.write_usdz(self.plants / f"{plant_id}.usdz", entries)
        (self.plants / f"{plant_id}-thumb.png").write_bytes(helpers.make_png(*thumb, 6))

    def add_plant(self, plant_id: str = "fern-a-01", **overrides: object) -> None:
        """Manifest entry plus valid files; overrides only change the manifest."""
        manifest_path = self.plants / "plants.json"
        current = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
        current.append(entry(plant_id, **overrides))
        self.write_manifest(current)
        self.add_files(plant_id)

    def run_checker(self, *extra: str) -> tuple[int, list[str]]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main(["--root", str(self.root), *extra])
        return code, out.getvalue().splitlines()

    def assertIssue(self, lines: list[str], level: str, subject: str, fragment: str) -> None:
        wanted = [line for line in lines if line.startswith(f"{level} {subject}: ") and fragment in line]
        self.assertTrue(wanted, f"no '{level} {subject}: ...{fragment}...' line in:\n" + "\n".join(lines))

    def assertNoIssues(self, lines: list[str]) -> None:
        self.assertEqual([l for l in lines if l.startswith(("ERROR", "WARN"))], [])


class HappyPathTests(CheckerTestCase):
    def test_valid_plant_passes(self) -> None:
        self.add_plant()
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertNoIssues(lines)
        self.assertEqual(lines[-1], "1 plants checked, 0 errors, 0 warnings")

    def test_two_plants_with_textures_at_the_limit(self) -> None:
        self.add_plant("fern-a-01")
        self.add_plant("succulent-b-02")
        textures = [("model.usdc", b"x"), ("tex/a.png", helpers.make_png(1024, 4)),
                    ("tex/b.jpg", helpers.make_jpeg(1024, 1024))]
        helpers.write_usdz(self.plants / "succulent-b-02.usdz", textures)
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "2 plants checked, 0 errors, 0 warnings")

    def test_empty_manifest_passes(self) -> None:
        self.write_manifest([])
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["0 plants checked, 0 errors, 0 warnings"])

    def test_constants_match_spec(self) -> None:
        self.assertEqual(checker.MAX_USDZ_BYTES, 8_000_000)
        self.assertEqual(checker.MAX_TOTAL_BYTES, 150_000_000)

    def test_default_root_is_parent_of_scripts(self) -> None:
        self.assertEqual(checker.default_root(), helpers.SCRIPTS_DIR.parent)

    def test_valid_ids(self) -> None:
        for plant_id in ("fern-a-01", "succulent-b-02", "fern-01", "monstera-c-100", "x1-y2-03"):
            with self.subTest(plant_id=plant_id):
                self.assertTrue(checker.ID_PATTERN.fullmatch(plant_id))


class ManifestErrorTests(CheckerTestCase):
    def test_manifest_missing(self) -> None:
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "not found")
        self.assertEqual(lines[-1], "0 plants checked, 1 errors, 0 warnings")

    def test_plants_folder_missing(self) -> None:
        self.plants.rmdir()
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "not found")

    def test_invalid_json(self) -> None:
        (self.plants / "plants.json").write_text("[{oops")
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "not valid JSON")

    def test_not_a_list(self) -> None:
        self.write_manifest({"id": "fern-a-01"})
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "must contain a JSON list")

    def test_entry_not_an_object(self) -> None:
        self.write_manifest(["fern-a-01", 5, None])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "entry #1 is not an object")
        self.assertIssue(lines, "ERROR", "manifest", "entry #3 is not an object")
        self.assertEqual(lines[-1], "0 plants checked, 3 errors, 0 warnings")

    def test_missing_fields(self) -> None:
        for name in ("id", "displayName", "category", "heightMeters", "thumbnail", "model"):
            with self.subTest(field=name):
                data = entry()
                del data[name]
                self.write_manifest([data])
                code, lines = self.run_checker()
                self.assertEqual(code, 1)
                self.assertIssue(lines, "ERROR", "fern-a-01" if name != "id" else "manifest",
                                 f"missing field '{name}'")

    def test_string_fields_must_be_nonempty_strings(self) -> None:
        for name in ("displayName", "category", "thumbnail", "model"):
            for bad in ("", "   ", 7, None, ["x"]):
                with self.subTest(field=name, value=bad):
                    self.write_manifest([entry(**{name: bad})])
                    code, lines = self.run_checker()
                    self.assertEqual(code, 1)
                    self.assertIssue(lines, "ERROR", "fern-a-01", f"field '{name}' must be a non-empty string")

    def test_id_must_be_nonempty_string(self) -> None:
        self.write_manifest([entry(id=12)])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "entry #1: field 'id' must be a non-empty string")

    def test_bad_height_values(self) -> None:
        for bad in ("0.6", True, False, None, 0, -1, 0.0, 30.01, 1e999, [1]):
            with self.subTest(height=bad):
                self.write_manifest([entry(heightMeters=bad)])
                code, lines = self.run_checker()
                self.assertEqual(code, 1)
                self.assertIssue(lines, "ERROR", "fern-a-01", "heightMeters")

    def test_good_height_values(self) -> None:
        for good in (0.01, 1, 30, 29.99):
            with self.subTest(height=good):
                self.add_plant(heightMeters=good)
                code, lines = self.run_checker()
                self.assertEqual(code, 0, lines)
                (self.plants / "plants.json").unlink()

    def test_bad_ids(self) -> None:
        bad_ids = ["Fern-a-01", "fern-a-1", "fern_a_01", "1fern-a-01", "fern-a", "fern--a-01",
                   "fern-a-01-", "-fern-a-01", "fern a-01", "fern-a-01\n", "fern-a-0x", "fer/n-a-01"]
        for bad in bad_ids:
            with self.subTest(plant_id=bad):
                self.write_manifest([entry(bad, thumbnail=f"{bad}-thumb", model=bad)])
                code, lines = self.run_checker()
                self.assertEqual(code, 1)
                self.assertTrue(any("id must be lowercase kebab case" in l for l in lines), lines)

    def test_duplicate_id(self) -> None:
        self.add_plant("fern-a-01")
        self.add_plant("fern-a-01")
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "duplicate id")
        self.assertEqual(sum("duplicate id" in l for l in lines), 1)
        self.assertEqual(lines[-1], "2 plants checked, 1 errors, 0 warnings")

    def test_model_must_equal_id(self) -> None:
        self.add_plant("fern-a-01", model="fern-a-02")
        helpers.write_usdz(self.plants / "fern-a-02.usdz")
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "model 'fern-a-02' must equal the id")

    def test_thumbnail_must_be_id_thumb(self) -> None:
        self.add_plant("fern-a-01", thumbnail="fern-a-01")
        (self.plants / "fern-a-01.png").write_bytes(helpers.make_png(8, 8))
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "thumbnail 'fern-a-01' must be 'fern-a-01-thumb'")

    def test_path_traversal_in_model_is_never_opened(self) -> None:
        self.write_manifest([entry(model="../../evil")])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "must equal the id")
        self.assertFalse(any("missing model" in l for l in lines))

    def test_manifest_level_errors_count_in_summary(self) -> None:
        self.write_manifest([entry(displayName="")])
        _, lines = self.run_checker()
        self.assertRegex(lines[-1], r"^1 plants checked, \d+ errors, \d+ warnings$")


class FileErrorTests(CheckerTestCase):
    def test_missing_usdz(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01.usdz").unlink()
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "missing model file fern-a-01.usdz")

    def test_missing_thumbnail(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01-thumb.png").unlink()
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "missing thumbnail file fern-a-01-thumb.png")

    def test_thumbnail_bad_signature(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01-thumb.png").write_bytes(helpers.make_jpeg(10, 10))
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "not a valid PNG")

    def test_usdz_over_size_limit(self) -> None:
        self.add_plant()
        limit = (self.plants / "fern-a-01.usdz").stat().st_size - 1
        with mock.patch.object(checker, "MAX_USDZ_BYTES", limit):
            code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "byte limit")

    def test_usdz_exactly_at_limit_passes(self) -> None:
        self.add_plant()
        limit = (self.plants / "fern-a-01.usdz").stat().st_size
        with mock.patch.object(checker, "MAX_USDZ_BYTES", limit):
            code, _ = self.run_checker()
        self.assertEqual(code, 0)

    def test_real_size_limit_with_large_file(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz",
                           [("model.usdc", b"\x00" * (checker.MAX_USDZ_BYTES + 1))])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "over the 8,000,000 byte limit")

    def test_usdz_not_a_zip(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01.usdz").write_bytes(b"this is not a zip file at all")
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "not a valid zip archive")

    def test_usdz_compressed_entry(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz", [("model.usdc", b"a" * 500)],
                           compression=zipfile.ZIP_DEFLATED)
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "'model.usdc' is compressed")

    def test_usdz_first_entry_must_be_usd(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz",
                           [("tex/leaf.png", helpers.make_png(8, 8)), ("model.usdc", b"x")])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "first entry is 'tex/leaf.png'")

    def test_usdz_first_entry_accepts_all_usd_flavours(self) -> None:
        for name in ("scene.usdc", "scene.usda", "scene.usd", "SCENE.USDC"):
            with self.subTest(name=name):
                self.add_plant()
                helpers.write_usdz(self.plants / "fern-a-01.usdz", [(name, b"x")])
                code, _ = self.run_checker()
                self.assertEqual(code, 0)
                (self.plants / "plants.json").unlink()

    def test_usdz_empty_archive(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz", [])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "empty archive")

    def test_png_texture_too_large(self) -> None:
        for width, height in ((1025, 4), (4, 1025), (2048, 2)):
            with self.subTest(size=(width, height)):
                self.add_plant()
                helpers.write_usdz(self.plants / "fern-a-01.usdz",
                                   [("model.usdc", b"x"), ("tex/big.png", helpers.make_png(width, height))])
                code, lines = self.run_checker()
                self.assertEqual(code, 1)
                self.assertIssue(lines, "ERROR", "fern-a-01", f"'tex/big.png' is {width}x{height}")
                (self.plants / "plants.json").unlink()

    def test_jpeg_texture_too_large(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz",
                           [("model.usdc", b"x"), ("tex/big.JPG", helpers.make_jpeg(1100, 512))])
        code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "fern-a-01", "'tex/big.JPG' is 1100x512")

    def test_total_size_over_limit(self) -> None:
        self.add_plant()
        total = sum(p.stat().st_size for p in self.plants.iterdir())
        with mock.patch.object(checker, "MAX_TOTAL_BYTES", total - 1):
            code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "byte limit")

    def test_total_size_at_limit_passes(self) -> None:
        self.add_plant()
        total = sum(p.stat().st_size for p in self.plants.iterdir())
        with mock.patch.object(checker, "MAX_TOTAL_BYTES", total):
            code, _ = self.run_checker()
        self.assertEqual(code, 0)

    def test_total_size_counts_all_files_including_subfolders(self) -> None:
        self.add_plant()
        (self.plants / "extras").mkdir()
        (self.plants / "extras" / "blob.bin").write_bytes(b"\x00" * 5000)
        total = sum(p.stat().st_size for p in self.plants.rglob("*") if p.is_file())
        with mock.patch.object(checker, "MAX_TOTAL_BYTES", total - 1):
            code, lines = self.run_checker()
        self.assertEqual(code, 1)
        self.assertIssue(lines, "ERROR", "manifest", "byte limit")


class WarningTests(CheckerTestCase):
    def test_exr_texture_warns_and_names_file(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz",
                           [("model.usdc", b"x"), ("tex/env.exr", b"\x76\x2f\x31\x01")])
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIssue(lines, "WARN", "fern-a-01", "'tex/env.exr'")
        self.assertEqual(lines[-1], "1 plants checked, 0 errors, 1 warnings")

    def test_unreadable_texture_size_warns(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-01.usdz",
                           [("model.usdc", b"x"), ("tex/broken.png", b"not really a png")])
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIssue(lines, "WARN", "fern-a-01", "size of texture 'tex/broken.png' can't be read")

    def test_unknown_manifest_keys_warn(self) -> None:
        self.add_plant(notes="hello", author="me")
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIssue(lines, "WARN", "fern-a-01", "'author', 'notes'")

    def test_orphan_usdz_and_thumbnail_warn(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "fern-a-02.usdz")
        (self.plants / "cactus-a-01-thumb.png").write_bytes(helpers.make_png(8, 8))
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIssue(lines, "WARN", "fern-a-02.usdz", "no matching entry")
        self.assertIssue(lines, "WARN", "cactus-a-01-thumb.png", "no matching entry")
        self.assertEqual(lines[-1], "1 plants checked, 0 errors, 2 warnings")

    def test_referenced_files_are_not_orphans(self) -> None:
        self.add_plant()
        _, lines = self.run_checker()
        self.assertFalse(any("no matching entry" in l for l in lines))

    def test_big_thumbnail_warns(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01-thumb.png").write_bytes(helpers.make_png(1025, 4))
        code, lines = self.run_checker()
        self.assertEqual(code, 0)
        self.assertIssue(lines, "WARN", "fern-a-01", "1025x4")

    def test_thumbnail_at_1024_is_fine(self) -> None:
        self.add_plant()
        (self.plants / "fern-a-01-thumb.png").write_bytes(helpers.make_png(1024, 4))
        _, lines = self.run_checker()
        self.assertNoIssues(lines)

    def test_strict_fails_on_warnings(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "orphan-a-01.usdz")
        self.assertEqual(self.run_checker()[0], 0)
        code, lines = self.run_checker("--strict")
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "1 plants checked, 0 errors, 1 warnings")

    def test_strict_passes_when_clean(self) -> None:
        self.add_plant()
        self.assertEqual(self.run_checker("--strict")[0], 0)

    def test_strict_with_empty_manifest_passes(self) -> None:
        self.write_manifest([])
        self.assertEqual(self.run_checker("--strict")[0], 0)


class OutputFormatTests(CheckerTestCase):
    def test_line_formats(self) -> None:
        self.add_plant()
        helpers.write_usdz(self.plants / "orphan-a-01.usdz")
        (self.plants / "fern-a-01-thumb.png").unlink()
        _, lines = self.run_checker()
        for line in lines[:-1]:
            self.assertRegex(line, r"^(ERROR|WARN) [^ :]+: .+")
        self.assertRegex(lines[-1], r"^\d+ plants checked, \d+ errors, \d+ warnings$")

    def test_no_id_uses_manifest_subject(self) -> None:
        data = entry()
        del data["id"]
        self.write_manifest([data])
        _, lines = self.run_checker()
        self.assertIssue(lines, "ERROR", "manifest", "entry #1: missing field 'id'")


class CommandLineTests(CheckerTestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_script_exit_codes_and_output(self) -> None:
        self.add_plant()
        ok = self.run_script("--root", str(self.root))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(ok.stdout.strip(), "1 plants checked, 0 errors, 0 warnings")
        (self.plants / "fern-a-01.usdz").unlink()
        bad = self.run_script("--root", str(self.root))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ERROR fern-a-01: missing model file", bad.stdout)

    def test_shebang(self) -> None:
        self.assertEqual(SCRIPT.read_text().splitlines()[0], "#!/usr/bin/env python3")


if __name__ == "__main__":
    unittest.main()
