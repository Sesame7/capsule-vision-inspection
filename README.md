# ViMo Inference Demo

Local industrial-vision inference demo based on the ViMo Windows Python SDK.

## Current goal

Build a minimal but modern demo that:

- loads one batch of 6 local images;
- runs ViMo segmentation inference;
- displays per-image OK/NG and defect information;
- reports inference and total batch timing;
- keeps inference independent from the desktop UI;
- leaves room for a future read-only web dashboard.

## Current environment

- Windows 10
- Python 3.14.7
- NVIDIA GeForce RTX 4060 Ti 16 GB
- smartmore.vimo.inference 3.20.0.2
- smartmore.vimo.inference.custom 3.20.0.2
- OpenCV 5.0.0
- NumPy 2.5.3
- PySide6

## Status

Confirmed:

- Python 3.14 virtual environment works.
- ViMo SDK installs successfully.
- `import smartmore.vimo.inference` succeeds.
- OpenCV, NumPy and PySide6 import successfully.
- `pip check` reports no broken requirements.
- NVIDIA GPU and driver are visible through `nvidia-smi`.

Not yet confirmed:

- ViMo license activation.
- `.vimosln` model loading.
- TensorRT runtime compatibility.
- Single-image inference.
- Batch-of-6 inference.
- Actual latency and GPU memory usage.

## Development phases

### Phase 0 — Engineering validation

- Qt image-display test
- background-worker/thread test
- ViMo model smoke test
- single-image inference
- sequential 6-image inference
- batch=6 inference
- benchmark

### Phase 0.1 — Application skeleton

- desktop control UI
- inference backend abstraction
- mock backend
- ViMo backend
- BatchResult data model

A read-only web dashboard will be considered after the inference path is stable.
