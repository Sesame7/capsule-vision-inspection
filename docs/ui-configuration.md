# UI configuration

This document owns the **Current** Recipe, Vision, Machine ROI, camera-source
and Size/Color tuning workflows. Widget names and responsive layout choices
describe the implemented baseline, not permanent Target architecture requirements.

[Configuration](configuration.md) owns schemas, identities, pure resolution,
artifact paths and atomic persistence. [Inspection](inspection.md) owns evidence,
algorithms and technical/product judgments. [Application](application.md) owns
preparation, candidate installation and successful-Start activation.
[UI shell](ui-shell.md) owns navigation, display delivery, shared feedback and
application close. These workflows do not redefine those contracts.

## Recipes

### Operator workspace

Recipe / 配方 has one expanding catalog selector with compact New, Duplicate and
Reload actions. The full-width workspace has two balanced sections:

- Basic information: editable Recipe name, optional product code, registered model
  selection and a read-only detection-rule summary.
- Inspection: Size/Color enablement and parameter summaries, with one shared
  **Tune preview / 调整预览** entry. Detailed tuning controls are inside that preview.

At sufficient width the sections remain side by side, including practical portrait
sizes; otherwise they stack according to readable control minimums. Name/product
fields share a row equally when they fit and stack when necessary. The Recipe and
model selectors use available width while short actions remain compact. Saved,
dirty and preparing state/local feedback are left on the bottom action row;
**Save and apply** is right. Layout changes do not create more configuration fields.

The page does not expose raw IDs, model paths/hashes/module objects, machine geometry,
ViMo inheritance/required/label-filter editors or a legacy traditional-check panel.
Production's read-only Recipe summary is not another editor.

Model choices use registered identity, not display-name parsing. One available model
is read-only. Deliberately selecting the default uses the existing default-model
mapping; unrelated edits preserve the original `model_override_id`, including an
explicit ID equal to that default. Unknown saved models remain unavailable/invalid,
never silently replaced. Selection does not load ViMo or discover model labels.

