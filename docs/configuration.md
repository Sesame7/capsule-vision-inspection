# Local configuration

Normal [app.py](../app.py) uses `data/` beside the entry file. It restores a stopped
candidate, never an active session, Batch, counters or old results. No first-file
fallback, automatic geometry creation or fake inference is used for recovery.

## Files and paths

| File | Content |
| --- | --- |
| `data/app.json` | v1: `default_model_id`, `last_applied_recipe_id`, `language` (`zh`/`en`) |
| `data/models/*.json` | v1 Model definition: `model_id`, `display_name`, `model_path`, `expected_sha256`, `module_id` |
| `data/recipes/*.recipe.json` | v1 Recipe, selected by explicit ID |
| `data/machine/machine.json` | v1 configured camera order, slots, ChannelIds and ROIs |
| `data/sources/camera_sources.json` | v2 Folder/Basler source mapping, Loop and replay FPS |

Model artifacts are relative to their Model JSON, Folder paths to their source
JSON. The public engine accepts arbitrary model paths; root `models/` is a separate
engineering input area. Normal startup does not substitute it for `data/models/`.
Model SHA-256 is checked during preparation. Artifact work stays off Qt dispatch;
runtime staging is under `outputs/vimo_cache/<sha256>/`, separate from source data.

Parsers reject unknown/duplicate fields, invalid types/versions, duplicate IDs,
malformed hashes and unresolved references. Integers reject booleans/floats. IDs
and labels are exact case-sensitive text, not trimmed aliases. Hashes normalize
to lowercase. File discovery is deterministic/nonrecursive. Missing required data
keeps the shell UNCONFIGURED; a structurally valid candidate with unavailable
Folder inputs can remain READY with a warning but fails Start preflight.
Unreliable saved preferences are not silently overwritten. Invalid optional language
can fall back visibly; required Model/Recipe identities cannot be guessed.

## MachineLayout

`cameras` is a nonempty ordered array. Camera identities are unique; JSON order,
not alphabetical names or Basler enumeration, is machine order. Each camera has
slots 0..5 exactly once, canonicalized into slot order. Every slot contains
`slot`, `channel_id` and `roi: {x,y,width,height}`. Global channels occur exactly
once as contiguous positive IDs 1..N; the six-camera UI requires 36 channels.

ROI origins are nonnegative integers and sizes positive integers in original
image coordinates. Overlap is allowed; actual reference/input dimensions supply
bounds validation. No silent clamping is performed. Saved MachineLayout is the
sole persisted geometry authority: Recipes cannot override it, and opening or
refreshing Vision never replaces or repairs saved geometry.

For newly generated demo geometry only, six vertical slots use boundaries
`floor(i*W/6)` for i=0..6, full height H. This covers the frame without gaps or
overlap, but is not production calibration. An existing saved layout always wins.

## Recipe

Required fields are `schema_version`, `recipe_id`, `display_name` and `defaults`.
Optional fields: `product_code`, `model_override_id`, `camera_overrides`,
`channel_overrides`, `capsule`. Explicit default and override Model IDs must both
exist. Duplicate/unknown camera/channel overrides fail.

ViMo leaves `enabled`, `required` and `disabled_labels` resolve per channel in order
defaults -> camera -> channel. Missing/null leaves inherit; false explicitly
replaces a boolean, and an empty label list clears an inherited filter. Nonempty
lists replace rather than merge. The whole structural objects cannot be null.
Defaults are enabled/required with an empty filter. Filtering changes consumed
findings, not model thresholds, inference masks, returned IDs or shared evidence.

`capsule` has uniform product-level Size/Color settings, without camera/channel
overrides or a configurable `required` switch:

| Fields | Values / omitted defaults |
| --- | --- |
| `size.enabled`, `color.enabled` | boolean / false |
| `size.target_width`, `size.target_height` | positive pixels / 1 |
| `size.width_tolerance`, `size.height_tolerance` | nonnegative pixels / 0 |
| `color.target_hue` | integer 0..179 / 0 |
| `color.hue_tolerance` | integer 0..90 circular distance / 0 |

Omitted child objects/fields use these defaults; explicit null objects are invalid.
Enabled Size/Color are required checks, subject to the explicit confirmed-EMPTY
skip. Occupancy is always on and has no persisted switch/threshold fields. Recipe
ROI, old presence/blob and old per-check color fields are rejected. There is no
automatic Recipe migration or threshold fitting.

## CameraSources

