# Inference and runtime

This document owns **Current** inference execution, bounded handoffs, resource
ownership and shutdown. The engine and runtime are independent of Qt and expose
our typed results, not SmartMore objects. Current classes/thread arrangements are
implementation pointers, not permanent Target architecture requirements.

| Boundary | Responsibility |
| --- | --- |
| `VimoSegmentationEngine` | Synchronous single-image inference, vendor adaptation and owned result conversion |
| `InferenceWorker` | Create/use/close one engine on its thread; bounded image tasks and task Futures |
| `RuntimeCoordinator` | Exclusive collector/worker lifecycle, serialized admission, bounded cycle dispatch and terminal delivery |
| `ProductionApplication` | Candidate activation, disposable sessions and production accounting; see the [application contract](application.md#activation) |

[Inspection](inspection.md) owns frame/cycle identity, collection outcomes and
judgment. [Configuration](configuration.md) owns persisted definitions, pure
resolution and expected-artifact verification. Qt delivery/presentation belongs
to [Qt integration](ui-shell.md#qt-delivery) and the [Production shell](ui-shell.md#production).
The GUI never runs native inference or waits for unfinished work with blocking
`Future.result()` / `join()`; completed-result delivery is independent of rendering.

## Input and results

### Synchronous API

Given an already decoded `image_bgr` and a local `.vimosln` `model_path`:

```python
from vimo_demo.inference import VimoSegmentationEngine

with VimoSegmentationEngine(model_path, device_id=0) as engine:
    result = engine.infer(image_bgr)
    print(result.has_defects, result.label_map, result.regions_per_label)
```

The input must be a nonempty **uint8 H × W × 3 BGR ndarray**. Type/dtype errors
raise `TypeError`; incompatible dimensions raise `ValueError`. Broad NumPy
annotations accommodate OpenCV's return typing but do not relax runtime checks.
The engine does no decoding, RGB guessing, resizing or orientation change.
Suitable C-contiguous input is reused; strided input is safely made contiguous
without changing its logical pixels. The caller keeps all aliases stable until
the synchronous call returns. Worker submission and owned inspection Frames have
the distinct lifetime rules below.

### Owned typed output

| Type / field | Meaning |
| --- | --- |
| `BoundingRect` | Original-image x/y/width/height |
| `RotatedRect` | SDK center-x/center-y/width/height/angle convention, unchanged |
| `DefectRegion` | Label, integer area, bbox, long/short sides, outer/inner contours and minimum-area rectangle |
| `ImageResult` | Image shape, actual label map, regions, full-image mask, ROI/rotated ROI and module ID |
| `EngineInfo` | Model SHA-256, module identity/name/version/labels, device ID and area-threshold snapshot |

`label_map` is the actual **label → mask ID** mapping, not a region count.
`regions_per_label` derives counts from returned regions; `has_defects` means
regions exist, not a final production NG or physical-product acceptance decision.
`mask_id_to_label` inverts actual mappings without inventing classes. Conversion
requires nonempty labels, unique IDs and the uint8 ID range. Background ID 0 is
the documented SDK convention; the core does not fabricate a background label.

Engine-produced dataclasses are frozen, maps are read-only snapshots, and region
collections/contours are tuples. Derived count/inverse-map dictionaries are fresh
values, not mutable views into the result. Empty contours stay empty.
`ImageResult` deliberately does not define array-based equality.

Both `(H, W)` and **`(H, W, 1)`** uint8 masks are valid. Only the final singleton
channel is squeezed; spatial singleton dimensions remain. The normalized mask
must match the original image's H/W. Incompatible shapes/dtypes fail rather than
being flattened, resized or coerced. Raw integer IDs stay intact; display colors
are outside the core.

The mask is an owned C-contiguous copy, read-only by default, because SDK response
storage can be reused/released. It remains valid after subsequent calls and engine
cleanup. Use `result.mask.copy()` for writable pixels. NumPy read-only flags are
an ownership convention, not deep immutability or a security boundary.

## Lifecycle ownership

### Engine setup and vendor boundary

Construction computes the source model's SHA-256 and stages a verified copy at
`<cache_dir>/<sha256>/model.vimosln`. Default `cache_dir="outputs/vimo_cache"`
is relative to the process working directory; keep it writable and separate from
the source model. Source files remain read-only. Existing staged copies must
match the digest; corruption fails instead of silently overwriting them. Missing
runtime caches may rebuild. The staged copy is verified before use, including
detecting source changes during copying. This does not impose the golden model's
identity on arbitrary engine callers: expected SHA verification is a separate
[configuration/artifact boundary](configuration.md#artifact-paths).

The adapter lazily imports ViMo, creates one Solution and one GPU segmentation
module with **`max_batch_size=1`**, default `device_id=0`. Optional keyword-only
`module_id` selects an exact segmentation-module ID; without it, exactly one
segmentation module must exist. Zero or ambiguous candidates fail. Module `2`
belongs to the reference model, not a generic hard-coded engine selection rule.

Setup enables region information and ORs these verified flags into the existing
flag value: AREA, LONG_SIDE, SHORT_SIDE, BOUNDING_RECT, MIN_AREA_RECT,
OUTER_CONTOUR and INNER_CONTOURS. Other thresholds, ROI, mask operation and SDK
settings are preserved. Mask-enabled, region-enabled and score-map-disabled
settings are checked after configuration; unexpected defaults fail rather than
being silently changed. No `vimo_params.json` reload, hidden warm-up, retry,
batch API or scheduling is performed by the engine.

Only the [adapter](../src/vimo_demo/inference/vimo_adapter.py) imports SmartMore
and converts vendor objects. Runtime narrowing checks module/parameter/response
types. Current local typing workarounds address the SDK's missing `py.typed`
marker, documentation-mode geometry classes versus actual runtime tuples, and
defensive checks of claimed C++ return types. They are documented at this boundary;
global attribute/argument checks remain enabled and no broad `Any` is used.
Pure models and fake-engine tests remain usable without importing the runtime SDK.

An engine belongs to **one serial owning context**, including construction and
cleanup. CLI tools can use it directly. In worker/application execution, provide
a factory that constructs it on the owning worker thread; do not pass an engine
already initialized elsewhere. There is no SDK thread-safety guarantee, engine
locking, parallel GPU execution or multiprocessing policy.

### Worker and coordinator owners

Constructing an `InferenceWorker` creates no engine. Its factory, every `infer()`
and exactly one `close()` for a successfully returned engine execute on the same
non-daemon `VimoInferenceWorker-...` thread. `InferenceEngine` is a small structural
`infer`/`close` Protocol for test doubles, not a vendor abstraction hierarchy.

The coordinator takes exclusive lifecycle ownership of a **NEW** worker. Do not
share it with a Qt bridge or submit unrelated tasks to it. The separate one-image
Qt diagnostic uses its own worker. Current coordinator execution has three
non-daemon threads:

```text
source callers -> bounded commands -> CycleCoordinator (sole collector owner)
                                          |
                                   bounded READY backlog
                                          |
                                   CycleInspector
                                          |
                                   InferenceWorker -> engine
```

Only the collector owner mutates `CycleCollector`. The serial inspector claims
one READY cycle, then waits on one worker task at a time in configured camera
order. Collection remains responsive while native inference runs. READY cycles
are FIFO by **collection completion**, not CycleId. Overlapping collection does
not imply concurrent GPU inference.

The default inspection callable is synchronous `inspect_cycle()`. The optional
`cycle_inspector` composition hook receives a snapshot and the worker-backed
inference callable. It runs on the inspector for READY work, or on the collector
owner for lightweight incomplete-result finalization. A supplied hook must preserve
identity, host-monotonic timing and incomplete-cycle no-inference semantics and
must not block collection with algorithm work. These are composition obligations;
the coordinator is not a general validator of arbitrary hook implementations.
Configured judgment is described in [inspection](inspection.md#configured-execution),
and its installation/activation remains application-owned.

Worker and coordinator each have one lifecycle:
`NEW → STARTING → RUNNING → STOPPING → STOPPED`, with FAILED for fatal startup,
runtime or cleanup failure. Stop before start ends the instance without creating
threads/engine. `start()` is only valid in NEW; stopped/failed instances are
replaced, not restarted. Worker readiness reports factory completion; coordinator
readiness additionally waits for its serial inspector startup. Neither is an
application candidate-activation promise.

## Admission and Futures

### Distinct observation boundaries

| Operation | Returned observation / meaning |
| --- | --- |
| `worker.start()` | `Future[None]`: engine factory succeeded or original initialization error |
| `worker.submit(image)` | `Future[ImageResult]`: one claimed task result/error; pending tasks can be cancelled |
| `runtime.start()` | `Future[None]`: worker/runner readiness, not cycle admission |
| `runtime.begin_cycle(id)` | `Future[CoordinatedCycleResult]`: terminal cycle observation; command enqueued, collector admission may still fail |
| `runtime.admit_cycle(id)` | `CycleAdmission`: separate `accepted: Future[None]` and terminal `result` Future |
| `runtime.submit_frame(frame)` | `Future[None]`: collector accepted that frame, not inspection completion or buffer return |
| `runtime.expire()` | `Future[None]`: serialized explicit expiration tick processed |
| `runtime.finished` | `Future[None]`: shutdown body completed; inspect failure and join for actual exit |

### Worker tasks and borrowing

Submission is accepted only in RUNNING. `WorkerStateError` / `WorkerQueueFullError`
reject invalid state/full pending capacity synchronously, without accepting an
image. Accepted submission borrows the ndarray without copying, validation,
conversion or resize; the engine validates when it executes. Every alias must
remain immutable until that task Future completes or is cancelled. Owned camera
buffers/views require a producer ownership decision; an array backed by a camera
buffer being reused asynchronously is unsafe.

Tasks are FIFO. Claiming and stop share a lock; claim marks the Future running.
A claimed task finishes even if stop arrives just before the native call.
Caller cancellation succeeds only before claim and prevents execution. A cancelled
entry can occupy pending capacity until removed. Readiness Futures cannot cancel
native construction. A `.result(timeout=...)` timeout merely stops the caller's
wait; it neither cancels a task nor establishes resource termination.

Ordinary task `Exception` fails only that task Future with the original exception
and cause. The engine remains the same, with no implicit retry or reconstruction;
the worker stays usable. A fatal `BaseException` outside `Exception` handling
fails the task and worker, cancels pending work, and still attempts cleanup.
Task errors live on their Futures; `worker.failure` records worker-level fatal/
startup/cleanup errors, not ordinary image failures.

A CLI/test owner may block deliberately; GUI code must observe asynchronously:

```python
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.runtime import InferenceWorker

worker = InferenceWorker(lambda: VimoSegmentationEngine(model_path))
try:
    worker.start().result(timeout=120)
    result = worker.submit(image_bgr).result(timeout=30)
finally:
    worker.request_stop()
    if not worker.join(timeout=30):
        raise TimeoutError("Worker still alive; retain ownership and join later")
if worker.failure is not None:
    raise worker.failure
```

The join timeout in this example is observable failure, not a way to abandon live
native work. An enclosing owner retains the worker and must finish shutdown.

### Cycle admission and terminal delivery

The public runtime command API is synchronized/nonblocking. Wrong lifecycle or
full command capacity raises synchronously before returning a handle. Once a begin
command is enqueued, collector capacity/identity rejection arrives asynchronously
on the returned cycle result and, with `admit_cycle()`, its `accepted` Future.
`accepted` succeeds only after collector admission. FIFO commands allow frames
to be enqueued after begin without waiting for its terminal result; concurrent
producers must coordinate the [increasing CycleId policy](inspection.md#identity-and-frames).

Every returned cycle result Future has exactly one terminal result or exception,
including begin commands rejected before collection. Emitted snapshots dispatch
at most once; late input cannot reopen them. There is no completed-result history
or unbounded deduplication cache. Runtime Futures are **noncancellable observation
handles**, unlike pending worker tasks. Callers must never set result/execution
state on any of these Futures.

Exactly-once completion is not a deadline/liveness guarantee while a native call
or completion callback is permanently blocked; waiting/stop timeouts do not
manufacture terminal results.

`CoordinatedCycleResult.result` is the domain `InspectionCycleResult`; its
COMPLETE/FAILED/TIMED_OUT/SHUTDOWN meanings remain in
[inspection](inspection.md#technical-cycle-outcomes). Ordinary image errors are
explicit frame failures and later frames/cycles continue. Worker dispatch rejection
(such as unexpected state/full capacity), fatal `BaseException`, or an escaping
inspection/infrastructure error instead fails the cycle Future and stops the runtime.
Dispatch rejection becomes `CoordinatorError` with its original cause. Runtime
overload is not fabricated as domain FAILED, product NG or a successful inspection.

Consumers own returned results, which can retain inference masks, and must bound
their retention. Runtime cleanup releases internal source-frame references; it
does not release caller-owned snapshots, Futures or exception context. The stronger
owned-Frame lifetime contract remains in [inspection](inspection.md#identity-and-frames).
Frame acknowledgement alone never authorizes pixel reuse.

Future callbacks follow standard-library completion contexts: often worker/owner/
inspector threads, the cancelling caller for worker task cancellation, or immediately
the registering caller for an already-completed Future. Keep them short/nonblocking,
never raise `BaseException`, never wait on this runtime and never mutate widgets.
Completion is outside admission locks but a blocking callback can still stall its
completing thread. Qt marshalling is a separate presentation responsibility, not
another runtime result queue.

## Bounds and timeouts

| Boundary | Current bound / overload |
| --- | --- |
| Commands | Default `max_commands=32` pending plus at most one executing; synchronous `CoordinatorCapacityError`, nothing enqueued |
| Incomplete collection | Default `max_collecting=2`; begin result/accepted Futures receive `CycleCapacityError` |
| READY backlog | Default `max_ready=2` waiting plus one claimed inspection; terminal cycle Future receives `CoordinatorCapacityError` |
| Worker pending | Default `max_pending=1`, excluding the active task; standalone submission raises `WorkerQueueFullError` |

Capacities are positive integers. Normal coordinator dispatch has only one worker
task outstanding. The application has an additional bounded receipt policy outside
these queues; see [application accounting](application.md#counters). Bounds count
objects, not pixel bytes, arbitrary producer queues, consumer histories or callback
execution time. They do not constitute a process-memory limit.

Expected command/collector/READY capacity rejections do not automatically stop the
runtime; other admitted cycles remain observable. An unexpected worker dispatch
rejection is a fatal infrastructure error, as distinguished above.

No queue silently evicts, pads, retries or reopens work. A rejected begin does not
consume a collector ID. Conversely, once the last frame finalizes a READY cycle,
a full READY backlog fails that **cycle Future** while the **frame acknowledgement
can succeed**. Observe both; acknowledgement is not proof of eventual inspection.
Overload does not reopen the finalized cycle or mix late frames into another one.
`snapshot` exposes state, current/peak command/collecting/READY counts and the
inspecting CycleId without retaining a completed history.

### Explicit expiration and timing

The runtime/source owner must call `runtime.expire()` and observe its acknowledgement.
There is no autonomous deadline scheduler, Qt-owned timeout, sleep-based polling
or automatic tick retry in this coordinator. Without ticks, an incomplete cycle
remains pending until a later expiration or shutdown. A full command queue explicitly
rejects a tick. Application source code may supply those events; that is composition,
not hidden coordinator scheduling.

Collection cutoff uses the [host-monotonic interval](inspection.md#collection).
Frame acceptance time is when the owner processes the command, not when a source
callback enqueues it; capture timestamps cannot backdate admission. Collection
timeout does not cancel READY/active inspection. Use a thread-safe monotonic clock
in the same time domain across collector/inspector and any supplied hook.

| Milestone | Existing field |
| --- | --- |
| Cycle admitted/start | `envelope.result.started_at` |
| Collection finalized | `envelope.result.collection_finished_at` |
| READY queued | `envelope.inspection_queued_at` |
| Inspection claimed | `envelope.inspection_started_at` |
| Inspection finished | `envelope.result.finished_at` |
| Result ready, before Future delivery | `envelope.result_ready_at` |

Queue/start timestamps are None for incomplete uninspected cycles. Exceptions do
not manufacture a successful result/timing envelope. Milestones are diagnostic,
not latency thresholds or GPU-kernel benchmarks; interpretation belongs to
[performance](inspection-performance.md). Collection deadlines, task-wait timeouts
and shutdown join timeouts are separate; none force-cancels native calls.

## Shutdown

### Engine cleanup

Use `with` or explicit idempotent `close()`. The adapter's `ExitStack` invokes
real SDK context managers, **module before Solution**, including setup failure.
There is no invented disposal API or dependence on garbage collection. Calls
after close fail. An ordinary cleanup error raises `VimoError` with its cause;
when preserving an existing caller/setup exception, cleanup context is added as
a note. Closed state alone does not prove cleanup succeeded or make retry safe.

| Engine failure boundary | Public exception |
| --- | --- |
| Staging/import/module/parameter setup | `VimoInitializationError` |
| Request construction / SDK execution | `VimoInferenceError` |
| Response type / owned conversion | `VimoResponseError` |
| Explicit cleanup/lifecycle | `VimoError`; `infer()` after close specifically raises `VimoInferenceError` |

All inherit from `VimoError`; wrapped ordinary exceptions retain the original
type/message/code through `__cause__`. Bad input retains direct TypeError/ValueError.
Process-control exceptions are not silently converted into ordinary engine success.
License/DLL/runtime errors do not trigger automatic installation/environment repair.

### Independent worker stop

`request_stop()` revokes admission and wakes the condition without waiting for
construction, inference, cleanup or callbacks. It needs no queue slot/sentinel;
repeated requests are harmless. Idle stop requires no polling.

- Before start, stop creates no engine/thread. During initialization, it lets the
  factory finish, fails readiness with `WorkerStateError` if construction succeeds,
  closes the returned engine and exits without admitting tasks. A constructor
  failure remains the original readiness error; no fictitious close is performed.
- A claimed task finishes. Then queued tasks are cancelled without execution and
  the engine closes on the worker. Queued cancellation can be deferred until the
  active call returns; stop itself does not run their cancellation callbacks.
- Cleanup failure is exposed by `failure` and FAILED; if a fatal error already
  exists, cleanup context is added as a note. Close is not retried.

`join(timeout)` **waits only**, does not request stop, and returns True for actual
thread termination even if FAILED. Inspect `failure` too. Joining NEW or the worker's
own thread raises `WorkerStateError`. FAILED may be visible before cleanup ends;
terminal state is not evidence of thread exit. A blocked native call/close leaves
shutdown observable and can keep the non-daemon thread alive indefinitely.

### Coordinator stop

Stop immediately revokes public admission, wakes the owner/inspector and is
independent of command capacity. Claiming READY work and stop share a lock:
whichever wins determines whether that cycle is active. An already executing
command can finish its processing; unprocessed commands do not start.

1. Reject unprocessed commands (including both begin observations) with
   `CoordinatorStoppedError`.
2. Finalize incomplete collected cycles through collector shutdown. They produce
   domain SHUTDOWN or already-due TIMED_OUT results, without inference.
3. Reject unclaimed READY cycles with `CoordinatorStoppedError`; they do not run.
4. Let the **whole claimed inspection cycle** finish its remaining frames after
   ordinary per-frame errors. Fatal worker/runtime failure may instead abort it
   with an explicit exception.
5. Join the serial inspector before requesting worker stop, so the worker remains
   available for all remaining calls in that active cycle. Then join the worker;
   its thread owns engine cleanup.
6. Record STOPPED/FAILED and resolve `finished` after the shutdown body completes.
   The owner thread may still be returning: external `join()` confirms actual exit.

`runtime.join(timeout)` checks actual termination of **all three threads** and
returns False while any remains alive. It does not initiate stop. NEW join and
self-join from a runtime thread are invalid. Read `failure`/`snapshot.state` as
well: successful termination can include fatal initialization/runtime/cleanup errors;
`finished` resolving is not a success-only signal or substitute for join.

Stopping during startup waits for the worker's factory before cleanup; stopping
before startup creates no threads/engine. Native construction/inference/close
cannot be forcibly cancelled. A join timeout retains ownership; observe/join again
rather than discarding the runtime or claiming successful shutdown. Higher-level
source/session/application termination has its own owner and must also be confirmed
by the [application](application.md#shutdown) before releasing that ownership.

## Verification

Current implementation: [engine adapter](../src/vimo_demo/inference/vimo_adapter.py),
[input/mask validation](../src/vimo_demo/inference/_validation.py),
[result models](../src/vimo_demo/inference/models.py),
[model staging](../src/vimo_demo/inference/_model_store.py),
[worker](../src/vimo_demo/runtime/inference_worker.py) and
[coordinator](../src/vimo_demo/runtime/coordinator.py).

GPU-independent tests use small arrays, SDK stand-ins, controlled clocks and
Events rather than sleep-driven races. They cover conversion/ownership, parameter
preservation/cleanup chains, bounded admission, pending cancellation, startup/stop/
fatal errors, explicit expiration, exactly-once cycle completion and blocked cleanup.
The coordinator suite includes a deterministic 200-cycle/1,200-frame scenario
checking overlapping collection, reversed arrival, bounded peaks and no residual
threads. This describes existing tests, not a new test-run result from a docs edit.

From the repository root:

```powershell
uv run --no-sync pytest tests/unit/test_contracts.py tests/unit/test_adapter.py tests/unit/test_model_store.py tests/unit/test_inference_worker.py tests/unit/test_runtime_coordinator.py tests/integration/test_cycle_worker.py --tb=short
```

Repository Ruff/format/strict Pyright and normal pytest commands remain in
[AGENTS](../AGENTS.md#typing-and-validation) and [README](../README.md). Preserve
the verified environment with daily `uv run --no-sync`; public dependency versions
are in pyproject/lock, while ViMo/custom/license/CUDA12/TensorRT are externally
provisioned. Unit/fake-runtime execution does not provision or certify that runtime.

### Optional real integration

Normal pytest skips these opt-in GPU checks. Existing short workflows:

```powershell
uv run --no-sync pytest tests/integration/test_reference.py::test_short_reference --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_worker_vimo.py::test_worker_one_image --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_worker_vimo.py::test_worker_repeated_lifecycle --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_coordinator_vimo.py --run-gpu --require-gpu -s --tb=short
```

The eight-image reference selects no-region cases and all four labels. It checks
semantics, mask/geometry properties and result lifetime, not bit-exact golden
geometry. Worker checks use one deterministic defect image; the repeated test
creates **10 successive new workers**, not concurrent/restarted engines. The
coordinator check sends six reference Frames through one worker/engine, verifies
per-image semantics/ownership and clean termination. These are integration/wiring
checks, not accuracy, throughput or leak-absence certifications.

The tracked [golden fixture](../tests/reference/phase0.json) is self-contained;
historical scan reports/scripts are not regression dependencies. Tests use local
`models/model_trt_fp16.vimosln` and `images/`. Missing/changed reference assets,
SDK/DLLs or authorization skip optional checks; `--require-gpu` makes missing
prerequisites fail. Other setup/programming errors fail rather than masquerading
as unavailable hardware. Full reference verification checks the exact root image
filename set and all 400 image hashes before per-image and aggregate comparisons;
model-specific assertions apply only to that model/dataset identity:

```powershell
uv run --no-sync pytest tests/integration/test_reference.py::test_full_reference --run-full-reference --require-gpu -s --tb=short
```

Reference reports go to `outputs/phase1a_regression/`. A full dataset, stress run
or benchmark is not routine validation for runtime composition or documentation
changes. Historical finite SDK/lifecycle observations remain in
[validation evidence](archive/validation-evidence.md) and its
[early worker/Qt record](archive/validation-evidence.md#early-worker-and-one-image-qt-evidence);
they are not continuously rerun guarantees.

### Deferred boundaries

Real trigger/session association, buffer leases, clock synchronization, capacity/
deadline values, upstream overload response and machine stop/safety policy require
hardware decisions. Multi-engine/GPU concurrency, batch optimization, autonomous
schedulers and new retry/recovery systems are **not implemented or approved here**.
Future design may change topology through explicit decisions/equivalence checks;
the current ownership/admission/termination guarantees remain accurate until then.
