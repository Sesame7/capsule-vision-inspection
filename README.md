# Capsule Vision Inspection

Windows desktop application for six-camera, 36-channel capsule inspection.
It combines passive Basler hardware-trigger acquisition or Folder Replay, one
serial ViMo engine, Occupancy/Size/Color checks, FINS UDP/TCP communication and
PLC result reporting. The Chinese/English Qt UI has Production, Recipe,
Communication and Vision workspaces; History is a placeholder. A read-only Web
observer starts by default.

## Setup

Use the existing vendor-provisioned Windows environment. Development uses Python
**3.14.7 x64**, ViMo inference/custom **3.20.0.2**, and CUDA 12/TensorRT with active
vendor authorization. Basler mode additionally requires externally installed
pylon/pypylon. GPU/driver compatibility and licenses must be provisioned separately;
the repository and uv do not supply them.

Public dependencies are locked in [pyproject.toml](pyproject.toml) and [uv.lock](uv.lock).
On a prepared machine, intentional public dependency setup uses:

```powershell
uv sync --locked --inexact --no-python-downloads
```

Keep `--inexact` to retain externally installed vendor packages. Do not copy a
`.venv` between machines or change an established GPU/runtime installation merely
to synchronize dependencies. Daily commands use `uv run --no-sync`.

Normal startup requires authorized local configuration and assets beside `app.py`:

```text
data/
  app.json                    # Explicit default Model / last-applied Recipe IDs
  machine/machine.json        # Saved camera/channel order and ROI geometry
  models/*.json               # Model definitions and their referenced artifacts
  recipes/*.recipe.json
  sources/camera_sources.json # Folder references and optional Basler serials
images/                       # Local Folder/reference images
models/                       # Optional independent engineering inputs
outputs/                      # Runtime caches and generated tool/test files
```

These assets are ignored by Git and are not supplied by a clone. Preserve saved
Recipes, mappings, ROIs, models, images and licenses. See
[configuration](docs/configuration.md) for schemas, relative paths and recovery.

## Run and use

```powershell
uv run --no-sync python app.py
```

The application opens stopped. Valid configuration produces READY; invalid
required data produces UNCONFIGURED with actionable feedback. It never chooses
the first available model/Recipe, invents ROIs or substitutes fake inference.

- **Vision:** select Folder Replay or Basler, assign all six serials explicitly,
  select reference folders and calibrate Machine ROIs. Save and apply before Start.
- **Recipe:** select the model and defect filtering; enable/tune Size and Color.
  Preview uses a frozen reference and never submits production cycles or counters.
  Save/preparation is separate from activation on the next successful Start.
- **Production:** Start performs readiness warm-up without production side effects.
  Folder Replay supports Pause/Continue/Step and End. Basler waits for actual
  ARMED, then receives external hardware triggers; Production never writes W10.7.
  Stop waits for safe cleanup and returns READY; close normally and verify exit.
- **Communication:** manual D/W reads/writes and register watches use independently
  selected UDP/TCP. Automatic W49–W52 reporting is opt-in, memory-only and captured
  at Start. Folder defaults to localhost/UDP; Basler defaults to field TCP.

Counters describe admitted cycles and channel judgment occurrences, not physical
capsules. Stop/Start or a Recipe change does not reset them; explicit Batch does.
Technical ERROR, product NG and application FAULT are distinct.

## Web observer

Open `http://127.0.0.1:8765/`. Default refresh is **500 ms**, coordinated between
publisher and browser. The service serves bounded latest snapshots/JPEGs and has
no controls, history, authentication or HTTPS. Optional startup failure is reported
without blocking Production. Diagnostics can disable it or select 1/2-second updates:

```powershell
uv run --no-sync python app.py --no-web-snapshots
uv run --no-sync python app.py --web-interval-ms 1000
```

`--web-snapshots` remains an explicit enable flag. `--web-port` changes the port.
For an isolated trusted LAN, `--web-host <assigned-local-IPv4>` selects one local
address; wildcards/hostnames are rejected. Restrict access through the network and
firewall; the application does not configure them. JPEG URLs returned by
`/api/snapshot` include exact publisher/revision identities; a superseded URL
returns 410. `/health` describes publication, not machine readiness.

## Developer tools and checks

| Tool | Purpose |
| --- | --- |
| [run_production_ui.py](scripts/run_production_ui.py) | Fake-engine UI review; no hardware/model required |
| [run_local_production_ui.py](scripts/run_local_production_ui.py) | Explicit configuration paths for real developer diagnosis; optional one-cycle Basler mode, not normal persistent startup |
| [visualize_inference.py](scripts/visualize_inference.py) | Typed inference masks/contours/boxes for a selected local model and images |
| [run_fins_simulator.py](scripts/run_fins_simulator.py) | Localhost UDP simulator, optionally tracing accepted W49 writes |

Use each tool's `--help`. The fake launcher remains fake even with local images.
Developer configuration generation never repairs normal saved MachineLayout.
The public inference API remains small:

```python
from vimo_demo.inference import VimoSegmentationEngine

with VimoSegmentationEngine(model_path) as engine:
    result = engine.infer(image_bgr)  # Nonempty uint8 H x W x 3 BGR ndarray
    print(result.has_defects, result.label_map, result.regions_per_label)
```

Normal development checks are GPU independent:

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
uv run --no-sync pytest --tb=short
git diff --check
```

Two optional real checks cover the typed SDK result and the real six-frame Qt
composition. They require the matching local model at
`data/models/model_trt_fp16.vimosln` and fingerprinted reference images under
`images/`; `tests/reference/phase0.json` is their tracked fixture, not application
configuration. Missing assets/runtime skip unless `--require-gpu` is supplied:

```powershell
uv run --no-sync pytest tests/integration/test_reference.py tests/integration/test_local_production_vimo.py --run-gpu --require-gpu --tb=short
```

## Troubleshooting

- **UNCONFIGURED / Start fails:** inspect the displayed detail, explicit IDs, file
  paths, model SHA, source availability and ROI bounds. Do not replace calibration
  with generated geometry.
- **Basler shows no images:** confirm saved Basler mode/serials, warm-up folders,
  actual ARMED and an external trigger. Do not open another camera owner. Very
  dark decoded frames are different from unavailable frames.
- **PLC reports fail:** verify transport, endpoint, node negotiation and reporting
  enablement before Start. ACK is not PLC scan consumption or physical rejection.
- **Web unavailable:** inspect startup stderr and port ownership. Stop/Start does
  not restart the process-owned observer; close releases its socket/threads.
- **Close waits:** a native call cannot be force-cancelled. A timeout is not proof
  of thread exit; preserve ownership and diagnose rather than terminating silently.

Current behavior and code navigation: [architecture](docs/architecture.md),
[configuration](docs/configuration.md), [communication](docs/communication.md).
Inspection integration is working; model accuracy, lighting/calibration and
mechanical reject commissioning are separate responsibilities. Git preserves
retired experiments; the working tree keeps current code and regression fixtures.

Project source uses the [MIT License](LICENSE), copyright 2026 Sesame. Proprietary
SDK/runtime components, models and local assets retain their own licensing and
authorization requirements; this repository does not distribute or license them.
