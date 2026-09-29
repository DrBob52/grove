#!/usr/bin/env python3
"""Convert one plant from GLB to USDZ and check the result.

Usage: python3 scripts/convert_plant.py <plant-id> [--install] [--root PATH]

Reads Assets/Plants/source/<plant-id>.glb, runs the GLB inspector, converts the
file with the vendored usdzconvert, writes Assets/Plants/usdz/<plant-id>.usdz
and checks it. With --install, a passing USDZ is copied into the app.
Exits 0 on success, 1 if the plant failed, 2 if the converter is not set up.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import inspect_glb
from check_plant_assets import (CHECKED_IMAGE_SUFFIXES, ID_PATTERN, MAX_TEXTURE_PX, MAX_USDZ_BYTES,
                                PLANTS_DIR, Report, check_usdz, default_root, read_zip_entry_head)
from imageinfo import read_image_info

EXIT_OK, EXIT_FAILED, EXIT_SETUP = 0, 1, 2

VENDORED_CONVERTER = Path(__file__).resolve().parent / "vendor" / "usdzconvert" / "usdzconvert"
SOURCE_DIR = Path("Assets") / "Plants" / "source"
STAGING_DIR = Path("Assets") / "Plants" / "usdz"
CONVERT_TIMEOUT_SECONDS = 600
REQUIRED_MODULES = (("pxr.Usd", "usd-core (pxr)"), ("numpy", "numpy"))

BASE_TOLERANCE_M = 0.005  # the lowest point may sit this far from Y = 0
HEIGHT_TOLERANCE = 0.02  # USD height may differ from the GLB's by this fraction
TRIANGLE_TOLERANCE = 0.01  # USD triangle count may differ from the GLB's by this fraction
MB = 1_000_000
DOCS = "docs/plant-pipeline.md"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TEXTURE_LINK = re.compile(r"^(?P<package>.+)\[(?P<inner>[^\[\]]+)\]$")


class ConvertError(Exception):
    """A problem that stops the run; the message is shown to the user."""


# --------------------------------------------------------------------------
# Check results and the facts the checks look at
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """One line of output: PASS, FAIL or WARN (a note that does not fail the run)."""

    status: str
    name: str
    detail: str

    def __str__(self) -> str:
        return f"{self.status}  {self.name}: {self.detail}"


def passed(name: str, detail: str) -> Check:
    return Check("PASS", name, detail)


def failed(name: str, detail: str) -> Check:
    return Check("FAIL", name, detail)


def note(name: str, detail: str) -> Check:
    return Check("WARN", name, detail)


@dataclass
class PackageInfo:
    """What the zip inside a USDZ holds."""

    size_bytes: int
    names: list[str]
    textures: list[tuple[str, int]] = field(default_factory=list)  # (entry name, longest side in px)


@dataclass
class MaterialFacts:
    """The parts of a USD material the leaf checks care about."""

    has_preview_surface: bool
    opacity_threshold: float | None
    opacity_connected: bool


@dataclass
class TextureFacts:
    """One UsdUVTexture shader's image link."""

    owner: str  # the material it belongs to, or its own name
    asset_path: str  # as written in the file, "" if none
    resolved_path: str  # where USD found it, "" if it did not


@dataclass
class Outcome:
    """Everything the checks produced for one USDZ."""

    checks: list[Check]
    summary: list[str] = field(default_factory=list)
    height_m: float | None = None  # measured from the USD bounds

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if c.status == "FAIL")


@dataclass
class UsdFacts:
    """What the USD scene inside a USDZ says. Sizes are in meters."""

    up_axis: str
    meters_per_unit: float
    bounds: tuple[float, float] | None  # (lowest Y, height), None if there is no geometry
    triangles: int
    materials: dict[str, MaterialFacts]
    textures: list[TextureFacts]


# --------------------------------------------------------------------------
# Reading the USDZ
# --------------------------------------------------------------------------

