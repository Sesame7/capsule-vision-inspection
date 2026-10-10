# Normal application startup

From the repository root, after provisioning the verified environment:

```powershell
uv run python app.py
```

Daily use can also use `uv run --no-sync python app.py` to avoid environment
synchronization. `uv run` normally retains extraneous vendor packages; do not
add `--exact`. Runtime/license/GPU prerequisites remain external. Startup verifies
files but does not construct an engine or execute GPU inference.

The entry resolves `data/` beside `app.py`, independently of the working directory:

```text
data/
  app.json
  machine/machine.json
  sources/camera_sources.json
  models/*.json, *.vimosln
  recipes/*.recipe.json
```

This entire directory is ignored local persistent configuration. `outputs/` is
still disposable generated/test data; developer fixtures do not become startup
defaults. Normal Recipe, machine and source saves target these data paths.

## Identity and discovery

The separate application manifest has these required fields (IDs may be null to
leave configuration unselected):

```json
{
  "schema_version": 1,
  "default_model_id": "general",
  "last_applied_recipe_id": "local",
  "language": "zh"
}
```

Model definitions are all nonrecursive `*.json` files in `data/models/`, parsed
through the strict model codec and existing duplicate-rejecting `ModelRegistry`.
Each relative artifact path is anchored to its own metadata file's directory;
the in-memory registry uses absolute paths. Model metadata on disk is unchanged.
The manifest's explicit default ID must exist even when a Recipe overrides it.
SHA-256 verification and module selection are unchanged.

Recipes use the existing nonrecursive `*.recipe.json` catalog and stable Recipe
IDs. Only the manifest's last-applied ID is restored. Missing, invalid or duplicate
definitions block readiness; there is no first-model/first-Recipe fallback.
The editor still allows explicit Recipe selection and New/Duplicate, using the
existing draft/resolution/persistence path.

## Readiness and persistence

Startup restores the saved machine order/ROIs and camera sources exactly, validates
the six-camera configuration, and decodes/discards one Folder reference group to
check availability and image bounds. This and model hashing happen **before Qt is
created**. Subsequent Recipe preparation, including saved-source preflight, uses
the existing background preparation slot. Start reopens replay from its beginning.
No machine geometry is generated, repaired or calibrated by bootstrap.

A valid startup prepares a candidate and opens the existing five-page shell in
READY. Active configuration remains absent until successful Start. No session,
cycle, Batch, counters or previous results are restored. Engine creation, warm-up
and serial inference still belong to the existing worker after an explicit Start.

Absent/invalid persistent data opens the shell in UNCONFIGURED with a configuration
message and disabled Start. Valid saved geometry can still be displayed. Without
machine data the Production workspace shows an unavailable placeholder, not
synthetic camera images or ROIs. Startup diagnostics also go to stderr; the full
configuration message is accessible in the status tooltip. Comprehensive recovery
and model-management UI are not implemented; repair missing files externally and
restart where necessary.

The normal entry listens to the existing successful Apply signal. Only after
saved Recipe preparation and candidate installation succeed does it atomically
update `last_applied_recipe_id`. Selection, an unsaved draft, Save alone and failed
preparation do not update it. Language changes use the existing language signal
and the same atomic preferences writer. Failed preference writes are surfaced as
operation errors and leave the previous file intact. A missing manifest may be
created by a language change with null IDs; a malformed manifest is never replaced
automatically. Preferences do not duplicate active/runtime or Recipe-draft state.

## Local migration and developer entries

The operator confirmed `outputs/manual_phase3c` as the saved machine/source set
and explicitly selected **Local test** for restoration. Migration copied its saved
machine/source JSON and both Recipes without changing their contents. The verified
model was copied into `data/models/`; only the copied model metadata's artifact
path changed to a relative filename. Originals remain backups. This confirmation
does not establish production calibration or model accuracy.

`scripts/run_local_production_ui.py` remains the explicit-path real launcher.
`scripts/run_production_ui.py` remains the equipment-free fake demonstration.
Both are separate from normal persistent startup. The real explicit launcher and
normal entry share `ui/local_production.py`; no runtime/source implementation is
duplicated. Existing CLI image overrides retain their process-local behavior.
