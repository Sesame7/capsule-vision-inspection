# Recipe UI — Size/Color and ViMo configuration

The normal page is **Recipe / 配方**. It presents product-level identity and model
selection, using the existing Qt-free `RecipeEditorSession`, `RecipeDraft` and
`RecipeCatalog`. Configuration schema, persistence, inspection and application
lifecycle remain authoritative. Phase 3E-1 adds demonstration Size/Color settings
and a draft-only tuning workspace. Only ViMo policies retain hidden overrides.

## Normal operator surface

- One toolbar: Recipe selector expands into the available space; New, Duplicate
  and Reload keep their compact button sizes.
- A full-width workspace with balanced Basic information / Inspection columns.
  Landscape and practical portrait widths keep both columns; narrower available
  widths stack them according to actual control minimum sizes.
- Basic information contains Recipe name, optional product code, model selection
  and the existing ViMo rule/override summaries. Name/code sit side by side when
  their column permits, otherwise stack. The two fields share the row equally
  with a consistent gap and readable minimum widths. The model selector fills
  the space beside its label. Qt stretch/size policies let these inputs grow
  and shrink with their section without changing control heights or spacing.
  Inspection summaries and actions keep their existing presentation.
- A single available model remains read-only.
- Inspection contains Size/Color enablement, associated parameter summaries and
  one shared **Tune preview / 调整预览** entry aligned at its right edge.
- Saved/dirty/preparing feedback, local errors and primary **Save and apply** share
  the bottom action row, with state on the left and Save and apply at the usable
  workspace's right edge. No additional configuration fields are exposed.

The unified entry initially selects Size, or Color when only Color is enabled.
Both modes remain available inside the same existing preview workspace. Its top
row has Back, configured-order camera icon/number selectors, Fit and Refresh;
the aspect-preserving image remains dominant. A compact bottom group contains
Size/Color selection, explicit Enabled/Disabled state and the existing selected-mode
controls. Whole groups stack when their minimum widths do not fit. Size targets
and tolerances, Color sampling/target swatch and Hue tolerance retain their draft
behavior. Overlay OK/NG always combines all enabled checks, including when the
selected mode is disabled; the enablement tooltip makes this distinction explicit.

Recipe selection remains on this page only. Production displays the Recipe
read-only: a candidate alone is the initial configuration summary; a differing
candidate appears beside the active Recipe as pending next successful Start.
It provides no Recipe selector or editing controls.

There is no long scrolling form, raw Recipe ID, model path/hash/module dump,
Defaults/Camera/Channel navigation, Inherit/Override UI, traditional enabled/required forms,
permanent traditional-threshold forms, raw disabled-label editor, or machine-layout dump.
The old `CheckFields` presentation was removed, not folded into an advanced panel.

Model selection maps to the existing default/override semantics. Unrelated edits
preserve `model_override_id` exactly, including an explicit ID equal to the default.
Unknown saved models remain invalid rather than being silently replaced. No model
loading or label-sidecar parsing occurs on selection. Normal default ViMo policy
shows “all model defect labels”; nondefault policies use a neutral saved-configuration
summary instead of falsely claiming all labels participate.

Recipes with camera/channel overrides show one exceptional notice that these data
are not editable here. Name/product/model edits and duplication preserve all hidden
ViMo defaults, camera/channel policies and label filters through the existing
draft round trip. Explicit False, empty lists and inheritance stay distinct.
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
The [normal bootstrap](application-bootstrap.md) uses `data/recipes/` and restores
only the manifest's last successfully applied Recipe ID, without the developer
demo's first-entry convenience selection. Its successful Apply notification updates
that ID; Save alone, unsaved drafts and failed preparation do not.
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

Validation, persistence and model/preparation failures use short localized
operation/resource summaries; the status tooltip retains the complete original
diagnostic, including paths and hashes. Unknown failures do not guess a cause.
Successful Apply feedback can coexist with unresolved startup Folder/manifest
guidance; Apply neither verifies acquisition nor repairs app.json.

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
The shell's fixed 33-pixel strip remains present during Recipe editing and preview;
ordinary product NG stays on Production. Global technical faults remain visible.
Recipe validation/save feedback stays in this workspace; Recipe/configuration
command feedback appears only on Recipe/Vision. Navigation does not clear backend state.

## Machine geometry

**Vision** directly contains the unified Camera setup workspace: Folder source
controls and the existing canvas-first Machine ROI editor. It uses the selected
saved Recipe for save/prepare/configure; sources have separate persistence.
Recipe pages do not contain machine geometry or source assignments. MachineLayout
remains the sole geometry owner; there is no separate Vision entry/subpage.
See the [ROI contract](graphical-roi-editor.md) for dirty navigation, frozen frames,
geometry, initialization and bounds rules. Recipe editing never writes machine JSON.

## Review and verification

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080
uv run --no-sync pytest tests/integration/test_parameters_ui.py tests/integration/test_traditional_preview.py tests/integration/test_recipe_refinement.py tests/unit/test_recipe_editor.py --tb=short
```

The fake demo uses local synthetic Demo A/B files without GPU. Demo B retains its
ViMo camera/channel settings and therefore shows the exceptional notice.
The real-local launcher retains the supplied model/layout/Recipe paths and adds
optional `--sources-config`; CLI image overrides remain explicit process-only input.

Review the Recipe page for simplicity, unnecessary remaining information, obvious
switch/New/Duplicate actions and understandable Save and apply. Check that there is
a compact entry for Size/Color with detailed controls only in preview. Exercise dirty
navigation, both languages and portrait/landscape. Phase 3J-1 screenshots under
`outputs/recipe_refinement_phase3j1/` cover Chinese Recipe portrait/landscape,
English Size landscape and Chinese Color portrait, using the existing two-view
demo images and fake-engine composition. Tests retain New/Duplicate/Reload,
Save/apply, runtime restrictions, draft synchronization, combined judgments and
preview job/identity checks. The subsequent full-width refinement screenshots in
`outputs/recipe_fullwidth_phase3j1/` cover Chinese Recipe landscape/portrait and
English landscape. Focused tests verify balanced columns, narrow stacking,
long-text wrapping and right-edge action alignment without changing preview.
The field-width refinement screenshots in `outputs/recipe_field_widths_phase3j1/`
use the same three main-page orientations, including longer fake-model display
names. Tests verify balanced expanding fields, compact toolbar actions,
shrink/grow behavior, unchanged heights and single-model read-only presentation.
Offscreen screenshots/tests do not replace usability, physical DPI or touch acceptance.

The [Size/Color preview](capsule-inspection.md) shares demonstration measurement and
judgment with production. Calibration remains deferred. Obsolete traditional configuration and draft helpers
have been removed; removed Recipe fields fail strict validation.
Technical `required` policy, raw ViMo label filtering and hierarchy
remain backend capabilities, documented in [configuration](inspection-configuration.md).
Model-export metadata/label integration is deferred. No accuracy, throughput,
production calibration, camera/PLC or GPU regression claim follows from this UI pass.
