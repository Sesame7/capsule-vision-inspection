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
  communication.json          # Optional global reporting/diagnostic/Web preferences
  machine/machine.json        # Saved camera/channel order and ROI geometry
  models/*.json               # Model definitions and their referenced artifacts
  recipes/*.recipe.json
  sources/camera_sources.json # Folder inputs and optional Basler serials
images/                       # Local Folder images
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

The native Windows window opens maximized within the available work area, stopped.
The shared navigation offers dark/light themes (dark on launch) and a fixed-width
中文/EN selector. Theme selection is session-only. Valid configuration produces READY; invalid
required data produces UNCONFIGURED with actionable feedback. It never chooses
the first available model/Recipe, invents ROIs or substitutes fake inference.

- **Vision:** edit Folder inputs or explicitly assign/discover Basler serials on
  the six camera cards, calibrate Machine ROIs and Save/apply. Basler Connect all
  is explicit; disconnect before editing source identity. While inspection is
  stopped, Capture image waits up to ten seconds for the selected camera's next
  external-triggered frame. Import and eligible frozen Runtime images are also
  temporary canvas inputs; none creates a production result or an image library.
- **Recipe:** select the model and defect filtering; enable/tune Size and Color.
  Preview uses a frozen reference and never submits production cycles or counters.
  Save/preparation is separate from activation on the next successful Start.
- **Production:** Start performs readiness warm-up without production side effects.
  Folder Replay and Basler support Pause/Continue/Step and Stop, with separate
  source-boundary semantics. Basler requires an already connected/ARMED source and external hardware
  triggers; paused frames are discarded, and Step waits for one subsequent capture.
  These controls only affect inspection admission; Production never writes W10.7.
  Stop settles inspection and returns READY while Basler remains connected.
  Vision Disconnect or normal application Close releases cameras; verify actual exit.
- **Communication:** manual D/W reads/writes and register watches use independently
  selected UDP/TCP. Automatic W49–W52 reporting is opt-in, restored from saved
  preferences and captured at Start only after successful Save. Unsaved edits stay drafts. Folder defaults to localhost/UDP; Basler defaults to field TCP.

Counters describe admitted cycles and channel judgment occurrences, not physical
capsules. Stop/Start or a Recipe change does not reset them; explicit Batch does.
Technical ERROR, product NG and application FAULT are distinct.

## Web observer

Open `http://127.0.0.1:8765/`. Default refresh is **500 ms**, coordinated between
publisher and browser. The service serves bounded latest snapshots/JPEGs and has
no controls, history, authentication or HTTPS. Optional startup failure is reported
without blocking Production.

Use **Communication → Web observer** to edit bind IPv4, port and
500/1000/2000 ms refresh. **Save settings** persists all three Communication
sections in `data/communication.json`. The header power action saves only Web
enablement and turns it on/off using saved network settings, leaving drafts intact.
Apply changes appears for differing saved/running settings; save/discard Web
network edits first. Save alone does not restart Web. Saved enablement is restored on the next launch.
The page lists local IPv4s and shows the actual running URL with Open Browser.
A trusted-LAN bind must be
one assigned local IPv4; wildcard/DNS/broadcast bindings remain unsupported.
Normal `app.py` accepts only `--help`, with no operator Web flags. Developer
launchers retain their own explicit options. Access/firewall configuration remains
external. JPEG URLs from `/api/snapshot` include publisher/revision identities;
superseded URLs return 410. `/health` describes publication, not machine readiness.

## Developer tools and checks

| Tool | Purpose |
| --- | --- |
| [run_production_ui.py](scripts/run_production_ui.py) | Fake-engine UI review; no hardware/model required |
| [run_local_production_ui.py](scripts/run_local_production_ui.py) | Explicit configuration paths for real developer diagnosis; Folder/fixed inputs or saved Basler mappings with explicit Vision connection |
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
- **Basler shows no images:** confirm saved Basler mode/serials, Vision Connect all,
  actual ARMED, then Start inspection before external triggering. Do not open another camera owner. Very
  dark decoded frames are different from unavailable frames.
- **PLC reports fail:** verify transport, endpoint, node negotiation and reporting
  enablement before Start. ACK is not PLC scan consumption or physical rejection.
- **Web unavailable:** inspect Communication's service tooltip and port ownership.
  Save reviewed settings, then turn Web on or Apply changes there. Production
  Stop/Start leaves Web independent; Close waits for its socket/threads.
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
