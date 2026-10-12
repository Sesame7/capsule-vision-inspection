# Validation evidence

**Historical — frozen engineering evidence.** This archive was consolidated on
2026-10-12 from the Phase 0 findings and early integration records at source
checkpoint `8fad326aaaf5e0e981224963607a7e000eb95604`. It records finite,
model/dataset-specific observations, not a maintained API or an accuracy,
annotation-quality, indefinite leak-freedom or industrial acceptance certificate.

Current execution/verification belongs to [inference and runtime](../inference-runtime.md);
cycle and judgment meanings belong to [inspection](../inspection.md). The tracked
[golden fixture](../../tests/reference/phase0.json) remains self-contained and
authoritative for its regression identities and per-image semantics.

## Environment and model identity

The recorded SDK validation used Windows 10, Python **3.14.7 x64**, an NVIDIA
**RTX 4060 Ti 16 GB**, driver **616.92**, ViMo inference and custom SDK **3.20.0.2**,
NumPy **2.5.3** and OpenCV **5.0.0.93**. Pillow and PySide6 were available.
Authorization was active and the ViMo CUDA 12/TensorRT runtime worked.

The model was `model_trt_fp16.vimosln`, originally under `vendor/` and later
moved without byte changes to `models/`. Its SHA-256 was:

`b2380a2c8714b36f0c045e2d79600ff3bed84f9a745822abe473830dd40d982b`

The segmentation module was id **"2"**, name **"分割1"**, AlgoVersion **4.0**,
ModelVersion **V2**. Serial GPU execution used `max_batch_size=1`.
Model/module settings were used without reloading `vimo_params.json`.
Other dimension thresholds were zero.

| Label | Observed label-map ID | Area threshold | Exported display color |
| --- | ---: | ---: | --- |
| 黑点 | 1 | 30 | `#D40005` |
| 压痕 | 2 | 100 | `#CCC900` |
| 破损 | 3 | 30 | `#69CC00` |
| 变形 | 4 | 1500 | `#00CC85` |

The IDs above were cross-checked against the actual per-image label maps retained
in the golden fixture; they are observations for this model, not a generic
hard-coded mapping. Label maps and region counts were distinct fields.
Display colors came from exported model metadata and were keyed by label name;
they did not define mask IDs.

Masks and region information were enabled; score maps were disabled. Region flags
included AREA, LONG_SIDE, SHORT_SIDE, BOUNDING_RECT, MIN_AREA_RECT, OUTER_CONTOUR
and INNER_CONTOURS. Inputs were uint8 OpenCV BGR, typically `(960,1056,3)`.
A returned `(H,W,1)` mask was normalized only by removing its final singleton
channel; meaningful uint8 IDs and source-image coordinates were preserved.

## Serial and batch observations

On this model/hardware, serial x6 averaged approximately **31.06 ms** and true
batch=6 approximately **38.07 ms** in the recorded steady-state experiment.
Serial was selected as the initial wrapper path. This did not establish that
serial is faster for every model/device or prescribe all future execution topology.
These raw-SDK timings were not measurements of the later formal wrapper or full
Production path; [performance records](inspection-performance-records.md) preserve
those different workloads.

The full-folder serial/batch comparison recorded **400/400** equal high-level
semantic results, no-region/has-region proxies, label sets, label maps and
per-label region counts. It matched **232 regions**, with **0 unmatched regions**.
Small numerical mask/geometry differences existed; bit-exact equality was not
claimed. The agreement was between two inference modes, not with ground-truth
annotations or a final machine acceptance rule.

## Folder survey

The original survey decoded and inferred **400/400** images successfully.
Its aggregate results were:

| Result/label | Images | Regions |
| --- | ---: | ---: |
| No returned regions | 304 | 0 |
| At least one returned region | 96 | 232 |
| 压痕 | 25 | 48 |
| 变形 | 28 | 54 |
| 破损 | 1 | 2 |
| 黑点 | 51 | 128 |

Labels could coexist in one image, so label-image counts were not disjoint.
“OK/NG” meant only no returned defect regions / one or more returned regions
for this survey/comparison. These were not physical capsule counts or production
quality judgments.

The original scan identity retained in the fixture was
`scan_20260928T062857_758498Z.json`, with recorded `source_timestamp`
`2026-09-28T06:28:51.342073+00:00`. The timestamp is quoted as stored;
no unrecorded start/end meaning is inferred from the filename.

