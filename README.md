# ViMo Inference Demo

Local industrial-vision integration with a typed `VimoSegmentationEngine`, a
bounded `InferenceWorker` and a PySide production shell. The engine runs
synchronously on its owning Python worker thread; Qt receives queued notifications
on the GUI thread in the same process. The original single-image integration demo
remains available alongside the simulated six-camera Production page.

SDK validation, the formal core, worker lifecycle and Qt integration are verified.
The [inspection-cycle contracts](docs/inspection-cycle.md) define bounded collection
and deterministic technical results. Phase 1D adds a
[runtime coordinator](docs/runtime-coordinator.md) for overlapping cycles, bounded
inspection dispatch and coordinated shutdown, verified with 200 fake cycles and
one six-frame GPU wiring test. The Qt demo remains single-image. Real camera/PLC
integration, hardware deadline events and web display are not implemented. See
[architecture and future direction](docs/architecture.md).

Phase 2A provides the [production inspection domain](docs/production-inspection.md):
channel layouts, defect assignment and separate technical/product judgments.
Phase 2B provides [Recipe/model/layout configuration](docs/inspection-configuration.md)
with strict JSON persistence and immutable per-channel resolution. Phase 2C adds
[ProductionApplication](docs/production-application.md): transactional activation,
worker-owned warm-up, disposable runtime sessions, configured checks and factual
cycle/channel counters. Phase 2D-1 adds the [Production screen](docs/production-ui.md),
driven by application snapshots with separate bounded display frames. Phase 2D-2A
adds [Parameters / Recipe editing](docs/recipe-configuration-ui.md), with distinct
Validate, Save and Apply actions. Diagnostics, History and Settings remain
placeholders; there is no commissioned camera/PLC integration.
Phase 2D-2B adds the [graphical ROI editor](docs/graphical-roi-editor.md): source-pixel
rectangle editing against a frozen reference, using the same Recipe draft.
The architecture document also maps all contracts and the production UI design reference.

