# AGENTS.md

## Operating safeguards

Read [architecture](docs/architecture.md) and the owning Current domain contract
before changing behavior. Work within the requested scope; keep changes small,
readable and independently testable. Do not introduce speculative frameworks,
future camera/PLC/web functionality or architecture redesign without authorization.
Do not commit or tag automatically.

- Preserve the verified Windows 10, Python **3.14.7 x64**, RTX 4060 Ti 16 GB and
  ViMo inference/custom **3.20.0.2** environment. Public dependency versions are
  authoritative in `pyproject.toml` / `uv.lock`.
- Preserve the vendor-provisioned `.venv`. Use `uv run --no-sync` daily; intentional
  public synchronization uses `uv sync --locked --inexact --no-python-downloads`
  to retain proprietary packages. uv does not provision ViMo/runtime/license.
- Do not install/upgrade/remove GPU dependencies, CUDA, cuDNN, TensorRT, vendor
  packages/runtime, change global environment/PATH or modify vendor binaries/model
  settings without explicit authorization. Keep dependency changes within approved
  scope; additions need a demonstrated reason.
- Protect persistent `data/`, local models/images, `tests/reference/phase0.json`,
  manual configurations, accepted reports/profiling evidence and local prototypes.
  Ignored/generated status does not authorize deletion, movement, regeneration or
  overwriting. Do not commit local assets, caches or generated evidence. Historical
  raw reports are not required regression inputs; never regenerate the golden
  fixture merely to make a changed implementation pass.

## Protected Current behavior

These safeguards bind implementation changes until explicitly superseded through
an approved design change, matching implementation/tests and an updated owning
contract. Current package/class/thread arrangements describe today's implementation,
not permanent Target architecture restrictions.

- Preserve the responsibility boundaries in [architecture](docs/architecture.md):
  Configuration persists/resolves definitions; Inspection owns synchronous identity,
  collection/evidence/judgment; Runtime owns scheduling/resources; Application owns
  activation/sessions/accounting; UI presents/commands those domains. Lower domains
  remain Qt independent. Direct SmartMore imports/vendor objects stay only in the
  inference adapter; callers and maintained tools use our typed API, not old spikes.
- Input is nonempty **uint8 H × W × 3 BGR ndarray**. Do not infer RGB, resize or
  change orientation. Preserve verified thresholds/ROI/settings and raw mask IDs/
  actual mappings. Only the final singleton mask channel may be squeezed;
  `label_map` is not counts and `has_defects` is not production acceptance.
- Keep one serial engine owner. In worker/GUI execution, supply an engine factory:
  `InferenceWorker` creates, uses and closes it on its thread. Workers have one
  lifecycle and cannot restart. CLI tools may own an engine in one serial context.
  Do not claim SDK thread safety or select GPU concurrency without measurement.
- Bound image queues and result retention; document pixel ownership. Every alias
  of a worker-borrowed array stays immutable until its task Future completes/cancels.
  Owned Frames remain immutable while snapshots/inspection retain them; frame
  admission acknowledgement is neither terminal completion nor pixel-buffer return.
- `RuntimeCoordinator` exclusively owns its collector and worker lifecycle;
  source threads use its bounded command API. Preserve explicit cycle/frame/camera
  identities, configured ordering, distinct admission/task/terminal observations,
  exactly-once terminal delivery and explicit overload, without silent loss/history.
  Technical failure and product judgment remain separate; ordinary check ERROR is
  not automatically application FAULT or model NG.
- The GUI never performs ViMo inference or blocks on unfinished `Future.result()`
  / `join()`. Future callbacks never mutate widgets; use queued Qt delivery.
  Result delivery/accounting must not depend on rendering. Separate bounded
  latest-per-camera display pixels require matching generation, cycle, full FrameId
  and dimensions for overlays. Keep artifact hashing off GUI dispatch.
- `ProductionApplication` owns candidate activation and disposable sessions.
  Readiness warm-up produces no production data/counters. Only successful Start
  promotes a candidate; failed activation preserves last-known-good active identity.
  Counters are admitted-cycle/channel-judgment occurrences, not physical products;
  only explicit Batch resets them. UI lifecycle commands go through the application.
- Preserve strict configuration resolution and atomic persistence. The operator's
  combined **Save and apply** still separates persistence, clean saved candidate
  preparation/installation and successful-Start activation. Preserve hidden Recipe
  policies and partial-success feedback. Saved MachineLayout is the geometry
  authority; opening/refreshing an editor never repairs or reinitializes it.
- Normal `app.py` restores entry-relative persistent `data/` with explicit default
  Model/last-applied Recipe IDs into a stopped candidate. No first-file selection,
  fake fallback, invented geometry or automatic inspection. Developer/fake launchers
  are separate workflows, not normal recovery paths.
- Shutdown remains observable: stop admission, finalize/reject pending work
  explicitly, let claimed native work finish and confirm actual owned-thread
  termination before release. Timeout/notification is not successful termination;
  retain unsafe ownership. Native SDK calls cannot be forcibly cancelled.

## Typing and validation

Keep strict Pyright for `src tests scripts app.py`. Do not use broad `Any`, global
ignores or exclusions to silence diagnostics. Narrow at runtime; unavoidable vendor
typing workarounds must be local, documented and justified at the adapter boundary.

For each implementation step, report what changed, the smallest relevant test/
check, its exact command and any failure. Run normal gates for source/configuration
changes, with additional focused checks proportional to the affected contract:

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
uv run --no-sync pytest --tb=short
git diff --check
```

Normal tests remain GPU independent; real checks are opt-in. Use relevant short
GPU checks for inference/lifecycle changes. Do not routinely rerun historical
full-dataset, stress or benchmark experiments. For Markdown-only changes, verify
links/anchors/paths, commands against current parsers/configuration, Current versus
Historical wording, structure and whitespace (including untracked files);
runtime tests, GPU runs and screenshots are unnecessary.

## Documentation ownership

[README](README.md) owns setup, entry points and tool discovery.
[Architecture](docs/architecture.md) maps Current responsibilities and deferred
Target direction. Detailed contracts belong to [configuration](docs/configuration.md),
[inspection](docs/inspection.md), [inference/runtime](docs/inference-runtime.md),
[application](docs/application.md), [UI shell](docs/ui-shell.md),
[UI configuration](docs/ui-configuration.md) and
[performance methodology](docs/inspection-performance.md).

Use the owning document rather than duplicating contracts. Keep Current, Target/
unimplemented and Historical claims distinct. The three archives linked from
[README](README.md#documentation) preserve dated evidence, not new requirements.
Git preserves retired implementation; do not restore obsolete experiments.
Temporary migration/audit reports are evidence, not maintained specifications.
