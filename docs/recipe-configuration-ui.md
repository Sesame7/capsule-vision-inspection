# Parameters / Recipe configuration UI — Phase 2D-2A

Parameters edits existing schema-v1 Recipes. It does not edit model registry
entries, machine mapping, algorithms or live runtime parameters. The
[Phase 2B contract](inspection-configuration.md) remains authoritative for values,
inheritance, validation and atomic persistence; the
[application contract](production-application.md) owns candidate activation.

## Components and lifecycle

`ParametersPage` and `CheckFields` present a Qt-free `RecipeEditorSession` and
`RecipeDraft`. `RecipeCatalog` wraps existing `load_recipe` / `save_recipe`; no
database, second configuration schema or new lower-layer API is introduced.
The session receives a catalog directory, immutable machine/model definitions,
default model ID and explicit artifact base directory. It never owns an engine
or changes an application. `ProductionWindow` optionally receives this session;
without it Parameters explains that no catalog is configured. The demo injects
a complete synthetic session.

| Action | Effect |
| --- | --- |
| Select / Reload | Fresh persisted Recipe becomes a clean draft; never apply |
| New | Empty ID/name and schema defaults; no silently copied overrides |
| Validate | Construct immutable Recipe and run Phase 2B resolution; no save or activation |
| Save | Validate/resolve, call existing atomic writer, then update the saved baseline |
| Save As | Require new unique ID and nonempty name; create/select a new file |
| Apply | Reload saved clean Recipe, resolve, verify artifact off-thread, configure candidate |

Save/Save As do not apply, start or reset counters. Apply does not mean active.
Only successful Start/warm-up promotes the candidate; failed startup preserves
the last-known-good active configuration. Candidate and active IDs appear separately
on both pages, including READY after Stop. An ID is not a revision/content hash:
editing a persisted Recipe with the same ID does not change its already-active
immutable configuration. No Recipe version-control UI is implied.

## Catalog and persistence

Discovery is nonrecursive and reads only `*.recipe.json` (case-insensitive suffix).
Files are visited in deterministic filename order; valid entries display by name
and exact ID. Unrelated files are ignored. Invalid files and duplicate IDs are
reported with file context; all copies of an ambiguous ID are excluded. Directory
errors use `ConfigurationIOError`, invalid data uses `ConfigurationError`.
Discovery never creates/writes files. Resolve catalog issues before saving.

New filenames use SHA-256 of the exact Recipe ID, so valid Unicode IDs or IDs
containing path characters cannot escape the injected directory. Existing selected
paths remain stable. Phase 2B owns temporary-file creation, flush/fsync and atomic
replacement. Validation/write failure preserves the draft and previous persisted
baseline. Model JSON, machine JSON, source models and images are never modified.

This is a single-writer local catalog. There is no watcher, deletion, cross-process
write arbitration or history. External edits require Reload/reselection. Apply
detects saved data differing from the loaded baseline and requires reload rather
than applying unseen edits. Normal Save does not implement conflict merging;
avoid concurrent editors.

## Draft, fields and inheritance

Mutable UI drafts are separate from immutable domain definitions. Integer fields
retain editing text, including invalid input, until domain conversion; invalid
values are not clamped. Equality against the loaded baseline drives dirty state.
Navigation, language, camera/channel selection and validation do not dirty it.
An untouched blank New form is not dirty, but is unsaved and cannot Apply.
Saved IDs are read-only; use Save As.

Defaults / Camera / Channel select existing inheritance levels. Camera/channel
choices come from the actual layout, including physical slot order. Optional
overrides visibly distinguish Inherit/Override; booleans distinguish Inherit/Yes/No.
Defaults area bounds distinguish Not set / Set bound. Effective values and ROI
come from `resolve_configuration()` for the selected channel, never UI precedence.

Only existing fields are exposed: Presence/Color enabled and required; Blob
enabled/required and optional integer min/max area (px²); ViMo enabled/required
and disabled labels; channel rectangle x/y/width/height.
Enabled requests execution; Required controls an enabled check's technical-validity
contribution. Explicit False, zero and empty disabled-label list remain distinct
from inherit. Labels are exact text, one per line; empty text with Override selected
means `[]`. No model-label catalog is invented. Schema v1 cannot clear an inherited
area bound with null, because null means inherit; the page explains this limitation.

