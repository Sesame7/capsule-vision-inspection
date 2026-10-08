# Capsule Size/Color screening and Recipe preview — Phase 3E-1

This is an **uncalibrated demonstration**, currently checked with yellow capsules
on black backgrounds. It is not industrial accuracy acceptance or a foreign-object
classifier. Other colors, lighting/cameras and severe powder contamination require
future evaluation. Phase 3E-2 legacy cleanup is explicitly deferred.

## Measurement and judgment

`inspection.production.capsule` is synchronous and Qt/SDK independent. It borrows
the existing BGR frame without resizing, mutation or retained arrays. Each saved
MachineLayout channel ROI is split at `height // 2` into upper/lower view regions.
One `measure_capsule()` call supplies both Size and Color. Production skips it
entirely when both are disabled; measurement runs on the existing cycle inspector,
never on Qt or a new engine thread.

Provisional internal constants (not Recipe settings): HSV Value > 20 foreground;
8-connected components; ignore areas below max(12 pixels, 0.5% of view area);
a second component at least 25% of the dominant area is competing. A body touching
any view edge, including the midpoint, is truncated; width/height below 3 pixels
is invalid. Small disconnected particles do not change the dominant bbox. Connected
powder can still change its size: this simple algorithm does not solve contamination.
No rotated geometry, physical units or segmentation morphology is implemented.

Valid bodies retain full-image axis-aligned boxes and exact pixel widths/heights.
For color, a one-pixel eroded interior mask excludes edges without changing the size
measurement. Pixels need S >= 40 and V >= 40; at least max(5, 10% of interior) usable
pixels are required. Hue is unwrapped about a circular center, then median-reduced;
an ambiguous circular mean magnitude < 0.5 is unusable. Hue distance uses the
OpenCV period 180, including wrap-around. These constants are provisional too.

`judge_capsule()` uses cached scalar measurements. Both views must satisfy enabled
inclusive width/height limits or circular color tolerance. Missing/competing/truncated
bodies are SUCCESS + NG, with no invented measurements for that view. Unusable
chroma is Color NG but need not invalidate Size. Execution/ROI failures are
ERROR + UNKNOWN. New enabled checks are always required internally.

Results reuse TRADITIONAL `CheckResult`, `Measurement` and `Reason`; `capsule.size.*`,
`capsule.color.*`, and `capsule.<view>.<invalid_reason>` distinguish failures. Scalar
measurements name the upper/lower view. The existing aggregation retains independent
technical and product states, including ERROR + NG with other defect evidence.
ViMo still runs once per enabled full camera, unchanged. No parallel result system.

## Additive Recipe contract

Optional root `capsule` data in schema v1 is explicitly separate from the preserved
legacy `defaults.presence/blob/color` and camera/channel overrides:

```json
"capsule": {
  "size": {"enabled": false, "target_width": 1, "width_tolerance": 0,
           "target_height": 1, "height_tolerance": 0},
  "color": {"enabled": false, "target_hue": 0, "hue_tolerance": 0}
}
```

Omission supplies these disabled defaults; 1 px / Hue 0 are uncalibrated initial
values, not pilot production requirements. Size targets are positive integers,
tolerances nonnegative integers; Hue is 0–179, tolerance 0–90. No operator `required`
or camera/channel override exists for these settings. Unknown fields/types are
rejected. Draft, duplicate, atomic persistence and immutable resolution retain
both new and legacy settings. ROI remains exclusively MachineLayout-owned.

## Preview isolation and lifecycle

Recipe adds only Size/Color enablement, summaries and **Tune preview**. One workspace
has configured-order camera tabs, aspect-preserving image, channel/body boxes, and
Size/Color modes. Channel colors and image labels show the combined enabled-check
judgment. Precise reason codes remain in the canvas tooltip; no duplicated channel
summary or explanatory footer is displayed. Meaningful measurement/sample errors
and unavailable-reference feedback remain visible. Size body labels use two compact
lines (W / H) on a dark backing, placed above the body where possible and bounded
to its channel/view and the visible viewport. Font size stays fixed in logical
pixels through Fit/zoom; Color mode has no Size labels. At extreme zoom-out where
readable labels cannot fit, zoom in; offscreen bodies do not acquire pinned labels.
The preview suppresses the shell's ordinary production event strip, without clearing
its history/counters; technical, command, runtime fault and shutdown feedback remains.
Returning to Production restores its usual event presentation. Width/height controls or the color tolerance slider judge cached measurements
immediately. Color sampling uses only complete valid two-view channels with usable
chroma; unavailable/ambiguous samples leave the target unchanged and explain why.

A frozen immutable display-store frame and saved layout identify each measurement.
Refresh/camera switches clear results; at most one `CapsulePreview` background job
and one latest desired reference exist. Stale completions are discarded. No algorithm
runs on Qt. With both checks disabled no new measurement starts. Threshold changes
reuse cached evidence; source publication alone never replaces the frozen image.
Leaving releases the workspace, including on shell navigation, so reopening uses the
current saved machine layout. Close waits asynchronously for actual job termination.

Preview creates no cycles, engine, counters, history, candidate or active state.
Edits are Recipe draft-only. Runtime states prohibit edits as before. Back retains
the draft; normal Save and apply validates/persists/prepares/configures. Only a
successful Start activates it. No source images or manual machine geometry are edited.

## Manual review

From the repository root, equipment-free synthetic two-view capsules and fake ViMo:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
```

Optional `--frames-dir images` uses stored camera images with fake ViMo. Existing
demo machine geometry stays authoritative; use Settings to calibrate if it does not
fit the selected references. The launcher does not repair it silently.

Real-local mode uses the existing ignored manual configuration, when available:

```powershell
$config = "outputs/manual_phase3c"
uv run --no-sync python scripts/run_local_production_ui.py --model-config "$config/model.json" --machine-config "$config/machine.json" --recipe "$config/local.recipe.json" --frames-dir images --language zh --size 1080x1920
```

1. Open Recipe, enable Size, open Tune preview and choose CAM1…CAM6.
2. Inspect both body boxes. Set targets to the shown dimensions; narrow/widen
   tolerances to observe OK/NG transitions. Invalid bodies require reviewing the
   saved ROI/image, not invented measurements or automatic correction.
3. Switch Color mode, enable it and Use current image color. Move tolerance;
   verify mode changes do not conceal NG from another enabled check.
4. Verify production counters remain unchanged throughout preview. Back retains
   edits; Save and apply writes the selected Recipe only and prepares a candidate.
5. Start actual inspection, inspect channel NG/technical states, then Stop. Preview
   itself never increments counters. Saved changes activate only through Start.

Screenshots and automated checks do not replace human usability/accuracy acceptance.
Legacy traditional controls stay hidden; legacy data/execution remain intact until
Phase 3E-2. No camera/PLC, history, calibration or throughput work is included.
