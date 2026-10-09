# Machine ROI workspace — Phase 3C

MachineLayoutDefinition owns exactly one integer `BoundingRect` per physical
channel. Recipes contain checks/product/model data only; Recipe ROI fields are
rejected. The current schema is updated in place, with no migration or legacy
support. Multiple views/ROIs per channel are not implemented.

## Workspace and ownership

Settings is one stacked workspace: CAM1–CAM6 tabs, compact Folder path/Browse
and Loop row, reference-source controls, dominant canvas, selected geometry and
bulk/save actions. There is no repeated page heading, entry button, separate
subpage, nested setup dialog or landscape side panel. The Recipe page contains no
machine-geometry controls.
Folder is the only operational source; hardware capture remains unimplemented.

- `machine_editor.py`: Qt-free machine draft, atomic geometry operations and save.
- `roi_geometry.py`: existing source-pixel transform, rounding and bounded gestures.
- `roi_canvas.py`: frozen BGR image, six labeled ROIs, selected handles, zoom/pan.
- `roi_editor.py`: camera tabs, reference browsing, selected geometry, bulk actions
  and Save and apply.
- `ParametersPage`: existing asynchronous preparation and application-state guards.

The machine draft is independent of Recipe drafts. Camera tabs follow explicit
MachineLayout order, never parsed/sorted names; normal local configuration is
CAM1→CAM6. All six actual ChannelIds are labeled on the canvas. There is no channel
selector or permanent per-ROI form. A compact `CHxx X/Y/W/H` row edits only the
selected channel in source pixels; **Apply** (or Enter) commits through the existing
atomic geometry operation. W/H also supply **Apply size to all**. Bulk actions sit
in a separate row from single-channel geometry, with save/status at the right.

The canvas fills available content space. Camera entry/switch and Refresh fit the
complete frozen image with aspect ratio preserved. Left drag moves/resizes ROIs;
mouse wheel zooms around the pointer where bounded pan permits; middle drag pans.
The compact corner tools are Fit and 1:1. Folder Browse, Previous, Next and
Refresh use icons with accessible names/tooltips; reference modes and state-changing
actions retain text labels. No zoom buttons or large instructional panel are present.

## Save, navigation and activation

In real-local mode, **Save and apply** first validates the six folders' first
images and all current ROI bounds off Qt. It then atomically writes the supplied machine
JSON through `save_machine_layout()` and separate source JSON through
`save_camera_sources()`, and prepares the currently selected saved
Recipe against that saved layout using the existing background preparation thread.
It then calls `ProductionApplication.configure()`. Successful **Start** remains the
only activation boundary. Saved/Unsaved/Saving/Save/apply failed status sits beside the
primary **Save and apply** button. Successful-save tooltip explains next-Start
activation; explicit errors remain visible text. There is no hot modification
of RUNNING configuration.

An unsaved/new Recipe must first be saved on Recipe. The workspace never
silently saves Recipe edits. Machine write failure preserves the previous saved
baseline and dirty geometry. Preparation/configuration failure after a successful
save leaves geometry saved, reports the error and does not claim successful Apply.
Cached prepared Recipe choices with an older layout are removed on successful
configuration; select/apply other Recipes again through Parameters.

Shell navigation away and normal application close cannot silently lose
machine or source edits. A clean workspace leaves immediately. Dirty geometry/source settings prompt
**Save and apply / Discard / Cancel**. Save continues navigation only after successful
preparation/configuration; Discard restores saved geometry and source settings; Cancel stays. Camera
switches retain edits. Preparation/reference IO disables repeat submission; application
close still observes thread termination and suppresses late configuration as before.

UNCONFIGURED/READY allow changes. STARTING/RUNNING/STOPPING/FAULT/CLOSED, preparation
and closing prohibit geometry edits. Gesture commits and actions recheck current
application state. Fit/zoom/pan and viewing remain usable. No reference means no
invented image dimensions and no geometry editing. Invalid batch operations report
errors without disabling subsequent visual correction of valid rectangles.