Model choice is Default or an existing registered model. Name/ID, path, expected
SHA-256 and module ID are read-only; changing selection never loads a model.
Machine details show camera/channel counts, mapping and base ROIs. The schema
has no machine name/ID, so none is invented. Numeric ROI reset removes only that
channel's override. Image-bound checks still require actual frame dimensions at
execution. There are no graphical handles, polygons or drag editing.

## State, validation and shutdown

Editing, Validate, Save, Save As and Apply are allowed only in UNCONFIGURED/READY.
STARTING/RUNNING/STOPPING/FAULT/CLOSED are view-only. Hierarchy navigation and Recipe
viewing remain available while running. Apply rechecks authoritative application
state on completion, so a concurrent Start cannot install live changes.

Validation errors show concise domain field/context messages; no traceback or
fabricated nested JSON path. Dirty/saved, validation, candidate and active states
use text, not color alone. Save As retains its dialog on invalid identity/data.
Preparation failure does not invent FAULT or modify candidate/persisted data.

Recipe JSON operations are small synchronous editor work. Model hashing uses at
most one `RecipePreparation` thread and the existing Future-to-queued-Qt gate.
There is no preparation queue/pool. Editing and duplicate Apply are disabled while
it runs. GUI code never loads a model, warms up, infers or waits on unfinished work.

Recipe switch, New, Reload and close resolve dirty drafts with an asynchronous
Save / Discard / Cancel dialog. Save continues only after successful persistence;
Discard reloads the saved baseline (or resets a new draft); Cancel stays. Switching
shell pages retains the draft, so no discard prompt is necessary. Close waits for
preparation termination, suppresses late Apply, then uses existing asynchronous
application close/actual-thread-termination behavior. No native call is cancelled.

## Presentation and synthetic acceptance walkthrough

Existing Chinese/English strings, logical sizes and dark tokens are extended.
A compact selector works in portrait/landscape; one main scroll area holds forms
and details. Identity/status and primary actions remain outside it. Controls use
44-pixel logical touch rows. Tests cover both languages at 1080×1920, 1920×1080 and
900×1200; physical Windows DPI, touch and font ergonomics still need field review.

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
```

The existing launcher seeds synthetic Demo A/B files only when absent under ignored
`outputs/production_ui_demo/recipes/`; subsequent edits are preserved. Both registry
choices use the known tiny fake artifact, not ViMo. Blob limits exercise resolution
while Blob stays disabled; these are not calibrated algorithm settings.

1. Open Parameters and select Demo B. Candidate stays Demo A.
2. Inspect Defaults / CAM1 / CH01: Blob maximum is 1800.
3. Inspect Camera override / CAM3 / CH13: maximum is 1850.
4. Inspect Channel override / CAM3 / CH17: maximum is 1900. Clear Override to see
   inherited 1850; restore Override for the next edit.
5. Change maximum to 1901. Observe Unsaved changes; Validate without saving.
6. Save. Observe Saved and unchanged candidate. Apply explicitly makes B candidate;
   Active stays absent or the previous successfully started Recipe.
7. Return to Production and Start. Successful warm-up activates B. While RUNNING,
   open Parameters: values remain visible, editing/Save/Apply are disabled.
8. Stop. Save As with a new ID/name, then Apply. Active stays B until another
   successful Start. Production counters remain unchanged.
9. Edit a name; attempt Recipe switch, New or close. Exercise Cancel, Discard, Save.
   Invalid data must prevent the Save continuation.
10. Switch languages and sizes. Scroll to numeric ROI/model/layout. Try width=0
    and Validate: it fails without clamping. Reset ROI to Inherit.

Automated application tests also demonstrate active A / candidate B / failed B
startup with A retained. No GPU or customer assets are needed.

Phase 2D-2B is the graphical ROI editor. Roles/login, model/machine management,
Recipe deletion/versioning, watching, calibration, archive and camera/PLC integration
remain separate future work.
