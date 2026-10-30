# Local configuration

Normal [app.py](../app.py) uses `data/` beside the entry file. It restores a stopped
candidate, never an active session, Batch, counters or current result. Historical
records remain separately browsable without restoring application state. No first-file
fallback, automatic repair of saved geometry or fake inference is used for recovery.

## Files and paths

| File | Content |
| --- | --- |
| `data/app.json` | optional `last_applied_recipe_id`, `language` (codes below) |
| `data/communication.json` | optional global Production reporting, diagnostic endpoint and Web preferences |
| `data/history.sqlite3` | generated internal SQLite Production history; not operator configuration |
| `data/models/<model_id>/model.json` | Model definition: `display_name`, `expected_sha256`, `module_id` |
| `data/models/<model_id>/model.vimosln` | Managed Model artifact, verified by SHA-256 |
| `data/models/<model_id>/vimo_trt_cache/` | SDK-owned generated TensorRT artifacts |
| `data/recipes/<recipe_id>.json` | Recipe with an optional direct `model_id` |
| `data/machine.tsv` | 30/36 explicit ROI rows keyed by ChannelId; fixed camera/slot mapping |
| `data/camera_sources.json` | Folder/Basler source mapping, Loop and replay FPS |

Each managed Model owns its fixed `model.vimosln` beside `model.json`. Folder
paths resolve relative to `data/camera_sources.json` (the `data/` directory). The
public engine still accepts arbitrary model paths; root `models/` is a separate
engineering input area. Normal startup does not substitute it for `data/models/`.
Model SHA-256 is checked during preparation and again on the SDK owner before
native initialization. Artifact work stays off Qt dispatch. Managed Production
and Prepare TensorRT cache directly load the same authoritative
`data/models/<model_id>/model.vimosln`; the SDK generates/reuses adjacent
`vimo_trt_cache/`, without another runtime model copy. Model content and
`model.json` remain unchanged. Normal SDK TEMP provider/timing-cache writes are
permitted and do not affect Model identity or configuration readiness.
Explicit-path engineering tools retain their default `outputs/vimo_cache/<sha256>/`;
old output caches are neither migrated nor removed automatically.
Obsolete managed `cache/<sha256>/` staging trees are not used or automatically
migrated/deleted.

The five JSON definition families are versionless; `schema_version` and obsolete aliases
are unknown fields, not migration inputs. Nothing automatically rewrites existing
files. Parsers reject unknown/duplicate fields, invalid present values, duplicate
IDs and malformed hashes. Incomplete definitions may be saved where documented;
preparation and runtime reject unresolved references. Integers reject booleans/floats. IDs
and labels are exact case-sensitive text, not trimmed aliases. Hashes normalize
to lowercase. Model discovery visits immediate child directories only, reading
`model.json` in each; root-level files and deeper descendants are not managed
entries. Recipe discovery reads immediate `*.json` files. Missing directories
are empty catalogs and discovery creates nothing. Malformed/unreadable entries
remain visible with their paths; unrelated valid entries remain usable. Identities that collide under casefold are all excluded with diagnostics, never
chosen arbitrarily.
Only the selected Recipe and its actual Model/layout/source dependencies govern
candidate readiness; unrelated catalog issues are nonblocking. Missing required data
keeps the shell UNCONFIGURED; a structurally valid candidate with unavailable
Folder inputs can remain READY with a warning but fails Start preflight.
Unreliable saved preferences are not silently overwritten. Invalid optional language
can fall back visibly; required Model/Recipe identities cannot be guessed.

`app.json` may be absent or `{}`: the language defaults to `zh` and no Recipe or
Model is selected. Its only fields are optional `last_applied_recipe_id` (nonempty
text or null) and `language`; there is no global/default Model. Startup never
chooses the first Recipe/Model. A selected Recipe resolves its own exact `model_id`.
Missing preferences do not block the shell or available configuration editors;
other required data still governs editor availability and Production readiness.
Only explicit language changes or successful Recipe Apply write preferences.
Malformed files remain unchanged, and unknown fields are errors. An unsupported
language retains the existing visible, in-memory `zh` fallback with writes blocked
until repair; it is never silently persisted.

Language identities, validation, native names and display ordering belong to the
Qt-independent `localization/languages.py`. Static base text/resolution and authored
translations live in `localization/catalog.py` and `localization/translations.py`.
Configuration preferences persist only the selected code and import that identity
contract; visual styling and widgets consume Localization directly. Missing optional
text uses canonical English; an unknown key fails before fallback.

