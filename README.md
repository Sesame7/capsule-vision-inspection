# ViMo Inference Demo

Phase 1A provides a typed serial segmentation core. Phase 0 reference scripts
remain unchanged in `scripts/`. UI, camera, PLC and acceptance-policy development
are outside this phase.

## Environment and setup

Verified: Windows 10, Python **3.14.7 x64**, RTX 4060 Ti 16 GB, ViMo SDK/custom
**3.20.0.2**, NumPy **2.5.3**, OpenCV **5.0.0.93**. An active ViMo license and the
already provisioned CUDA 12/TensorRT runtime are external prerequisites.

ViMo was installed with pip from local vendor wheels whose recorded paths no
longer exist in this checkout. Obtain wheels/runtime/license through SmartMore
on another machine. No public package URL is assumed. These proprietary packages
are excluded from `pyproject.toml`/`uv.lock`; the lock covers public dependencies.

Preserve the existing `.venv`. Install locked public dependencies and the editable
core while retaining external vendor packages:

```powershell
uv sync --locked --inexact --no-python-downloads
```

`--inexact` matters: an exact sync may remove externally provisioned packages.
Everyday commands use `--no-sync` to leave the working environment unchanged.
Nothing here installs or changes GPU/runtime components.

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

with VimoSegmentationEngine("vendor/model_trt_fp16.vimosln") as engine:
    result = engine.infer(image_bgr)
    print(result.has_defects, result.regions_per_label)
    # result.mask: owned uint8 (H, W), preserving SDK label IDs.
```

`has_defects` means only “SDK returned regions”; it is not production OK/NG.
Reuse one engine serially in one owning worker context. Inference is synchronous;
a future UI must invoke it outside its main thread. Use `with` or `close()`.
Runtime staging/cache defaults to `outputs/vimo_smoke/<model-sha256>/`, relative
to the working directory; the source model remains untouched.

## Quality gates

```powershell
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pyright src tests
uv run --no-sync pytest
```

Normal pytest runs pure unit tests and skips GPU tests. Only formal source/tests
are checked; Phase 0 scripts are outside these gates. Pytest scratch files live
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

See [Phase 0 findings](docs/phase0-findings.md) and
[inference-core design](docs/inference-core.md) for evidence, contracts and limits.
