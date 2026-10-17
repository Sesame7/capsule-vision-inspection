# AGENTS.md

Read [README](README.md), [architecture](docs/architecture.md) and the relevant
[configuration](docs/configuration.md) or [communication](docs/communication.md)
contract before changing behavior. Keep changes small and testable. Do not commit,
tag, push, add features or redesign architecture without authorization.

## Environment and assets

- Preserve the vendor-provisioned `.venv`, Windows/Python/vendor environment and
  locked public dependency versions. Development targets Python 3.14.7 x64 and
  ViMo inference/custom 3.20.0.2. Preserve an existing field installation as found.
  Daily commands use `uv run --no-sync`; intentional public synchronization uses
  `uv sync --locked --inexact --no-python-downloads`.
- Do not install/change GPU dependencies, CUDA/TensorRT, vendor packages/runtime,
  licenses, global PATH or model parameters without explicit authorization.
- Protect `data/`, local models/images, licenses and `tests/reference/phase0.json`.
  Ignored/generated status alone does not authorize deletion. Verify actual
  references and the approved scope before cleaning outputs or local assets.
  Never regenerate the golden fixture merely to make a regression pass.

## Implementation rules

- Configuration persists/resolves definitions; Inspection owns synchronous
  identity/evidence/judgment; Runtime owns scheduling/resources; Application owns
  activation/sessions/accounting; Qt/Web presents those domains. Lower domains
  remain Qt independent. Keep direct SmartMore imports/vendor objects at the
  inference adapter; callers use our typed API.
- Input is nonempty uint8 H x W x 3 **BGR**. Do not infer RGB, resize, reorient,
  alter thresholds/ROI or invent mask IDs. Only a final singleton mask channel
  may be squeezed. `label_map` is not region counts; `has_defects` is not acceptance.
- One serial engine owner constructs, uses and closes the SDK on its worker
  thread. No unverified thread-safety/concurrency claims. Bound queues/results;
  borrowed array aliases stay immutable until task completion/cancellation.
  Frame admission does not return pixel ownership or mean terminal completion.
- Preserve explicit cycle/camera/full FrameId and configured ordering, exactly-once
  terminal delivery, observable overload and actual thread termination. Timeout
  is not successful shutdown; native work cannot be forcibly cancelled.
- Production is passive to external hardware triggers and never commands W10.7.
  Preserve early serial inspection, channel identity and W49–W52 reporting.
  Result/accounting delivery must not depend on UI/Web speed. Display overlays
  require matching generation, cycle, complete FrameId and image dimensions.
- Qt never infers or waits on unfinished `Future.result()`/`join()`. Worker
  callbacks never mutate widgets; use queued delivery. Bound latest display/Web
  caches and keep artifact hashing outside GUI dispatch.
- Start warm-up has zero production side effects. Only successful Start promotes
  a candidate; failed startup preserves active identity. Counters are occurrences,
  not physical products; explicit Batch resets them. ERROR, NG and FAULT differ.
- Preserve strict configuration validation, atomic persistence, hidden Recipe
  settings and Save/preparation/activation separation. Saved MachineLayout is
  authoritative; opening an editor never repairs or reinitializes geometry.
  Normal `app.py` uses explicit entry-relative `data/`, with no fake fallback.

## Validation

Keep strict Pyright for maintained source/tests/scripts. No broad `Any`, global
ignores or weakened diagnostics. Vendor typing workarounds must be local and
justified at the adapter boundary. During UI iteration, use actual Qt screenshots,
focused interaction tests and checks relevant to changed files. Review screenshots
and fix visual issues before final comprehensive validation; do not rerun the full
suite for each styling adjustment. At phase completion, run the normal gates once:

```powershell
uv run --no-sync ruff check src tests scripts app.py
uv run --no-sync ruff format --check src tests scripts app.py
uv run --no-sync pyright src tests scripts app.py
uv run --no-sync pytest --tb=short
git diff --check
```

Report commands/results and unexecuted checks honestly. Real GPU/hardware checks
are opt-in and proportional to changed behavior; do not routinely benchmark,
stress or run full datasets. For documentation-only changes, check links/anchors,
commands and whitespace; runtime tests/screenshots are unnecessary. Documentation
describes current operation; avoid phase narratives or duplicate contract trees.
