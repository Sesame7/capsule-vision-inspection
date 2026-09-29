# Phase 0 verified findings

These are SDK integration findings, not validation of model accuracy or
annotation quality. These historical experiments are not continuously rerun.
Findings are frozen here and in the maintained golden fixture; Git history
(including the Phase 1A checkpoint `be61707`) preserves exploration code.
`VimoSegmentationEngine` is now the supported reference API. Current contracts
and future direction belong in [architecture](architecture.md), not this evidence log.

## Environment and model

- Windows 10; Python 3.14.7 x64; RTX 4060 Ti 16 GB; NVIDIA driver 616.92.
- ViMo SDK/custom 3.20.0.2; active license; working CUDA 12/TensorRT runtime.
- NumPy 2.5.3, OpenCV 5.0.0.93; Pillow and PySide6 available.
- Current local model: `models/model_trt_fp16.vimosln` (moved without byte changes).
- SHA-256: `b2380a2c8714b36f0c045e2d79600ff3bed84f9a745822abe473830dd40d982b`.
- Segmentation module `2`, `分割1`, AlgoVersion 4.0, ModelVersion V2.
- Area thresholds: 黑点 30, 压痕 100, 破损 30, 变形 1500; other current dimension
  thresholds zero. SDK parameters were used without reloading `vimo_params.json`.
- Masks/regions enabled, score maps disabled. Region flags: area, long/short
  sides, bounding/minimum-area rectangles, outer/inner contours.
- Input: uint8 BGR, typically `(960,1056,3)`. Mask `(H,W,1)` is safely squeezed
  only along its last singleton channel; meaningful uint8 IDs are preserved.
- Reference display colors from the exported model metadata: 黑点 `#D40005`,
  压痕 `#CCC900`, 破损 `#69CC00`, 变形 `#00CC85`. These are visualization colors
  keyed by label name, not a mask-ID mapping. Actual IDs come from each result.

## Observations

Serial x6 mean was approximately **31.06 ms**, versus **38.07 ms** for batch=6
on this model/hardware. Serial (`max_batch_size=1`) is the initial core path;
the result does not generalize to all models/hardware.

The survey decoded and inferred 400/400 images:

| Result/label | Images | Regions |
| --- | ---: | ---: |
| No returned regions | 304 | 0 |
| At least one returned region | 96 | 232 |
| 压痕 | 25 | 48 |
| 变形 | 28 | 54 |
| 破损 | 1 | 2 |
| 黑点 | 51 | 128 |

Labels can coexist in one image. “OK/NG” was only a no-region/has-region proxy.
The full serial/batch comparison found 400/400 equal semantic results, proxies,
label sets, label maps and per-label counts: 232 matched regions, none unmatched.
Small mask/geometry numerical differences exist; bit-exact equality is not claimed.

The 5,000-round serial stability run completed 30,000 successful calls, with zero
inference failures, processing failures or semantic mismatches. No meaningful
latency degradation or persistent RAM/device-memory growth was observed. The user
confirmed the full stress run passed again after reboot/cold start. This is finite
evidence, not proof of indefinite stability or no leaks. Device GPU memory is
shared; Windows/WDDM process GPU memory was unavailable, not interpreted as zero.

The user manually compared representative overlays against SmartMore training
software/Runtime Manager. Class, position, mask/contour, region and area behavior
were visually consistent on inspected samples. Accuracy, annotation quality and
false-positive/negative rates were not established.

## Phase 1A baseline and regression

[`tests/reference/phase0.json`](../tests/reference/phase0.json) freezes label maps
and per-label counts from
`scan_20260928T062857_758498Z.json`, plus model identity and current image-file
SHA-256 fingerprints. Phase 0 did not record image hashes: fingerprints were
captured during Phase 1A from the user-confirmed local reference folder. No model
bytes, source images, masks or contour arrays are tracked in this fixture.
The fixture is self-contained: the original raw scan JSON is not required for
regression. Keep it tracked under `tests/reference/`, not under `outputs/`.

On 2026-09-28 the formal wrapper passed eight representative images and the full
400-image regression, matching every recorded semantic result and the totals
above. Normalized mask properties, geometry presence and retained result lifetime
after subsequent calls/cleanup were also checked. Generated reports live under
`outputs/phase1a_regression/` and are not required by the tests.
