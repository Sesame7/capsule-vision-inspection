# Recipe UI — Phase 3D first pass

The normal page is **Recipe / 配方**. It presents product-level identity and model
selection, using the existing Qt-free `RecipeEditorSession`, `RecipeDraft` and
`RecipeCatalog`. Configuration schema, persistence, inspection and application
lifecycle remain authoritative; this is a presentation/workflow simplification.

## Normal operator surface

- Recipe selector showing display names; secondary New, Duplicate and Reload.
- Recipe name and optional product code.
- Inspection model using registered display names. A single model is read-only.
- A concise detection-rule summary, saved/unsaved/preparing state and error feedback.
- One primary **Save and apply** action.

There is no long scrolling form, raw Recipe ID, model path/hash/module dump,
Defaults/Camera/Channel navigation, Inherit/Override UI, enabled/required controls,
traditional-check forms, raw disabled-label editor, or machine-layout dump.
The old `CheckFields` presentation was removed, not folded into an advanced panel.

Model selection maps to the existing default/override semantics. Unrelated edits
preserve `model_override_id` exactly, including an explicit ID equal to the default.
Unknown saved models remain invalid rather than being silently replaced. No model
loading or label-sidecar parsing occurs on selection. Normal default ViMo policy
shows “all model defect labels”; nondefault policies use a neutral saved-configuration
summary instead of falsely claiming all labels participate.

Recipes with camera/channel overrides show one exceptional notice that these data
are not editable here. Name/product/model edits and duplication preserve all hidden
defaults, camera/channel policies, thresholds and label filters through the existing
draft round trip. Explicit False, zero, empty lists and inheritance stay distinct.
Normal Recipes without overrides do not show the override notice.

## New, Duplicate and persistence

New and Duplicate open a small name dialog. A UUID supplies a new opaque Recipe ID;
the operator does not need to enter one. Existing IDs are never renamed. New starts
with current domain defaults (ViMo enabled, traditional checks disabled). Duplicate
copies the current clean Recipe, including product/model and all hidden inspection
settings, changing only identity and the supplied name. Cancelling the dialog leaves
the previous draft intact. Creation produces a draft; Save and apply persists it.

Discovery still reads only nonrecursive `*.recipe.json`, reports malformed/duplicate
entries and shows registered display names. Reload supports external edits.
Existing paths remain stable; the catalog derives new filenames from the ID hash.
The codec remains responsible for validation and atomic writes. No deletion,
versioning, external watcher, conflict merging or new persistence layer is added.

## Save and apply, failures and navigation

1. Validate/resolve the draft using the existing configuration layer.
2. Atomically persist changed/new Recipe data.
3. Capture the saved clean Recipe and prepare/verify the model on the existing
   `RecipePreparation` thread, with queued Qt completion.
4. Configure it through `ProductionApplication`; only successful **Start** activates.

A clean Recipe is re-read and checked for external changes rather than rewritten.
Save/write failure retains the draft and previous file. Preparation failure after
successful persistence leaves the new file saved while preserving previous candidate
and active configuration. It reports an error, does not claim successful Apply and
does not continue pending navigation. There is deliberately no rollback transaction
spanning disk persistence and application configuration.

Success says changes become active on the next successful Start. No counters reset,
implicit Start or live activation is introduced. Candidate/active remain backend
concepts and are not required vocabulary on the Recipe page.

Recipe switch, New, Duplicate, Reload and close guard dirty data with the same small
**Save and apply / Discard / Cancel** flow. Save continues only after preparation and
configuration succeed; Discard reloads/reset the draft; Cancel stays. Ordinary shell
navigation retains the Recipe draft. Closing during preparation waits for actual
thread termination, suppresses late configuration, then follows existing asynchronous
application shutdown. No native inference is performed or waited on by widgets.

UNCONFIGURED/READY permit editing. STARTING/RUNNING/STOPPING/FAULT/CLOSED allow viewing
but not editing or Save and apply. Readiness is checked again before configuration.
Preparation disables duplicate commands. Existing engine/warm-up ownership is unchanged.

## Machine geometry

**Settings → Machine ROI** opens the unchanged canvas-first workspace, using the
selected saved Recipe for its existing save/prepare/configure operation. Settings
contains only this concise entry; there is no broader Settings redesign. Recipe
pages do not contain machine geometry. MachineLayout remains its sole owner.
See the [ROI contract](graphical-roi-editor.md) for dirty navigation, frozen frames,
geometry, initialization and bounds rules. Recipe editing never writes machine JSON.

## Review and verification

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
uv run --no-sync pytest tests/integration/test_parameters_ui.py tests/integration/test_roi_editor.py tests/unit/test_recipe_editor.py --tb=short
```

The fake demo uses local synthetic Demo A/B files without GPU. Demo B retains its
historical camera/channel settings and therefore shows the exceptional notice.
The real-local launcher and supplied configuration paths are unchanged.

Review the Recipe page for simplicity, unnecessary remaining information, obvious
switch/New/Duplicate actions and understandable Save and apply. Check that there is
room for future size/color parameters without introducing them now. Exercise dirty
navigation, both languages and portrait/landscape. Offscreen screenshots/tests do
not replace usability, physical DPI or touch acceptance.

Traditional capsule size/color UI is deferred until algorithms and calibrated
parameters exist. Technical `required` policy, raw ViMo label filtering and hierarchy
remain backend capabilities, documented in [configuration](inspection-configuration.md).
Model-export metadata/label integration is deferred. No accuracy, throughput,
production calibration, camera/PLC or GPU regression claim follows from this UI pass.
