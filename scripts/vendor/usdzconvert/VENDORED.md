# Vendored: usdzconvert

This folder is a copy of a community Python 3 port of Apple's `usdzconvert`, the command-line GLB to USDZ converter that Reality Converter was built on. `scripts/convert_plant.py` runs it. Nobody should need to run it by hand.

| | |
|---|---|
| Source | https://github.com/niw/usdzconvert |
| Commit | `f32c0670bb2cbb84494f902f16f5285b270460c6` (`f32c067`, "FIX: bug.", committed 2026-04-15) |
| Vendored on | 2026-09-29 |
| License | MIT terms, "Copyright © 2018 Apple Inc." See `LICENSE` in this folder. It allows copying, modifying and redistributing the software, provided the copyright notice and permission notice stay with it. They do, so vendoring is allowed. |
| Files | Every file tracked at that commit, unchanged except for the patch below. No `.git` folder. |

## Local patches

Any change to the files in this folder must be listed here with the reason, so a future update from upstream can be re-applied.

### 1. `usdARKitChecker`: skip the compliance check when `UsdUtils.ComplianceChecker` is missing

- **Problem.** After it has written a correct USDZ, `usdzconvert` runs `usdARKitChecker`, which calls `UsdUtils.ComplianceChecker`. Newer `usd-core` releases (26.8 is the one pinned in `scripts/requirements-convert.txt`) removed that class, so the script crashed with `AttributeError: module 'pxr.UsdUtils' has no attribute 'ComplianceChecker'` and exited with status 1, which looks like a failed conversion. The upstream README says to ignore this error, but `convert_plant.py` treats a nonzero exit as a failure.
- **Change.** In `runValidators`, a block of nine lines (three of them comments) was added at the top: if `UsdUtils` has no `ComplianceChecker`, print a one-line note, run only the checker's other validators (`validateMesh` and `validateMaterial`, which do not depend on the missing class), print the usual `[Pass]` or `[Fail]` line and return. When `ComplianceChecker` exists, the original code runs exactly as before. Nothing else was touched.
- **Effect.** Conversion exits with status 0 on `usd-core` 26.8, and the mesh and material validators still run. The USD compliance rules are not checked. `convert_plant.py` makes its own checks of the result instead.

## Notes

- `requirements.txt` here is upstream's own (`usd-core==23.11`, `numpy==1.26.4`). Do not install from it. The pins that matter are in `scripts/requirements-convert.txt`.
- The USD scene inside a USDZ is named after the output file, with dashes turned into underscores: `placeholder-a-01.usdz` contains a scene called `placeholder_a_01`.
- Only the GLB to USDZ path is used and tested. Upstream says the other input formats may not work with newer Python and `usd-core`.