def read_package(path: Path) -> PackageInfo | None:
    """List the zip inside a USDZ and measure its textures; None if it is not a readable zip."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            textures = []
            for info in infos:
                if Path(info.filename.lower()).suffix in CHECKED_IMAGE_SUFFIXES:
                    data = read_zip_entry_head(zf, info)
                    image = read_image_info(data) if data is not None else None
                    textures.append((info.filename, max(image.width, image.height) if image else 0))
            return PackageInfo(path.stat().st_size, [i.filename for i in infos], textures)
    except (zipfile.BadZipFile, OSError):
        return None


def read_usd_facts(usdz: Path) -> UsdFacts:
    """Open the USDZ with pxr and collect the facts the checks need."""
    from pxr import Usd, UsdGeom, UsdShade  # here so this module loads without usd-core

    try:
        stage = Usd.Stage.Open(str(usdz))
    except Exception as exc:  # pxr raises its own error type for unreadable files
        raise ConvertError(f"the USD scene inside {usdz.name} could not be opened: {exc}") from exc
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    box = cache.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    bounds = None if box.IsEmpty() else (box.GetMin()[1] * meters, (box.GetMax()[1] - box.GetMin()[1]) * meters)

    triangles = 0
    materials: dict[str, MaterialFacts] = {}
    textures: list[TextureFacts] = []
    predicate = Usd.TraverseInstanceProxies(Usd.PrimIsActive & Usd.PrimIsDefined & ~Usd.PrimIsAbstract)
    for prim in stage.Traverse(predicate):
        if prim.IsA(UsdGeom.Mesh):
            counts = UsdGeom.Mesh(prim).GetFaceVertexCountsAttr().Get() or []
            triangles += sum(c - 2 for c in counts if c >= 3)
        elif prim.IsA(UsdShade.Material):
            materials[prim.GetName()] = material_facts(prim)
        elif prim.IsA(UsdShade.Shader) and UsdShade.Shader(prim).GetIdAttr().Get() == "UsdUVTexture":
            textures.append(texture_facts(prim))
    return UsdFacts(str(UsdGeom.GetStageUpAxis(stage)), meters, bounds, triangles, materials, textures)


def material_facts(prim: Any) -> MaterialFacts:
    """Find a material's UsdPreviewSurface and read its opacity settings."""
    from pxr import Usd, UsdShade

    for child in Usd.PrimRange(prim, Usd.TraverseInstanceProxies()):
        if child.IsA(UsdShade.Shader):
            shader = UsdShade.Shader(child)
            if shader.GetIdAttr().Get() == "UsdPreviewSurface":
                threshold = shader.GetInput("opacityThreshold")
                opacity = shader.GetInput("opacity")
                value = threshold.Get() if threshold else None
                return MaterialFacts(True, None if value is None else float(value),
                                     bool(opacity and opacity.HasConnectedSource()))
    return MaterialFacts(False, None, False)


def texture_facts(prim: Any) -> TextureFacts:
    """Read where a UsdUVTexture shader's image file points, and where USD found it."""
    from pxr import Sdf, UsdShade

    owner = prim.GetParent().GetName() if prim.GetParent() else prim.GetName()
    file_input = UsdShade.Shader(prim).GetInput("file")
    value = file_input.Get() if file_input else None
    if isinstance(value, Sdf.AssetPath):
        return TextureFacts(owner, value.path, value.resolvedPath)
    return TextureFacts(owner, "", "")


# --------------------------------------------------------------------------
# The checks (plain data in, PASS/FAIL lines out, so they run without pxr)
# --------------------------------------------------------------------------

def check_packaging(path: Path, package: PackageInfo | None) -> list[Check]:
    """Zip layout and budgets, using the same rules as check_plant_assets.py."""
    report = Report()
    check_usdz(path, path.stem, report)
    results = [failed("Package", i.message) if i.level == "ERROR" else note("Package", i.message)
               for i in report.issues]
    if package is None and not any(c.status == "FAIL" for c in results):
        results.append(failed("Package", f"{path.name} could not be read as a zip archive"))
    if package is None or any(c.status == "FAIL" for c in results):
        return results
    largest = max((size for _name, size in package.textures), default=0)
    textures = (f"{len(package.textures)} texture(s), the largest is {largest} px (limit {MAX_TEXTURE_PX} px)"
                if package.textures else "the package has no textures")
    return results + [
        passed("Stored entries", f"all {len(package.names)} file(s) in the package are stored uncompressed, "
                                 "as USDZ requires"),
        passed("USD file first", f"the first file in the package is {package.names[0]}"),
        passed("Size budget", f"{package.size_bytes / MB:.2f} MB (limit {MAX_USDZ_BYTES / MB:.0f} MB)"),
        passed("Texture budget", textures),
    ]


