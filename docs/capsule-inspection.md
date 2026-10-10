# Capsule Size/Color screening and Recipe preview — Phase 3E-1

This is an **uncalibrated demonstration**, currently checked with yellow capsules
on black backgrounds. It is not industrial accuracy acceptance or a foreign-object
classifier. Other colors, lighting/cameras and severe powder contamination require
future evaluation. Size/Color are the sole supported traditional inspection path.

## Selective measurement and judgment — Phase 3F-2B

`inspection.production.capsule` is synchronous and Qt/SDK independent. It borrows
BGR pixels without resizing, mutation or retained arrays. Each saved MachineLayout
ROI is split at `height // 2` into upper/lower views. Production skips measurement
when both checks are disabled; enabled work runs on the existing cycle inspector.

| Size | Color | Measurement |
| --- | --- | --- |
| off | off | None |
| on | off | Geometry only; no HSV, Hue or Color sampling mask |
| off | on | View Color only; no components, body boxes or dimensions |
| on | on | Independent geometry and Color, with one shared HSV conversion |

`measure_capsule(image, roi, settings)` records capability flags. Each view has
optional body geometry, a Size rejection reason, and optional `ColorMeasurement`
(Hue, usable pixel count, minimum count and rejection reason). Disabled paths do
not fabricate evidence. Targets/tolerances affect `judge_capsule()`, not measurement;
requesting an enabled capability absent from the measurement is a technical error.

**Size is unchanged.** Foreground is HSV Value >20, equivalently at least one BGR
channel >20. Size-only uses a BGR range mask; combined mode reuses HSV Value with
an exact binary threshold. Eight-connected components below max(12 pixels, 0.5%
of view area) are ignored. A second component at least 25% of dominant area is
competing. Touching any view edge (including the midpoint) is truncated; either
dimension below 3 pixels is invalid. Valid bodies retain full-image axis-aligned
boxes, pixel width/height and existing inclusive target/tolerance limits. Missing,
competing, truncated and invalid geometry retain their existing reason codes.
Small disconnected particles do not change the dominant bbox; connected powder can.

**Color is an independent whole-view screen.** Only pixels with S >=40 and V >=40
are usable; these already satisfy black-background foreground V >20. Each view
requires **max(12, ceil(view area / 20))** usable pixels (5% with a 12-pixel floor).
This provisional internal rule is not a Recipe setting. The current six real images'
72 views contained 25.3–37.3% usable pixels; that observation does not calibrate a
presence criterion. No component selection or erosion is performed for Color.

Integer Hue uses the existing optimized 180-bin circular-center/median algorithm
with its numerical fallback guards. Circular mean magnitude <0.5 is ambiguous;
distance uses OpenCV's period 180. Each view must have sufficient pixels, usable Hue
and inclusive target/tolerance agreement. Both views must pass. Explicit reasons
are `capsule.color.<view>.insufficient_foreground`, `.unusable` and `.tolerance`.
Color evidence records usable pixels and, only when available, Hue; Color reasons
have no invented body rectangle. Shape invalidity does not suppress Color evidence.
Either enabled check's rejection contributes NG to the one traditional result.

Color deliberately replaces the former eroded body-interior sampling. It can pass
a bright truncated/competing scene that Size rejects. Background contamination or
multiple objects may affect whole-view Hue; this is not physical capsule-presence,
shape or multi-color classification. No bit-exact equivalence with historical Color
results is claimed, even when representative yellow-image Hues happen to agree.

Content rejection remains SUCCESS + NG. Execution/ROI/configuration failure remains
ERROR + UNKNOWN. Results still use TRADITIONAL `CheckResult`, `Measurement` and
`Reason`; production aggregation retains independent technical/product states,
including ERROR + NG with other evidence. ViMo still runs once per enabled full
camera through the same serial worker, with no lifecycle or parameter changes.
See [performance measurements](inspection-performance.md).

## Recipe contract

Optional root `capsule` data in schema v1 supplies uniform product-level settings.
`defaults` and camera/channel overrides contain only ViMo policies:

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
capsule settings and independent ViMo policies. ROI remains exclusively MachineLayout-owned.

## Preview isolation and lifecycle

Recipe retains Size/Color enablement/summaries and one shared **Tune preview**
entry. The same workspace opens in Size, or Color when only Color is enabled.
A compact top toolbar has Back, configured-order camera icon/number tabs, Fit
and reference Refresh. The aspect-preserving image and channel/body overlays stay
unchanged. The bottom group contains compact Size/Color tabs, explicit selected-mode
Enabled/Disabled state and the existing numeric or sampling/tolerance controls;
groups stack at narrow widths rather than stretching numeric fields across the page.
The Color swatch tooltip exposes the existing target Hue. No new settings are added.
Channel colors and image labels show the combined enabled-check
judgment, including when the selected mode is disabled: its unchecked Disabled
control does not suppress NG from another enabled check. The enablement tooltip
states the combined meaning. Precise reason codes remain in the canvas tooltip;
no duplicated channel summary or explanatory footer is displayed. Meaningful measurement/sample errors
and unavailable-reference feedback remain visible. Size body labels use two compact
lines (W / H) on a dark backing, placed above the body where possible and bounded
to its channel/view and the visible viewport. Font size stays fixed in logical
pixels through Fit/zoom; Color mode has no Size labels. At extreme zoom-out where
readable labels cannot fit, zoom in; offscreen bodies do not acquire pinned labels.
The shell reserves the same fixed 33-pixel status strip during preview. Ordinary
production NG stays in Production evidence; current technical, relevant command,
runtime fault and shutdown feedback remains visible across pages. Preview never
clears application results/counters. Width/height controls or the color tolerance slider judge cached measurements
immediately. Color sampling uses channels with usable Color evidence in both views,
independent of body boxes; unavailable/ambiguous samples leave the target unchanged and explain why.

A frozen immutable display-store frame and saved layout identify each measurement.
Refresh/camera switches clear results; at most one `CapsulePreview` background job
and one latest desired reference exist. Stale completions are discarded. No algorithm
runs on Qt. With both checks disabled no new measurement starts. Threshold changes
reuse cached evidence. Enabled Size/Color flags identify cache capabilities: changing
them clears/recomputes evidence, and any old in-flight completion is discarded before
one latest job starts. Turning both off clears results without starting a job.
Source publication alone never replaces the frozen image.
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
demo machine geometry stays authoritative; use Vision to calibrate if it does not
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
Removed traditional fields are rejected; no compatibility execution exists.
No camera/PLC, history, calibration or throughput work is included.


## Existing local manual Recipe

After schema consolidation, `outputs/manual_phase3c/local.recipe.json` may still
contain old disabled checks. Edit it explicitly before launching: remove only
`defaults.presence`, `defaults.blob` and `defaults.color`. Keep `defaults.vimo`,
root `capsule` values, identity and model selection. Apply the same removal inside
camera/channel `checks` if present. Do **not** remove `capsule.color`.
There is no automatic migration. Never replace the saved machine layout to fix a
Recipe schema error; model, image and ROI files are independent and unchanged.
