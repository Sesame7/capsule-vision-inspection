# Capsule Vision Inspection

Windows desktop application for five-camera/30-channel or six-camera/36-channel capsule inspection.
It combines passive Basler hardware-trigger acquisition or Folder Replay, one
serial ViMo engine, Occupancy/Size/Color checks, FINS UDP/TCP and Modbus TCP communication and
PLC result reporting. The multilingual Qt UI has Production, Recipe,
Communication, History and Vision workspaces. A read-only Web
observer is optional and starts automatically only when saved enabled.

## Setup

The Python distribution is `capsule-vision-inspection`; its import namespace is
`capsule_vision`.

Use Python **3.14.x x64** for development. Public packages are declared by
compatible ranges and resolved reproducibly by `uv.lock`. ViMo inference/custom
**3.20.0.2**, CUDA 12/TensorRT, Basler pylon/pypylon and vendor authorization are
separate prerequisites, not project dependencies or fresh-clone requirements.

For a fresh clone, create a disposable environment and install the locked public
dependencies:

```powershell
git clone https://github.com/Sesame7/capsule-vision-inspection.git
cd capsule-vision-inspection
uv python install 3.14
uv sync --locked --inexact --no-python-downloads
```

Do not rely on an existing editable installation. Recreate the environment after
dependency or Python policy changes. Intended public upgrades are explicit:

```powershell
uv lock --upgrade
uv sync --locked --inexact --no-python-downloads
```

Keep `--inexact` to retain externally installed vendor packages. Do not copy a
`.venv` between machines or change an established GPU/runtime installation merely
to synchronize dependencies. Daily commands use `uv run --no-sync`.

Production readiness requires local configuration and assets beside `app.py`:

```text
data/
  app.json                    # Optional last-applied Recipe ID and language
  communication.json          # Optional global reporting/diagnostic/Web preferences
  history.sqlite3              # Generated compact Production history (SQLite)
  machine.tsv                 # 30/36-channel ROI table; canonical camera/slot mapping
  models/<model_id>/model.json # Display name, SHA-256 and module
  models/<model_id>/model.vimosln
  recipes/<recipe_id>.json     # Stable filename identity; editable display name
  camera_sources.json         # Camera-keyed Folder inputs and Basler serials
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
uv run --no-sync capsule-vision
```

The native Windows window opens maximized within the available work area, stopped.
The shared navigation offers dark/light themes (dark on launch) and a native-name
language selector. Theme selection is session-only. Valid configuration produces READY; invalid
required data produces UNCONFIGURED with actionable feedback. It never chooses
the first available model/Recipe, repairs saved ROIs or substitutes fake inference.

- **Vision:** an empty installation offers six unsaved source cards. New camera
  ROIs initialize once from actual image/dimension evidence, never guessed sizes.
  Edit Folder inputs or explicitly assign/discover Basler serials, calibrate ROIs,
  and Save independently of Recipe/Model readiness. Complete Machine/source setup
  enables Recipe Prepare/Apply in the same process. CAM6 can
  be explicitly removed while stopped/disconnected; its + action initializes an
  editable CAM5 ROI template on an existing saved machine, with fresh CAM6/CH31–CH36
  identities and empty sources. First-run CAM6 waits for its own dimensions.
  Calibrate it against CAM6's actual image and assign its source before use. Basler Connect all
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
- **Communication:** manual D/W reads/writes and register watches use FINS UDP/TCP;
  Modbus TCP supports zero-based Holding Registers. Automatic first-station
  reporting is opt-in (FINS W49–W52 by default, or an explicit Modbus HR base), restored from saved
  preferences and captured at Start only after successful Save. Unsaved edits stay
  drafts. Missing communication settings leave reporting and Web disabled; no PLC
  endpoint is inferred from Folder or Basler mode.
- **History:** browse terminal Folder/Basler Production results by local date,
  Batch segment and result, newest first in pages of up to 50 cycles. Select a
  cycle for its camera/channel states and abnormal reason codes. NG, technical
  ERROR and channel EMPTY remain independent observations. Refresh reloads the
  list; browsing never changes current counters. Records use host completion
  observations and are retained for 30 days. Persistence is bounded and best-effort;
  visible warnings identify unconfirmed records or storage failures.

Counters describe admitted cycles and channel judgment occurrences, not physical
capsules. Stop/Start or a Recipe change does not reset them; explicit Batch does.
Technical ERROR, product NG and application FAULT are distinct.

## Web observer

After explicitly enabling Web, open its actual URL (initial editing address:
`http://127.0.0.1:8765/`). Default refresh is **500 ms**, coordinated between
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
Normal `app.py` accepts only `--help`, with no operator Web flags. Access/firewall
configuration remains external. JPEG URLs from `/api/snapshot` bind publisher and
immutable camera-image identities, remaining stable across metadata-only revisions.
Each camera permits current plus one previous compatible JPEG; expired or
invalidated URLs return 410. `/health` describes
publication, not machine readiness.