def check_stage(facts: UsdFacts) -> list[Check]:
    """The scene must be Y-up and measured in meters, like RealityKit."""
    axis_ok = facts.up_axis == "Y"
    meters_ok = math.isclose(facts.meters_per_unit, 1.0, rel_tol=1e-6)
    return [
        passed("Up axis", "Y, the same as the app") if axis_ok else
        failed("Up axis", f"the scene is {facts.up_axis}-up but the app is Y-up, so the plant would lie down "
                          f"or stand wrongly. See {DOCS}, section 7.6."),
        passed("Units", "1 unit is 1 meter, the same as the app") if meters_ok else
        failed("Units", f"1 unit is {facts.meters_per_unit:g} m but the app expects 1 m, so the plant "
                        f"would be the wrong size. See {DOCS}, section 7.6."),
    ]


def check_bounds(facts: UsdFacts, glb_height: float | None) -> list[Check]:
    """The base must sit on Y = 0 and the height must match the GLB's."""
    if facts.bounds is None:
        return [failed("Bounds", "the USDZ contains no geometry to measure")]
    min_y, height = facts.bounds
    out = [
        passed("Base at zero", f"the lowest point is at Y = {min_y:.4f} m")
        if abs(min_y) <= BASE_TOLERANCE_M else
        failed("Base at zero", f"the lowest point is at Y = {min_y:.4f} m, more than {BASE_TOLERANCE_M * 1000:g} mm "
                               f"from the floor, so the plant would float or sink. See {DOCS}, section 7.6."),
    ]
    if glb_height is None:
        out.append(note("Height", f"the GLB has no size information, so the {height:.3f} m height of the USDZ "
                                  "could not be compared with it"))
    elif abs(height - glb_height) <= HEIGHT_TOLERANCE * glb_height:
        out.append(passed("Height", f"{height:.3f} m in the USDZ and {glb_height:.3f} m in the GLB "
                                    f"(within {HEIGHT_TOLERANCE:.0%})"))
    else:
        out.append(failed("Height", f"{height:.3f} m in the USDZ but {glb_height:.3f} m in the GLB, more than "
                                    f"{HEIGHT_TOLERANCE:.0%} apart, so the plant was scaled or cut off"))
    return out


def usd_material_name(gltf_name: str | None, index: int) -> str:
    """The name usdzconvert gives a glTF material: letters and digits only, or material_<index>."""
    if gltf_name:
        name = re.sub(r"[^A-Za-z0-9]", "_", gltf_name)
        return "_" + name if name[0].isdigit() else name
    return f"material_{index}"


def check_leaf_material(glb_material: dict[str, Any], facts: UsdFacts) -> list[Check]:
    """One MASK or BLEND material must keep its transparency in the USD material of the same name."""
    mode = glb_material["alphaMode"]
    name = usd_material_name(glb_material["name"], glb_material["index"])
    label = f"Leaf material '{name}'"
    material = facts.materials.get(name)
    if material is None:
        known = ", ".join(sorted(facts.materials)) or "none"
        return [failed(label, f"the GLB material has no USD material named '{name}' (the USDZ has: {known})")]
    if not material.has_preview_surface:
        return [failed(label, "the USD material has no UsdPreviewSurface shader, which RealityKit needs")]

    problems = []
    if mode == "MASK" and not (material.opacity_threshold or 0) > 0:
        problems.append("it has no opacityThreshold above 0, so the whole card would draw as a solid rectangle")
    if not material.opacity_connected:
        problems.append("its opacity is not connected to the texture's alpha, so nothing is cut out")
    if problems:
        return [failed(label, f"{mode}: " + "; ".join(problems) + f". See {DOCS}, section 7.2.")]
    detail = f"{mode}: opacity comes from the texture's alpha"
    if mode == "MASK":
        detail += f" and opacityThreshold is {material.opacity_threshold:g}"
    out = [passed(label, detail)]
    if mode == "BLEND":
        out.append(note(label, "blended leaves may flicker or swap order as you orbit; alpha cutout (MASK) "
                               f"is safer. See {DOCS}, section 7.2."))
    return out


