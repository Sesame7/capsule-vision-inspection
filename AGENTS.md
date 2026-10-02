# AGENTS.md

## Project and workflow

Local industrial-vision integration: typed ViMo inference, a Python worker and
a minimal PySide6 demo. The demo is not the final production UI. Read
[architecture](docs/architecture.md) for implemented boundaries and future direction.
The intended inspection cycle contains six images; the current worker accepts
one image per task and executes serially.

- Target Windows 10, Python **3.14.7**, RTX 4060 Ti 16 GB and ViMo **3.20.0.2**.
  Public dependency versions live in `pyproject.toml` / `uv.lock`.
- Use uv and preserve the verified `.venv`, including externally provisioned ViMo
  packages. Use `uv run --no-sync` daily; when syncing is needed, use
  `uv sync --locked --inexact --no-python-downloads` to retain vendor packages.
- Do not install/upgrade GPU dependencies, CUDA, cuDNN, TensorRT or vendor runtime,
  or modify vendor binaries/model settings, without explicit authorization.
- Keep changes small and independently testable. Prefer readable code over
  speculative abstractions. Add dependencies only when clearly necessary.
- Do not commit or tag automatically unless explicitly asked. Stay within the
  requested scope; do not implement future camera/PLC/web features prematurely.

## Architecture and ownership invariants

- Dependency direction is **ui -> runtime -> inference**, never runtime -> ui
  or inference -> runtime/ui. Lower-layer result types and engine factories may
  be used by higher-layer composition code. Runtime and inference stay Qt independent.
- Keep direct SmartMore SDK imports and vendor objects in the inference adapter.
  Callers use `VimoSegmentationEngine` and our typed results. Production code and
  maintained tools must not depend on historical spike implementations.
- Input is a nonempty **uint8 H x W x 3 BGR ndarray**. Do not silently reinterpret
  RGB, resize or change orientation. Preserve raw mask IDs and verified parameters.
  `has_defects` means returned regions exist, not production acceptance.
- In worker/GUI execution, pass an engine factory: the owning `InferenceWorker`
  thread creates, uses and closes the engine. One worker has one lifecycle and
  cannot restart. CLI tools may own an engine directly in one serial context.
- Queues carrying full images must be bounded. Make ownership explicit; every
  alias of a submitted array stays immutable until its Future completes/cancels.
  Do not claim SDK thread safety or choose GPU concurrency without measurement.
- The GUI thread never performs ViMo inference or waits for unfinished backend
  work through blocking `Future.result()` / `join()`. Future callbacks must not
  mutate widgets; marshal notifications through the Qt bridge.
- Qt is a thin notification/presentation layer. Inspection result delivery must
  not depend on UI rendering. Preserve the same separation for future consumers.
- A RuntimeCoordinator exclusively owns its collector and worker lifecycle;
  source threads use its bounded command API. Frame admission acknowledgements
  are not inspection completion or pixel-buffer return notifications.
- ProductionApplication owns candidate activation and disposable runtime sessions.
  Warm-up is readiness work, never production data. Counters measure admitted
  cycles/channel judgments, not physical products; only explicit Batch resets them.
- Shutdown remains observable: stop admission, allow active native work to finish,
  and confirm actual thread termination before releasing ownership. Timeout must
  not pretend success. Native SDK calls cannot be forcibly cancelled.

## Repository responsibilities

- `src/vimo_demo/inference/`: synchronous typed engine and SDK boundary.
- `src/vimo_demo/runtime/`: Qt-independent worker and bounded cycle coordination.
- `src/vimo_demo/inspection/`: single-owner cycle collection/results; depends on
  inference models, never UI or vendor APIs. Keep cycle logic out of Qt bridges.
- `src/vimo_demo/inspection/production/`: channel layouts, evidence assignment and
  independent technical/product judgments; no runtime scheduling.
- `src/vimo_demo/configuration/`: Recipe/model/layout persistence, validation and
  pure resolution into existing inspection types; no activation or active state.
- `src/vimo_demo/application/`: Qt-independent activation, lifecycle, snapshots,
  factual counters and resolved-check composition using the existing runtime.
- `src/vimo_demo/ui/`: Qt bridge and integration demo.
- `tests/`: unit/integration tests and tracked golden `tests/reference/phase0.json`.
- `scripts/`: maintained engineering tools using the public inference API.
- `models/`, `images/`: ignored local inputs; `outputs/`: ignored generated data.
  Never require historical generated reports for regression or commit local assets.
- `docs/`: contracts, rationale and historical evidence. Git archives old experiments;
  preserve the golden fixture and verified findings rather than restoring spike code.

## Typing and validation

Keep strict Pyright for `src tests scripts`. Do not use broad `Any`, global ignores
or exclusions to silence diagnostics. Narrow at runtime; any necessary vendor
typing workaround must be local, documented and justified.

```powershell
uv run --no-sync ruff check src tests scripts
uv run --no-sync ruff format --check src tests scripts
uv run --no-sync pyright src tests scripts
uv run --no-sync pytest --tb=short
git diff --check
```

For each implementation step, state what changed, run the smallest relevant test,
give the exact command and report failures. Run the normal gates for source/config
changes. Normal tests must remain GPU independent; GPU regressions are opt-in.
Use relevant short GPU checks for inference/lifecycle changes; do not routinely
rerun historical full-dataset, stress or benchmark experiments. For Markdown-only
changes, check links/paths, commands against configuration, stale wording and diff
whitespace; runtime testing is unnecessary. Do not proceed into unrelated phases.
