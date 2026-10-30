# AGENTS.md

Read [README](README.md), [architecture](docs/architecture.md) and the relevant
[configuration](docs/configuration.md) or [communication](docs/communication.md)
contract before changing behavior. Keep changes small and testable. Do not commit,
tag, push, add features or redesign architecture without authorization.

For field UI smoke checks, follow [field UI validation](docs/field-ui-validation.md)
for interactive-desktop evidence, normal launch/Close and protected-asset checks.

## Environment and assets

- Create and use disposable Windows environments from the lock file. Development
  targets Python 3.14.x x64 and
  ViMo inference/custom 3.20.0.2. Preserve an existing field installation as found.
  Daily commands use `uv run --no-sync`; intentional public synchronization uses
  `uv sync --locked --inexact --no-python-downloads`.
- Do not install/change GPU dependencies, CUDA/TensorRT, vendor packages/runtime,
  licenses, global PATH or model parameters without explicit authorization.
- Protect `data/`, local models/images, licenses and operator History.
  Ignored/generated status alone does not authorize deletion. Verify actual
  references and the approved scope before cleaning outputs or local assets.
  Test-only fixtures may be renamed or retired within explicitly approved scope,
  with retained evidence mapped. Never regenerate expected results merely to
  make a regression pass.

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
  Preserve early serial inspection, channel identity and configured FINS/Modbus
  reporting, including the default FINS W49–W52 layout.
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
- Prefer concise contextual captions, meaningful project-native icons and minimal
  redundant text. Coordinate related controls and keep action sizes stable across
  languages. Update geometry tests when an approved presentation redesign changes
  the contract; preserve operational distinctions, tooltips and accessibility.

## Validation

Keep strict Pyright for maintained source/tests/scripts. No broad `Any`, global
ignores or weakened diagnostics. Vendor typing workarounds must be local and
justified at the adapter boundary. During UI iteration, use actual Qt screenshots,
focused interaction tests and checks relevant to changed files. Review screenshots
and fix visual issues before final comprehensive validation; ordinary phases do
not require an unchanged full suite run when focused evidence covers the change.

Every independent pytest invocation, focused or full, must use a fresh, unique
`--basetemp`; never reuse an earlier test directory. Generate the path anew for
each invocation, as below. Isolation reduces possible cross-run interference but
does not establish the cause of intermittent Windows WinError 5.

Fast checks for ordinary changes:

```powershell
uv run --no-sync ruff check <changed-files>
uv run --no-sync ruff format --check <changed-files>
uv run --no-sync pyright <changed-files>
$pytestBase = Join-Path "outputs/pytest" ([guid]::NewGuid().ToString("N"))
uv run --no-sync pytest <focused-test-files> --tb=short --basetemp $pytestBase
```

Run the complete Ruff, formatting, Pyright and pytest gates for substantial,
cross-domain changes, dependency changes, packaging changes, startup changes, or
before committing a release baseline. Node tests are required for Web changes.

Report commands/results and unexecuted checks honestly. Real GPU/hardware checks
are opt-in and proportional to changed behavior; do not routinely benchmark,
stress or run full datasets. For documentation-only changes, check links/anchors,
commands and whitespace; runtime tests/screenshots are unnecessary. Documentation
describes current operation; avoid phase narratives or duplicate contract trees.