def check_leaf_materials(glb_materials: list[dict[str, Any]], facts: UsdFacts) -> list[Check]:
    """Every MASK and BLEND material of the GLB must have kept its transparency."""
    leaves = [m for m in glb_materials if m["alphaMode"] in ("MASK", "BLEND")]
    if not leaves:
        return [passed("Leaf transparency", "no material uses alpha cutout or blending, so there is nothing to check")]
    return [check for material in leaves for check in check_leaf_material(material, facts)]


def texture_problem(texture: TextureFacts, names: set[str]) -> str | None:
    """Why a texture link does not lead to an image inside the package, or None if it does."""
    if not texture.asset_path:
        return "has no image file set"
    if not texture.resolved_path:
        return f"points at '{texture.asset_path}', which USD cannot find"
    link = _TEXTURE_LINK.match(texture.resolved_path)
    if link is None:
        return f"points at '{texture.resolved_path}', a file outside the package"
    if link["inner"].removeprefix("./") not in names:
        return f"points at '{link['inner']}', which is not in the package"
    return None


def check_textures(facts: UsdFacts, package: PackageInfo) -> list[Check]:
    """Every texture link must lead to an image packed inside the USDZ."""
    if not facts.textures:
        return [passed("Texture links", "the scene has no textures")]
    names = set(package.names)
    problems = [(t, texture_problem(t, names)) for t in facts.textures]
    bad = [failed("Texture links", f"a texture of material '{t.owner}' {why}. See {DOCS}, section 4 "
                                   "(external-texture).") for t, why in problems if why]
    return bad or [passed("Texture links", f"all {len(facts.textures)} texture link(s) lead to images "
                                           "packed inside the USDZ")]


def check_triangles(usd_triangles: int, glb_triangles: int) -> list[Check]:
    """The USD meshes must hold about as many triangles as the GLB."""
    detail = f"{usd_triangles:,} triangles in the USDZ, {glb_triangles:,} in the GLB"
    if usd_triangles == 0:
        return [failed("Triangles", detail + ", so the conversion produced no geometry")]
    if abs(usd_triangles - glb_triangles) > TRIANGLE_TOLERANCE * glb_triangles:
        return [failed("Triangles", detail + f", more than {TRIANGLE_TOLERANCE:.0%} apart, so geometry "
                                             "was lost or added in conversion")]
    return [passed("Triangles", detail)]


def summary_lines(package: PackageInfo, facts: UsdFacts) -> list[str]:
    """The closing numbers: size, triangles, textures and materials."""
    largest = max((size for _name, size in package.textures), default=0)
    textures = f"{len(package.textures)}" + (f" (largest {largest} px)" if package.textures else "")
    return [
        f"File size:  {package.size_bytes / MB:.2f} MB (limit {MAX_USDZ_BYTES / MB:.0f} MB)",
        f"Triangles:  {facts.triangles:,}",
        f"Textures:   {textures}",
        f"Materials:  {len(facts.materials)}",
    ]


def run_checks(usdz: Path, glb_report: dict[str, Any]) -> Outcome:
    """Run every check on a converted USDZ."""
    package = read_package(usdz)
    checks = check_packaging(usdz, package)
    if package is None:
        return Outcome(checks)
    facts = read_usd_facts(usdz)
    bounds = glb_report["bounds"]
    checks += check_stage(facts)
    checks += check_bounds(facts, bounds["heightMeters"] if bounds else None)
    checks += check_leaf_materials(glb_report["materials"], facts)
    checks += check_textures(facts, package)
    checks += check_triangles(facts.triangles, glb_report["geometry"]["triangleCount"])
    return Outcome(checks, summary_lines(package, facts), facts.bounds[1] if facts.bounds else None)


# --------------------------------------------------------------------------
# Running the pipeline
# --------------------------------------------------------------------------

def missing_packages() -> list[str]:
    """The converter's Python packages that this Python cannot import, as they should be shown to the user."""
    missing = []
    for module, package in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    return missing


