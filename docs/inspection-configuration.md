# Inspection configuration — Phase 2B

`vimo_demo.configuration` is a pure data/persistence/resolution layer above the
[production inspection domain](production-inspection.md). It owns no engine,
threads, active configuration, UI or application state. Loading a Recipe never
imports the SmartMore SDK or loads a model.

```text
MachineLayoutDefinition + RecipeDefinition + ModelRegistry + caller default ID
    -> resolve_configuration()
    -> immutable ResolvedInspectionConfiguration
    -> application/pipeline composition
```

## Separate persisted definitions

All three JSON families require integer `schema_version: 1`.

| Definition | Required JSON fields besides version | Optional fields |
| --- | --- | --- |
| `ModelDefinition` | `model_id`, `display_name`, `model_path`, `expected_sha256`, `module_id` | None |
| `RecipeDefinition` | `recipe_id`, `display_name`, `defaults` | `product_code`, `model_override_id`, `camera_overrides`, `channel_overrides` |
| `MachineLayoutDefinition` | `cameras` | None |

Recipes and models are independent: many Recipes can use one model. Resolution
uses the caller's default ID unless the Recipe specifies an override. Both IDs
must exist when supplied, even if the override would replace the default.
`ModelRegistry` is an immutable, ID-sorted tuple with duplicate rejection and a
typed `get()` lookup. It does not discover files or activate models.

IDs/labels are exact, case-sensitive, nonempty strings without control characters;
no case folding or whitespace stripping changes their meaning. Display names and
product codes support Chinese. SHA-256 is exactly 64 hexadecimal characters,
canonicalized to lowercase. Neither model-specific labels nor thresholds are
invented from model names.

A machine camera entry contains `camera_id` and six `slots`. Each slot contains
`slot` (0–5), `channel_id` and `roi` (`x`, `y`, `width`, `height`). Existing
`CameraLayout`, `ChannelROI` and `InspectionLayout` validate this data: unique
cameras, slots exactly once, channels exactly 1..N, integer nonnegative origins
and positive sizes. Six cameras yield 36 channels. Camera array order is explicit
machine order; slot order is canonicalized. Mirrored/custom channel mappings
remain data, independent of camera names or image orientation.

## Defaults and inheritance

`defaults` contains optional `presence`, `blob`, `color`, `vimo` objects.
`CheckConfiguration` contains exact values; `CheckOverrides` contains optional
leaf values. Presence and color use `CheckConfig` rather than redundant identical
classes. Each config exposes the existing Phase 2A `.policy: CheckPolicy`.

| Check | Initial policy | Additional parameters |
| --- | --- | --- |
| Presence | disabled, required | None; no guessed detection threshold |
| Blob | disabled, required | Optional integer `min_area`, `max_area`, in pixels² |
| Color | disabled, required | None; no justified color-space/algorithm contract yet |
| ViMo | enabled, required | `disabled_labels`, initially empty |

Traditional checks start disabled because calibrated implementations are absent.
This is a new configuration constructor default, **not a change to Phase 2A's
enabled/required pipeline defaults**. Enabling an unparameterized check describes
intent; it does not certify an algorithm as ready. Phase 2C reports an enabled
but unavailable check as technical ERROR during execution. Production calibration
and readiness gating remain future hardware work. Area bounds are nonnegative
with min <= max when both exist. Zero is valid data, not an invented sentinel.

Resolution applies **default -> camera -> channel** for each leaf. Missing or
JSON `null` leaf values inherit. Explicit `false`, zero and an empty label list
override. A label list replaces the inherited list rather than merging it.
At defaults, missing/null leaves use the constructor defaults above. Structural
objects/arrays must have their stated types; `null` is not an entire check object.
Schema v1 cannot clear an inherited area bound using null; null means inherit.

Camera overrides are an array of `{camera_id, checks}` objects. Channel overrides
are an array of `{channel_id, checks?}` objects. Duplicate/unknown IDs fail.
**MachineLayoutDefinition is the sole ROI owner.** Every Recipe resolves the same
machine geometry; Recipes cannot remap camera/channel/slot identity or override
rectangles. Recipe `roi` fields are rejected as unknown fields, including null.
The current schema remains v1 and is updated in place; no legacy loader or migration
is supported. Image bounds are checked against reference/actual frame dimensions.

Ranges are checked after each merge, so invalid inherited combinations fail with
Recipe/camera/channel context before any resolved object is returned.

## Resolved output and Phase 2A boundary

`ResolvedInspectionConfiguration` contains Recipe ID/name/product code, selected
`ModelDefinition`, resolved `InspectionLayout` and an ascending ChannelId tuple of
`ResolvedChannelConfiguration`. Each channel holds its existing camera/channel IDs,
slot, ROI and exact check configuration. Frozen dataclasses and tuple snapshots
prevent normal mutation. No override objects/chains remain in this hot-path data.
Repeated equivalent input produces equal output; JSON key, registry and override
entry order do not affect resolution. Explicit machine camera order remains meaningful.

`enabled` controls whether a check is requested; `required` controls technical
validity when enabled. Optional NG remains evidence; optional failure does not
invalidate another successful check. Disabled does not mean OK. The unchanged
Phase 2A aggregation remains authoritative, including all-enabled-checks-failed
ERROR/UNKNOWN. No application acceptance rule is added here.

