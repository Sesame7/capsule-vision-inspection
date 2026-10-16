# Capsule Vision Inspection

Configurable capsule-inspection application with six configured cameras and 36
inspection channels in the current layout. It combines serial ViMo inference,
traditional inspection checks and saved Folder replay or controlled continuous Basler acquisition in a Chinese/English
PySide6 desktop UI, with optional FINS/UDP or FINS/TCP communication/reporting and
read-only Web observation. Communication and optional Production reporting use
configured numeric PLC endpoints; the localhost simulator remains UDP-only.

Passive continuous Basler acquisition and opt-in PLC result submission are
implemented; mechanical-cycle commissioning and physical sorting remain unimplemented. Size/Color screening is an uncalibrated demonstration; SDK
regression does not certify model accuracy or industrial acceptance.

The public repository name is `capsule-vision-inspection`. The Python distribution
remains `vimo-demo`, with `vimo_demo` imports; existing checkout directories do not
need renaming.

## Environment and setup

The verified development environment uses **Windows 10**, **Python 3.14.7 x64**,
an **RTX 4060 Ti 16 GB**, and SmartMore ViMo inference/custom **3.20.0.2**.
An active ViMo license and the externally provisioned CUDA 12/TensorRT runtime
are prerequisites.
Obtain proprietary SDK packages, runtime and authorization through SmartMore;
uv does not provision them. Vendor packages are not public project dependencies.

Public dependency versions are authoritative in [pyproject.toml](pyproject.toml)
and [uv.lock](uv.lock). Preserve the vendor-provisioned `.venv`; daily commands
use `uv run --no-sync`. Intentional public setup/resynchronization uses the
`uv sync --locked --inexact --no-python-downloads` command below: an exact sync
can remove externally installed vendor packages. Public synchronization does not
install or change GPU/runtime components. The current application setup uses the
default development dependency group, including OpenCV; the inference core accepts
already decoded arrays.

### First deployment on another Windows PC

1. Check the target Windows/GPU/driver compatibility with the vendor's SDK/runtime
   requirements; the development hardware above is a verified baseline, not a
   minimum specification or a compatibility claim for another PC. Prepare Git, uv
   and **Python 3.14.7 x64**, then clone and install the locked public environment:

   ```powershell
   $repositoryUrl = "<published GitHub clone URL for capsule-vision-inspection>"
   git clone "$repositoryUrl" capsule-vision-inspection
   Set-Location capsule-vision-inspection
   uv sync --locked --inexact --no-python-downloads
   ```

   Replace the URL placeholder with the actual repository URL after publication.
   `.venv` is machine-local: create it on this PC, never copy it between PCs.
   Use the same inexact sync when a vendor-provisioned environment already exists.

2. Separately provision SmartMore ViMo inference/custom **3.20.0.2** into this
   `.venv`, the compatible CUDA 12/TensorRT runtime, and valid inference
   authorization through the vendor-approved procedure. Neither the clone nor uv
   supplies those proprietary prerequisites. Do not assume they are public PyPI
   dependencies or copy another PC's runtime/license installation.

