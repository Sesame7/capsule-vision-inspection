# Configuration

This document owns the **Current** persisted Model, Recipe, MachineLayout and
camera-source contracts, pure resolution, and artifact identity. Loading or
resolving these definitions creates no engine, thread, candidate or active session.
The [inspection contract](inspection.md) owns evidence and judgment;
[application startup](application.md#startup-recovery) owns discovery/preferences, and
the [application contract](application.md#activation) owns preparation/activation.
Operator workflows remain in [Recipe editing](ui-configuration.md#recipes) and
[machine/source editing](ui-configuration.md#machine-roi).

## Definitions

All four persisted families require an integer `schema_version` equal to **1**.
The field lists below describe JSON requirements, which can be stricter than
Python constructor defaults. Unknown fields are rejected at every structural level.

| Definition | Required fields besides `schema_version` | Optional fields |
| --- | --- | --- |
| `ModelDefinition` | `model_id`, `display_name`, `model_path`, `expected_sha256`, `module_id` | None |
| `RecipeDefinition` | `recipe_id`, `display_name`, `defaults` | `product_code`, `model_override_id`, `camera_overrides`, `channel_overrides`, `capsule` |
| `MachineLayoutDefinition` | `cameras` | None |
| `CameraSources` | `cameras`, `loop`, `target_fps` | None |

IDs, labels and nonempty metadata text fields are exact, case-sensitive strings without
control characters. Validation does not trim or case-fold them; surrounding
whitespace is not an alias. Display names/product codes may contain Chinese.
`product_code` and `model_override_id` may be absent or null. Structural objects
and arrays cannot be null. Integers reject booleans and floats; boolean fields
require booleans. SHA-256 requires 64 hexadecimal characters and is canonicalized
to lowercase. Folder paths require strings but do not use this metadata text
validation; their empty-string allowance is described below.

### Models and Recipes

A Model definition identifies an artifact and segmentation module; it is not the
model binary or SDK parameter export. The parser retains `model_path` as supplied.
It does not read the artifact or infer labels/thresholds from its name.
`ModelRegistry` is an immutable model-ID-sorted tuple, rejects duplicate IDs, and
raises `ConfigurationError` for an unknown ID.

Recipes can share a model. `resolve_configuration()` first validates the caller's
explicit default model ID, then selects the Recipe's `model_override_id` if present.
Both IDs must exist even when the override wins. No first-model fallback exists.

`defaults` contains only an optional `vimo` object. Camera overrides are
`{camera_id, checks}`; both fields are required. Channel overrides are
`{channel_id, checks?}`; absent `checks` means no override. Each `checks` object
also contains only optional `vimo`. Duplicate override identities fail;
resolution rejects camera/channel IDs not present in the machine layout.
Override arrays are canonicalized by their respective IDs.

### Machine geometry

**MachineLayout is the sole persisted ROI/slot/channel-geometry authority.**
Each camera contains `camera_id` and `slots`; each slot contains `slot`,
`channel_id` and `roi`. Each ROI requires `x`, `y`, `width`, `height`.

- Cameras are unique and nonempty. Their JSON array order is configured machine
  order, preserved through resolution and execution; camera names are not sorted
  to derive wiring.
- Each camera has slots **0..5 exactly once**, canonicalized into slot order,
  with distinct positive integer ChannelIds. Across the layout, channels map
  exactly once to contiguous IDs **1..N**. Six cameras therefore have 36 channels.
- ROI origins are nonnegative integers; widths/heights are positive integers in
  original image pixels. Mirrored/custom channel mappings are explicit data.
  Overlap is allowed. Actual/reference image dimensions determine bounds checks;
  the JSON definition itself supplies no image shape or physical calibration.

Every Recipe resolves the machine's unchanged geometry. Recipes cannot remap
camera/channel/slot identity or override ROIs. Saved layouts remain authoritative;
opening an editor does not replace or repair them. Default six-partition geometry
is a creation policy of the demo/editor workflow, not calibrated production data
or an implicit parser/resolver default. See [machine editing](ui-configuration.md#machine-roi).

### Camera sources

`CameraSources.cameras` is a nonempty array of unique
`{camera_id, source_type, folder}` entries, all three fields required. Currently
`source_type` must be exactly `"folder"`; `folder` is a string and may be empty
for an unconfigured draft. Persisted entry order is preserved, but
`validate_cameras(layout.camera_ids)` checks an exact **identity set**, not positional
wiring. Frame/camera execution order comes from MachineLayout.

`loop` is boolean. `target_fps` is a finite positive JSON number, not a boolean.
The Python constructor defaults are `loop=True`, `target_fps=8.0`; **both must
still be supplied in JSON**. Pacing is a local replay setting, not a guaranteed
hardware throughput. Parsing does not prove folders exist or contain usable
images. Source availability/readiness belongs to application/replay handling.

Sources are separate from model metadata, Recipe and ROI geometry. Relative
folders use the source JSON's directory as their base. Source comparison keys
are lexical normalized paths, without filesystem traversal; they are not content
hashes or a guarantee that two differently spelled paths identify different files.

## Resolution

```text
MachineLayoutDefinition + RecipeDefinition + ModelRegistry + default_model_id
    -> resolve_configuration()
    -> ResolvedInspectionConfiguration
    -> application execution composition
```

### ViMo inheritance and filtering

The three leaves of `ViMoCheckConfig` are `enabled` (default true), `required`
(default true), and `disabled_labels` (default empty). `enabled` requests the
check; `required` affects technical validity when enabled. Disabled is not OK;
optional NG remains evidence. The [judgment rules](inspection.md#judgment)
define aggregation, including the all-enabled-checks-failed case.

Resolve each leaf in order **defaults → camera → channel**:

| Supplied leaf | Meaning |
| --- | --- |
| Missing or null | Inherit; at `defaults`, use the constructor default |
| `false` | Explicit replacement for a boolean leaf |
| `[]` for `disabled_labels` | Explicitly clear an inherited filter |
| Nonempty label list | Replace the entire inherited list; do not merge |

A whole `vimo`/`checks`/`defaults` object cannot be null. Disabled-label entries
are exact nonempty strings, reject duplicates, and are canonicalized into sorted
tuples. Membership in the model's complete label catalog is not validated here.

`label_enabled(label)` is a **consumption** policy: enabled and not filtered.
An empty filter preserves every valid returned label, including unexpected ones.
It does not mutate shared `ImageResult` evidence, SDK parameters/thresholds, masks
or raw mask IDs. Configured execution filters assigned findings per channel;
unassigned evidence survives when at least one enabled channel in that camera
accepts its label. Scheduling and evidence assignment are separate from pure
resolution; see [configured inspection](inspection.md#configured-execution).

### Capsule settings

Optional root `capsule` contains only `size` and `color`. These uniform
product-level settings apply across all channels, independently of ViMo
inheritance. There is no capsule `required` control or camera/channel override.
Enabled capsule checks are required in execution.
Confirmed EMPTY Occupancy can explicitly skip their execution without changing
these saved settings; the [inspection contract](inspection.md#always-on-occupancy)
owns that distinction. Occupancy has no persisted Recipe enable/threshold fields.

| Object / field | Omitted value | Valid supplied value |
| --- | --- | --- |
| `size.enabled` | false | boolean |
| `size.target_width`, `size.target_height` | 1 | positive integer, pixels |
| `size.width_tolerance`, `size.height_tolerance` | 0 | nonnegative integer, pixels |
| `color.enabled` | false | boolean |
| `color.target_hue` | 0 | integer 0..179, OpenCV Hue |
| `color.hue_tolerance` | 0 | integer 0..90, circular Hue distance |

Omitting `capsule`, a child object or an individual field supplies these defaults;
explicit null is invalid at these levels. Both checks are initially disabled.
The 1-pixel/Hue-0 defaults are uncalibrated starting values, not production targets.
Measurement/capability/reason semantics belong to
[capsule screening](inspection.md#capsule-screening), not configuration persistence.

Recipe ROI fields and removed `presence`, `blob` and former unparameterized
`color` check fields are unknown and rejected, including null or disabled values.
Root **`capsule.color` remains supported**. Current schema v1 was updated in place;
the version number does not imply old Recipes remain compatible. There is no
automatic migration, legacy loader or compatibility execution. External files
must be explicitly maintained against this schema; no model or layout repair is
implied by a Recipe error.

### Resolved snapshot

`ResolvedInspectionConfiguration` is a frozen data snapshot containing Recipe
ID/name/product code, selected `ModelDefinition`, `InspectionLayout`, root capsule
settings, and an ascending-ChannelId tuple of `ResolvedChannelConfiguration`.
Each channel holds camera/channel identity, slot, unchanged ROI and exact checks.
No override chain, engine, active-state identity or source-image storage remains.

Equivalent input yields equal resolved output regardless of JSON key order,
registry order or override entry order. Explicit machine camera order remains
meaningful. Resolution validates references/geometry structure but neither hashes
nor loads the model. Candidate preparation and successful-Start activation belong
to the [application](application.md#activation); editing belongs to the UI.

This synthetic example uses existing tracked fixtures and performs no inference:

```python
from pathlib import Path

from vimo_demo.configuration import (
    ModelRegistry,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)

folder = Path("tests/fixtures/configuration")  # Run from the repository root.
registry = ModelRegistry(tuple(
    load_model_definition(folder / name)
    for name in ("general.json", "special.json")
))
resolved = resolve_configuration(
    load_machine_layout(folder / "machine.json"),
    load_recipe(folder / "overrides.json"),
    registry,
    "general",
)
ch17 = resolved.channels[16]  # Ascending ChannelId view, not arrival order.
print(resolved.model.model_id, ch17.camera_id, ch17.checks.vimo)
```

The [fixture definitions](../tests/fixtures/configuration/README.md) select
`special`: CH01 uses defaults; CAM3/CH13 is optional by camera override;
CAM3/CH17 is required and filters 黑点 by channel override. Their geometry/model
paths/zero hashes are synthetic, not deployment metadata or calibration.

## Persistence

The four public `load_*` functions accept `Path`; matching `save_*` functions
accept `(path, definition)`. Domain validation rejects unknown fields, duplicate
JSON keys, missing required fields, wrong types, invalid references and unsupported
versions rather than guessing a fallback.

`ConfigurationError` covers invalid data/references. `ConfigurationIOError`
covers file read/write errors; `ModelArtifactError` covers artifact verification.
Both inherit from `ConfigurationError` (a `ValueError`). Load/save/verification
boundaries include useful file/model context and chain their original cause.
Nested parse diagnostics identify the file and offending fields, but not always
the complete JSON path. Unknown model-ID errors do not distinguish default versus
override lookup. These diagnostic limits do not relax validation.

Domain saves require an **existing parent directory**. They serialize UTF-8,
sorted keys, two-space indentation and a trailing LF, with nonfinite numbers
disallowed. Optional null override leaves are omitted; arrays preserve configured
camera/source order or canonical slot/override/label order. Round trips preserve
meaning, not original formatting or explicit-null spelling.

A same-directory temporary file is written, flushed, fsynced and closed before
`os.replace()` publishes it. Replacement failure preserves the previous target;
temporary-file cleanup is attempted and a cleanup failure is noted on the typed
error. This is single-file atomic replacement, **not** a multi-file transaction,
concurrent-editor merge/conflict detector, backup history or stronger power-loss
guarantee. The separate application-preferences writer has its own parent,
formatting and error behavior; do not infer these domain-writer details apply to
`data/app.json`. See [startup persistence](application.md#preference-persistence-and-feedback).

## Artifact paths

There is no universal implicit model-path base:

| Entry/boundary | Base for a relative model artifact |
| --- | --- |
| `load_model_definition()` / `resolve_configuration()` | None; retain supplied path, no filesystem verification |
| `validate_model_artifact(model, base_directory=...)` | Explicit caller base |
| Normal `app.py` startup | Each Model JSON's own directory; registry path anchored in memory |
| Explicit real-local UI launcher | Model JSON directory |
| `scripts/benchmark_production.py` | Repository root, as documented in its [workload contract](inspection-performance.md#workload-and-invocation) |

`validate_model_artifact()` resolves the path, reads it and streams SHA-256,
returning the resolved path only when the digest matches. Absolute artifact paths
are also supported. Verification is separate from parsing/resolution and is a
point-in-time check, not a file lease or activation. Engine runtime staging/cache
remains a separate [inference responsibility](inference-runtime.md#engine-setup-and-vendor-boundary).

Normal startup's ignored `data/models/` metadata/artifacts and engineering tools'
root `models/` inputs are distinct supported workflows; do not relocate one to
make the other path convention true. Relative Folder sources always use the
source JSON's directory, independently of Model or Recipe paths. See
[normal startup](application.md#startup-recovery) and
[explicit local UI entry](application.md#developer-entry-points).

## Verification

Current implementation pointers are the [definitions](../src/vimo_demo/configuration/models.py),
[source definitions](../src/vimo_demo/configuration/sources.py),
[codecs](../src/vimo_demo/configuration/persistence.py) and
[resolver/artifact verifier](../src/vimo_demo/configuration/resolve.py).
These locate today's implementation, not a requirement to retain its internal
file organization indefinitely.

GPU-independent verification covers strict parsing, precedence/null/false/empty
replacement, identity/order, machine-only geometry, capsule ranges/defaults,
atomic failure preservation, artifact hashes and resolved policy execution:

```powershell
uv run --no-sync pytest tests/unit/test_configuration.py tests/unit/test_capsule.py tests/integration/test_configuration_pipeline.py --tb=short
```

[Source/replay tests](../tests/unit/test_folder_replay.py) and
[startup tests](../tests/unit/test_application_bootstrap.py) cover their additional
identity/readiness/path-base responsibilities. Schema evolution, physical
calibration and concurrent editing would require separate decisions; none is
implemented merely by this document.
