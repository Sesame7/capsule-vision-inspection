# Machine ROI workspace — Phase 3C

MachineLayoutDefinition owns exactly one integer `BoundingRect` per physical
channel. Recipes contain checks/product/model data only; Recipe ROI fields are
rejected. The current schema is updated in place, with no migration or legacy
support. Multiple views/ROIs per channel are not implemented.

## Workspace and ownership

Parameters → **Machine ROI setup** replaces the Parameters form with a dedicated
canvas workspace inside the existing application window. It is not a nested ROI
dialog. The rest of Parameters and the Production screen are unchanged.

- `machine_editor.py`: Qt-free machine draft, atomic geometry operations and save.
- `roi_geometry.py`: existing source-pixel transform, rounding and bounded gestures.
- `roi_canvas.py`: frozen BGR image, six labeled ROIs, selected handles, zoom/pan.
- `roi_editor.py`: camera tabs, canvas overlays, common size and Save and apply.
- `ParametersPage`: existing asynchronous preparation and application-state guards.

The machine draft is independent of Recipe drafts. Camera tabs follow explicit
MachineLayout order, never parsed/sorted names; normal local configuration is
CAM1→CAM6. All six actual ChannelIds are labeled on the canvas. There is no channel
selector or permanent per-ROI coordinate form. Selected geometry is informational
text (`CH03 · x374 y14 · 182×860`). Only common Width and Height remain editable
numeric controls. The integer geometry API remains available to code/tests.

The canvas fills available content space. Camera entry/switch and Refresh fit the
complete frozen image with aspect ratio preserved. Left drag moves/resizes ROIs;
mouse wheel zooms around the pointer where bounded pan permits; middle drag pans.
The compact corner tools are Fit, 1:1 and Refresh reference, with accessible names
and tooltips. No zoom buttons or large instructional panel are present.

## Save, navigation and activation

**Save and apply** validates known bounds, atomically writes the supplied machine
JSON through `save_machine_layout()`, and prepares the currently selected saved
Recipe against that saved layout using the existing background preparation thread.
It then calls `ProductionApplication.configure()`. Successful **Start** remains the
only activation boundary. Success feedback says changes take effect on the next
successful Start; there is no hot modification of RUNNING configuration.

An unsaved/new Recipe must first be saved on Parameters. The workspace never
silently saves Recipe edits. Machine write failure preserves the previous saved
baseline and dirty geometry. Preparation/configuration failure after a successful
save leaves geometry saved, reports the error and does not claim successful Apply.
Cached prepared Recipe choices with an older layout are removed on successful
configuration; select/apply other Recipes again through Parameters.

Back, shell navigation away and normal application close cannot silently lose
machine edits. A clean workspace leaves immediately. Dirty geometry prompts
**Save and apply / Discard / Cancel**. Save continues navigation only after successful
preparation/configuration; Discard restores saved geometry; Cancel stays. Camera
switches retain edits. Preparation disables repeat submission and Back; application
close still observes thread termination and suppresses late configuration as before.

UNCONFIGURED/READY allow changes. STARTING/RUNNING/STOPPING/FAULT/CLOSED, preparation
and closing prohibit geometry edits. Gesture commits and actions recheck current
application state. Fit/zoom/pan and viewing remain usable. No reference means no
invented image dimensions and no geometry editing. Invalid batch operations report
errors without disabling subsequent visual correction of valid rectangles.

This is a single-writer local configuration tool, with no external-edit watcher,
conflict merging or history. Save modifies the supplied machine JSON only, never
models, source images or Recipe geometry.

## Geometry and reference contract

Common size changes all six widths/heights while retaining their prior centers
with deterministic integer rounding. Distribute uses configured slots 0 and 5 as
endpoints, interpolates both center X and Y at i/5 for slots 1–4, and retains each
intermediate ROI's current size. Endpoints are unchanged. Both operations validate
all six rectangles before committing. Nonpositive/out-of-bounds geometry rejects
atomically; no silent clamping or invented production layout.

Rounding is nearest pixel, exact halves toward positive infinity
(`floor(value + 0.5)`). Gestures use the original drag rectangle and existing bounded
move/resize calculations. Fit, zoom, pan, window size and DPI never persist display
coordinates. Canvas zoom is bounded to 5–800%; fitting axes remain centered.

Existing hit rules remain: a hit on the selected ROI/its handles takes precedence;
otherwise the first containing ROI in configured slot order is selected. Overlapping
fixtures may require moving/resizing the selected rectangle to expose another.
The intended commissioned layout has six nearly non-overlapping rectangles.

Camera switching/Refresh retains one immutable display-store reference and detached
BGR QImage. New runtime frames do not replace it until requested. The canvas shows
camera/source dimensions and simulation labeling when applicable. Leaving releases
pixel references. Runtime retains actual-frame bounds validation. An out-of-bounds
starting layout requires a suitable reference or correction through the existing
configuration API; no hidden coordinate dialog or automatic repair is added.

## Manual review

From the repository root:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
```

These use fake inference and synthetic images. Optional `--frames-dir images`
uses local reference images with the same fake engine. The fake machine layout
persists at `outputs/production_ui_demo/machine.json`. Only when that file is absent,
the demo initializes six full-height vertical partitions per reference image, using
integer boundaries `i * width // 6` for i=0..6. Adjacent boundaries determine each
width, including remainder pixels. Existing saved layouts are always loaded as-is;
opening or refreshing the workspace never regenerates geometry.

Real ViMo uses the unchanged
`run_local_production_ui.py` and supplied `--machine-config` path. Old local Recipes
containing ROI fields fail validation rather than being silently rewritten.

1. Open Parameters → Machine ROI setup; select a camera tab.
2. Set common Width/Height and Apply size to all.
3. Drag the first and last configured slot ROIs into position.
4. Distribute evenly, then fine-tune middle rectangles directly.
5. Switch cameras and repeat; edits remain in the machine draft.
6. Save and apply; next successful Start activates the saved layout.
7. Exercise dirty Back → Cancel / Discard / Save and apply.
8. While RUNNING, inspect using Fit/wheel/pan; geometry edits are unavailable.

The ignored local `outputs/manual_phase3c/` configuration, when prepared, provides
real-model wiring-only geometry for manual calibration; it is not a shipped fixture.
Neither test rectangles nor screenshot geometry constitute production calibration.

## Verification and limits

```powershell
uv run --no-sync pytest tests/unit/test_machine_editor.py tests/unit/test_roi_geometry.py tests/integration/test_roi_editor.py tests/unit/test_configuration.py --tb=short
```

Focused tests cover camera order, direct selection, source-pixel move/resize,
view-only transforms, frozen BGR references, atomic batch rejection, combined Save
and apply, activation boundary, save/preparation failures, dirty navigation and
bilingual portrait/landscape layout. Normal suites retain fake Production and
real-local launcher tests with fake engines. No inference/runtime contract changed.

Offscreen screenshots check composition, not final usability. Physical Windows
DPI/touch, commissioning with actual images, real camera/PLC timing and calibrated
geometry still require human/hardware acceptance. Historical 400-image regression,
long stress and benchmarking are not required for this presentation correction.