A default ViMo policy says all model defect labels participate. A nondefault saved
policy receives a neutral configured-rule summary. Camera/channel overrides show
one notice that these policies are not editable here. Editing or duplicating a
Recipe retains its hidden defaults, overrides and label filters; explicit false,
empty lists and inheritance remain distinct. Only the displayed product settings
change. Their meanings and supported root capsule settings belong to
[configuration resolution](configuration.md#resolution).

### Catalog and draft identity

`RecipeCatalog` discovers only nonrecursive, case-insensitive `*.recipe.json`
filenames. It reports malformed files and duplicate Recipe IDs; ambiguous entries
are not offered as valid choices, and catalog issues block catalog writes. Discovery
does not create or repair files. Valid choices are ordered by display name
(case-folded), then exact Recipe ID. Reload is the explicit external-edit workflow;
there is no watcher or conflict merge.

New/Duplicate open a small name dialog. The UI generates an opaque UUID identity;
the operator supplies a name, not a filesystem path or ID. Existing IDs are not
renamed. New starts with domain defaults: ViMo enabled, Size/Color disabled.
Duplicate copies the current clean draft, including product/model and hidden
policies, changing only identity/name. Resolve previous dirty data first. Cancelling
the creation dialog leaves that resolved draft intact. Creation alone writes nothing.

Saving an existing identity retains its path. A new identity receives a filename
derived from its ID's SHA-256 within the catalog directory; IDs are not paths.
The Qt-free editor's existing Save As support is separate from the normal UI's
New/Duplicate controls, not a newly exposed button or a retired backend capability.

Clean selection/Reload rereads the selected file. A clean **Save and apply** also
rereads it and rejects an external difference instead of rewriting it. Discard
reloads the saved Recipe, or resets an unsaved draft. These checks are single-editor
safeguards, not concurrent-write locking. Normal startup restores only the explicit
last-applied ID; the fake demo's first-entry selection convenience is not normal
discovery policy. See [startup recovery](application.md#startup-recovery).

### Editing and navigation guards

With an available editor, UNCONFIGURED/READY allow product edits and Save and apply.
STARTING/RUNNING/STOPPING/FAULT/CLOSED allow viewing, including clean Recipe selection
and Reload, but prohibit edits/New/Duplicate/Save and apply. Preparation or closing
also blocks duplicate submissions. Handlers and final candidate installation recheck
authoritative state; an enabled control is not permission to bypass it.

Recipe switch, New, Duplicate, Reload and normal close guard a dirty Recipe with
**Save and apply / Discard / Cancel**. Save continues the pending action only after
successful preparation/configuration; failure stays on the current Recipe. Discard
restores the saved baseline before proceeding; Cancel stays. Ordinary shell
navigation retains the Recipe draft. Leaving Tune preview waits for its job and
retains draft changes; it does not save them.

Recipe Save and apply refuses outstanding machine/source edits. Tune preview is
unavailable while machine geometry is dirty and uses the saved layout. Conversely,
Vision Save and apply requires a saved, clean Recipe and never silently saves its
edits. Resolve each draft explicitly; there is no combined all-drafts transaction.
Preparation/setup state and dirty drafts participate in the shell's existing Start
guards, described in [Production controls](ui-shell.md#production).

## Machine ROI

### Vision workspace and identity

**Vision / 视觉** is the operator-facing page name; `settings` is an internal route.
It directly hosts one vertically stacked workspace in both orientations:

| Area | Current groups |
| --- | --- |
| Upper toolbar | Camera icon + number left; Folder/Runtime, Previous/Next, index/dimensions and Refresh center; Fit/1:1 right |
| Source row, when a source draft exists | Folder path, Browse and Loop together, with room for long paths |
| Canvas | Dominant aspect-preserving frozen reference and six labeled rectangles |
| Lower toolbar | Selected ChannelId, X/Y/W/H and numeric Apply left; common-size/distribution center; saved/dirty state and Save and apply right |

Flexible gaps center the middle groups when space permits. Whole groups wrap when
needed, retaining field readability and current touch targets. Icons, selected/
disabled treatment, Chinese/English tooltips and accessibility follow the shell;
there is no separate setup subpage, landscape side panel or permanent six-ROI form.

Camera selectors preserve explicit MachineLayout order, normally CAM1 → CAM6;
custom/reversed order is equally authoritative. The six labeled ChannelIds come
from that camera's configured slots, not parsed camera numbers or arrival order.
Camera changes retain machine/source drafts. Their definitions and sole machine
geometry ownership remain in [configuration](configuration.md#machine-geometry).

### Source-pixel editing

Select a rectangle on the canvas, then left-drag its interior or handles to move/
resize. Selected-rectangle/handle hits take precedence; otherwise the first
containing ROI in configured slot order wins. Overlapping layouts can require
exposing another rectangle before selecting it. This is deterministic hit testing,
not a new nonoverlap constraint.

X/Y/W/H always describe original image pixels. **Apply**, or Enter in a field,
commits only the selected rectangle to the machine draft through the atomic geometry
operation; it does not write a file or install a candidate. W/H also feed
**Apply size to all** for the selected camera:

- Common size gives all six rectangles those dimensions while preserving their
  previous centers to integer rounding precision.
- Distribute evenly keeps configured slots 0 and 5 unchanged, interpolates both
  center X and Y at `i / 5` for slots 1–4, and retains each intermediate size.
- Integer geometry rounds to the nearest pixel, exact halves toward positive
  infinity: `floor(value + 0.5)`. Distribution is by slot, even for mirrored IDs.

Every replacement validates all six resulting rectangles before committing.
Malformed/nonpositive/out-of-bounds numeric or bulk edits reject atomically,
without partial changes or silent correction. The canvas's existing gestures are
bounded at image edges and a one-pixel minimum, computing from the original drag
rectangle rather than accumulating rounding. Those gesture limits are distinct
from rejecting invalid numeric input or repairing a saved layout.

Fit, 1:1, pointer-centered wheel zoom and middle-drag pan change only the view.
The current wheel range is 5–800%; fitting axes stay centered and pan is bounded.
Image aspect ratio/orientation, centered letterboxing and source/display transforms
are preserved. Window size, zoom, pan or DPI never become saved ROI coordinates.
Camera/reference refresh installs a frozen image and returns its view to Fit.

Geometry edits require an eligible decoded reference, allowed application state
and no pending setup/preparation. Without pixels there are no invented dimensions.
An incompatible saved layout is shown with an error, not resized/repaired; commits
still require every resulting rectangle to fit. State changes revoke active ROI
edits; read-only viewing retains Fit/zoom/pan. Save validates available reference
bounds and, for Folder sources, the first complete source group before writing.
This is not validation of every future image; runtime still validates actual frames.

### Saved authority and initialization

Opening, Refresh, camera switching, reopening or selecting another reference never
regenerates MachineLayout. Saved geometry is authoritative even if intentionally
unusual or incompatible. Source changes do not repair geometry.

Only creation of an **absent fake-demo machine file** initializes six full-height
vertical partitions from each reference's actual width W and height H:

```text
boundary[i] = floor(i * W / 6), i = 0..6
slot i = (boundary[i], 0, boundary[i+1] - boundary[i], H)
```

Adjacent boundaries retain remainder pixels, with exact coverage and no overlap;
1056 × 960 gives six 176 × 960 rectangles at x = 0, 176, 352, 528, 704, 880.
This is initial demonstration geometry, not calibration or a parser/resolver default.
Normal startup does not invent a missing layout. Existing files remain unchanged
even when a later demo launch uses different image dimensions.

## Source reference

### Folder settings versus runtime input

Folder is the only operational source type. The selected camera has a folder path
and Browse; Loop is a shared source setting, not per-camera wrapping for the browser.
This page exposes no replay-rate editor or hardware capture feature. Source settings
have their own draft/file, independent of Recipe and MachineLayout. Paths and schema
rules are owned by [camera sources](configuration.md#camera-sources).

The real explicit-path launcher's `--frames-dir`/`--images` are process-only input
overrides. The workspace indicates that override; editing saved Folder sources does
not disable it or persist the fixed-image selection. Fake review has display
references but no Folder source draft. Replay/admission/preflight and exact launcher
precedence belong to [application](application.md#folder-replay), not this browser.

### Frozen Folder / Runtime references

On camera entry the initial reference prefers the latest same-camera display frame
whose source key matches the configured folder. Otherwise it uses that folder's
first supported file. Without a source draft, available same-camera display frames
provide the reference. Explicit Folder/Runtime controls then select the source;
Runtime is disabled without an eligible frame, and a missing explicit reference
does not silently switch sources.

Reference bytes are frozen and owned; canvas QImage uses the same detached BGR
painting ownership as [the shell](ui-shell.md#display-identity). New display frames
update Runtime eligibility only, never current pixels. Runtime Refresh explicitly
freezes the latest eligible frame. These setup references need not be current
production-generation evidence; Production overlay matching has its own stricter
generation/cycle/FrameId/shape contract.

Folder enumeration is nonrecursive, deterministic natural filename order for
PNG/JPG/JPEG/BMP/TIF/TIFF, with case/numeric ties broken by original spelling.
Only the selected file is decoded; there is no folder-wide image cache.

- Previous/Next stop at the first/last file. Replay Loop never wraps the browser.
- Folder Refresh re-enumerates and rereads the same selected filename, not its old
  index; adding earlier files cannot turn Refresh into Next.
- A removed/corrupt/unreadable selected file produces explicit feedback and no
  substitute. The operator may explicitly choose another available file.
- Folder positions, lists and reference modes are per-camera workspace state,
  discarded on leaving. They do not persist or advance runtime replay positions.
- Changing a folder invalidates that camera's old reference/bounds/list/position;
  its next load starts from the new folder. ROI geometry is retained.

Index/count and dimensions are visible; the selected filename/path is in the
tooltip. Folder reads use the existing single setup/preparation slot off Qt:
at most one IO job and one latest camera/reference intent, not an image queue.
Revision checks reject stale completions after camera/folder changes. Controls
prevent repeated reads; camera switching can replace the desired intent. Leaving/
closing waits for actual IO-thread termination before releasing the workspace.

Browsing does not infer, publish display frames, submit cycles, alter counters,
change geometry or write configuration. In particular, browsing a Vision Folder
reference does not supply that file to Tune preview; the latter freezes a frame
from the display store as described below.

## Tuning preview

### Modes, reference and presentation

The single Recipe entry opens Size, or Color when only Color is enabled. Both
modes remain selectable, even if the selected check is disabled. Its explicit
Enabled/Disabled control edits that check's draft capability. Channel OK/NG combines
**all enabled Size/Color checks**, not just the selected mode and not ViMo findings
or final machine acceptance.

The top row has Back, configured-order camera icon/number selectors, Fit and Refresh.
The image remains dominant. Compact mode/enablement and selected-mode controls
wrap as groups rather than stretching numeric fields. Size has width/height targets
and tolerances; Color has target swatch/current-image sampling and Hue tolerance.
Definitions/ranges belong to [capsule settings](configuration.md#capsule-settings);
the shared measurement/judgment algorithms belong to
[inspection](inspection.md#capsule-screening).

On opening, camera change or Refresh, preview freezes the latest display-store
frame **for the selected CameraId** and the saved camera layout. It does not decode
or publish a Folder image, use Vision's browsing position, or perform Vision's
folder-source-key eligibility test. A missing frame shows unavailable feedback
and creates no measurements. Source publication alone does not replace frozen pixels.
Results retain configured slot/ChannelId association; out-of-bounds references
report measurement failures rather than inventing bodies or resizing ROIs.

The canvas shows per-channel combined judgments and available body boxes. Size
annotations use separate `W ...` and `H ...` lines on compact dark backings, with a
fixed logical-pixel font bounded to the correct channel/view and visible viewport.
Labels prefer space above the body, otherwise inside it; offscreen bodies are not
given pinned labels. Extreme zoom-out can leave too little readable room: zoom in
rather than infer missing values. Color mode has no Size captions; Color-only
measurement supplies no fabricated body geometry.

There is no duplicate channel-status row or explanatory footer. Meaningful
measurement/sample errors, unavailable-reference and transient work/disabled
feedback remain. Canvas tooltips retain available rejection reason codes. Shared
technical/fault/closing feedback remains visible under
[shell feedback rules](ui-shell.md#feedback); an earlier production NG does not
describe the current frozen preview.

### Cache and asynchronous lifetime

`TraditionalPreview` holds only the current frozen frame/layout evidence: at most
one background `CapsulePreview` job and one latest desired reference/capability
revision. Measurement snapshots immutable pixels, saved camera layout and enabled
capabilities. It runs image processing off Qt; small cached-evidence rejudgment and
control presentation run on Qt. No ViMo engine or runtime task is created here.

| Change | Current cache behavior |
| --- | --- |
| Targets/tolerances, including a sampled target Hue | Rejudge cached evidence immediately; no remeasurement |
| Selected Size/Color mode or language | Preserve measured capabilities/reference; present/rejudge the existing evidence |
| Enabled Size/Color flags | Clear evidence and request the new capabilities; stale in-flight result is discarded |
| Both checks disabled | Clear results and start no new measurement; an already-running job finishes naturally |
| Camera change or Refresh | Clear results, freeze the newly selected frame and request measurement if enabled |
| New source/display publication alone | Leave the frozen frame and measurements unchanged |
| Leave/close and reopen | Release the workspace after its job exits; reopen from current display/saved layout |

Completion is consumed only after actual job-thread termination. Revision mismatch
cannot install an old camera/reference/capability result; then one latest job may
start. There is no backlog of obsolete measurement requests. Runtime read-only
states disable parameter edits/sampling; viewing remains separate from editing.
Back, shell navigation and close wait asynchronously for active work, disable a
closing workspace, discard late work and release its pixel references. They do
not force-cancel native or CPU work or block GUI dispatch on an unfinished Future.

### Current-image color selection and isolation

Sample image color / 画面取色 is enabled only when editing is allowed, Color is
enabled, no job is active and measured Color evidence is available. The target
uses both view Hues from every channel with usable Color in **both** views; a
one-view-only channel contributes neither sample. Body boxes/Size validity are
not a sampling gate. The existing circular-Hue median combines those samples;
the UI rounds the result and wraps it modulo 180 into the draft target.
Unavailable/ambiguous samples retain the target and explain why. The swatch tooltip
shows the target Hue; it is not a physical color calibration or pixel eyedropper.

Preview calls the same Size/Color measurement and judgment path as production,
with the current draft thresholds. Content rejection remains NG; per-slot
measurement exceptions remain explicit unavailable/error feedback. It submits
no production cycles, inference tasks or engines; it changes no candidate, active
configuration, counters or history. Draft edits synchronize back to Recipe on Back.
Only the normal Save and apply / successful Start workflow persists/activates them.

## Save apply

### Distinct operations and files

The visible operator action is **Save and apply**, not separate Validate, Save
and Apply buttons. It composes persistence and candidate preparation/installation;
it never activates a session. Numeric ROI **Apply** is a different draft operation.

| Action | Files affected |
| --- | --- |
| Recipe Save and apply | Selected Recipe entry only when new/dirty; clean data is reread, not rewritten |
| Numeric Apply, common size, distribute, tuning edits | None; update their respective drafts only |
| Vision Save and apply | Configured machine JSON, then the independent camera-source JSON when a source draft exists |
| Successful candidate Apply in normal `app.py` | Separately requests last-applied Recipe preference persistence; see application policy |

Machine/source files are the paths supplied to the editor, not hard-coded by UI.
Normal startup uses `data/machine/machine.json`, `data/sources/camera_sources.json`
and the selected entry under `data/recipes/`. The explicit real launcher uses
`--machine-config`, `--recipe` and `--sources-config`, defaulting the last to
`camera_sources.json` beside machine JSON. Fake review uses
`outputs/production_ui_demo/machine.json` and its Recipe catalog, without a source
draft or normal manifest persistence. No action here writes model metadata/binaries,
source images or an image archive. Preference outcomes are separately owned by
[application](application.md#preference-persistence-and-feedback).

Recipe Save and apply resolves/validates, atomically saves changed/new data, captures
the saved clean Recipe, then hashes/verifies/prepares on `RecipePreparation`.
Queued completion is observed after thread exit; `ProductionApplication.configure()`
rechecks state before candidate installation. Saved-source requirements remain
part of preparation; Recipe Apply does not prove Folder availability.

Vision first requires a saved clean Recipe. With Folder sources it checks each
configured camera's first naturally sorted image and all ROI bounds off Qt through
the single setup slot. Only successful preflight permits machine save followed
by source save. It then prepares that Recipe against the saved layout and installs
the candidate. Fake/no-source setup uses its available reference bounds instead
of Folder preflight. Saved-data capture and atomic file writes retain their current
GUI-dispatch contexts; heavy hashing and image IO are background, not a blanket
promise that all configuration work is asynchronous.

Successful configuration removes cached Recipe choices with obsolete layouts;
other Recipes need selection/preparation against the new saved layout. It does
not update an already RUNNING session. Only
[successful Start](application.md#activation) promotes a candidate. None of these
actions resets production counters.

### Local feedback and partial success

Saved/dirty/preparing and Save/apply-failed states are factual, not one all-or-nothing
transaction flag. Local Recipe/Vision feedback uses short localized operation/
resource summaries; existing status/message tooltips retain full original details,
including relevant paths and hash diagnostics. Unknown errors do not guess causes.
Successful retry or the relevant local edit/reload clears obsolete local details,
not unrelated global warnings.

| Outcome | Preserved state / feedback |
| --- | --- |
| Recipe validation/write fails | Draft and previous file retained; no preparation/configuration, pending navigation does not continue |
| Recipe saved, preparation/configuration fails | New file remains saved/clean; previous candidate/active remain; report failed Apply and keep pending navigation in place |
| Clean Recipe changed externally | Reject Apply and require Reload/explicit draft resolution; do not rewrite the external file |
| Vision source/bounds validation fails | Neither machine nor source written; retain drafts and report validation failure |
| Machine write fails | Previous machine baseline and geometry draft retained; source save and Apply are not attempted |
| Machine saved, source write fails | Machine is saved/clean; source baseline unchanged and source draft retained; explicitly report partial save, no Apply |
| Machine/source saved, preparation/configuration fails | Both saved baselines remain; failed Apply is explicit and old candidate/active remain |
| Candidate installed, preference write fails | Apply succeeded; candidate is not rolled back or activated; separate manifest repair/restart feedback remains |

The two domain writes are individually atomic, not a cross-file transaction.
Likewise, persistence/preparation/installation/preferences are separate outcomes.
The original active configuration is unaffected until successful Start; background
completion while closing is not allowed to install a late candidate.

Successful Recipe Apply can coexist with unresolved Folder/manifest guidance.
Verified all-source/bounds Save and Apply emits the existing source-verification
notification; browsing one file or editing a path does not. Shared warning priority,
scope and clearing remain authoritative in [UI shell](ui-shell.md#scope-and-recovery).
Independent Apply success and protected/failed preference persistence are presented
together, not reinterpreted as rollback or repaired by an unrelated command.

### Leaving and closing

Vision navigation/normal close prompts **Save and apply / Discard / Cancel** for
machine/source dirtiness. Save leaves only after actual preparation/configuration
success; Discard restores both saved baselines; Cancel stays. Camera switches keep
drafts. A partially successful save means Discard restores each file's **current**
saved baseline, not a fictional rollback of earlier successful writes.

Reference/preparation jobs use one shared slot. A leave request while busy waits
for termination before retrying workspace guards. Normal close also waits for
preview jobs, suppresses late preparation/configuration, detaches delivery after
jobs finish and then follows [application close](ui-shell.md#closing). Keeping a
window responsive or revoking delivery alone is not proof of resource termination.

## Review

### Supported equipment-free workflow

From the repository root, using the verified environment:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
uv run --no-sync python scripts/run_production_ui.py --frames-dir images --language zh --size 1920x1080
```

These use fake ViMo, even with stored images. The default synthetic references have
two-view capsules; Demo B exercises retained hidden ViMo policies. Launching may
create/update ignored demo configuration, so existing demo geometry still matters.
Do not replace calibrated persistent layouts for a screenshot or assume fake
results verify real model behavior. Normal real startup is:

```powershell
uv run --no-sync python app.py
```

It uses existing entry-relative `data/`, not a historical manual configuration.
Prerequisites, explicit-path real launch and CLI-image versus saved Folder input
remain in [application entry points](application.md#developer-entry-points).
Commands are supported workflows, not runs performed by this documentation batch.

For Chinese/English portrait/landscape and a narrower window, review:

1. Recipe selector/New/Duplicate/Reload, expanding fields, single-model read-only
   behavior, long names, hidden-policy notice and aligned Save and apply.
2. Dirty switch/close Save, Discard and Cancel; external clean Reload and failed
   validation/write/preparation without accidental candidate promotion.
3. Vision camera/ChannelId order, frozen Folder/Runtime eligibility, same-file
   Refresh, browser boundaries and camera positions without production side effects.
4. Numeric Apply versus persistence, source-pixel drag/handles, Fit/1:1/wheel/pan,
   common size/distribution and atomic invalid-edit feedback. Calibrate explicitly,
   never infer initial partitions or test rectangles are production geometry.
5. Both preview modes, combined enabled-check judgment, upper/lower W/H captions,
   target/tolerance rejudgment, capability recomputation and current-image color
   selection. Check counters/candidate/active remain unchanged until explicit Apply/Start.
6. Saved-but-not-prepared and partial machine/source outcomes; independent Apply
   success and global preference/source feedback; responsive guarded close.

### Existing verification and limits

Existing tests cover these contracts with synthetic/fake inputs and deterministic
job coordination. Focused commands for future workflow verification are:

```powershell
uv run --no-sync pytest tests/unit/test_recipe_editor.py tests/unit/test_machine_editor.py tests/unit/test_roi_geometry.py --tb=short
uv run --no-sync pytest tests/integration/test_parameters_ui.py tests/integration/test_recipe_refinement.py tests/integration/test_traditional_preview.py --tb=short
uv run --no-sync pytest tests/integration/test_roi_editor.py tests/integration/test_camera_setup.py tests/integration/test_feedback_corrections.py --tb=short
```

The initial-partition/saved-layout tests also live in `tests/unit/test_tools.py`.
Existing UI tests include bilingual orientation/screenshot cases; this batch
inspects their assertions without generating screenshots or rerunning tests.
Previous accepted screenshots and phase measurements remain **Historical** evidence,
not a claim that this reconstruction repeated physical or GPU acceptance.
Documentation-only checks follow [AGENTS](../AGENTS.md): links/anchors, commands,
current wording and whitespace, including direct checks of new untracked files.

**Current limits:** single-writer editing, explicit Reload, no external watcher,
conflict merging, multi-file rollback, automatic schema migration or geometry
repair. Removed Recipe ROI/legacy traditional fields fail strict validation;
`capsule.color` remains supported. External files need deliberate maintenance
against [the current schema](configuration.md#capsule-settings), not a dated
assumption that a particular local Recipe still contains old fields.

**Target / deferred:** commissioned geometry, other lighting/colors/cameras,
accuracy/physical-presence acceptance, native Windows DPI/touch usability, real
camera/PLC integration and hardware throughput. Offscreen DPR=1 tests do not
establish these. The demonstration preview is not calibration, a defect-model
accuracy test or a production acceptance decision. No new diagnostics, History,
Communication, archive or hardware workflow is implied.