Phase 3B adds an opt-in [local real-inference Production mode](docs/production-ui.md#local-real-inference-mode--phase-3b):
`scripts/run_local_production_ui.py` loads supplied Recipe/model/layout JSON and
repeatedly inspects one fixed six-image set using the real ViMo worker. The existing
fake Production launcher is unchanged. Configuration/launch commands and the short
GPU UI smoke are documented there; calibrated channel layouts are not bundled.

## Environment and setup

Verified: Windows 10, Python **3.14.7 x64**, RTX 4060 Ti 16 GB, ViMo SDK/custom
**3.20.0.2**, NumPy **2.5.3**, OpenCV **5.0.0.93**, PySide6 **6.11.2**.
An active ViMo license and the provisioned CUDA 12/TensorRT runtime are
external prerequisites.

Obtain SDK wheels/runtime/license through SmartMore when provisioning a machine.
Vendor packages and manuals are not retained in this checkout. The working
installation is preserved in `.venv`. These proprietary packages
are excluded from `pyproject.toml`/`uv.lock`; the lock covers public dependencies.

Preserve the existing `.venv`. Install locked public dependencies and the editable
core while retaining external vendor packages:

```powershell
uv sync --locked --inexact --no-python-downloads
```

`--inexact` matters: an exact sync may remove externally provisioned packages.
Everyday commands use `--no-sync` to leave the working environment unchanged.
Nothing here installs or changes GPU/runtime components.
OpenCV is currently in the default development dependency group, included by
this sync; the inference core itself accepts arrays.

Local inputs belong in root-level `models/` and `images/`, generated data in
`outputs/`; all three are ignored. The reference model is
`models/model_trt_fp16.vimosln`. Engine callers may supply any model path.

## Minimal API

This synchronous CLI example uses one engine in one owning context. Run from the
repository root after setup:

```python
from pathlib import Path

import cv2
import numpy as np

from vimo_demo.inference import VimoSegmentationEngine

path = next(iter(sorted(Path("images").glob("*.png"))))
image_bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
if image_bgr is None:
    raise ValueError(f"Cannot decode {path}")

with VimoSegmentationEngine("models/model_trt_fp16.vimosln") as engine:
    result = engine.infer(image_bgr)
    print(result.has_defects, result.regions_per_label)
    # result.mask: owned uint8 (H, W), preserving SDK label IDs.
```

Input is a uint8 H x W x 3 BGR ndarray. `ImageResult` owns its normalized mask and
separates label IDs from region counts. `has_defects` is not a production OK/NG
decision. See the [engine contract](docs/inference-core.md).
Runtime staging/cache defaults to `outputs/vimo_cache/<model-sha256>/`, relative
to the working directory; the source model remains untouched.

GUI callers use `InferenceWorker` with an engine factory so construction,
inference and cleanup stay on its thread. Its bounded queue defaults to one
pending task plus the active call; submitted arrays remain immutable until Future
completion. The [worker guide](docs/runtime-worker.md) contains the CLI Future
example and full lifecycle contract.

## Production screen and integration demo

The six-camera shell needs no GPU or local assets for its simulated input:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
```

Click **Start inspection**. Use `--scenario ng`, `warning` or `fault` to review
abnormal states; `--language en --size 1920x1080` shows the landscape layout.
Counters represent channel judgment occurrences, not physical capsule counts.
Open **Parameters** for the synthetic Demo A/B Recipe editor. Save explicitly;
Apply installs a candidate, and successful Start activates it. Parameters remain
viewable but read-only while running. See the
[Recipe acceptance walkthrough](docs/recipe-configuration-ui.md#presentation-and-synthetic-acceptance-walkthrough).
For graphical ROI review, select Channel / CAM3 / CH17, then open the editor.
The [ROI walkthrough](docs/graphical-roi-editor.md#equipment-free-acceptance-walkthrough)
covers move/resize, numeric synchronization, reference refresh and Save/Apply.
See the [UI contract and review guide](docs/production-ui.md) for ownership,
shutdown, alternate sizes and remaining physical-screen validation.

The older one-image SDK integration demo is separate:

```powershell
uv run --no-sync python -m vimo_demo.ui.app models/model_trt_fp16.vimosln
```

Click **Start Worker**, wait for READY, select a local image and run inference.
Optional `--image images/<filename>.png` pre-fills the path. The window remains
responsive during backend work and defers closing until the worker exits.
Native calls cannot be forcibly cancelled.
This is an integration demo, not final UI. See [Qt integration](docs/qt-integration.md)
for thread/lifetime contracts, timeout behavior and a short manual check.

## Quality gates and optional GPU regression

```powershell
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyright src tests scripts
uv run --no-sync pytest --tb=short
git diff --check
```

Normal pytest runs unit/headless Qt tests and skips GPU tests. Maintained source,
tests and scripts are checked in strict mode. Pytest scratch files live
in `outputs/pytest-tmp/` and are disposable. Pyright uses installed vendor inline
annotations; unit tests do not import the real SDK.

Optional GPU regressions using the local model and `images/`:

```powershell
uv run --no-sync pytest tests/integration/test_qt_vimo.py tests/integration/test_worker_vimo.py::test_worker_one_image --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_coordinator_vimo.py --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_application_vimo.py --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_reference.py -m "gpu and not full_reference" --run-gpu --require-gpu -s
uv run --no-sync pytest -m full_reference --run-full-reference --require-gpu -s
```

The commands cover one-image Qt/worker checks, a six-frame coordinator cycle,
application startup/warm-up/cleanup,
a short engine reference subset, and the full reference dataset respectively.
Remove `--require-gpu` to skip
unavailable prerequisites. The tracked `tests/reference/phase0.json` is the
self-contained golden fixture; old reports and exploration scripts are not
required. Full regression is specific to its recorded model and 400 image hashes.
See [regression details](docs/inference-core.md#development-and-regression) and
[historical findings](docs/phase0-findings.md); SDK regression does not establish
model accuracy or production acceptance.

## Maintained tools

All tools use the formal inference API. Run from the repository root and replace
`images/example.png` with a local filename. Use each tool's `--help` for options:

```powershell
uv run --no-sync python scripts/example_inference.py models/model_trt_fp16.vimosln images/example.png
uv run --no-sync python scripts/benchmark_inference.py models/model_trt_fp16.vimosln images --rounds 100 --warmup 20
uv run --no-sync python scripts/stress_inference.py models/model_trt_fp16.vimosln images --rounds 5000 --pool-size 24 --sample-interval 100 --gpu-memory
uv run --no-sync python scripts/visualize_inference.py models/model_trt_fp16.vimosln images --limit 10
```

| Tool | Purpose / output |
| --- | --- |
| Example | One image; semantic and region output in the terminal |
| Benchmark | Serial wrapper latency, including normalization/copies; `outputs/benchmark/` |
| Stress | Six calls per round, semantic checks, latency/resource samples; `outputs/stress/` |
| Visualizer | Mask, contours, boxes and label legend; `outputs/visualization/` |

Selection is deterministic and non-recursive; source models/images are unchanged.
Wrapper timings differ from historical raw SDK measurements. Long stress runs
are not daily checks and make no automatic leak verdict. The visualizer uses
optional existing Pillow/Microsoft YaHei for Chinese labels, otherwise ASCII
references link to Chinese labels in JSON; it installs nothing automatically.