3. Transfer only authorized local model, configuration and Folder image assets.
   Normal startup needs a valid entry-relative `data/` tree with explicit Model/
   Recipe IDs, model artifact identity, saved MachineLayout and camera-source
   definitions. Check target paths and Folder availability against
   [configuration](docs/configuration.md#artifact-paths) and
   [startup/recovery](docs/application.md#startup-recovery); preserve saved geometry
   rather than inventing replacement ROIs. CLI/reference `models/` and `images/`
   remain separate inputs. These assets are not supplied by the public repository.

4. From the repository root, launch normal startup:

   ```powershell
   uv run --no-sync python app.py
   ```

   With valid inputs it opens stopped in READY. Explicitly Start or Step the saved
   Folder sources, verify real six-camera/36-channel results without technical
   ERROR/FAULT, then End/Stop, wait for READY and close normally; confirm the
   process exits. Follow the existing
   [local replay verification](docs/application.md#normal-entry-manual-verification)
   for Pause/Continue/Step details. A successful clone or Qt startup alone does
   **not** establish ViMo inference readiness. This local software check is not
   camera/PLC commissioning or industrial acceptance.

## Local assets and persistent configuration

| Location | Role |
| --- | --- |
| `data/` beside `app.py` | Persistent `app.json`, `machine/machine.json`, `sources/camera_sources.json`, Model definitions/artifacts under `models/` and Recipes under `recipes/` |
| Root `models/`, `images/` | Local CLI/reference inputs; the reference model is `models/model_trt_fp16.vimosln` |
| `outputs/` | Generated caches/reports; the selected historical evidence is under `retained-evidence/` |
| [tests/reference/phase0.json](tests/reference/phase0.json) | Tracked, self-contained golden fixture; historical raw reports are not regression dependencies |

The first three locations are ignored by Git. Preserve persistent calibration/
configuration, models, images and deliberately retained evidence. Historical
outputs, manual snapshots and prototypes can be retired after explicit scoped
approval; ignored/generated status alone does **not** authorize cleanup.
Historical validation and retained-evidence provenance are documented in
[validation evidence](docs/archive/validation-evidence.md). The golden fixture
remains independent of historical raw reports. Engine callers can supply arbitrary
model paths. Root `models/` and normal startup's `data/models/` serve different
workflows; do not substitute one for the other.

Normal startup uses explicit default-Model and last-applied-Recipe IDs in
`data/app.json`. Relative model artifacts are anchored to their Model JSON;
relative Folder paths to their source JSON. See
[configuration paths](docs/configuration.md#artifact-paths) and
[startup/recovery](docs/application.md#startup-recovery) before preparing local files.
Saved MachineLayout geometry is authoritative; startup/editing does not repair or
calibrate it automatically.

## Application entry points

Run from the repository root in the existing environment:

| Entry | Purpose |
| --- | --- |
| [app.py](app.py) | Normal persistent **real-inference** Production application |
| [run_local_production_ui.py](scripts/run_local_production_ui.py) | Explicit-path **real** Production developer launcher; Folder/fixed inputs or opt-in one-cycle Basler |
| [run_production_ui.py](scripts/run_production_ui.py) | Equipment-free Production review with a **fake engine** |
| [python -m vimo_demo.ui.app](src/vimo_demo/ui/app.py) | Separate **real one-image** inference/worker/Qt diagnostic |
| [acquire_basler_group.py](scripts/acquire_basler_group.py) | Isolated one-window six-camera Basler acquisition, using externally provisioned pypylon; no Production or automatic trigger |

[Basler acquisition](docs/application.md#isolated-basler-acquisition) documents
operator coordination, owned images, failure/cleanup guarantees and independent
field staging. Startup restoration and Folder workflows do not require pypylon;
actual Basler acquisition requires separately provisioned pypylon/pylon. Vision
saves one global source mode, six explicit serials and independent Folder reference/
warm-up paths. See the [normal Basler workflow](docs/application.md#persistent-basler-continuous-inspection)
for Start/ARMED, passive external hardware triggering, early serial inspection and
Stop/READY. Production generates no PLC photo commands. No environment synchronization
is performed; hardware trigger identity and reject-result commissioning remain limited.
The separate [developer Basler workflow](docs/application.md#developer-basler-single-cycle)
uses Folder images only for warm-up, requires explicit serial mapping verification,
and runs at most one hardware group through Production per Start. It requires
separate owner-approved pypylon provisioning; no dependency is installed automatically.

```powershell
uv run --no-sync python app.py
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python -m vimo_demo.ui.app models/model_trt_fp16.vimosln
```

Normal `app.py` has no language/size CLI. It restores persistent configuration and
opens stopped with a prepared candidate, without creating an engine or restoring
Batch/counters/results. Invalid required data leaves it UNCONFIGURED; a valid
candidate with unavailable Folder inputs can remain READY with a warning and a
failing Start preflight. It never chooses a first configuration, creates missing
geometry or substitutes fake inference.
Validated local Folder sources expose the existing Start/Step, Pause/End and
Continue/Step controls through normal startup. Pause retains the RUNNING session
and editing locks; End performs genuine Stop. Sources/compositions without this
capability keep generic Start/Stop. See
[local replay controls](docs/application.md#local-replay-controls) for cursor,
shutdown and manual verification.

Optional developer-only **read-only snapshots** use the same normal process:

```powershell
uv run --no-sync python app.py --web-snapshots --web-port 8765
Invoke-RestMethod http://127.0.0.1:8765/api/snapshot
Invoke-RestMethod http://127.0.0.1:8765/health
```

Open `http://127.0.0.1:8765/` for the responsive read-only observer. Disabled by
default, with `127.0.0.1` still the default host; no persistent settings or commands.
Start/Step
the existing Folder source in the application to obtain Production images. JSON
contains each camera's exact revision-qualified JPEG URL; request that URL rather
than guessing a camera/image identity. Publication is nominally every two seconds,
at most 640px per dimension, JPEG quality 70. Superseded URLs return unavailable.
See [snapshot publication](docs/ui-shell.md#read-only-snapshot-publication) for
identity, lifecycle, API and manual verification.

On a **trusted, isolated test LAN**, explicitly select an assigned local IPv4:

```powershell
uv run --no-sync python app.py --web-snapshots --web-host 192.168.10.100 --web-port 8765
```

Replace the illustrative address with this machine's actual LAN IP, then open
`http://<LAN-IP>:8765/` on the computer/phone. The observer polls about every two
seconds and reuses unchanged images. No login/HTTPS is provided; a specific bind
address is not a client-subnet restriction. Network isolation and Windows Firewall
must restrict access externally. The application does not configure either.

The five pages are **Production / 生产**, **Recipe / 配方**, **Communication / 通信**,
**History / 历史** and **Vision / 视觉**. Communication provides session-local manual
D/W reads/writes and optional register-watch refresh on configured endpoints. Manual
Communication and optional Production reporting independently support UDP/TCP.
Folder defaults to localhost/UDP; saved Basler reporting defaults to field TCP. Reporting enablement/settings are memory-only, initially
disabled and captured on Start; closing/relaunching disables reporting again. The
maintained localhost simulator CLI remains UDP-only; commissioned real-PLC
integration is unimplemented. See [communication](docs/communication.md).
History remains a placeholder.
Recipe and Vision use **Save and apply**: persistence, candidate preparation/
installation and successful-Start activation are distinct outcomes. Only successful
Start activates. Counters measure admitted cycles/channel judgments, not physical
capsules/products; only explicit Batch resets them. Workflow details belong to
[UI configuration](docs/ui-configuration.md) and [application](docs/application.md).

For explicit-path real review, these illustrative local files must exist and
validate; their names are not universal startup defaults:

```powershell
uv run --no-sync python scripts/run_local_production_ui.py --model-config data/models/model.json --machine-config data/machine/machine.json --recipe data/recipes/local.recipe.json --sources-config data/sources/camera_sources.json --language zh --size 1080x1920
```

This launcher uses saved Folder sources by default. Mutually exclusive
`--frames-dir images` or `--images <six paths>` select fixed images in configured
camera order for that process; they do not persist source changes or restore/write
manifest preferences. These developer options are not normal startup recovery.
See [entry-point details](docs/application.md#developer-entry-points) and
[Folder replay](docs/application.md#folder-replay).

The fake launcher remains fake with `--frames-dir images`; its default synthetic
inputs need no GPU/model/dataset. Use `--scenario ng`, `warning` or `fault` for
abnormal-state review. Launching may create ignored demo configuration. The
one-image diagnostic optionally accepts `--image <path>`; Start Worker, wait for
READY, then Run Inference. It does not produce Production cycles/counters. See
[UI review](docs/ui-shell.md#review) for both workflows and close behavior.

## Synchronous inference

Supply an existing local image filename. This CLI example owns one engine in one
serial context; GUI execution instead uses a worker engine factory.

```python
from pathlib import Path

import cv2
import numpy as np

from vimo_demo.inference import VimoSegmentationEngine

path = Path("images/example.png")  # Replace with an existing image.
image_bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
if image_bgr is None:
    raise ValueError(f"Cannot decode {path}")

with VimoSegmentationEngine("models/model_trt_fp16.vimosln") as engine:
    result = engine.infer(image_bgr)
    print(result.has_defects, result.label_map, result.regions_per_label)
    print(result.mask.shape)  # Owned, read-only uint8 (H, W); raw SDK mask IDs.
```

Input is nonempty uint8 H × W × 3 **BGR**, without resizing or orientation changes.
`label_map` maps labels to mask IDs; it is not region counts. `has_defects` means
returned regions exist, not production acceptance. The
[inference/runtime contract](docs/inference-runtime.md) owns input/result details,
model staging, borrowing, errors and cleanup. Default runtime cache is
`outputs/vimo_cache/<model-sha256>/`, relative to the working directory and separate
from the source model.

## Maintained engineering tools

Use each tool's `--help` for options. These are explicit engineering workflows,
not mandatory daily validation:

| Tool | Scope / output |
| --- | --- |
| [example_inference.py](scripts/example_inference.py) | One image through the typed API; semantic/region output in the terminal |
| [benchmark_inference.py](scripts/benchmark_inference.py) | Per-call serial wrapper latency, including normalization/copies; `outputs/benchmark/` |
| [benchmark_production.py](scripts/benchmark_production.py) | Fixed six-image configured Production cycles, including ingress copies/admission/result delivery; `outputs/inspection_performance/` |
| [stress_inference.py](scripts/stress_inference.py) | One engine, six calls per round, semantic/latency/resource observations; `outputs/stress/` |
| [visualize_inference.py](scripts/visualize_inference.py) | Raw-mask/contour/bbox overlays and label legend; `outputs/visualization/` |
| [run_fins_simulator.py](scripts/run_fins_simulator.py) / [fins_local_demo.py](scripts/fins_local_demo.py) | Standalone localhost FINS/UDP DM/WR-word simulator and D read/write/error demo; no Qt/GPU or Production integration |

Replace `images/example.png` with an existing image. Benchmark and stress commands
below intentionally execute GPU work; run them only for a scoped investigation.

```powershell
uv run --no-sync python scripts/example_inference.py models/model_trt_fp16.vimosln images/example.png
uv run --no-sync python scripts/benchmark_inference.py models/model_trt_fp16.vimosln images --pool-size 6 --warmup 20 --rounds 100
uv run --no-sync python scripts/stress_inference.py models/model_trt_fp16.vimosln images --rounds 5000 --pool-size 24 --sample-interval 100 --gpu-memory
uv run --no-sync python scripts/visualize_inference.py models/model_trt_fp16.vimosln images --limit 10
```

[Production benchmark invocation](docs/inspection-performance.md#workload-and-invocation)
documents its configuration arguments, distinct model-path base, A–F workloads
and timing limits. Wrapper, historical raw SDK and Production timings are different
measurement scopes. Neither benchmark proves hardware throughput; stress reports
make no automatic leak verdict. The visualizer uses existing Pillow/fonts for
Chinese where available, otherwise ASCII references with Chinese labels in JSON;
it installs nothing.

The [standalone FINS/TCP client](docs/communication.md#standalone-finstcp-client)
supports automatic node allocation and serial D/W words. Communication manual
operations/watches and optional reporting independently select UDP/TCP.
The developer launcher accepts `--fins-host` and `--fins-transport tcp`; see the
[localhost TCP procedure](docs/communication.md#tcp-localhost-verification).
The existing simulator CLI remains UDP-only. The
[FINS/UDP contract and two-terminal demo](docs/communication.md#local-simulator-and-manual-demonstration)
cover the independent communication subset. The
[first-station protocol](docs/communication.md#first-station-result-projection-and-submission)
adds explicit 36-channel projection and acknowledged W50–W52 / W49 writes. Optional
[local automatic reporting](docs/communication.md#local-production-reporting) is
enabled through normal `app.py`'s Communication page or explicitly by
`--report-first-station` in `scripts/run_local_production_ui.py`, with a bounded
serial owner and configurable numeric target. The
[normal two-terminal procedure](docs/communication.md#normal-application-manual-verification)
uses the existing simulator. Manual clients remain independent; new manual
W49–W52 writes on the same endpoint are excluded while its automatic owner is
alive; reads and independent endpoints remain available. Saved Basler can enable
[continuous field reporting](docs/communication.md#continuous-basler-reporting),
including attributable partial-cycle rejections. ACKs are protocol observations;
PLC consumption and physical commissioning remain unimplemented.

## Verification

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
uv run --no-sync pytest --tb=short
git diff --check
```

Strict typing covers maintained source, tests, scripts and `app.py`. Normal pytest
uses GPU-independent tests/headless Qt and skips real-GPU checks. Proportional
validation rules are in [AGENTS](AGENTS.md#typing-and-validation).

A short opt-in engine regression, requiring local reference assets and SDK/runtime:

```powershell
uv run --no-sync pytest tests/integration/test_reference.py::test_short_reference --run-gpu --require-gpu -s --tb=short
```

Without `--require-gpu`, missing prerequisites skip. Other programming/regression
errors still fail. [Inference verification](docs/inference-runtime.md#verification)
owns additional worker/coordinator and full-reference commands;
[application verification](docs/application.md#verification) covers local real
composition, and [UI verification](docs/ui-shell.md#verification-and-limits) covers
Qt wiring. Full reference assertions require the fixture's exact model SHA-256,
400-image filename set and image hashes. Full-dataset, stress and benchmark runs
are opt-in, not routine requirements for documentation or unrelated changes.

## Documentation

Current contracts and procedures:

| Document | Authority |
| --- | --- |
| [Architecture](docs/architecture.md) | Implemented scope, responsibility boundaries and unimplemented Target direction |
| [Configuration](docs/configuration.md) | Persisted definitions, pure resolution, artifact identity and path bases |
| [Inspection](docs/inspection.md) | Frame/cycle identity, collection, evidence, technical/product judgment and Size/Color |
| [Inference and runtime](docs/inference-runtime.md) | Typed API, serial ownership, bounded handoffs, Futures and shutdown |
| [Application](docs/application.md) | Persistent startup/recovery, activation, readiness, counters and local sources |
| [UI shell](docs/ui-shell.md) | Bilingual presentation, display identity, feedback, Qt delivery and closing |
| [UI configuration](docs/ui-configuration.md) | Recipe/Vision drafts, ROI/reference/tuning and Save and apply |
| [Inspection performance](docs/inspection-performance.md) | Measurement workloads, boundaries, reports and interpretation |
| [Communication](docs/communication.md) | Standalone FINS UDP/TCP subsets, socket ownership/errors, UDP localhost simulator and hardware limitations |

Frozen **Historical** archives, separate from Current contracts:

- [Validation evidence](docs/archive/validation-evidence.md): SDK/model/dataset,
  finite stability, reboot/visual observations and golden provenance.
- [Inspection performance records](docs/archive/inspection-performance-records.md):
  dated measurements, source/workload identities and qualifications.
- [Production UI exploration](docs/archive/production-ui-exploration.md): selected
  early rationale/prototype provenance, including superseded/unimplemented ideas.

Historical regressions/measurements do not establish current hardware acceptance.
The domain documents own Current contracts; the archives own dated evidence.
Git checkpoint `8fad326aaaf5e0e981224963607a7e000eb95604` preserves the original
documents and exploration narrative.

## License

Project source is licensed under the [MIT License](LICENSE), copyright 2026 Sesame.
Proprietary SmartMore SDK/runtime components, models and other protected local
assets are not included or licensed by this repository's MIT license; obtain any
required rights and authorization separately.
