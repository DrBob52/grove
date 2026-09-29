#!/usr/bin/env python3
"""Check the bundled plant assets in App/Resources/Plants/ against the spec.

Usage: python3 scripts/check_plant_assets.py [--root PATH] [--strict]

Prints one line per issue (`ERROR id: message` / `WARN id: message`) and a
summary line. Exits 1 on any error (or any warning with --strict).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from imageinfo import has_png_signature, read_image_info

MAX_USDZ_BYTES = 8_000_000  # the spec says "under 8 MB" per plant; larger is an error
MAX_TOTAL_BYTES = 150_000_000  # spec: total plant assets under 150 MB
MAX_TEXTURE_PX = 1024  # spec: textures 1K (1024 px) max per map
MAX_HEIGHT_METERS = 30

PLANTS_DIR = Path("App") / "Resources" / "Plants"
MANIFEST_NAME = "plants.json"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*-[0-9]{2,}$")
SAFE_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
STRING_FIELDS = ("id", "displayName", "category", "thumbnail", "model")
KNOWN_KEYS = frozenset(STRING_FIELDS) | {"heightMeters"}

USD_SUFFIXES = (".usdc", ".usda", ".usd")
CHECKED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
OTHER_IMAGE_SUFFIXES = frozenset(
    {".exr", ".hdr", ".tif", ".tiff", ".bmp", ".tga", ".gif", ".webp", ".avif", ".heic",
     ".ktx", ".ktx2", ".dds", ".psd"}
)
HEADER_READ_BYTES = 1 << 20  # image headers are read from at most the first MiB

_READ_ERRORS = (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error, OSError)


@dataclass(frozen=True)
class Issue:
    """One problem found, printed as `LEVEL subject: message`."""

    level: str  # "ERROR" or "WARN"
    subject: str
    message: str

    def __str__(self) -> str:
        return f"{self.level} {self.subject}: {self.message}"


@dataclass
class Report:
    """Everything the checker found."""

    issues: list[Issue] = field(default_factory=list)
    plants_checked: int = 0

    def error(self, subject: str, message: str) -> None:
        self.issues.append(Issue("ERROR", subject, message))

    def warn(self, subject: str, message: str) -> None:
        self.issues.append(Issue("WARN", subject, message))

    @property
    def errors(self) -> int:
        return sum(1 for i in self.issues if i.level == "ERROR")

    @property
    def warnings(self) -> int:
        return sum(1 for i in self.issues if i.level == "WARN")

    def summary(self) -> str:
        return f"{self.plants_checked} plants checked, {self.errors} errors, {self.warnings} warnings"


def default_root() -> Path:
    """The repo root: the parent of the scripts/ directory."""
    return Path(__file__).resolve().parent.parent


def is_number(value: Any) -> bool:
    """True for int/float but not bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def read_head(path: Path) -> bytes | None:
    """First HEADER_READ_BYTES of a file, or None if it can't be read."""
    try:
        with path.open("rb") as fh:
            return fh.read(HEADER_READ_BYTES)
    except OSError:
        return None