def setup_help(missing: list[str], has_venv: bool) -> str:
    """Tell the user how to set up the converter environment (docs section 2.2)."""
    lines = [
        f"ERROR This Python ({sys.executable}) cannot import: {', '.join(missing)}.",
        "The converter needs Python 3.10 or newer and a virtual environment with its packages.",
    ]
    if has_venv:
        lines += ["A .venv folder already exists. If you have not activated it in this Terminal window, run:",
                  "", "  source .venv/bin/activate", "",
                  "If that does not fix it, install the packages again:"]
    else:
        lines += ["Set it up once, from the repo root:", ""]
    lines += ["  python3 -m venv .venv", "  .venv/bin/pip install -r scripts/requirements-convert.txt",
              "  source .venv/bin/activate", "",
              "Then run this command again. Repeat the 'source' line in every new Terminal window.",
              f"Details: {DOCS}, section 2.2."]
    return "\n".join(lines)


def shown(path: Path, root: Path) -> str:
    """A path relative to the repo root when possible, for readable messages."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def inspect_input(glb: Path, root: Path) -> dict[str, Any]:
    """Run the GLB inspector, print its findings, and stop on any ERROR."""
    try:
        report = inspect_glb.inspect_file(glb)
    except inspect_glb.GlbError as exc:
        raise ConvertError(f"{shown(glb, root)} is not a usable GLB: {exc}") from exc
    geometry, bounds = report["geometry"], report["bounds"]
    height = f"{bounds['heightMeters']:.3f} m tall" if bounds else "size unknown"
    print(f"Inspecting {shown(glb, root)}: {geometry['triangleCount']:,} triangles, {height}, "
          f"{len(report['materials'])} material(s), {len(report['images'])} image(s)")
    for item in report["findings"]:
        print(f"  [{item['severity']}] {item['code']}: {item['message']}")
    errors = [f["code"] for f in report["findings"] if f["severity"] == "ERROR"]
    if errors:
        raise ConvertError(f"the inspector found {len(errors)} error(s) ({', '.join(errors)}). Fix the GLB "
                           f"(see {DOCS}, section 4) and run this command again.")
    return report


def run_converter(glb: Path, usdz: Path) -> list[str]:
    """Run the vendored usdzconvert in a subprocess; returns its warnings, raises ConvertError on failure."""
    usdz.parent.mkdir(parents=True, exist_ok=True)
    usdz.unlink(missing_ok=True)  # so a stale file can never pass for a fresh one
    command = [sys.executable, "-B", str(VENDORED_CONVERTER), "-v", str(glb), str(usdz)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                timeout=CONVERT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConvertError(f"could not run usdzconvert: {exc}") from exc
    # usdzconvert prints its own errors and warnings on stdout, so look at both streams.
    output = _ANSI.sub("", "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip()))
    if result.returncode != 0 or not usdz.is_file():
        reason = f"exit code {result.returncode}" if result.returncode != 0 else "it wrote no output file"
        raise ConvertError(f"usdzconvert failed ({reason}):\n{output}")
    return [line.strip() for line in output.splitlines() if line.strip().startswith("Warning:")]


def print_install_help(plant_id: str, height: float | None, root: Path) -> None:
    """Print the manifest entry to add and the manual steps that remain."""
    entry: dict[str, Any] = {"id": plant_id, "displayName": "TODO", "category": plant_id.split("-")[0],
                             "heightMeters": round(height, 2) if height is not None else "TODO",
                             "thumbnail": f"{plant_id}-thumb", "model": plant_id}
    plants = shown(root / PLANTS_DIR, root)
    check_command = "python3 scripts/check_plant_assets.py" + (
        f" --root {root}" if root != default_root() else "")
    print("")
    print("Suggested plants.json entry (replace the TODO displayName):")
    print(f"  {json.dumps(entry)}")
    print("")
    print("Steps left to do by hand:")
    print(f"  1. Copy the thumbnail PNG (Assets/Plants/source/{plant_id}.png) to {plants}/{plant_id}-thumb.png")
    print(f"  2. Add the entry above to {plants}/plants.json")
    print(f"  3. Run: {check_command}")
    print("  4. Log the plant in Assets/Plants/LICENSE-RECORD.md")
    print("  5. Run: xcodegen")


def convert(plant_id: str, root: Path, install: bool) -> int:
    """Convert and check one plant; returns the process exit code."""
    if not ID_PATTERN.fullmatch(plant_id):
        print(f"ERROR '{plant_id}' is not a valid plant ID. Use lowercase words joined by dashes and end with "
              "a number, like 'fern-a-01' or 'succulent-b-02'.", file=sys.stderr)
        return EXIT_FAILED
    missing = missing_packages()
    if missing:
        print(setup_help(missing, (root / ".venv").is_dir()), file=sys.stderr)
        return EXIT_SETUP

    glb = root / SOURCE_DIR / f"{plant_id}.glb"
    usdz = root / STAGING_DIR / f"{plant_id}.usdz"
    if not glb.is_file():
        print(f"ERROR {shown(glb, root)} does not exist. Export the plant from FABOTANIC as a Normal GLB and "
              f"save it there (see {DOCS}, section 3).", file=sys.stderr)
        return EXIT_FAILED
    try:
        glb_report = inspect_input(glb, root)
        print(f"\nConverting to {shown(usdz, root)}")
        for warning in run_converter(glb, usdz):
            print(f"  usdzconvert: {warning}")
        print("\nChecking the USDZ")
        outcome = run_checks(usdz, glb_report)
    except ConvertError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return EXIT_FAILED

    for check in outcome.checks:
        print(f"  {check}")
    if outcome.summary:
        print("\nSummary")
        for line in outcome.summary:
            print(f"  {line}")
    if outcome.failures:
        print(f"\n{outcome.failures} check(s) failed, so {shown(usdz, root)} is not ready. Use the repair "
              f"table in {DOCS}, section 5, then run this command again.")
        return EXIT_FAILED
    print(f"\nAll checks passed. Staged {shown(usdz, root)}")
    if not install:
        print("To copy it into the app, run again with --install.")
        return EXIT_OK

    destination = root / PLANTS_DIR / f"{plant_id}.usdz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(usdz, destination)
    print(f"Installed {shown(destination, root)}")
    print_install_help(plant_id, outcome.height_m, root)
    return EXIT_OK


HELP_DESCRIPTION = """\
Convert one plant from GLB to USDZ, then check that the result is usable.
GLB is the single-file 3D format FABOTANIC exports. USDZ is the single-file 3D
format the app loads.

