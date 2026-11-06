# Capsule Vision Inspection

Capsule Vision Inspection is first-party industrial software for Windows that
inspects capsules through 30-channel or 36-channel inspection pipelines.
It combines camera acquisition, ViMo inference, configurable production rules,
recipe management and PLC-facing result reporting in a multilingual desktop
application.

## Main capabilities

- Five-camera/30-channel or six-camera/36-channel inspection layouts
- Passive Basler acquisition and Folder Replay input
- ViMo segmentation, mandatory Occupancy, and configurable Size (Length) and Color
  checks
- Recipe, Production, Vision, Communication and History workspaces
- Omron FINS UDP/TCP and Modbus TCP result reporting
- Multilingual Qt interface and an optional read-only Web observer

Capsule Vision Inspection is an inspection application, not a complete sorting
machine controller. Mechanical reject hardware, production lighting and model
accuracy remain separate commissioning responsibilities.

## Application workspaces

| Workspace | Purpose |
| --- | --- |
| Production | Select and prepare saved Recipes, run inspection and review results |
| Recipe | Edit and save Recipe definitions, models and inspection rules |
| Vision | Configure sources, camera layouts and channel ROIs |
| Communication | Configure PLC diagnostics, reporting, read-only alarm monitoring and the Web observer |
| History | All roles can browse inspection records. Operators and higher roles can review Alarm History and export named Batches to Excel. Engineers and Administrators can review the local Audit Trail. |

## Quick Start

### Development environment

The project targets Windows Python 3.14 x64. From PowerShell:

```powershell
git clone https://github.com/Sesame7/capsule-vision-inspection.git
cd capsule-vision-inspection
uv python install 3.14
uv sync --locked --inexact --no-python-downloads
```

Public Python dependencies are resolved through `uv.lock`. Daily commands use
the existing environment without resynchronizing dependencies:

```powershell
uv run --no-sync python app.py
uv run --no-sync capsule-vision
```

To run the equipment-free Demo:

```powershell
uv run --no-sync python scripts/run_demo.py
```

### Launch paths and local assets

`python app.py` resolves application data relative to the directory containing
`app.py`. The installed `capsule-vision` command resolves application data
relative to its current working directory.

Set the working directory correctly in Windows shortcuts. In particular, a
shortcut for the installed command must start from the intended configuration
root.

For a troubleshooting session, run `uv run --no-sync python app.py --diagnostics standard`.
Use `--diagnostics detailed` for frame and PLC stage timings. The same arguments
can follow the script or installed command in a Windows shortcut Target.
Omitting the option leaves logging off. See [runtime diagnostics](docs/architecture.md#runtime-diagnostics)
for file retention and how to trace a Run.

Normal operation requires local configuration, Recipes, MachineLayout, camera
sources, models and proprietary runtime assets. These items are not supplied by
the repository and must be provisioned separately. Managed models and related
definitions belong under `data/`; engineering inputs may also use the repository
`models/` directory.

Normal startup offers first Administrator setup when account storage is uninitialized.
For the offline Windows recovery tool and CLI fallback, see [Local accounts](docs/configuration.md#local-accounts).
See [configuration](docs/configuration.md) for data layout and path contracts.

## Runtime and vendor prerequisites

Development setup does not install proprietary hardware or inference components.
Production and native equipment checks can require:

- ViMo inference/custom runtime version 3.20.0.2
- Compatible CUDA and TensorRT components
- Basler pylon runtime and pypylon support
- Vendor authorization, licensed SDK files and models
- Supported Basler/Folder Replay sources and PLC endpoints

Do not copy `.venv` directories or synchronize vendor installations without a
controlled deployment process. Proprietary SDK/runtime components, models,
licenses and local images retain their own licensing and authorization.

## Project structure

| Path | Purpose |
| --- | --- |
| `app.py` | Normal Windows application entry point |
| `src/capsule_vision/` | Application, UI, runtime and inspection packages |
| `scripts/` | Engineering and equipment-free diagnostic tools |
| `tests/` | Unit and integration coverage |
| `docs/` | Current operational contracts and target product requirements |
| `data/` | Local configuration and generated application data |
| `models/` | Optional independent engineering model inputs |
| `outputs/` | Optional tool diagnostics and generated artifacts |

## Documentation

The capabilities above and the following operational contracts describe the
current application:

- [Architecture](docs/architecture.md) - runtime ownership and system composition
- [Configuration](docs/configuration.md) - files, paths and local configuration
- [Communication](docs/communication.md) - FINS/Modbus diagnostics and reporting
- [Field UI validation](docs/field-ui-validation.md) - Windows launch and UI smoke checks

[Product scope](docs/product-scope.md) records the reviewed PRODUCT-0 target and
open decisions, not a list of already implemented capabilities. An explicitly
authorized implementation Phase defines only its scoped change to current behavior;
target requirements do not authorize adjacent work. Follow
[AGENTS.md](AGENTS.md#documentation-authority), preserve current contracts outside
that scope, and update affected operational documentation after validation.

## Development and validation

Use `--help` for maintained entry points:

```powershell
uv run --no-sync python app.py --help
uv run --no-sync capsule-vision --help
```

For a UI review on a **development checkout**, close `app.py` and run these
commands from its repository root:

```powershell
uv run --no-sync python scripts/populate_development_data.py accounts
uv run --no-sync python scripts/populate_development_data.py populate
uv run --no-sync python app.py
```

`accounts` prompts locally for masked credentials. On an empty account store it
sets up `DEV-Admin`. On an existing store, sign in with an enabled Administrator.
It creates `DEV-Engineer` and `DEV-Operator` with temporary passwords that must
be changed at first login. Existing accounts keep their credentials.

`populate` records twelve fictitious commissioning shifts against the saved
Recipe, verified model and channel layout in this checkout's `data/`. The first
import selects `DEV-LOT-F`. Re-running keeps the original dates and any later
Batch selection. Conflicting sample identities or incompatible History schemas
stop the import. In History, choose **All dates** to page through the examples;
use Administrator to review both Audit and Alarm History. `DEV-LOT-*` Batches
and `DEV-Example-*` audit actors identify the samples. The usual History
retention period applies.

The normal repository quality gates are:

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
$pytestBase = Join-Path "outputs/pytest" ([guid]::NewGuid().ToString("N"))
uv run --no-sync pytest --tb=short --basetemp $pytestBase
git diff --check
```

Focused checks are appropriate for ordinary documentation changes. Native Qt
screenshots, real-model startup and camera/PLC acceptance remain separate,
manual validation.

## License

This project is licensed under the [MIT License](LICENSE), copyright 2026
Sesame. Proprietary SDK/runtime components, models and local assets retain
their own licenses and are not distributed by this repository.