## Developer tools and checks

For workstation access, native desktop evidence and normal application smoke tests,
see [field UI validation](docs/field-ui-validation.md).

| Tool | Purpose |
| --- | --- |
| [run_demo.py](scripts/run_demo.py) | Fake-engine UI review; no hardware/model required |
| [visualize_inference.py](scripts/visualize_inference.py) | Typed inference masks/contours/boxes for a selected local model and images |
| [run_fins_simulator.py](scripts/run_fins_simulator.py) | Localhost UDP simulator, optionally tracing accepted W49 writes |

Use each tool's `--help`. The fake launcher remains fake even with local images.
Developer configuration generation never repairs normal saved MachineLayout.
The demo seeds missing fixtures under `outputs/production_ui_demo` and uses the
same explicit fixed-image Application composition and generation-owned source as
engineering runs. Inference, label metadata and model preparation are explicitly
fake; its window title identifies simulated input. Existing geometry and Recipe
edits are retained, and original selected images are never overwritten.
The public inference API remains small:

```python
from capsule_vision.inference import VimoSegmentationEngine

with VimoSegmentationEngine(model_path) as engine:
    result = engine.infer(image_bgr)  # Nonempty uint8 H x W x 3 BGR ndarray
    print(result.has_defects, result.label_map, result.regions_per_label)
```

For a fast, optional regression while editing, run this CPU/fake-engine selection:

```powershell
$pytestBase = Join-Path "outputs/pytest" ([guid]::NewGuid().ToString("N"))
uv run --no-sync pytest --tb=short --basetemp $pytestBase `
  tests/unit/test_inference_contracts.py tests/unit/test_configuration.py `
  tests/unit/test_inspection_cycle.py tests/unit/test_inference_worker.py `
  tests/unit/test_runtime_coordinator.py tests/unit/test_production_application.py `
  tests/unit/test_production_inspection.py tests/integration/test_global_shell.py `
  tests/integration/test_recipe_ui.py::test_compact_recipe_layout `
  tests/integration/test_production_ui.py::test_nonblocking_start_polling_commands_and_deferred_close `
  tests/integration/test_communication_ui.py::test_settings_and_disclosure_do_not_dispatch_or_change_active_watch `
  tests/integration/test_web_production.py::test_saved_web_interval_and_publication_metadata
```

This covers typed inputs, configuration, cycles, worker/coordinator ownership,
judgment, application accounting and representative Qt interactions. The Web
interval cases use a real constructor/publisher with controlled startup, without
a listener. This selection excludes OS-process and real-server lifecycle tests
and is **not an acceptance substitute**. The full GPU-independent quality gate
retains those checks:

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
$pytestBase = Join-Path "outputs/pytest" ([guid]::NewGuid().ToString("N"))
uv run --no-sync pytest --tb=short --basetemp $pytestBase
git diff --check
```

Ordinary edits use only the focused Ruff, Pyright and pytest subset that covers
the changed domain. Substantial cross-domain or release changes run the complete
gates above. Native Qt screenshots and real-model/vendor/hardware evidence remain
manual and are not implied by automated tests.

Real-model startup, inference correctness and business accuracy are accepted
manually with representative, known OK/NG inputs. Synthetic regression tests do
not establish native SDK accuracy. The serial visualization tool supports a
bounded real-model smoke check without camera or PLC operations:

```powershell
uv run --no-sync python scripts/visualize_inference.py <model.vimosln> <images-directory> --limit 8
```

Review exported labels, masks and known expectations separately from loading
success. Do not change model thresholds, calibrated ROIs or expected judgments
to make a smoke check pass.

Package verification uses the built wheel and a disposable environment:

```powershell
uv build --wheel
$wheel = (Resolve-Path dist/*.whl | Select-Object -First 1).Path
$clean = Join-Path $env:TEMP "capsule-vision-wheel-test"
uv venv $clean --python 3.14
uv pip install --python "$clean\Scripts\python.exe" $wheel
& "$clean\Scripts\capsule-vision.exe" --help
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
- **Web unavailable:** inspect Communication's Web status, settings feedback and port ownership.
  Save reviewed settings, then turn Web on or Apply changes there. Production
  Stop/Start leaves Web independent; Close waits for its socket/threads.
- **Close waits:** a native call cannot be force-cancelled. A timeout is not proof
  of thread exit; preserve ownership and diagnose rather than terminating silently.

Current behavior and code navigation: [architecture](docs/architecture.md),
[configuration](docs/configuration.md), [communication](docs/communication.md).
Inspection integration is working; model accuracy, lighting/calibration and
mechanical reject commissioning are separate responsibilities. Git preserves
current code and regression fixtures only; retired experiments have no runtime
or test compatibility requirement.

Project source uses the [MIT License](LICENSE), copyright 2026 Sesame. Proprietary
SDK/runtime components, models and local assets retain their own licensing and
authorization requirements; this repository does not distribute or license them.