v2 requires global `mode` (`folder`/`basler`), `cameras`, `loop`, `target_fps` and
`schema_version: 2`. Each camera has exactly `camera_id`, `folder`, `basler_serial`.
Folders are strings. Serials are empty or ASCII digit strings, never numbers or
inferred positions. Basler requires exactly six distinct nonempty serials.
Mappings are resolved by CameraId into MachineLayout order. Incomplete drafts
cannot be saved or executed as Basler.

`loop` is boolean; FPS is finite/positive. They pace Folder Replay only. Stored
Folder paths are retained across mode changes but are not shown or used as
independent references in Basler mode. They may be empty. Connect all explicitly uses saved/applied mappings; Start requires
the connected source, validates actual dimensions and warms ViMo with synthetic BGR.
Switching mode preserves both folders and serials. Disconnect before changing
source mapping/mode; Recipe/Batch/page changes do not disconnect cameras.
Live source keys are `basler:<serial>`; Folder keys are lexical normalized paths.

Strict v1 Folder-only files are still in operational use and remain readable.
Loading, browsing and Recipe Apply do not rewrite them. Explicit Vision source
Save writes v2 atomically. This is source configuration, not Recipe/ROI data.

## Editing and persistence

Recipe stacks Recipe management, Model information, Inspection Rules and one
shared Image Tuning workspace. New/Duplicate, explicit selection, metadata and
model selection retain their drafts. Reload remains a catalog-refresh action,
including external new/repaired files; it is distinct from Discard. Single-model
selection is read-only. The compact Model selector shows its display name and
configured SHA-256 prefix; its tooltip exposes the full ID/hash, not a runtime
verification claim. The second row shows compact default defect-label checkboxes,
wrapping at the available width. There is no filter popup, technical-details or
policy-summary panel. The three summary cards use their content's natural height;
one enclosing scroll area updates on wrapping/content/language changes and scrolls
only when needed, leaving Save/apply accessible.
Opening Recipe reads the selected segmentation module's complete
`ModuleInfo.features` catalog through the inference adapter, off Qt dispatch. One
bounded metadata job and one current catalog are owned by the page; stale results
cannot populate another model. Metadata IO does not occupy preparation or gate
Start; Close observes actual job termination before detaching delivery. It
hash-checks a memory snapshot before SDK metadata loading; no inference module,
pipeline or cache is created. Missing/invalid metadata reports an error without
offering an inferred catalog. Checked labels participate; unchecked labels update
only draft `defaults.vimo.disabled_labels`. Checking all catalog labels leaves no
filter when no other exclusions exist. Previously saved out-of-catalog exclusions
remain preserved with a tooltip, not offered as catalog choices. Metadata failure
leaves saved/draft exclusions unchanged and uses existing feedback; re-entry retries
the read without a polling/retry loop. Labels retain exact
case-sensitive identities. Default enabled/required and camera/channel overrides
are unchanged; defaults -> camera -> channel resolution still applies. Save/apply,
Discard and runtime editing locks govern this edit like other Recipe changes.

