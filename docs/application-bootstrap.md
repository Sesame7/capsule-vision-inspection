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

The separate application manifest requires `schema_version`, `default_model_id`
and `last_applied_recipe_id` (IDs may be null to leave configuration unselected).
Only `language` is optional:

```json
{
  "schema_version": 1,
  "default_model_id": "general",
  "last_applied_recipe_id": "local",
  "language": "zh"
}
```

An omitted language uses Chinese (`zh`). An explicitly invalid language also
uses `zh` **in memory**, with a localized warning; valid explicit production IDs
are preserved. Unsupported versions, duplicate keys, unknown fields, missing
identity fields or invalid identity values still invalidate the manifest. No
identities are recovered from an unreliable manifest. Startup never writes it.

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
created**. Subsequent Recipe preparation verifies the artifact, catalog and saved
source schema/identities in the existing background slot. Folder availability is
acquisition preflight, not candidate validity. Start reopens replay from its beginning
and validates its first group before constructing a session/engine.
No machine geometry is generated, repaired or calibrated by bootstrap.

A valid startup prepares a candidate and opens the existing five-page shell in
READY. Active configuration remains absent until successful Start. No session,
cycle, Batch, counters or previous results are restored. Engine creation, warm-up
and serial inference still belong to the existing worker after an explicit Start.

The shell remains navigable independently of readiness. Valid saved geometry can
still be displayed. Without machine data the Production workspace shows an
unavailable placeholder, not synthetic camera images or ROIs. Concise startup
messages are localized in the shell and Recipe feedback; full path/error context
is in tooltips and stderr. Runtime faults and command failures retain precedence.

Recipe Apply does not establish Folder recovery: unresolved source warnings and
their full diagnostic tooltips survive it. They clear after successful Start
(the existing acquisition preflight) or successful Vision source verification,
save and Apply. Reading one reference or editing a path is not that evidence.
There is no Folder monitoring or configuration rediscovery.

| Startup condition | State / behavior |
| --- | --- |
| Missing data/manifest; malformed or unsupported manifest; absent/unknown explicit ID | UNCONFIGURED; no first-entry fallback |
| Missing, malformed or duplicate Model/Recipe definitions | UNCONFIGURED; catalog problems cannot be bypassed by Apply |
| Missing/unreadable artifact or SHA-256 mismatch | UNCONFIGURED; no alternate model or fake engine |
| Missing/invalid machine or source JSON; source-camera identity mismatch | UNCONFIGURED; no invented geometry/assignments |
| Valid candidate and source configuration, but Folder is missing, empty, unreadable or incompatible | READY with warning; unchanged Start preflight prevents engine creation until acquisition is usable |
| Missing/invalid language only | Valid candidate remains READY, using `zh` in memory |

Permission/IO failures follow the same distinction: configuration/artifact IO
blocks candidate preparation; Folder IO prevents acquisition. Original files,
camera order, mappings, geometry and inspection settings remain authoritative.
Legitimately omitted optional Recipe fields use the existing strict codec defaults;
invalid explicit values remain errors.

The normal entry listens to the existing successful Apply signal. Only after
saved Recipe preparation and candidate installation succeed does it atomically
update `last_applied_recipe_id`. Selection, an unsaved draft, Save alone and failed
preparation do not update it. Language changes use the existing language signal
and the same atomic preferences writer. Failed preference writes are surfaced as
operation errors and leave the previous file intact. A definitely missing manifest
may be created by an explicit language change with null IDs. Invalid-language,
malformed, unsupported or unreadable manifests are read-only for this session,
including language changes and Apply notifications. If the file changes externally
after startup, preference saving refuses to overwrite it. Correct it externally
and restart. Preferences do not duplicate active/runtime or Recipe-draft state.

Preference-persistence failure is shown separately from successful Recipe Apply,
with localized app.json repair/restart guidance and full details in the existing
tooltip/stderr. Unresolved manifest guidance survives unrelated Apply, Start or
Stop. A successful preference write clears its own error, without clearing another
command's feedback. The read-only protection and candidate installation are unchanged.

## Supported recovery

1. Open `app.py`; identify the localized startup message and inspect its tooltip
   or stderr for the affected file/identity.
2. Where valid supporting definitions exist, use Recipe selection/Save and apply
   or Vision's Folder/source and machine tools. Folder-path repair does not replace
   saved ROI geometry. Missing/invalid machine, model, manifest or source schema
   requires external maintenance; no recovery wizard or fabricated defaults exist.
3. Correct only the affected configuration/artifact and restart `app.py` for fresh
   discovery, resolution and verification. Permissions must permit reading it.
4. Check READY and the intended candidate. Successful preparation does not
   activate inference; explicitly Start after acquisition is usable.

There is no in-process rediscovery guarantee or automatic rollback/backup. Recovery
tests use isolated temporary data, not the migrated local `data/` configuration.

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
