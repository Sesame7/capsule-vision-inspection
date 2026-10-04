# Graphical ROI editor — Phase 2D-2B

Parameters → Channel → Camera/channel → **Graphical ROI editor** opens a focused,
nonblocking Qt dialog. It edits the existing schema-v1 integer `BoundingRect`
override in the same `RecipeDraft`. No model, runtime, persistence or acceptance
logic belongs to the dialog.

## Components and draft contract

- `roi_geometry.py`: Qt-free `RoiInteractionModel`, inverse mapping, hit testing,
  deterministic rounding and bounded move/resize calculations. Its rectangle is
  a drawable projection, not a second configuration store.
- `roi_canvas.py`: detached BGR `QImage`, overlay painting and mouse delivery.
- `roi_editor.py`: selected draft/camera/channel, frozen display reference,
  numeric controls and explicit override/reset actions.
- `ParametersPage`: existing validation/dirty/Save/Apply workflow and lifecycle
  editability. The graphical entry remains available when numeric editing is locked.

The existing Phase 2B resolver supplies effective ROI. Inherited geometry has a
dashed outline and no handles. **Create override** copies the current effective
rectangle. An editable override has a solid highlighted outline, black contrast
halo and eight square handles. **Reset to inherit** removes only that channel's
ROI override, retaining its check overrides and the immutable machine layout.
Both operations modify only the draft; equality with its persisted baseline still
defines dirty state (undoing every edit can therefore return to clean).

The dialog and existing page numeric fields project the same four draft strings.
Mouse edits update both immediately. Numeric input may temporarily be empty,
noninteger or invalid; it is preserved verbatim and existing validation reports
the error. The canvas retains its last valid rectangle, disables gestures and
shows the error until resolution succeeds. Out-of-reference-bounds numeric ROI
is shown with a warning, never silently clamped/rescaled. Domain validation and
runtime frame-shape validation retain their existing responsibilities.

Close retains the draft without saving/applying. Close the dialog to select
another channel/camera on Parameters; returning restores unsaved edits without a
Save prompt. Recipe switch/New/Reload/application close retain the existing dirty
workflow. The editor holds the original draft identity and cannot write into a
replacement draft.

**Validate → Save → Apply → successful Start → Active** is unchanged. Save alone
does not configure a candidate; Apply uses saved clean data. Failed startup
preserves the last-known-good active configuration. ROI editing never resets
production counters. No configuration/schema-version change was made.

## Reference pixels and geometry

Opening/reopening or explicit **Refresh frame** selects the latest display frame
for the selected camera. The editor retains one immutable, byte-backed
`DisplayFrame` from `LatestDisplayFrameStore`, plus one `QImage.copy()` using
`Format_BGR888`. New publications cannot replace this frozen reference or reuse
its pixels. No camera acquisition, polling, history, masks or inference is added.
Closing releases the reference and Qt image. Overlay painting never modifies pixels.

The header identifies camera, generation, cycle, frame sequence and source size;
the demo additionally labels simulated input. No frame means no invented dimensions:
the blank canvas cannot drag, a neutral message explains the state, and numeric
editing remains available when lifecycle permits. Refresh after a resolution
change keeps draft values exactly; an ROI outside new bounds warns and blocks
dragging until the numbers or reference are corrected.

Production overlays and this editor share `ImageTransform` / `image_transform()`.
The original Fit defaults remain unchanged. The additive options support fixed
scale and pan; `source_point()` applies the exact inverse:

```text
display = centered image origin + pan + source * scale
source  = (display - origin - pan) / scale
```

Qt coordinates are logical widget pixels; Qt handles device DPI. Fit uses the
largest aspect-preserving destination rectangle, with letterboxing. Paint and
hit testing use that same transform. Presses in letterbox space cannot begin ROI
gestures. Invalid/zero viewport dimensions produce no interactive transform.

Integer commits use **nearest integer, exact halves toward positive infinity**
(`floor(value + 0.5)`). Every gesture calculates from its original rectangle and
pointer position, avoiding accumulated rounding. Only source-image integers are
written to the draft; display coordinates never enter Recipe JSON.

| Interaction | Contract |
| --- | --- |
| Left drag inside override | Move; preserve width/height and clamp whole ROI to image |
| Left drag handle | Resize selected edges only; eight corner/edge handles |
| Resize past opposite edge | Clamp at one pixel; no handle flipping |
| Handle targeting | Nearest handle within 12 logical pixels per axis; stable tie order |
| Fit | Recompute centered aspect fit on viewport resize |
| Zoom − / 100% / Zoom + | Fixed logical-pixel scale, factor-two steps bounded to 5–800% |
| Middle drag | Pan enlarged image, bounded to its extents; fitting axes stay centered |
| Resize/zoom/Refresh | Cancel any in-progress gesture; presentation only |

Fit, fixed zoom and window resize never modify ROI. Selecting zoom/Fit or resizing
recenters pan; fixed zoom remains fixed across resize. Pan never doubles as a left
ROI drag. Handle targets and strokes remain logical-pixel sized at every zoom.

## Lifecycle and presentation

UNCONFIGURED/READY permit changes; STARTING/RUNNING/STOPPING/FAULT/CLOSED, candidate
preparation and closing are read-only. Both the canvas gesture and draft commit
recheck current application editability, so writes cannot slip through before a
queued Qt snapshot arrives. Numeric edits, Create and Reset have the same guard.
Viewing, Refresh, Fit, zoom and pan remain available while RUNNING.

The normal resizable dialog uses the shared dark tokens and Chinese/English
catalog, minimum 700×620 logical pixels, an expanding canvas and visible numeric
and action rows. Automated offscreen checks cover 1080×1920, 1920×1080 and
900×1200 parent windows in both languages. Physical Windows DPI/touch ergonomics
still require workstation review; this is not a kiosk/calibration wizard.

## Equipment-free acceptance walkthrough

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
```

The existing launcher provides six synthetic frozen references without GPU work.
Newly seeded Demo B has a CH17 ROI override initially equal to its base rectangle.
Existing saved demo files are deliberately not overwritten; if an older Demo B
has no ROI override, explicitly Create override. Initial full-height geometry can
move horizontally; shrink it with an edge/corner to allow vertical movement.

1. Parameters → Demo B → Channel → CAM3 → CH17 → Graphical ROI editor.
2. Confirm reference identity and override/inherited text. Fit; create override
   if inherited. Move, then resize a corner and an edge; observe numeric updates.
3. Change a numeric coordinate. Try invalid text: it stays visible, error appears,
   handles stop. Correct it; try 100%, zoom and middle-drag pan without data changes.
4. Close, select CH18/CAM4 and return to CAM3/CH17; unsaved ROI remains.
5. Reset to inherit, then Create override; geometry comes from effective inheritance.
6. Close, Validate and Save: candidate unchanged. Apply: candidate changes, active
   unchanged. Successful Start activates it.
7. While RUNNING reopen: geometry is visible, edits disabled, zoom/pan still work.
   New source frames do not move the frozen background; Refresh replaces it without
   changing ROI. Close, Stop, reopen: editing returns.
8. Automated tests additionally cover absent references, changed-resolution bounds,
   failed candidate activation and immediate state changes before Qt notification.

Focused tests (offscreen platform comes from the shared fixture):

```powershell
uv run --no-sync pytest tests/unit/test_roi_geometry.py tests/integration/test_roi_editor.py --tb=short
```

No polygons, rotated/multiple ROIs, bulk copy, automatic ROI generation,
calibration, physical units, live acquisition/tuning, model management, autosave
or auto-Apply are implemented. Those require separately scoped work.