This is a single-writer local configuration tool, with no external-edit watcher,
conflict merging or history. Save writes the supplied machine JSON and, in real-local mode, separate
`camera_sources.json` (or `--sources-config`). Recipe/model/source images are not
written. The two saves are individually atomic, not one transaction: source-write
failure after machine save explicitly reports that machine geometry was saved,
sources were not, and Apply did not occur. Preparation failure after both saves
leaves both files saved but reports failure. Fake demo has no Folder source draft.

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

The initial reference for each camera prefers its latest runtime display frame
only when the source key matches the configured folder, otherwise the first
supported naturally sorted folder file. Explicit **Folder / Runtime** controls
then select the frozen reference source. Runtime is disabled without an eligible
same-camera/source frame; no fallback silently substitutes another source.
New runtime frames only update eligibility, never the frozen pixels. Runtime
Refresh fetches the latest eligible frame again.

Folder Previous/Next do not wrap, even when replay Loop is on. Refresh re-enumerates
filenames and re-reads the currently chosen file; adding earlier files cannot
advance it. If that file disappears or becomes unreadable, an explicit error is
shown without substituting the next file. Paths, counts and viewed positions are
kept per camera only while Settings remains open; no indices are persisted and
no folder-wide decoding occurs. Metadata shows index/count and current dimensions;
the filename is available in a tooltip. Changing a folder invalidates its old
reference/cache/position and selects that folder's initial reference.

Reference loads are off Qt using the existing single preparation slot, with at
most one IO job and one latest camera intent. Stale completion cannot install a
previous camera/folder image; close observes IO-thread termination. Browsing does
not infer, publish display frames, submit cycles, alter replay indices/counters,
write configuration or change ROI coordinates. Leaving releases pixel references.
Runtime retains actual-frame bounds validation. An out-of-bounds starting layout
is shown with explicit incompatibility and
cannot be saved/applied until corrected. No automatic ROI rescaling, silent clamp,
layout regeneration or resolution conversion occurs; existing atomic geometry
operations still require every resulting rectangle to fit.

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

Real ViMo uses the existing
`run_local_production_ui.py` and supplied `--machine-config` path. Old local Recipes
containing ROI fields fail validation rather than being silently rewritten.

1. Open Settings; select a camera tab and, in real-local mode, choose its Folder.
2. Select Folder, browse Previous/Next, Refresh the current file, and switch to
   Runtime when eligible; camera switching retains folder positions in this session.
3. Select a channel and edit X/Y/W/H with Apply, or set W/H and Apply size to all.
   Drag the first and last configured slot ROIs into position.
4. Distribute evenly, then fine-tune middle rectangles directly.
5. Switch cameras and repeat; edits remain in the machine draft.
6. Save and apply; next successful Start activates the saved layout.
7. Exercise dirty navigation → Cancel / Discard / Save and apply.
8. While RUNNING, inspect using Fit/wheel/pan; geometry edits are unavailable.

The ignored local `outputs/manual_phase3c/` configuration, when prepared, provides
real-model wiring-only geometry for manual calibration; it is not a shipped fixture.
Neither test rectangles nor screenshot geometry constitute production calibration.

## Verification and limits

```powershell
uv run --no-sync pytest tests/unit/test_machine_editor.py tests/unit/test_roi_geometry.py tests/integration/test_roi_editor.py tests/integration/test_camera_setup.py tests/unit/test_configuration.py --tb=short
```

Focused tests cover camera order, direct selection, source-pixel move/resize,
view-only transforms, frozen BGR references, atomic batch rejection, combined Save
and apply, activation boundary, save/preparation failures, dirty navigation and
bilingual portrait/landscape layout. Settings tests also cover same-file Refresh,
non-looping browsing, per-camera session positions, source eligibility, frozen
runtime switching, numeric selected-ROI edits, failed-save status and reference
browsing with zero production side effects. Normal suites retain fake Production and
real-local launcher tests with fake engines. No inference/runtime contract changed.

Offscreen screenshots check composition, not final usability. Physical Windows
DPI/touch, commissioning with actual images, real camera/PLC timing and calibrated
geometry still require human/hardware acceptance. Historical 400-image regression,
long stress and benchmarking are not required for this presentation correction.