UI language codes are exact: `zh` (Simplified Chinese), `zh-TW` (Traditional Chinese),
`en` (English), `ja` (Japanese),
`ko` (Korean), `es` (Spanish), `pt-BR` (Brazilian Portuguese), `it` (Italian),
`id` (Indonesian), `fr` (French), `de` (German), `ru` (Russian), `tr` (Turkish),
`nl` (Dutch), `pl` (Polish), `uk` (Ukrainian), `ro` (Romanian), `cs` (Czech),
`hu` (Hungarian), `sv` (Swedish), `da` (Danish), `fi` (Finnish),
`nb` (Norwegian Bokmål), `sk` (Slovak), `bg` (Bulgarian), `hr` (Croatian),
`sr-Latn` (Serbian Latin), `el` (Greek), `pt-PT` (European Portuguese),
`vi` (Vietnamese), `ms` (Malay), `fil` (Filipino), `sw` (Swahili),
`th` (Thai), `hi` (Hindi), `bn` (Bengali), `ta` (Tamil), `te` (Telugu),
`mr` (Marathi), `ne` (Nepali), `si` (Sinhala), `am` (Amharic), `km` (Khmer),
`lt` (Lithuanian), `lv` (Latvian), `et` (Estonian), `sl` (Slovenian),
`sq` (Albanian), `ca` (Catalan), `af` (Afrikaans), `zu` (Zulu),
`ha` (Hausa, Latin/Boko), `kk` (Kazakh Cyrillic), `uz` (Uzbek Latin),
`mn` (Mongolian Cyrillic), `so` (Somali Latin), `tk` (Turkmen Latin),
`tg` (Tajik Cyrillic), `az-Latn` (Azerbaijani Latin), `mg` (Malagasy Latin),
`ky` (Kyrgyz Cyrillic), `ka` (Georgian), `hy` (Armenian) and `ti` (Tigrinya, Ethiopic).
Tigrinya has an independent catalog, not Amharic translations.
Afrikaans and Zulu use Latin script;
Hausa preserves ɓ/ɗ/ƙ and Uzbek uses oʻ/gʻ. Alternative scripts are not aliases.
`az-Latn` supports Latin-script Azerbaijani only; `az` and other scripts are not aliases.
The collapsed selector uses compact language labels; its popup shows full native
names in a single column that expands to fit all 64 languages when screen height
permits. On shorter screens the popup stays within the available work area and
provides scrolling. Display order pins `zh`, `zh-TW`, `en`, then sorts Latin-script
native names case/accent-insensitively. Greek, Cyrillic, Armenian, Georgian,
Japanese, Korean, Thai, Devanagari, Bengali, Tamil, Telugu, Sinhala, Ethiopic and Khmer groups follow;
native-name order is stable within groups.
This presentation order is independent of saved codes and does not change with
the active language or theme. `pt-PT` and `pt-BR` are distinct.
It saves only the existing language preference;
switching does not save configuration drafts or change an active inspection.
Existing `zh`/`en` and omitted-language files remain compatible. Unknown codes
fall back to Chinese in memory with a warning, preserving the file for repair.
Optional untranslated text uses English. Technical identifiers, model labels and
reason codes are preserved. Visible captions, short states, confirmations and
icon-only action names remain localized. Routine explanations that repeat labels,
states or other controls are omitted. Tooltips identify actions, explain essential
operation limits, or retain elided identities and specific operator-recovery
information. Routine runtime telemetry and diagnostic expansions are omitted.
Layout is left-to-right; the supported minimum is
1080 Qt logical pixels. Windows must provide fonts for the selected script,
including Georgian, Armenian, Vietnamese marks, Thai, Indic scripts, Sinhala,
Ethiopic and Khmer.
Qt uses existing Windows font fallback (such as Nirmala UI for Indic scripts and
Sinhala, Ebrima for Ethiopic, Tahoma for Thai and Leelawadee UI for Khmer); fonts
are not downloaded, bundled or converted at runtime.

Normal GUI startup suppresses Info messages in exactly `qt.text.font.db` through
Qt's native logging rules; candidate-font rejection messages do not establish
missing glyphs. Warning/Critical/Fatal and other categories retain their normal
behavior and logging destinations. Explicit `QT_LOGGING_CONF` and
`QT_LOGGING_RULES` settings retain Qt's precedence over this application rule.

## History storage