Run it from the repo root with the converter environment active. Set that up
once with:
  python3 -m venv .venv
  .venv/bin/pip install -r scripts/requirements-convert.txt
and in each new Terminal window run:  source .venv/bin/activate

What it does, in order:
  1. Reads Assets/Plants/source/<plant-id>.glb and runs the GLB inspector.
     An ERROR stops the run. A WARN is shown and the run continues.
  2. Converts the file with usdzconvert (Apple's converter; a copy is kept in
     scripts/vendor/usdzconvert) and writes Assets/Plants/usdz/<plant-id>.usdz.
  3. Checks the result and prints PASS or FAIL for each check: the file layout
     and size limits; that the scene is Y-up (the Y axis points up) and in
     meters; that the plant's base sits at zero, so it stands on the floor;
     that its height and triangle count match the GLB; that leaf transparency
     survived; and that every texture image is packed inside the file. A WARN
     line is a note and does not fail the plant.
  4. With --install, and only if every check passed, copies the USDZ to
     App/Resources/Plants/<plant-id>.usdz. Then it prints the entry to add to
     plants.json and the steps that are left to do by hand.

Exit status:
  0  the plant converted and every check passed
  1  the plant failed: bad ID, missing GLB, inspector ERROR, failed
     conversion, or a failed check
  2  the converter is not set up (its Python packages are missing)
"""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=HELP_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Example: python3 scripts/convert_plant.py fern-a-01 --install")
    parser.add_argument("plant_id", metavar="plant-id",
                        help="the plant's ID, like fern-a-01: lowercase words joined by dashes, ending in a "
                             "number. It names the GLB in Assets/Plants/source/.")
    parser.add_argument("--install", action="store_true",
                        help="if every check passes, also copy the USDZ into App/Resources/Plants/ "
                             "(the folder the app loads plants from)")
    parser.add_argument("--root", type=Path, default=default_root(), metavar="PATH",
                        help="repo root that holds Assets/ and App/ (default: the parent of scripts/)")
    args = parser.parse_args(argv)
    return convert(args.plant_id, args.root, args.install)


if __name__ == "__main__":
    sys.exit(main())