## Finite stability and visual checks

The recorded serial stability exercise used one module for **5,000 rounds × six
calls = 30,000 calls**, all successful, with **0 inference failures**,
**0 response-processing failures** and **0 semantic mismatches**. No meaningful
latency degradation or persistent RAM/device-memory growth was observed during
that finite run.

The user separately confirmed that the full stress run passed again after
reboot/cold start. This was a reported repeat confirmation, not a raw artifact
reconstructed during this archive migration. Device-wide GPU memory could include
other applications; Windows/WDDM process GPU memory was unavailable, not zero.
Neither observation proved indefinite stability or absence of a memory leak.

The user manually compared representative overlays with SmartMore training
software and Runtime Manager. Class, position, mask/contour, region and area
behavior appeared consistent on the inspected samples. That limited visual
comparison did not measure annotation quality, false-positive/negative rates,
model accuracy or industrial acceptance.

## Golden provenance and formal-wrapper regression

Phase 0 did **not** record source-image hashes. During Phase 1A, fingerprints were
collected from the user-confirmed local reference folder and added to the tracked
`tests/reference/phase0.json`, alongside model identity, the original scan
identity/timestamp and each image's expected `label_map`/`regions_per_label`.

The fixture contains all **400** file fingerprints and per-image semantics; this
archive does not duplicate them. It contains no model bytes, source images,
masks or contour arrays. Historical raw scan reports and removed spike scripts
are not regression prerequisites. The Phase 1A checkpoint
`be617078e89148eb3e7fb3ade09dc5e5e9db2dd7` retained the formal implementation
and the exploration history.

The findings document recorded an initial formal-wrapper regression on
**2026-09-28**: eight representative images and the full 400-image reference
matched the recorded semantics/totals. Checks also covered normalized mask
properties, geometry presence and result lifetime after subsequent calls/cleanup.
The representative subset contained two no-region images and all four labels.

The later local reports were under `outputs/phase1a_regression/`, not asserted to
be the original September 28 files. These generated reports were deliberately
retired in the owner-approved Phase 3M-3B-1 cleanup; the following records preserve
their previously checked identities and observations, not current file availability:

- `short.json`: timestamp `2026-09-29T09:49:19.919176+00:00`,
  **8** per-image semantics matched, **2** without regions and **6** with regions.
- `full_reference.json`: timestamp `2026-09-29T03:27:50.866143+00:00`,
  **400** per-image semantics matched and the survey totals above.

Both recorded the same model hash and reference-manifest SHA-256:

`efb80c393738c28ac973eabfc8fc508ad32fe43cdc19fafacfd0bb1f43811118`

That digest matched the retained tracked fixture during this read-only migration.
All 400 current local reference file hashes also matched it; this was a file
identity check, not a newly executed inference regression. Exact totals remain
conditional on this model and reference dataset, never generic engine behavior.

## Early worker and one-image Qt evidence

The original `docs/runtime-worker.md` recorded Phase 1B-01 validation on
**2026-09-29**: event-coordinated worker unit checks, one real defect-image
lifecycle and **10 successive new-worker lifecycles** passed. Construction,
inference and cleanup thread IDs matched within each lifecycle and differed
from the caller. Initialization/inference/shutdown failures were zero, with no
remaining named worker threads. The existing eight-image regression also passed.

These were serial successive lifecycles, not concurrent engines, a throughput
measurement or proof of leak absence. The full 400-image/stress runs were not
repeated in that worker validation.

The original `docs/qt-integration.md` recorded Phase 1B-02 validation on
**2026-09-29**: Qt unit checks, a real one-image Qt smoke and the worker one-image
guard passed with no residual named workers. Event-gated fake engines checked
responsive blocked initialization/inference, queued GUI delivery, original errors,
admission rejection, stop/close/quit, timeout ownership retention and late callbacks
after bridge destruction. The real smoke checked one golden defect image,
GUI/engine thread separation, asynchronous delivery and clean termination.
Ruff, formatting and strict Pyright were reported clean without new suppressions.

That evidence concerned the separate one-image diagnostic, not six-camera
Production UI, physical touch usability or model accuracy. Full dataset,
stress and benchmark experiments were not rerun. Current lifecycle and Qt
contracts are maintained in [inference/runtime](../inference-runtime.md) and
[UI shell](../ui-shell.md), rather than here.