The Model selector row also offers **Prepare TensorRT cache**, independent of
Recipe Save/apply. It submits the selected immutable Model identity to the
[application owner](architecture.md#inference); the UI requests initialization only,
so neither connected cameras nor Folder inputs are needed. The adjacent icon shows
unknown, preparing, succeeded or failed with localized tooltips. Preparation can
take minutes; Close waits asynchronously for native work and cleanup, and Start
is unavailable while preparation owns resources. Recipe selection/editing retains
its existing stopped-state rules; changing the Model never retags an earlier result.

Status is a bounded, session-only observation of the latest request, never inferred
from cache files or restored on launch. Success requires initialization and actual
worker cleanup; the tooltip distinguishes initialization-only from optional sample
inference evidence supplied through the application API. It does not certify
permanent cache validity, activate a Recipe or replace Production warm-up. The
operation never saves/discards drafts, changes sources, produces counters/results
or reports to PLC. SDK artifacts remain opaque; no delete/rebuild policy is added.

Occupancy stays always on. The Size/Color rule rows alone open/switch/collapse
tuning; their separate enablement checkboxes never expand it. Both checks may be
enabled, with only one tuning panel visible and no second Collapse action.
Switching panels reuses
the same frozen image and cached measurement ownership. Camera selection, Fit,
Refresh, numeric parameters and image color sampling remain draft-only, with no
production admission, counters, source commands or activation. The former separate
Tune page and duplicate enablement controls are removed. Size and Color summaries
use actual draft limits, including `H <target> ± <tolerance>` for circular Hue.

On first selection, Folder tuning reads the camera's first decodable image in the
normal deterministic Folder order. Previous/Next and the image selector browse
that Folder without wrapping; Refresh rereads the selected image, not the next.
Basler tuning instead uses a frozen display frame matching the camera and current
source key, without acquisition or a Folder fallback. Temporary image import is
available in both modes. Each camera retains one selected frozen reference while
tuning remains open; new display publications and switching Size/Color never
replace it. Import/selection/Refresh are explicit changes. Closing tuning releases
its references. IO and measurement share one bounded background job; stale work
cannot attach pixels or measurements to another camera.
The centered image selector reports actual pixel dimensions, connected camera
dimensions on a blank canvas, or explicitly schematic ROI extents when neither
is available. Schematic extents never become measured image bounds or evidence.
Image tools reuse Vision's icons, whole-group wrapping, Fit and 1:1; Recipe
management icons retain localized names/tooltips and the existing confirmations.

Click an upper/lower view in the current image to select its camera, ChannelId
and view. A single detail area beside the parameters shows actual body W/H with
configured limits, or actual Hue with target/circular tolerance. View status uses
the existing Size/Color judge's reasons for that capability and view; it is not
the combined channel result or production acceptance. Image overlays keep compact
channel judgments/body outlines and a selection highlight, without twelve repeated
W/H captions. Threshold edits rejudge cached evidence; mode switches retain the
selection. New images/camera changes clear it; stale asynchronous evidence is
never displayed as the selected view's measurements.
Disabled checks, unavailable/pending evidence, execution ERROR and content NG are
distinct; missing scalars display `—`, never ROI-derived measurements. Tune does
not collect Occupancy evidence or infer EMPTY from a missing body. Routine
always-on/unavailable explanations are omitted from Qt presentation; actual
production Occupancy state/evidence and technical errors remain visible. This
does not change Size/Color judgment or the production EMPTY skip contract.

Recipe Discard beside Save/apply confirms reloading the selected Recipe from its
actual saved file. Read/identity/validation failure preserves the current draft;
MachineLayout and sources are independent. Delete requires a confirmation naming
the saved Recipe and ID, refuses active/candidate references and the normal app's
persisted last-applied ID, and fails closed on unreadable preferences. The catalog
only deletes the unchanged selected Recipe file inside its directory. It never
deletes shared models/images. Success clears selection explicitly without choosing
another Recipe or modifying application references. Runtime editing locks apply
again when confirmed, and persistence errors stay observable.
Vision owns source setup and canvas-first Machine ROI editing, numeric Apply,
Common size and Distribute evenly. Those operations reject invalid bounds atomically;
they never silently clamp.
Vision stacks two subtly contrasted source and ROI containers. One rounded
Folder/Camera track moves its selected thumb without animation; contextual Loop/FPS
or connection/discovery controls align right. Cards retain configured row-major order:
two columns on narrow workspaces, three when at least 1092 logical pixels are
available for cards. Camera headers use the same icon + numeric identity as
Production, with full localized names in accessibility text/tooltips. Card headers
select the displayed camera; inline Folder path/browse and discovered-device
selection edit that camera's existing source draft without selecting it.
Basler serials are selected from discovered devices, not entered manually.
Undiscovered saved assignments remain visible and unchanged; discovery never
auto-assigns or clears them. Devices used by another card cannot be assigned twice;
clear the previous assignment explicitly before moving one. Device model/serial,
connection state and actual dimensions are shown only where observed.
Connected serial/model/dimensions come from the application-owned connection
snapshot and survive leaving/reopening Vision. An empty page-local discovery
list means no discovery evidence, never disconnection; only discovered devices
are newly assignable. Actual Disconnect/source FAULT still determines status.
Discovery reads device information off Qt dispatch without opening cameras or
assigning them by enumeration order. Cards show explicit connection state; assignment alone is not
connection status or live preview. Connect all/Disconnect are stopped operations
off Qt dispatch, owned by the application rather than the widget. Source fields
are locked until Disconnect; ROI editing remains subject to inspection locks.
There is no separate source dialog. Folder Loop and an editable integer FPS stepper
are directly accessible on the source toolbar. Explicit FPS edits accept 1–10;
boundary step buttons are disabled. Existing fractional or out-of-range FPS values
remain unchanged, with the full value in a tooltip and stepping disabled until
explicit valid entry. Focus changes never clamp or rewrite them. This UI range does not
change the persisted positive/finite FPS contract. Basler does not use these pacing
settings. Connection/discovery actions are compact icons with localized tooltips;
the connection summary is numeric. Routine device/status prose is omitted;
Vision errors use the existing bottom feedback strip with full diagnostic tooltips.
That feedback is page-scoped and never supersedes application FAULT or shutdown.
The global strip normally shows the saved source mode rather than repeating READY:
Folder is a source selection, not device connectivity or a continuously verified
camera count. Camera counts come from actual connected CameraId/serial matches.
Unsaved selections do not change that strip. Lifecycle transitions and errors keep
precedence; source availability does not establish full inspection readiness.
Save/apply remains primary, Discard secondary; a compact status symbol exposes
clean, dirty, saving or failed state through localized accessibility text/tooltips.
The image selector and previous/next browse Folder inputs. Import loads a temporary
image; Use runtime image freezes the latest eligible same-camera/source display
frame, with explicit refresh. Neither tracks live pixels. Fit and 1:1 retain their
canvas transforms. The image toolbar center always describes the current canvas:
real pixels show dimensions and the Folder index where applicable; a blank view
shows known dimensions; an unbounded view says ROI schematic, with its illustrative
meaning in a tooltip. The chooser list is separate from this current caption;
source changes and asynchronous image delivery cannot reuse an old schematic caption.
The canvas does not repeat status text. ROI channel selection changes
the displayed slot; numeric Apply edits only the machine draft until Save/apply.
Mode selection edits the same CameraSources draft and requires explicit Save/apply;
selecting a card never changes saved sources or geometry. Runtime editing locks remain.
Save/apply permits structurally valid ROIs without canvas images; numeric
editing does not require image pixels. Available connected dimensions supply bounds
checks, always repeated before Start. Without pixels, known dimensions display a
blank bounded canvas with the saved/draft ROIs. Without trustworthy dimensions,
ROI extents provide a dashed, explicitly labeled schematic only: they never become
camera dimensions, draft bounds, Frames or persisted data. Selection/Fit/zoom and
numeric editing remain available; real bounds reject incompatible geometry without
repair when an image or connected dimensions become available.
Folder mode still validates all configured inputs at Save/Start. Drawing needs an
actual image; synthetic warm-up images are never published. Images are temporary
canvas content, not independent persisted
reference configuration. Replacing an image never rescales, repairs or saves ROIs.

With Basler connected and inspection stopped, **Capture image** waits for the
selected camera's next valid external-triggered frame (ten-second deadline).
It never sends a trigger. Only one request can be pending; Cancel, camera/source
changes, Disconnect, Start and Close invalidate it. Waiting, timeout, cancellation
and success are explicit. Delivery is asynchronous and owns immutable BGR pixels;
late callbacks cannot replace a different camera's image. This operation does not
publish Production frames, infer, count or report. During inspection only existing
frozen Runtime viewing is available. No image history/export/library is created.

Tune preview measures the current draft without Production admission, accounting,
candidate activation or source reconfiguration. Frozen-reference ownership,
camera/mode switching and cache invalidation remain separate from live display.

**Save and apply** separates three outcomes: atomic file persistence; clean saved
candidate preparation/installation; activation on a later successful Start. Failure
after a file save must not be described as a complete rollback or successful
activation. Recipe saves its Recipe file; Vision saves MachineLayout/source files
appropriate to its draft; the manifest records successfully applied selection and
language preferences. Model binaries and source images are never edited by these UIs.
Dirty navigation/close asks to save/discard/cancel; runtime lifecycle locks editing.

Vision's restore icon beside Save/apply confirms discarding all unsaved source,
Loop/FPS and machine geometry edits together. It reloads and validates the current
files off Qt dispatch before replacing either draft/baseline, including partially
saved multi-file outcomes. Read/validation failure keeps both drafts and reports
details in the bottom strip. It neither writes files nor prepares/activates a
candidate, commands sources or resets counters. Viewing pixels, selection and zoom
remain temporary viewing state. The action is disabled when clean, editing is locked,
IO is busy or a capture is pending. Changed saved camera identities/order require
reopening the application; connected sources must be disconnected explicitly before
reloading an externally changed source mapping. Navigation's Discard uses the same
file reload and waits for its completion.

Files use same-directory temporary write, fsync and atomic replace. Failures retain
the previous file where replacement did not succeed, and preserve distinct partial
success feedback across multi-file operations. Do not hand-edit running sessions
or treat a saved candidate as the active session's configuration.
