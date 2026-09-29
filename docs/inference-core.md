# Inference core contract

The synchronous engine is the lowest project layer. See
[architecture](architecture.md) for its relationship to the worker and Qt demo.

## API and structure

```python
from vimo_demo.inference import VimoSegmentationEngine

with VimoSegmentationEngine(model_path, device_id=0) as engine:
    result = engine.infer(image_bgr)
```

Typed models, pure validation/staging helpers and one SDK adapter are sufficient.
The public engine lives in
[`vimo_adapter.py`](../src/vimo_demo/inference/vimo_adapter.py); maintained tools
use only the public API.
Optional keyword-only `module_id` disambiguates segmentation modules; otherwise
exactly one must exist in SDK metadata. The reference model selects module `2`;
ambiguity fails explicitly.

## Input/results

Runtime input contract: nonempty **uint8 H x W x 3 BGR** ndarray. The annotation
permits NumPy scalar dtypes to accommodate OpenCV's broad return annotations;
runtime validation is strict. No decoding, RGB guessing, resizing or orientation
change occurs. Contiguous input is reused; strided input is safely made contiguous.
The caller must not mutate input pixels during inference.

- `BoundingRect`: x/y/width/height in original decoded-image coordinates.
- `RotatedRect`: unchanged SDK center-x/center-y/width/height/angle convention.
- `DefectRegion`: label, area, rectangle, long/short sides, outer/inner contours
  and minimum-area rectangle. These outputs are always requested; empty contours
  remain empty sequences.
- `ImageResult`: shape, actual label-to-mask-ID map, regions, mask, ROI/rotated ROI
  and module ID. `regions_per_label` derives from regions, not `label_map`.
  `mask_id_to_label` only inverts actual mappings, never inventing class names.
  `has_defects` means regions exist, not a final NG judgment.
- `EngineInfo`: model hash, module identity/labels, device ID and area thresholds.

Engine-produced dataclasses are frozen, maps are read-only snapshots, and contours
are tuples. The mask owns a copy of SDK pixels and is read-only by default. It
survives later calls and cleanup; use `.copy()` for writable pixels. NumPy arrays
are not deeply immutable security objects. ImageResult deliberately has no
array-based equality operation.

Both `(H,W)` and `(H,W,1)` uint8 masks are valid; only the last singleton channel
is squeezed. Incompatible shapes/dtypes fail. IDs remain raw integers, not display
colors. The documented background ID is 0; no background label is fabricated.

## Lifecycle and settings

Construction stages a checksum-verified model, loads one Solution and creates one
GPU segmentation module (`device_id=0` by default, `max_batch_size=1`). Default
`cache_dir` is `outputs/vimo_cache`, relative to the working directory. Runtime
files live under `<sha256>/`. Keep cache_dir writable and separate from source
models. Sources remain read-only; corrupt staged models fail; missing caches rebuild.

Setup preserves the verified settings: enable region output and OR AREA,
LONG_SIDE, SHORT_SIDE, BOUNDING_RECT, MIN_AREA_RECT, OUTER_CONTOUR and INNER_CONTOURS.
Thresholds, ROI and mask operation remain SDK values. No `vimo_params.json` reload.
Mask-enabled/score-map-disabled defaults are checked, not silently changed. No
hidden warmup or retry occurs.

One engine belongs to **one serial owning context**, including construction and
cleanup. In the Qt application, the [InferenceWorker](runtime-worker.md) factory,
inference calls and close all run on its dedicated Python worker thread. CLI tools
may use the context manager directly on their calling thread. The engine itself
provides no SDK thread-safety guarantee, locks, batching or scheduling; runtime
and UI dependencies do not enter this layer. GUI inference is never performed on
the Qt main thread.

Use `with` or explicit idempotent `close()`. ExitStack invokes actual SDK context
managers, module before Solution, including failed setup. No invented disposal
method or reliance on garbage collection. Calls after close fail. A cleanup error
is raised, or attached as a note when preserving an existing caller exception.

## Exceptions and typing

Bad inputs raise `TypeError`/`ValueError`. Setup raises `VimoInitializationError`,
request/run failures `VimoInferenceError`, and conversion failures
`VimoResponseError`. All derive from `VimoError`, also used for lifecycle errors.
Original exceptions are chained, keeping SDK type/code/message in `__cause__`.
No automatic recovery follows a runtime/license error.

Only the adapter imports ViMo, lazily. Runtime `isinstance` narrows the module
union before parameter access. The SDK has inline annotations but no `py.typed`
marker: import lines locally suppress `reportMissingTypeStubs`, while Pyright
still reads those annotations. Documentation-only geometry classes obscure runtime
tuples: three small conversion functions locally cast verified tuple forms.
Defensive checks of claimed C++ return types locally suppress
`reportUnnecessaryIsInstance`. Global attribute/argument checks remain enabled;
formal source contains no `Any`. NumPy dtype casts follow explicit runtime checks.

## Development and regression

Use the [README setup and quality commands](../README.md). ViMo/custom remain
external prerequisites; the public dependency lock does not replace them.

Unit tests use pure arrays and small SDK stand-ins, without real SDK/GPU/model
access. They cover validation, mask ownership, counts versus IDs, staging,
parameter preservation, exception chains and cleanup. Normal pytest skips GPU.
Ruff and strict Pyright cover `src/`, `tests/` and maintained `scripts/`.
The golden fixture is [`tests/reference/phase0.json`](../tests/reference/phase0.json);
historical raw scan outputs and exploration scripts are not runtime or regression
dependencies. Its provenance and historical results are in
[Phase 0 findings](phase0-findings.md).

Eight-image regression includes two no-region images and all four labels. It
checks semantics, mask properties, geometry presence and result lifetime. Full
regression verifies all 400 file hashes before comparing per-image semantics and
aggregate counts. Golden model/dataset assertions live only in tests. Geometry
and masks are not required to be bit-exact golden outputs.

Missing files, SDK/DLLs or license skip optional regressions; other setup errors
fail. `--require-gpu` makes missing prerequisites fail too. Reports go to
`outputs/phase1a_regression/`. These tests establish integration behavior, not
model accuracy, annotation quality or a production acceptance policy.