## Confirmed local configuration migration

The bootstrap record at checkpoint `8fad326aaaf5e0e981224963607a7e000eb95604`
documented the operator's confirmation of `outputs/manual_phase3c` as the saved
machine/source set and explicit selection of **Local test** for restoration.
At that migration, saved machine/source JSON and both Recipes were copied without
content changes; the verified model was copied into `data/models/`. Only the
copied model metadata's artifact path changed to a relative filename. Originals
remained backups at that time. The owner-approved Phase 3M-3B-1 cleanup later
retired `outputs/manual_phase3c/` after checking its four configuration copies
against current `data/` and the Model/module/artifact identity. Active persistent
configuration and both current model locations were left intact.

This records migration provenance, not a claim that later operator-edited files
still match those originals. It established neither production calibration nor
model accuracy. Current startup/discovery and recovery belong to
[application](../application.md#startup-recovery); this history creates no fallback
or permission to overwrite saved geometry or unapproved backups.

## Evidence availability and limits

During the 2026-10-12 documentation consolidation, the fixture aggregates, observed
label IDs, its hash, local reference file identities and both retained regression
reports were checked directly. Relevant Git checkpoint identities were also read.
Those Phase 1A reports are no longer locally retained after the scoped cleanup;
the golden fixture, full reference images and model assets were preserved.

The original Phase 0 scan, serial/batch comparison and stability JSON artifacts
were not found in the retained `outputs/` tree. Their observations, reboot
confirmation and manual visual parity are therefore preserved as documented/user-
reported historical evidence, not newly independently reproduced raw results.
Early worker/Qt outcomes likewise come from their dated documentation; no new
unit/GPU execution or historical test logs were manufactured.

The fixture remains the self-contained regression authority. Optional local
reports, models and photographs were not made Markdown dependencies or copied
into Git. These records do not certify accuracy, annotation quality, safety,
machine throughput, a guaranteed deadline or indefinite runtime stability.

## Phase 3M final acceptance — 2026-10-12

**Historical — functional smoke acceptance**, recorded in Asia/Shanghai at Git
checkpoint `736d9b95c989eacc88ce0923042a904054278b12` on `main`.
Phase 3M-3D passed normal Ruff, formatting (114 files), strict Pyright
(0 errors/warnings) and pytest: **751 passed / 10 skipped**.
The skips were opt-in GPU/reference tests, not executed real-inference successes.

The saved startup graph resolved Model `general`, Recipe `local` (**Local test**),
the real `data/models/model_trt_fp16.vimosln` artifact and module `2`. Its verified
SHA-256 was:

`b2380a2c8714b36f0c045e2d79600ff3bed84f9a745822abe473830dd40d982b`

Machine/source order agreed as CAM1 → CAM6, with all 36 configured channel slots.
Each camera's first actual Folder image decoded as uint8 BGR `(960,1056,3)`;
saved ROIs passed bounds checks without replacement or calibration. Normal
[`app.py`](../../app.py) startup was directly observed in READY with Local test
and zero completed cycles, without automatic inspection. Preparation was distinct
from active-session activation; the [normal application workflow](../application.md#developer-entry-points)
remained authoritative.

Real Start/readiness, a completed six-camera/36-channel Production cycle without
technical ERROR/FAULT, Stop to READY and normal Close were **operator-confirmed**:
the response to the bundled observation request was “成功”. Automated GUI control
did not independently capture that sequence. Exact manual cycle counts, per-cycle
traces/identities and the manual run's exit code were not supplied. Subsequent
window/process checks found no Production window or matching `app.py` process;
the separately monitored launcher exited 0. These observations are not an
independently measured GPU regression or per-thread trace.

Tracked-file/Git integrity and protected identities passed verification: all
1,348 image files, seven persistent `data/` files, the 25 retained evidence files
and the [golden fixture](../../tests/reference/phase0.json) were preserved.
The licensed environment and existing native caches were not altered by cleanup.
Root `models/` remained absent, affecting optional defaults but not the verified
normal `data/models/` startup; restoration/path consolidation stayed deferred.

The owner approved retiring the four completed Phase 3M temporary audit
directories after this summary was preserved and checked; those ignored detailed
reports are not promised recoverable from Git. Earlier frozen evidence above
remains unchanged. This acceptance did **not** establish model accuracy,
calibrated geometry, throughput, long-term reliability or industrial acceptance.