`ViMoCheckConfig.label_enabled(label)` is the explicit pure consumption policy:
enabled and not in `disabled_labels`. No-filter configuration preserves all valid
returned labels, including unfamiliar labels. Exact strings and duplicates are
validated; membership in a real model's complete label catalog is deferred until
reliable metadata is available. It neither mutates `ImageResult`/findings nor
changes SDK thresholds, masks, label IDs or parameters.

The layout and individual `.policy` values already use Phase 2A types. Uniform
policies can be passed to the existing `ProductionPipeline`. Its current policies
are source-wide: heterogeneous per-channel checks/filtering are **not automatically
applied by that pipeline**. Future explicit composition will consume the resolved
channel configuration, apply filtering at the appropriate channel and combine
traditional subchecks. Do not filter a shared full-camera result globally using
one channel's list or discard unassigned findings. That composition and the
policy for unassigned evidence with heterogeneous filters remain later work.
No Phase 1/2A execution or judgment behavior is changed in this phase.

## Persistence and artifact verification

Public `load_recipe`, `load_model_definition`, `load_machine_layout` accept `Path`.
Matching `save_*` functions accept `(path, definition)`. The parent directory must
already exist. Unknown fields at every structural level, duplicate JSON keys,
wrong types (including bool/float as integers), missing required fields and
unsupported versions fail. Future schemas need explicit support; no migration
framework is present.

Writers use UTF-8, sorted keys, two-space indentation and a trailing LF. Optional
override leaves are omitted; semantic round trips are stable. Arrays preserve
meaningful order or use domain canonical order. A same-directory temporary file
is written, flushed, fsynced and closed, then `os.replace()` replaces the target.
Replacement failure preserves the old target and attempts temporary-file cleanup.
Cleanup failure is noted on the original typed error. There is no concurrent
editor conflict detection, backup history or stronger power-loss guarantee.

`ConfigurationError` covers invalid data/references, `ConfigurationIOError` read/
write failures, and `ModelArtifactError` artifact verification. The latter two
inherit from the first. Load errors include file context and chained causes;
normal parse failures do not leak raw JSONDecodeError/KeyError/TypeError.

Known diagnostic limitations: nested unknown-field errors identify the file and
field name but not the full JSON path; unknown model-ID errors do not distinguish
the caller default from the Recipe override. More precise context is a future
usability improvement, not a change to the current validation rules.

Model paths are stored as supplied. Use portable relative paths in shared files.
`validate_model_artifact(model, base_directory=Path(...))` resolves a relative
path against the **explicit caller base**, checks readability and streams SHA-256,
returning the resolved path. Absolute paths remain usable for local metadata.
It does not depend on the JSON file's location or implicit current active model.
Verification is optional and separate from parsing/resolution, and is a point-in-time
check, not a file lease. Activation should revalidate as appropriate; the existing
engine's staging/checksum behavior is unchanged.

## Usage and synthetic review example

```python
from pathlib import Path
from vimo_demo.configuration import (
    ModelRegistry, load_machine_layout, load_model_definition, load_recipe,
    resolve_configuration,
)

folder = Path("tests/fixtures/configuration")
models = ModelRegistry(tuple(
    load_model_definition(folder / name)
    for name in ("general.json", "special.json")
))
resolved = resolve_configuration(
    load_machine_layout(folder / "machine.json"),
    load_recipe(folder / "overrides.json"), models, "general",
)
channel = resolved.channels[16]  # Stable ascending view: CH17.
print(resolved.recipe_id, resolved.model.model_id, channel.checks.blob.max_area)
```

[Fixtures](../tests/fixtures/configuration/README.md) are synthetic/non-calibrated;
model paths and zero hashes are placeholders, not deployable model metadata.
Product A has defaults only and shares the caller's model with any similar Recipe.
Product B selects `special`:

| Channel | ROI | Presence | Blob max_area | ViMo |
| --- | --- | --- | --- | --- |
| CAM1 / CH01 | base (0,0,8,10) | enabled/required | 1800, default | enabled/required, all labels |
| CAM3 / CH13 | base (0,0,8,10) | enabled/required | 1850, camera override | enabled/required, all labels |
| CAM3 / CH17 | machine (40,0,8,10) | enabled/required | 1900, channel override | enabled/required, 黑点 filtered |

GPU-independent tests cover all three JSON families, inheritance, references,
geometry, immutable resolution, filtering, atomic failure preservation, artifact
hashes and Phase 2A policy compatibility:

```powershell
uv run --no-sync pytest tests/unit/test_configuration.py tests/integration/test_configuration_pipeline.py --tb=short
```

The [Phase 2C application](production-application.md) owns activation/lifecycle,
safe model load/warm-up, stopped-state replacement and counters. Production UI,
camera/PLC work, calibration and traditional algorithms are outside this contract.

## Phase 3E-1 additive capsule settings

`RecipeDefinition.capsule` and `ResolvedInspectionConfiguration.capsule` carry uniform
product-level `SizeSettings`/`ColorSettings`. Optional root `capsule` JSON defaults
to both disabled and has no inheritance or ROI fields. The existing legacy checks
are preserved with their original semantics. See [capsule inspection](capsule-inspection.md)
for exact ranges/defaults, execution and preview; this is additive schema-v1 support,
not the deferred Phase 3E-2 cleanup.
