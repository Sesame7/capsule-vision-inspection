# Inference worker: ownership and lifecycle

`vimo_demo.runtime.InferenceWorker` owns one engine on one dedicated,
non-daemon `VimoInferenceWorker-...` thread. Construction of the worker creates
no engine. Its factory, every `infer()` and exactly one `close()` all execute
on that thread. The [inference core](inference-core.md) and its parameters are
unchanged. See [architecture](architecture.md) for the layer boundaries.
`InferenceEngine` is a structural `infer`/`close` Protocol for small test doubles;
the runtime layer has no SmartMore or Qt imports.

Given `model_path` and an already decoded `image_bgr`, a CLI caller can use:

```python
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.runtime import InferenceWorker

worker = InferenceWorker(lambda: VimoSegmentationEngine(model_path))
try:
    worker.start().result(timeout=120)  # Waiting is for CLI/tests, not a GUI thread.
    result = worker.submit(image_bgr).result(timeout=30)
finally:
    worker.request_stop()
    if not worker.join(timeout=30):
        raise TimeoutError("Native work still running; worker remains alive")
if worker.failure is not None:
    raise worker.failure
```

## State and readiness

```text
NEW -> STARTING -> RUNNING -> STOPPING -> STOPPED
          |                      ^
          +-- stop requested -----+
NEW -- stop before start --> STOPPED
startup / fatal execution / cleanup failure --> FAILED
```

- `start()` returns promptly with `Future[None]`. Factory errors are delivered
  as the original exception, including its cause, and end in FAILED. An engine
  that never finished construction is not given a fictitious `close()` call.
- Submit is accepted only in RUNNING. Startup readiness cannot be cancelled with
  `Future.cancel()`; use `request_stop()`. A stop during initialization lets the
  factory finish, fails readiness with `WorkerStateError`, closes the engine and
  exits without accepting work. If initialization itself fails, its error wins.
- One worker object has one lifecycle. Repeated `start()` is rejected in every
  state except NEW. After STOPPED/FAILED, construct a new worker.
- State/failure reads and admission/stop transitions are synchronized. Terminal
  state records the cleanup outcome immediately before thread exit; `join()` and
  `is_alive` establish actual thread termination. FAILED may be visible while
  cleanup is still finishing. `join()` returns True for either terminal outcome;
  inspect `failure` for worker-level errors, especially cleanup failures.

## Work and ownership

- The FIFO queue defaults to `max_pending=1`, **excluding the active task**.
  A full queue raises `WorkerQueueFullError` immediately. Other invalid-state
  operations raise `WorkerStateError`. There is no unbounded queue or frame drop.
- Submit does not copy, convert, validate or resize images. The engine keeps its
  existing uint8 H x W x 3 BGR contract and validates inside the worker. Once
  submission succeeds, the caller must treat the array and all aliases as
  immutable until the Future finishes or is cancelled. Acquisition adapters
  must supply owned arrays; retaining a view into a reused camera buffer is unsafe.
- Task claiming and stop use the same lock. A task starts when its Future is
  marked running under that lock; it then finishes even if stop arrives just
  before the native call. Each normal `Exception` completes only that task's
  Future. The same engine remains usable; there are no retries/reconstruction.
- Caller cancellation succeeds only before a task is claimed. Cancelled entries
  can occupy queue capacity until the worker removes them. Result Futures may be
  observed/cancelled, but callers must not set their results/execution states.

## Stop and failure

`request_stop()` immediately revokes admission, signals the condition and returns.
It never waits for native initialization, inference, cleanup or Future callbacks.
After the active call returns, the worker cancels queued tasks without running
them, then closes the engine on its owning thread. Queued cancellation may
therefore be deferred until the active call finishes. Idle stop wakes the worker
without polling or sleeps. Repeated stop requests are harmless; close is not retried.

`join(timeout)` waits only; it does not request stop. False means the thread is
still alive. A blocked native inference or close leaves the normal shutdown state
STOPPING. No references are discarded to pretend shutdown succeeded. Joining a
NEW worker or joining from its own thread raises `WorkerStateError`.

A task `BaseException` outside normal `Exception` handling is fatal: its Future
fails, pending work is cancelled, cleanup runs and state becomes FAILED. Cleanup
errors are exposed by `failure`; if another fatal error already exists, cleanup
error context is added as a note. Native SDK calls cannot be forcibly cancelled;
a permanently blocked call prevents termination of this non-daemon thread.

## Future callbacks and Qt integration

The worker exposes Futures, not Qt callbacks. They use standard-library thread contexts:
completion generally runs them on the worker, caller cancellation on the caller,
and registering on an already completed Future can invoke them immediately.
Keep callbacks short/nonblocking, never raise BaseException, and never manipulate
widgets or wait for this worker from them. Completions occur outside the worker's
state lock. The implemented [QtInferenceBridge](qt-integration.md) marshals
notifications through queued Qt signals to the GUI thread. It receives an existing
worker, not an already constructed engine, and does not change this ownership model.

## Verification

Event-coordinated fake engines test construction/use/close thread IDs, serial
execution, bounded admission, cancellation, startup/cleanup errors and races.
Normal pytest never loads ViMo for these tests. Two opt-in real tests reuse the
existing golden checks for one deterministic defect image:

```powershell
uv run --no-sync pytest tests/integration/test_worker_vimo.py::test_worker_one_image --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_worker_vimo.py::test_worker_repeated_lifecycle --run-gpu --require-gpu -s --tb=short
```

The second test constructs/infers/closes **10 successive new workers**, never
concurrent engines. Both report completed iterations, failure counts and remaining
named threads, and verify real entry-point thread ownership. Missing prerequisites
skip without `--require-gpu`. Ten lifecycles are not evidence of leak absence or
throughput; the historical long-run test is not repeated here.

Historical Phase 1B-01 validation on 2026-09-29: worker unit tests, the one-image
GPU lifecycle and all 10 repeated lifecycles passed with
zero initialization/inference/shutdown failures and no remaining named workers.
Construct/infer/close IDs matched within each lifecycle and differed from the
caller. The existing eight-image reference regression also passed. The inference
package was unchanged, so the full 400-image and 30,000-call runs were not repeated.
