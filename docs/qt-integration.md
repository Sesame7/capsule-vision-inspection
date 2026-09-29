# Qt integration exploration (Phase 1B-02)

One process, two ownership contexts:

```text
GUI thread: QApplication -> DemoWindow -> QtInferenceBridge
                                             |
                                       InferenceWorker
                                             |
Python worker thread:       engine factory -> infer -> close
                                             |
                        Future completion -> queued Qt signal -> GUI slot
```

The existing Python worker already provides bounded admission, readiness Futures
and thread-owned cleanup. It remains unchanged and Qt independent; a QThread
replacement provides no necessary capability here. Neither the window nor the
bridge holds a vendor engine. The application supplies an engine factory.

## Bridge contract

`QtInferenceBridge(worker, parent=None, shutdown_timeout_ms=30000)` lives on the
GUI thread. Call `start()`, `submit(image_bgr)` and `request_stop()` there.
`worker` exposes the existing backend for state diagnostics; `stopped` means
actual thread exit was observed. Submission borrows the same input array under
the worker's immutability contract, without a bridge copy.

Signals emitted on the GUI thread:

| Signal | Payload / meaning |
| --- | --- |
| `startup_succeeded()` | Ready for admission |
| `startup_failed(object)` | Original initialization/start exception |
| `result_ready(object)` | Our typed `ImageResult` |
| `inference_failed(object)` | Original task exception, including cancellation |
| `submission_failed(object)` | State/queue admission exception |
| `state_changed(object)` | `WorkerState` snapshot |
| `shutdown_failed(object)` | Timeout or terminal worker failure |
| `shutdown_finished()` | Actual thread termination, including failed cleanup |

Future callbacks only read an already-complete Future with `result(timeout=0)`
and emit a private signal through an explicit `QueuedConnection`. They do not
inspect masks, serialize results, render, wait or touch widgets. This works when
a callback runs on the worker, on cancellation's caller, or immediately while
registering an already-complete Future. Public slots then run on the GUI thread.
Exception identity and chaining survive; only the display converts to text.
An initialization error can appear both as startup failure and terminal worker
failure; those describe readiness and lifecycle respectively.

Readiness/results/errors are event driven. A 250 ms timer observes lifecycle
state and uses only `join(timeout=0)` to confirm exit, including unexpected worker
failure. It never polls results. State snapshots can skip transient states; this
is a status notification, not a complete transition log.

## Lifetime and close

The entry point retains the application/window; the window owns the bridge, and
the bridge owns its timer. Signal receivers are QObject slots. Start is explicit
via **Start Worker** and never waits for native initialization. Run is disabled
while a task is active. The demo decodes one local file on the GUI thread before
submission; it is not an asynchronous large-file loader. Inference and all
engine lifecycle calls stay on the backend thread.

Closing disables submission, requests stop, ignores the first close event and
leaves Qt running. The active call (or constructor) finishes naturally; queued
tasks are cancelled and engine cleanup runs on its owning thread. Only observed
thread exit triggers the final close. Repeated closes do not reset the deadline
or timer. The application also intercepts normal Qt Quit events and routes them
through this policy. Direct `exit()`, forced process termination and OS shutdown
are not recovery paths provided by this exploration.

A 30-second shutdown deadline reports `TimeoutError` once, keeps ownership and
continues observing the worker. It never declares successful termination, retries
cleanup or kills the process. A permanently blocked native call therefore keeps
the window/process alive; process isolation is a future option only if native
hangs become an observed requirement. Cleanup errors remain original exception
objects; `shutdown_finished` certifies exit, not cleanup success.

The window ignores successful late results while closing. `detach()` is an
idempotent GUI-thread operation that revokes delivery and discards queued
notifications. Future callbacks retain a small Python gate rather than the
bridge/window. Its separate unparented signal sender is protected by a lock:
detach disables emission before scheduling sender deletion on the Qt thread.
Bridge destruction also detaches through a direct destruction connection. Thus
a late callback never emits on the deleted sender; Qt disconnects destroyed
receivers. An owner deliberately deleting the bridge early must independently
retain, stop and join the worker; detachment alone is not shutdown. Normal app
close retains everything until the worker terminates.

## Typing and verification

Installed PySide6 **6.11.2** is pinned as a runtime dependency, with matching
Addons, Essentials and Shiboken in `uv.lock`. No other versions changed. Qt's
`Signal(object)` boundary is narrowed with `isinstance` in slots; there are no
new typing suppressions, casts, broad `Any`, or relaxed strict rules.

Tests use Qt's offscreen platform, QEventLoop, QTimer, QSignalSpy and event-gated
fake engines, without pytest-qt or sleeps. They verify GUI/engine thread IDs,
responsive blocked startup/inference, original errors, admission rejection,
idle/active/startup/repeated close, explicit application quit, timeout retention,
deleted bridges with late callbacks, and no surviving named workers. Offscreen
configuration applies only to tests.

```powershell
uv run --no-sync pytest tests/unit/test_qt_integration.py --tb=short
uv run --no-sync pytest tests/integration/test_qt_vimo.py tests/integration/test_worker_vimo.py::test_worker_one_image --run-gpu --require-gpu -s --tb=short
```

The real Qt smoke checks one golden defect image, asynchronous result delivery,
GUI/engine thread identity and clean termination. This is not a model-accuracy,
performance, 400-image or long-run regression.

Validation on 2026-09-29: all 11 Qt unit cases passed (66 non-GPU cases total;
5 GPU cases skipped by default). The real Qt one-image smoke and existing worker
one-image guard both passed, with no residual named workers. Ruff, formatting and
strict Pyright passed without suppressions. Inference/runtime implementation was
unchanged; the full dataset and historical stress/benchmark were not rerun.

## Manual check (about 1–2 minutes)

From the repository root:

```powershell
uv run --no-sync python -m vimo_demo.ui.app models/model_trt_fp16.vimosln
```

1. Click **Start Worker**; wait for READY while moving/resizing the window.
2. Select an image in `images/` (or pass `--image images/<filename>.png`).
3. Click **Run Inference**; inspect labels/counts/has_defects/mask shape. Interact
   with the window during work; the current model may finish too quickly to see
   a busy interval, which is why blocked fake engines provide automated evidence.
4. Run again, then close. Confirm the process returns to the terminal.

This standard-widget window is an integration instrument. It has no production
acceptance policy, overlays, camera/PLC/web integration, task scheduler, native
cancellation, automatic restart or final workstation design.
