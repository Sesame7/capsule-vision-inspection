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

`loop` is boolean; FPS is finite/positive. They pace Folder Replay only. Basler
uses Folder images for references and readiness warm-up, then waits passively for
external hardware triggers. Switching mode preserves both folders and serials.
Live source keys are `basler:<serial>`; Folder keys are lexical normalized paths.

Strict v1 Folder-only files are still in operational use and remain readable.
Loading, browsing and Recipe Apply do not rewrite them. Explicit Vision source
Save writes v2 atomically. This is source configuration, not Recipe/ROI data.

## Editing and persistence

Recipe supports selection, New/Duplicate/Reload, metadata/model selection, defect
filters, Size/Color enablement and one Tune preview entry. Hidden policies remain
in drafts and saved data. Single-model configuration is read-only selection.
Vision owns source setup and canvas-first Machine ROI editing, numeric Apply,
Common size and Distribute evenly. Those operations reject invalid bounds atomically;
they never silently clamp. Folder/Runtime references are explicit frozen images.

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

Files use same-directory temporary write, fsync and atomic replace. Failures retain
the previous file where replacement did not succeed, and preserve distinct partial
success feedback across multi-file operations. Do not hand-edit running sessions
or treat a saved candidate as the active session's configuration.