Normal startup creates the history owner independently of configuration readiness.
Both Folder and Basler terminal Production results are eligible. The database
uses internal schema version 1 and may have SQLite `-wal`/`-shm` sidecars. Preserve
these files with other local assets; do not copy an active database without its
SQLite consistency requirements. An unsupported schema, corrupt database or
unwritable path is reported as unavailable, never silently reset or replaced.

New records capture the active resolved Recipe's display name alongside its stable
ID in the compact detail payload. Renaming a Recipe does not relabel past cycles.
The current detail payload contains channel evidence, camera evidence and an
optional Recipe name. Exactly three elements are required; former two-element
payloads are unsupported and follow the existing corrupt-record query error
behavior. A current null name remains valid and displays the existing missing-name
Recipe ID presentation. History never looks up a current file to invent a name.

Records expire incrementally after 30 days by their host completion observation,
not camera exposure time. This does not remove archived images, link filenames or
restore Batch labels/counters on launch. History has no retention settings or JSON
preferences. In-memory diagnostic/fake compositions may omit persistence. See
[ownership and bounds](architecture.md#production-history) for shutdown, loss
observations and query limits.

## Communication preferences

`data/communication.json` is independent from `app.json`, Recipes, MachineLayout
and CameraSources. Communication's explicit **Save settings** atomically replaces
this file (same-directory temporary write, fsync, replace); it does not change any
other asset or start a network owner. Network/reporting fields are drafts until a
successful Save. The Web header's power action is the specific exception: it
persists only `web.enabled` from the actual validated file, then requests runtime
start/stop using saved Web network settings. It never saves unrelated drafts or
changes effective reporting selection. An invalid file is not silently replaced;
first-use defaults can establish a missing file. Unsaved reporting edits never change next-Start admission; diagnostic
requests continue to use the effective saved endpoint, not draft text. Save and
reporting publication are serialized with Start under the existing Application
lock. An unchanged reporting selection permits saving independent Web/diagnostic
preferences during Production. Active sessions keep their frozen settings.

Confirmed Discard reloads the actual file through this codec, without writing or
undoing requests/results. On any reload/validation failure (including a missing
file before the first Save), the complete draft is preserved. Default settings
can be explicitly saved to establish that initial file. Save/Discard feedback
and actions remain outside the scrolling content.

The versionless object permits optional `reporting`, `diagnostic` and `web`
sections; `{}` means neutral defaults, with reporting and Web disabled. Unknown
or duplicate fields and invalid present types/ranges are rejected. Superseded
`endpoint` envelopes, `transport` fields and protocol aliases are not accepted;
startup reports them without rewriting the file. Explicit Save writes this format.

The codecs construct the same immutable protocol-specific endpoint values for
Diagnostics and reporting. Reporting's address/capacity belong to its separate
in-memory settings; this does not add an endpoint envelope to the saved format.

Reporting and Diagnostics are flat endpoint objects. `protocol` is `fins_udp`
(omitted default) or `fins_tcp`; `host` defaults to `127.0.0.1`, `port` to 9600
(1–65535), and `timeout` to 1 second (0.05–30). UDP alone permits `destination`
and `source` objects: defaults are network/node/unit 0/2/0 and 0/1/0 respectively;
individual routing fields may be omitted, with existing FINS ranges enforced.
TCP rejects persisted UDP routing fields and negotiates its nodes internally.
Switching to TCP and saving drops those fields, without retaining a hidden route.

Both sections also accept `protocol: "modbus_tcp"`. Its optional `host`,
`port`, `timeout` and `unit_id` default to `127.0.0.1`, 502, 1 second and 255.
Unit ID must be an integer 0–255; port/timeout retain the ranges above. FINS
`source`/`destination` and all unknown fields are rejected for Modbus. Diagnostics
rejects reporting-only `base_address`/`enabled`. A diagnostic endpoint never
enables reporting or shares its Unit ID implicitly. Explicit Save removes fields
inapplicable to the selected protocol. For example, default port and
Unit ID are omitted:

```json
{"diagnostic": {"protocol": "modbus_tcp", "host": "192.168.1.20"}}
```

Holding Register addresses are zero-based, independent of FINS DM/WR. Watch
groups remain session-only and are never implicitly converted by a protocol edit.

Reporting additionally permits `enabled` (default false) and `base_address`.
FINS defaults to 49 (integer 0–508, WR area). Modbus has **no default base**:
it must be explicitly supplied as an integer 0–65532 whenever reporting is enabled.
A disabled Modbus definition may omit it for later completion. Present null,
invalid types and out-of-range bases are rejected. Save always retains an explicit
Modbus base, including 49; it is never interpreted as the FINS default.
Sequence is at base; bitmap words start at
base+1. A 30-channel session writes two bitmap words and never base+3; 36 channels
writes three. The four-register window must fit the protocol's address space.
Modbus initializes through FC03, writes bitmaps through FC16, and commits through
FC06 only after validated bitmap ACK and attribution. Save only changes
future Starts; active settings/address range stay frozen. Queue limits and channel
bit mapping are not settings. Defaults retain W49–W52 behavior.

For example, an enabled Modbus reporting selection with default port/timeout/Unit
ID serializes as:

```json
{"reporting": {"protocol": "modbus_tcp", "enabled": true, "base_address": 100}}
```

Save omits default fields and whole default sections. Nondefault settings remain
even when a service is disabled; endpoint sections written by Save identify their
protocol explicitly. For example:

```json
{
  "reporting": {
    "protocol": "fins_tcp",
    "host": "192.168.251.1",
    "enabled": true,
    "base_address": 120
  },
  "diagnostic": {
    "protocol": "fins_udp",
    "destination": {"node": 7}
  },
  "web": {"enabled": true}
}
```

`web` permits `enabled` (default false), `host` (127.0.0.1), `port` (8765) and
`interval_ms` (500 default; 500/1000/2000). Only
127.0.0.1 or a specific unicast IPv4 is supported. Saving/reloading preserves
unavailable adapter addresses; actual service startup checks local assignment and
subnet validity. There is no DNS, wildcard fallback or automatic IP substitution.

A missing file leaves reporting and Web disabled, without blocking valid
Production startup or creating a file. Communication shows **Defaults · not saved**;
localhost/UDP and Web 127.0.0.1:8765 at 500 ms are neutral editing defaults, not a
saved Production destination. Camera source mode never selects a PLC endpoint.
Explicit Save establishes the file; Discard cannot reload a nonexistent file and
preserves the draft with feedback. Valid files restore their exact saved
settings, including Web auto-start when enabled, without rewriting them.
Invalid files remain unchanged, with visible recovery
feedback; reporting and Web stay disabled in memory until reviewed. An explicit
Save replaces the invalid file with validated settings. If a saved Web address is
no longer assigned or its port cannot bind, optional service startup fails visibly
without changing the file or invalidating Production readiness.

Restored reporting selection is frozen at the next Production Start, never a
connection or authorization to transmit at startup/warm-up. Runtime observations,
ACKs, watches and current counters are not persisted as Communication settings.
Terminal Production results have separate History storage. Saving does not alter the
running Web service. The header power action changes saved enablement; a failed
write prevents runtime action, while failed startup leaves saved enabled distinct
from actual FAILED. Apply changes is available only for differing saved/running
settings, after Web network drafts are saved/discarded. It uses the existing safe
restart path. The next process launch restores saved enablement. Runtime settings
and URL are presented separately from edited/saved values.

## MachineLayout

`data/machine.tsv` is UTF-8 tab-separated text with exactly this header:

```text
channel	x	y	width	height
1	0	0	176	960
```

The example shows one row; a valid table requires exactly 30 or 36 data rows.
Channels must be unique and contiguous 1..30 or 1..36. All five cells are unsigned
ASCII decimal integers; width/height and ChannelId are positive. Headers, column
counts, malformed numbers, blank data rows, duplicate/missing/out-of-range channels
and invalid ROI geometry are rejected. UTF-8 with or without BOM is accepted.
Reads use explicit ChannelIds regardless of row order; Save writes ascending IDs.
The resulting immutable layout is canonical CAM1..CAM5/CAM6 with six slots each:
`camera_index = (channel - 1) // 6 + 1`, `slot = (channel - 1) % 6` and
`camera_id = CAM{camera_index}`. CameraId and slot are not separately persisted.
An in-memory machine definition with a contradictory mapping/order is invalid;
mirrored spatial arrangements use explicit ROI coordinates without remapping IDs.
There is no JSON MachineLayout loader or fallback to `data/machine/machine.json`.

ROI origins are nonnegative integers and sizes positive integers in original
image coordinates. Overlap is allowed; actual reference/input dimensions supply
bounds validation. No silent clamping is performed. Saved MachineLayout is the
sole persisted geometry authority: Recipes cannot override it, and opening or
refreshing Vision never replaces or repairs saved geometry.

With no saved MachineLayout, Vision starts with six editable source placeholders
and no ROI geometry. CAM1–CAM5 are required; removing/restoring CAM6 selects 30/36
channels. Each new camera initializes once from actual Folder/imported/captured
pixels or observed connected dimensions: boundaries `floor(i*W/6)`, i=0..6,
partition the full width, with y=0 and height H. Width must be at least six pixels.
Without dimensions, geometry stays uninitialized. This is an editable template,
not calibrated geometry; changing references never replaces initialized ROIs.

Vision keeps six configuration positions. An unconfigured CAM6 is not an offline
configured camera. While stopped and disconnected, its minus action confirms
removing CAM6 geometry/source from the draft; ordinary Save/apply makes the edit
effective. No automatic backup is created. On an existing saved machine, its plus
action copies only the current CAM5 draft's six ROI rectangles into independent
CAM6 values, preserving slots 0–5
and assigning CH31–CH36. CAM1–CAM5 are unchanged; CAM6's Folder path and Basler
serial remain empty. This is an editable starting template, not verified calibration;
actual CAM6 image/dimension bounds are still validated without clamping. Nothing is
persisted or activated by + alone. Before the first MachineLayout Save, restoring
CAM6 instead leaves geometry uninitialized until its own dimensions are known.
Incompatible channel numbering on removal fails instead of being remapped.
MachineLayout count changes do not depend on unrelated Recipes or Model resolution.
Prepare/Apply still resolves the selected Recipe's Model and verifies its artifact
before replacing the candidate.
Discard reloads actual persisted files, including after partial multi-file saves;
inconsistent saved MachineLayout/CameraSources fail without replacing the draft.

## Model identity and storage

A managed Model's directory name is its sole persistent ID. `model.json` requires
exactly `display_name`, `expected_sha256` (64 hexadecimal digits) and `module_id`.
There are no persisted `model_id` or `model_path` fields. Loading constructs the
existing typed Model identity and absolute artifact path to the owning directory's
`model.vimosln`; missing artifacts/SHA mismatches still fail preparation. Normal
Recipe preparation re-reads its selected Model metadata; external changes require
explicit Recipe catalog Refresh while stopped, rather than silently replacing
the registry. Refresh discovers Model/Recipe metadata without choosing a first
entry. Discovery never initializes an engine, copies an artifact or creates a cache. Explicit-path
tools accept any `<model_id>/model.json` using this same contract, without requiring
the normal application's `data/` root. They do not accept the old JSON schema.

Model/Recipe IDs are exact safe single filename components, independent of display
names. Separators, traversal, Windows reserved/invalid names, control characters,
leading/trailing whitespace and trailing dots are rejected, not trimmed or repaired.
Case variants are not aliases; ambiguous casefold collisions cannot be selected.
Metadata-file symlinks and managed Model directory symlinks are rejected.

Recipe's Model row offers Import, Rename and Delete, alongside Prepare TensorRT
cache and its session-only result observation. File-import and acceleration icons
keep those actions distinct; labels remain available through tooltips/accessibility.
Import reads supported segmentation metadata through the SDK adapter without an
engine/GPU module, on the existing background IO slot. It hashes/copies into a
private sibling staging directory, verifies the copy and requires a module choice
when several segmentation modules exist. A single module is selected automatically.
Only a complete validated directory is published. Cancel/failure cleans the owned
unpublished copy; failed cleanup reports its path rather than claiming success.
Import makes the Model selectable without changing an unsaved Recipe or applying it.

New Model IDs use the first 12 lowercase SHA-256 digits. Duplicate full digests or
identical managed artifact bytes are rejected regardless of old ID/module. An
occupied prefix (including invalid entries) extends only the new ID to 16, 20,
etc. digits. Existing IDs/directories are never renamed or overwritten.
Rename changes only `display_name`. Delete requires confirmation, no saved Recipe
reference and no candidate/active reference or unfinished native owner. Deletion
checks each Recipe JSON's root `model_id` independently of full Recipe validation:
an exact matching string blocks deletion even if other fields are invalid;
a different string, absent field or null does not. Unrelated field errors do not
block deletion when that reference is unambiguous. Unreadable/unparseable files,
non-object roots, invalid reference types and duplicate JSON keys block deletion
with the Recipe path reported. All reference checks finish before any Model or
cache removal. Normal catalog discovery and full Recipe validation are unchanged.
Deletion removes only that managed Model directory, including its own cache; the
external imported source and other Models are untouched.

There is no cache inspection or clear action. Cache file presence does not establish
successful initialization. Model file operations reject live native ownership;
Start/preparation cannot race admitted management work, and Close waits for its
actual completion. Recursive
operations reject symlinks/junctions/external paths instead of traversing them.
New management tooltips/confirmations have Chinese/English resources; other
languages use the existing optional English fallback. Existing translations remain unchanged.

## Recipe

The `<recipe_id>.json` filename stem is its sole persisted identity; `recipe_id`
is not a JSON field. Only `display_name` is required. Optional fields are `model_id`,
`product_code`, `defaults`, `capsule` and
`image_archive`. A nonempty `model_id` selects that exact Model; there is no global
Model, inheritance or first-Model fallback. Save validates the Recipe's structure
and persistence identity, so a missing or unresolved Model reference can remain
saved. Prepare/Apply explicitly rejects it without replacing the current candidate
or active session. Referenced artifacts must still pass path/SHA/module validation.
The combined Qt Save/apply action can save successfully and then report an Apply
failure; the saved definition remains available for completion.
`camera_overrides` and `channel_overrides` are unknown fields and are rejected,
including empty arrays. They are never ignored or migrated.
Renaming changes only `display_name`, never the ID/path. Save writes the unchanged
selected target; New/Duplicate creates a separate Recipe identity.
New/Duplicate drafts propose `YYYYMMDD-XXXXXXXX` IDs using the local creation date
and eight lowercase Base32 characters from secure random bytes. Case-insensitive
collisions allocate a new suffix. First Save atomically publishes a complete file
without replacement; a late identity collision reallocates only the still-uncreated
generated object. Other IO errors are not retried. Once saved, identity remains
stable across renaming, Model/parameter edits and later Saves. No identity sidecar
is written; old valid Model/Recipe IDs remain supported without migration.
External definition changes, invalid/ambiguous target files and identity conflicts
require explicit reload/repair. Invalid unrelated files do not block Save,
preparation or Delete. Explicit Save may create a missing Recipe directory; atomic
replacement still uses a closed, flushed/fsynced same-directory temporary file.
Delete protects current candidate/active and persisted applied identities and
refuses an externally changed selection. No operation implicitly selects another
Recipe or updates an active session.

Default-valued optional groups/fields are omitted on explicit Save. For example,
`data/recipes/sample.json` containing `{"display_name":"Sample"}` is a complete
saved definition but cannot be prepared without selecting a Model. Default ViMo settings are enabled
and required with no exclusions; Size/Color and archiving default to disabled.
Changed settings remain explicit even when their check is disabled.

Recipe `defaults.vimo` defines one shared `enabled`, `required` and `disabled_labels`
policy for every configured camera/channel. Missing/null leaves use the effective
defaults: enabled and required, with an empty filter. The structural objects cannot
be null. Exclusions use exact case-sensitive label identities; an empty list excludes
nothing. Each channel retains its own image, ROI, identity, findings and judgment.
Filtering changes consumed findings, not model thresholds, inference masks,
returned IDs or shared evidence.

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

`image_archive` optionally contains `root_directory` (string, omitted default `""`)
and independent booleans `save_ok`, `save_ng`, `save_error`, `save_empty` (all
default false, including a missing `save_empty` in older archive settings).
Existing Recipes therefore record nothing. Any nonempty root must be a valid
absolute directory path; enabling any switch requires one. Saving validates its
syntax without creating directories or certifying permissions/free space. Runtime
write failures are observable separately from inspection and PLC results.
The responsive Image Archive card edits only this Recipe draft. The four labeled
switches share one row with the directory/actions whenever the card's available
width fits their requirements; otherwise they sit above the directory/actions.
Label/padding clicks toggle the whole condition control. Browse selects a
directory and Open Folder opens an existing directory without creating it.
Save/apply, Discard and lifecycle editing locks use the ordinary Recipe workflow.
Only the immutable successfully started Basler session records images; Folder
Replay, warm-up, tuning and snapshots never record Production archives.
See [archive ownership and output](architecture.md#image-archiving).

## CameraSources

`data/camera_sources.json` stores camera identities directly at the root:

```json
{
  "CAM1": {"folder": "../images/cam1", "basler_serial": "12345678"},
  "CAM2": {"folder": "../images/cam2"}
}
```

Keys are exact identities, not array positions or enumeration/serial order.
Each row optionally contains `folder` and `basler_serial`, defaulting to empty
strings. Optional `mode` defaults to `folder`, `loop` to true and `target_fps` to
8.0; Save omits default-valued fields. Serials must be empty or ASCII digit text.
Invalid present values, duplicate/unknown keys, arrays, embedded `camera_id`,
`source_type`, the obsolete `cameras` envelope and versioned envelopes are rejected.
There is no fallback to `data/sources/camera_sources.json`.

Missing entries, including `{}`, are valid incomplete saved definitions.
Vision presents missing configured cameras with blank fields; only an explicit
edit adds that camera entry. Opening/browsing never fills or rewrites the file.
References outside the current MachineLayout are rejected when applying it.
Basler preparation/connection/Start requires the complete configured camera set
and distinct nonempty serials, resolved in MachineLayout order. Folder definitions
with missing/unavailable inputs may remain READY with a warning; Start preflight
rejects them before engine creation. Saving is not proof of source readiness.
Vision Save/apply can persist an incomplete Basler mapping and then report failed
Apply without replacing the previous candidate.

Folder paths resolve relative to the new JSON's parent (`data/`); for example,
`../images/cam1` selects entry-relative `images/cam1`. No old base-directory
substitution is attempted. Folder and serial values survive mode changes, even
when inactive. No automatic scanning, identity inference or mapping repair occurs.

`loop` is boolean; FPS is finite/positive. They pace Folder Replay only. Stored
Folder paths are retained across mode changes but are not shown or used as
independent references in Basler mode. They may be empty. Connect all explicitly uses saved/applied mappings; Start requires
the connected source, validates actual dimensions and warms ViMo with synthetic BGR.
Switching mode preserves both folders and serials. Disconnect before changing
source mapping/mode; Recipe/Batch/page changes do not disconnect cameras.
Live source keys are `basler:<serial>`; Folder keys are lexical normalized paths.

Loading, browsing and Recipe Apply do not rewrite source files. Explicit Vision
Save writes the versionless definition atomically. This is source configuration,
not Recipe/ROI data.

## Editing and persistence

Configuration owns the Recipe/Model catalogs and Recipe, MachineLayout and source
drafts. Application coordinates Recipe editing and preparation; Qt presents these
owners without becoming the authority for saved or active configuration.

Recipe stacks Recipe management, Model information, Inspection Rules and one
shared Image Tuning workspace. New/Duplicate, explicit selection, metadata and
model selection retain their drafts. Reload remains a catalog-refresh action,
including external new/repaired files; it is distinct from Discard. Model selection is explicit even with one registered Model; an unset or unknown
reference remains visibly unavailable until selected. The compact Model selector shows its display name and
configured SHA-256 prefix; its tooltip exposes the full ID/hash, not a runtime
verification claim. The second row shows compact default defect-label checkboxes,
wrapping at the available width. There is no filter popup, technical-details or
policy-summary panel. The summary cards use their content's natural height;
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
case-sensitive identities. Recipe `defaults.vimo` supplies one shared `enabled`,
`required` and `disabled_labels` policy for all configured cameras and channels;
MachineLayout remains the sole authority for their identities and ROI geometry.
Save/apply, Discard and runtime editing locks govern this edit like other Recipe
changes.

The Model selector row also offers **Prepare TensorRT cache**, independent of
Recipe Save/apply. It submits the selected immutable Model identity to the
[application owner](architecture.md#inference); the UI requests initialization only,
so neither connected cameras nor Folder inputs are needed. The adjacent icon shows
unknown, preparing, succeeded or failed with localized accessible names. Preparation can
take minutes; Close waits asynchronously for native work and cleanup, and Start
is unavailable while preparation owns resources. Recipe selection/editing retains
its existing stopped-state rules; changing the Model never retags an earlier result.

Status is a bounded, session-only observation of the latest request, never inferred
from cache files or restored on launch. Success requires initialization and actual
worker cleanup. Preparation performs initialization without sample inference;
it does not certify
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
The compact tuning fields use W/H, pixel units and ± notation; full localized
parameter names remain in tooltips and accessibility labels.

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
The centered image selector reports actual pixel dimensions, `— · W×H` for known
dimensions on a blank canvas, or `ROI ≈` for schematic ROI extents when neither
is available. Short localized image-state descriptions remain accessible;
the tuning canvas retains the detailed standalone-check scope tooltip.
Schematic extents never become measured image bounds or evidence.
Image tools reuse Vision's icons, whole-group wrapping, Fit and 1:1; Recipe
management icons retain localized names/tooltips and the existing confirmations.

Click an upper/lower view in the current image to select its camera, ChannelId
and view. A single detail area beside the parameters shows actual body W/H with
configured limits, or actual Hue with target/circular tolerance. View status uses
the existing Size/Color judge's reasons for that capability and view; it is not
the combined channel result or production acceptance. Image overlays keep compact
selected Size or Color check judgments/body outlines and a selection highlight, without twelve repeated
W/H captions. Threshold edits rejudge cached evidence; mode switches retain the
selection. New images/camera changes clear it; stale asynchronous evidence is
never displayed as the selected view's measurements.
Disabled checks, unavailable/pending evidence, execution ERROR and content NG are
distinct; missing scalars display `—`, never ROI-derived measurements. Tune does
not collect Occupancy evidence or infer EMPTY from a missing body. Routine
always-on/unavailable explanations are omitted from Qt presentation; actual
production Occupancy state/evidence and technical errors remain available. This
does not change Size/Color judgment or the production EMPTY skip contract.
Routine tuning scope/success and selection-invitation prose is omitted; the scope
and selection instructions remain in static parameter/canvas tooltips. The canvas
does not aggregate changing per-channel diagnostic text. Selected-view judgment
and localized reasons remain visible without raw measurement-error tooltips. Without a selected
view, a disabled check shows `OFF` rather than unavailable measurements.
Selected-view feedback translates existing missing-body, unusable-color
and tolerance reasons; exact evidence remains in the existing preview results. Missing reference,
pending measurement, technical ERROR and disabled checks remain distinct; a valid
image without usable measurement evidence keeps its existing NG and `—` values.

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
the connection summary is numeric, with a localized accessible state and no
runtime tooltip.
Source cards do not duplicate device/status details in tooltips; full Folder paths
and selected device identities remain on their fields. Routine prose is omitted;
Vision errors use the existing bottom feedback strip. Specific setup/ROI/source
recovery details remain available there, without duplicating normal runtime state.
That feedback is page-scoped and never supersedes application FAULT or shutdown.
The global strip normally shows the saved source mode rather than repeating READY:
Folder is a source selection, not device connectivity or a continuously verified
camera count. Camera counts come from actual connected CameraId/serial matches.
Unsaved selections do not change that strip. Lifecycle transitions and errors keep
precedence; source availability does not establish full inspection readiness.
Independent Save sits beside Save/apply and Discard; a compact status symbol exposes
clean, dirty, saving or failed state through its symbol and localized accessible
name, without a runtime tooltip.
The image selector and previous/next browse Folder inputs. Import loads a temporary
image; Use runtime image freezes the latest eligible same-camera/source display
frame, with explicit refresh. Neither tracks live pixels. Fit and 1:1 retain their
canvas transforms. The image toolbar center always describes the current canvas:
real pixels show dimensions and the Folder index where applicable; a blank view
shows known dimensions as `— · W×H`; an unbounded view with ROIs says `ROI ≈`, with
its illustrative meaning in a tooltip. Without dimensions or initialized ROIs it
shows `—`, with ROI editing unavailable until dimensions are known. The chooser
retains image identity/blank-state meaning; the canvas does not duplicate it.
Capture keeps a static action tooltip; waiting/cancellation use the existing
control states, successful pixels use the image selector, and failures/timeouts
use the global feedback strip. The chooser list is separate from this caption;
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
Independent Save does not require Folder availability, a Recipe/Model or an engine.
It saves CameraSources even when other setup is incomplete. MachineLayout is saved
only when every selected camera has complete, valid geometry; partial ROI edits
remain an unsaved draft. Each file has its own outcome: a source Save can succeed
while MachineLayout validation/write fails, or vice versa. Invalid or externally
changed saved files require explicit reload/repair and are not silently replaced.
Save never prepares/applies a candidate or changes the connected source.

After valid MachineLayout/source Saves, Recipe editing, explicit catalog Refresh
and Prepare/Apply are available in the same window/process. No Application, bridge
or frame store is replaced. The selected Recipe must still resolve its Model and
pass artifact/source validation. Only Apply installs a candidate; a successful
Start activates it. Saved-but-incomplete setup remains UNCONFIGURED. Folder input
availability is checked at Start, before engine creation. Drawing needs an
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
No matching pixels means no measurement/judgment overlay. Black pixels in a valid
frame are still image evidence: enabled Size/Color can report missing-body/unusable
color NG with unavailable numeric measurements. Tune has no Occupancy fusion and
does not reinterpret that result as Production EMPTY or consume historical NG.

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
