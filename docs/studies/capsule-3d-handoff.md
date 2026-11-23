# Capsule 3D handoff

## Source identity

In four-cycle mode, identify a capsule position by Run, stored group and channel. Resolve slots 1–4 from History group membership even when cycle IDs are nonconsecutive. Match each member’s cycle, camera, channel and frozen ROI to its archived PNG. Each exposure supplies simultaneous upper and lower reflected views at one rotation angle. Quarter turns are an initial pose estimate. If membership, occupancy or image continuity is uncertain, show the available photographs and identify the gap. [Grouping and optical evidence][grouping]

## From photographs to an illustration

- **Enhanced Mesh** is the baseline for regular shells and surface marks. Register the two optical paths, suppress camera-fixed reflections, and blend broad color and fine markings separately onto the nominal shell. The finish at poorly observed tips includes synthesized texture. [Enhanced preset][enhanced]
- **Revised mirror-aware fitting** projects one smooth shell through two translated virtual cameras with parallel axes. Each path favors a different end of the capsule. Fit both native silhouettes jointly with one rotation per exposure. Withhold a complete exposure, including both paths, when evaluating the outline. Keep the baseline when masks are clipped or the held-out outline gains too little. [Optical model][optics] · [Fit acceptance and fallback][fitting]
- **Guided V-crease** is an optional inward deformation for a clearly visible fold. Select the shot and optical path, click the fold on the native photograph and optionally drag along its axis. Save the crop coordinates, source identity, hashes and preset. Use **Strong** (0.48 nominal shell radii) for a pronounced, well-located fold. Moderate was usable across more cases in the archived study and suits folds that Strong turns into a cut. [Placement and presets][crease]

Automatic placement can choose the wrong face or reduce branching folds to one groove. Compare the previous model and candidate through a full turn in photographic and plain material, especially near profile. Prefer the photographs when neither conveys the defect. A click selects an assumed near-side point. The recorded Dent box and these uncalibrated views leave physical depth and severity unverified. The recorded inspection judgment stays authoritative.

## Recovery

The authoritative snapshot is `C:/Users/Sesame/Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20`. Run `python -B verify_archive.py` there for an archive-only check. For replay, restore `payload/` as a fresh working checkout with `Ly/Images/` beside it. [Recovery guide][guide] · [File and hash manifest][manifest]

[grouping]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/payload/docs/studies/capsule-multiview/review.md
[enhanced]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/payload/experiments/capsule_mesh_refine/review.md
[optics]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/payload/experiments/capsule_optical_shape/review.md
[fitting]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/payload/experiments/capsule_generalization/review.md
[crease]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/payload/experiments/capsule_v_crease/review.md
[guide]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/README.md
[manifest]: ../../../../Archives/FIELD-3D-STUDY-ARCHIVE-1_2026-11-20/manifest.jsonl