def load_manifest(path: Path, report: Report) -> list[Any] | None:
    """Parse plants.json; report an error and return None if unusable."""
    if not path.is_file():
        report.error("manifest", f"{MANIFEST_NAME} not found at {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        report.error("manifest", f"{MANIFEST_NAME} could not be read: {exc}")
        return None
    except json.JSONDecodeError as exc:
        report.error("manifest", f"{MANIFEST_NAME} is not valid JSON: {exc}")
        return None
    if not isinstance(data, list):
        report.error("manifest", f"{MANIFEST_NAME} must contain a JSON list, not {type(data).__name__}")
        return None
    return data


def check_entry_fields(entry: dict[str, Any], number: int, report: Report) -> str:
    """Validate one manifest entry's fields; returns the subject used in messages."""
    entry_id = entry.get("id")
    has_id = is_nonempty_str(entry_id)
    subject = entry_id if has_id else "manifest"
    prefix = "" if has_id else f"entry #{number}: "

    def err(message: str) -> None:
        report.error(subject, prefix + message)

    for name in STRING_FIELDS:
        if name not in entry:
            err(f"missing field '{name}'")
        elif not is_nonempty_str(entry[name]):
            err(f"field '{name}' must be a non-empty string")
    if "heightMeters" not in entry:
        err("missing field 'heightMeters'")
    else:
        height = entry["heightMeters"]
        if not is_number(height):
            err("field 'heightMeters' must be a number")
        elif not 0 < height <= MAX_HEIGHT_METERS:
            err(f"heightMeters must be greater than 0 and at most {MAX_HEIGHT_METERS} (got {height})")

    if has_id:
        if not ID_PATTERN.fullmatch(entry_id):
            err("id must be lowercase kebab case ending in a number, like 'fern-a-01'")
        if is_nonempty_str(entry.get("model")) and entry["model"] != entry_id:
            err(f"model '{entry['model']}' must equal the id")
        if is_nonempty_str(entry.get("thumbnail")) and entry["thumbnail"] != f"{entry_id}-thumb":
            err(f"thumbnail '{entry['thumbnail']}' must be '{entry_id}-thumb'")

    extra = sorted(k for k in entry if k not in KNOWN_KEYS)
    if extra:
        report.warn(subject, prefix + "unknown manifest key(s): " + ", ".join(f"'{k}'" for k in extra))
    return subject


def read_zip_entry_head(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes | None:
    """First HEADER_READ_BYTES of a zip entry, or None if it can't be read."""
    try:
        with zf.open(info) as fh:
            return fh.read(HEADER_READ_BYTES)
    except _READ_ERRORS:
        return None


def check_zip_entries(zf: zipfile.ZipFile, name: str, subject: str, report: Report) -> None:
    """Check the USDZ rules that need the archive's entry list."""
    infos = zf.infolist()
    if not infos:
        report.error(subject, f"{name} is an empty archive")
        return
    for info in infos:
        if info.compress_type != zipfile.ZIP_STORED:
            report.error(subject, f"{name}: entry '{info.filename}' is compressed; USDZ requires "
                                  "uncompressed (ZIP_STORED) entries")
    if not infos[0].filename.lower().endswith(USD_SUFFIXES):
        report.error(subject, f"{name}: first entry is '{infos[0].filename}' but must be a "
                              ".usdc, .usda or .usd file")
    for info in infos:
        check_texture(zf, info, name, subject, report)


def check_texture(zf: zipfile.ZipFile, info: zipfile.ZipInfo, name: str, subject: str,
                  report: Report) -> None:
    """Check one archive entry if it is an image."""
    suffix = PurePosixPath(info.filename.lower()).suffix
    if suffix in CHECKED_IMAGE_SUFFIXES:
        data = read_zip_entry_head(zf, info)
        image = read_image_info(data) if data is not None else None
        if image is None:
            report.warn(subject, f"{name}: size of texture '{info.filename}' can't be read")
        elif max(image.width, image.height) > MAX_TEXTURE_PX:
            report.error(subject, f"{name}: texture '{info.filename}' is {image.width}x{image.height}, "
                                  f"over the {MAX_TEXTURE_PX} px limit")
    elif suffix in OTHER_IMAGE_SUFFIXES:
        report.warn(subject, f"{name}: texture '{info.filename}' is a {suffix} image; only "
                             "png/jpg/jpeg sizes can be checked")


def check_usdz(path: Path, subject: str, report: Report) -> None:
    """Check a model file: exists, size, zip layout and texture sizes."""
    name = path.name
    if not path.is_file():
        report.error(subject, f"missing model file {name}")
        return
    size = path.stat().st_size
    if size > MAX_USDZ_BYTES:
        report.error(subject, f"{name} is {size:,} bytes, over the {MAX_USDZ_BYTES:,} byte limit")
    try:
        with zipfile.ZipFile(path) as zf:
            check_zip_entries(zf, name, subject, report)
    except zipfile.BadZipFile:
        report.error(subject, f"{name} is not a valid zip archive")
    except _READ_ERRORS as exc:
        report.error(subject, f"{name} could not be read as a zip archive: {exc}")


def check_thumbnail(path: Path, subject: str, report: Report) -> None:
    """Check a thumbnail: exists, is a PNG, and is not oversized."""
    name = path.name
    if not path.is_file():
        report.error(subject, f"missing thumbnail file {name}")
        return
    data = read_head(path)
    if data is None:
        report.error(subject, f"{name} could not be read")
    elif not has_png_signature(data):
        report.error(subject, f"{name} is not a valid PNG (bad file signature)")
    else:
        image = read_image_info(data)
        if image is None:
            report.warn(subject, f"{name}: size of thumbnail can't be read")
        elif max(image.width, image.height) > MAX_TEXTURE_PX:
            report.warn(subject, f"{name} is {image.width}x{image.height}; thumbnails should be at "
                                 f"most {MAX_TEXTURE_PX} px on a side")


def check_entry_files(entry: dict[str, Any], subject: str, plants_dir: Path, seen: set[str],
                      report: Report) -> None:
    """Check the model and thumbnail files an entry points at (each file once)."""
    model, thumbnail = entry.get("model"), entry.get("thumbnail")
    if is_nonempty_str(model) and SAFE_NAME_PATTERN.fullmatch(model):
        if f"{model}.usdz" not in seen:
            seen.add(f"{model}.usdz")
            check_usdz(plants_dir / f"{model}.usdz", subject, report)
    if is_nonempty_str(thumbnail) and SAFE_NAME_PATTERN.fullmatch(thumbnail):
        if f"{thumbnail}.png" not in seen:
            seen.add(f"{thumbnail}.png")
            check_thumbnail(plants_dir / f"{thumbnail}.png", subject, report)


def referenced_files(entries: list[dict[str, Any]]) -> set[str]:
    """Every model/thumbnail file name the manifest entries could be pointing at."""
    names: set[str] = set()
    for entry in entries:
        for key in ("id", "model"):
            if is_nonempty_str(entry.get(key)):
                names.add(f"{entry[key]}.usdz")
        if is_nonempty_str(entry.get("id")):
            names.add(f"{entry['id']}-thumb.png")
        if is_nonempty_str(entry.get("thumbnail")):
            names.add(f"{entry['thumbnail']}.png")
    return names


def check_orphans(plants_dir: Path, entries: list[dict[str, Any]], report: Report) -> None:
    """Warn about .usdz / -thumb.png files that no manifest entry uses."""
    referenced = referenced_files(entries)
    for path in sorted(plants_dir.iterdir()):
        lower = path.name.lower()
        if path.is_file() and lower.endswith((".usdz", "-thumb.png")) and path.name not in referenced:
            report.warn(path.name, f"no matching entry in {MANIFEST_NAME}")


def total_bytes(folder: Path) -> int:
    """Total size of every file under folder."""
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def run_checks(root: Path) -> Report:
    """Run every check against <root>/App/Resources/Plants."""
    report = Report()
    plants_dir = root / PLANTS_DIR
    manifest = load_manifest(plants_dir / MANIFEST_NAME, report)
    if manifest is not None:
        entries: list[dict[str, Any]] = []
        seen_ids: dict[str, int] = {}
        seen_files: set[str] = set()
        for number, item in enumerate(manifest, start=1):
            if not isinstance(item, dict):
                report.error("manifest", f"entry #{number} is not an object")
                continue
            entries.append(item)
            report.plants_checked += 1
            subject = check_entry_fields(item, number, report)
            if subject != "manifest":
                if subject in seen_ids:
                    report.error(subject, f"duplicate id (first used by entry #{seen_ids[subject]})")
                else:
                    seen_ids[subject] = number
            check_entry_files(item, subject, plants_dir, seen_files, report)
        check_orphans(plants_dir, entries, report)
    if plants_dir.is_dir():
        total = total_bytes(plants_dir)
        if total > MAX_TOTAL_BYTES:
            report.error("manifest", f"plant assets total {total:,} bytes, over the "
                                     f"{MAX_TOTAL_BYTES:,} byte limit")
    return report


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Check bundled plant assets against the spec.")
    parser.add_argument("--root", type=Path, default=default_root(),
                        help="repo root containing App/Resources/Plants (default: parent of scripts/)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures too")
    args = parser.parse_args(argv)

    report = run_checks(args.root)
    for issue in report.issues:
        print(issue)
    print(report.summary())
    failed = report.errors > 0 or (args.strict and report.warnings > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
