# ViMo Inference Demo

Typed serial segmentation core with maintained engineering tools. The supported
API is `VimoSegmentationEngine`; historical exploration lives in Git, with
findings in `docs/` and a golden fixture in `tests/reference/phase0.json`.
UI, camera, PLC and acceptance-policy development are outside the current scope.

## Environment and setup

Verified: Windows 10, Python **3.14.7 x64**, RTX 4060 Ti 16 GB, ViMo SDK/custom
**3.20.0.2**, NumPy **2.5.3**, OpenCV **5.0.0.93**. An active ViMo license and the
already provisioned CUDA 12/TensorRT runtime are external prerequisites.

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

Local inputs belong in root-level `models/` and `images/`, generated data in
`outputs/`; all three are ignored. The reference model is
`models/model_trt_fp16.vimosln`. Engine callers may supply any model path.

## Minimal API

Run from the repository root after setup:

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

`has_defects` means only “SDK returned regions”; it is not production OK/NG.
Reuse one engine serially in one owning worker context. Inference is synchronous;
a future UI must invoke it outside its main thread. Use `with` or `close()`.
Runtime staging/cache defaults to `outputs/vimo_cache/<model-sha256>/`, relative
to the working directory; the source model remains untouched.

## Quality gates

```powershell
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyright src tests scripts
uv run --no-sync pytest --tb=short
```

Normal pytest runs pure unit tests and skips GPU tests. Formal source, tests and
maintained tools are checked in strict mode. Pytest scratch files live
in `outputs/pytest-tmp/` and are disposable. Pyright uses installed vendor inline
annotations; unit tests do not import the real SDK.

Optional GPU regressions using the local model and `images/`:

```powershell
uv run --no-sync pytest -m "gpu and not full_reference" --run-gpu --require-gpu -s
uv run --no-sync pytest -m full_reference --run-full-reference --require-gpu -s
```

The short test covers eight representative images. Full regression checks all
400 image hashes and Phase 0 semantic results. Remove `--require-gpu` to allow
unavailable local prerequisites to skip. Reports go under
`outputs/phase1a_regression/`; tests do not persist images or masks.
The tracked golden fixture is self-contained; old Phase 0 scan reports are not
needed. Full regression is specific to its recorded model and image hashes.

## Maintained tools

Run from the repository root. Replace `images/example.png` with a local filename:

```powershell
uv run --no-sync python scripts/example_inference.py models/model_trt_fp16.vimosln images/example.png
uv run --no-sync python scripts/benchmark_inference.py models/model_trt_fp16.vimosln images --rounds 100 --warmup 20
uv run --no-sync python scripts/stress_inference.py models/model_trt_fp16.vimosln images --rounds 5000 --pool-size 24 --sample-interval 100 --gpu-memory
uv run --no-sync python scripts/visualize_inference.py models/model_trt_fp16.vimosln images --limit 10
```

- Example: one inference, concise semantic and region output.
- Benchmark: one serial engine, predecoded pool (default 6); each round visits
  the entire pool. Measures `engine.infer()` including normalization/copies,
  **not** the historical raw SDK timing. Warmup counts individual calls.
- Stress: one engine, six serial calls per round, fixed predecoded pool. Checks
  baseline semantics, reports latency/drift and Windows memory; optional
  `nvidia-smi` samples are outside timing. WDDM unavailable values remain null.
  Stops on the first exception and saves partial results; mismatch examples are
  capped at 20. No automatic leak or drift verdict.
- Visualizer: one image or directory, optional `--filenames name.png ...` or
  `--limit`. Preserves image coordinates, adds a legend below, and saves raw mask
  IDs plus compact metadata. Uses existing Pillow/Windows Microsoft YaHei for
  Chinese labels when available; otherwise ASCII region/ID references link to
  exact Chinese labels in JSON. No dependency is installed automatically.

All selection is deterministic and non-recursive. Tools write timestamped runs
under `outputs/benchmark/`, `outputs/stress/`, and `outputs/visualization/`.
Source images/models are never modified. The 30,000-call historical stress test
is not part of daily checks.

See [Phase 0 findings](docs/phase0-findings.md) and
[inference-core design](docs/inference-core.md) for evidence, contracts and limits.
