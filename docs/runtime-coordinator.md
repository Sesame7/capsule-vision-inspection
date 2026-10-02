# Runtime coordinator contract

`RuntimeCoordinator` composes the existing cycle collector, synchronous
`inspect_cycle()` and `InferenceWorker`. It is pure Python, independent of Qt,
SmartMore objects and camera/PLC libraries. It adds no production OK/NG rule.
See [inspection-cycle](inspection-cycle.md) for frame/result semantics and
[runtime-worker](runtime-worker.md) for engine ownership.

Phase 2C adds three optional composition observations/hooks without changing the
default path: `admit_cycle()` returns separate accepted/result Futures (accepted
only after collector admission), `finished` signals shutdown-body completion,
and `cycle_inspector` can replace the synchronous snapshot inspection callable.
`begin_cycle()` still returns its existing result Future. `join()` remains the
authority for actual thread termination, not `finished`. An injected inspector
must preserve cycle identity, host monotonic timestamps and incomplete-cycle
no-inference semantics; it runs on the cycle runner for READY snapshots, or the
collection owner for lightweight incomplete-result finalization. See
[application composition](production-application.md).

## Ownership and scheduling

Three non-daemon threads have distinct responsibilities:

```text
source threads -> bounded commands -> CycleCoordinator (sole collector owner)
                                             |
                                      bounded READY backlog
                                             |
                                      CycleInspector
                                      inspect_cycle()
                                             |
                                      InferenceWorker
                                      engine.infer()
```

The coordinator receives exclusive lifecycle ownership of a NEW worker. Only its
worker thread constructs, calls and closes the engine. Do not share that worker
with a Qt bridge or submit independent tasks to it. The existing single-image Qt
demo remains separate. No inference/inspection-domain implementation changed.

The collection owner never waits on image inference. The serial inspector claims
one READY cycle and waits on one worker Future per frame, in configured camera
order. Collection, expiration and shutdown commands remain responsive while a
native call runs. READY cycles are FIFO by collection completion, which may differ
from cycle-ID order. No parallel engines or GPU concurrency are introduced.

## API and terminal outcomes

```python
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inspection import CameraId, CycleId
from vimo_demo.runtime import InferenceWorker, RuntimeCoordinator

worker = InferenceWorker(lambda: VimoSegmentationEngine("models/model_trt_fp16.vimosln"))
runtime = RuntimeCoordinator(
    worker,
    tuple(CameraId(f"camera-{i}") for i in range(6)),
    timeout=1.0,
    max_collecting=2,
    max_ready=2,
    max_commands=32,
)
try:
    runtime.start().result()  # Non-GUI composition context only.
    cycle_future = runtime.begin_cycle(CycleId(1))
    # Source callbacks submit explicitly identified, owned Frames:
    # acknowledgement = runtime.submit_frame(frame)
    # Runtime/source deadline events call runtime.expire().
    # envelope = cycle_future.result() after all six frames or expiration.
finally:
    runtime.request_stop()
    terminated = runtime.join(10)
    # If False, retain runtime ownership and observe/join again later.
```

- `start() -> Future[None]` resolves after worker initialization and runner startup.
  Admission before readiness raises `CoordinatorStateError`. One instance has one
  lifecycle; restart is rejected.
- `begin_cycle(CycleId) -> Future[CoordinatedCycleResult]` enqueues a begin command.
  Returning a Future acknowledges command admission; collector admission may still
  fail asynchronously with `CycleCapacityError` or `CycleStateError`. FIFO commands
  permit submitting frames immediately after begin without waiting for a cycle's
  terminal Future. Concurrent producers must coordinate increasing begin IDs.
- `submit_frame(Frame) -> Future[None]` acknowledges **collector acceptance**, not
  inspection completion or permission to reuse pixels. Invalid/duplicate/late
  frames fail this acknowledgement explicitly and never replace accepted frames.
- `expire() -> Future[None]` acknowledges an explicit serialized deadline tick.
- `request_stop()` is nonblocking and repeatable, independent of queue capacity.
  `join(timeout) -> bool` confirms actual termination of all three threads. A
  runtime thread cannot join itself. Inspect `failure` / `snapshot.state` as well:
  termination may include a recorded fatal startup/runtime/cleanup failure.

Every returned cycle Future has one terminal result or exception, including
commands rejected before collector admission. Futures are noncancellable
observation handles; callers must not set their state. There is no internal
completed-result history. Consumers own and must bound retained results.
Callbacks may run on the owner or inspector (or immediately on the registering
thread for an already-completed Future); keep them short and nonblocking, never
wait on this runtime, and marshal GUI notifications separately. A blocking callback
can delay its completing thread; callback delivery is not another unbounded queue.

`CoordinatedCycleResult.result` is the unchanged `InspectionCycleResult`:
COMPLETE, per-frame technical FAILED, TIMED_OUT or SHUTDOWN. Ordinary image
inspection exceptions preserve explicit frame failure and inspection continues
with later frames. Worker dispatch rejection or a fatal `BaseException` instead
fails the cycle Future, stops the runtime and resolves remaining work explicitly.
Original causes remain available. Runtime overload is never fabricated as an
inspection FAILED or a model defect. No acceptance rule is implied by COMPLETE.

## Bounds and backpressure

| Boundary | Bound / rejection |
| --- | --- |
| Ingress | `max_commands` pending commands, plus at most one executing command; synchronous `CoordinatorCapacityError` when full (nothing enqueued) |
| Incomplete collection | `max_collecting` active cycles; cycle Future gets the collector's `CycleCapacityError` |
| READY backlog | `max_ready` waiting cycles plus one active inspection; cycle Future gets `CoordinatorCapacityError` when full |
| Worker | Existing bounded pending queue; coordinator has only one task outstanding |

No queue silently evicts, retries or pads work. A last frame can be accepted while
its cycle Future fails due to a full READY backlog; observe both acknowledgement
and cycle Future. A rejected begin consumes no collector ID; once a READY cycle
has finalized, overload does not reopen it. Late input cannot enter another cycle.
One high-water cycle ID enforces the existing session policy without a growing
deduplication set. `snapshot` exposes current/peak command, collecting and READY
counts plus the inspecting cycle ID for bounded diagnostics.

Bounds count objects, not image bytes or external producer/consumer queues.
`Frame` retains owned immutable BGR storage without automatic copies. Producers
must follow the existing exclusive ownership contract; acknowledgements are not
buffer-return events. Native camera-buffer leasing and safe reuse notifications
remain future hardware work. Successful runtime cleanup releases source frames;
results retain inference outputs, not input pixels.

## Timeout ownership and timing

Timeout observation uses **explicit runtime ticks**, not Qt or a polling thread.
The application/source deadline-event owner must call `runtime.expire()` and
observe its acknowledgement. Without ticks, incomplete cycles remain pending
until a later tick or shutdown. A full command queue explicitly rejects a tick;
there is no hidden automatic retry. Tests inject a thread-safe controlled clock
and ticks rather than sleeping. A future hardware event loop must supply these
events; autonomous deadline scheduling is not implemented in this phase.

Deadline checks use the existing host-monotonic collection interval; camera
capture timestamps never determine deadlines. Acceptance time is when the owner
processes a frame, not when a callback enqueues it. Collection timeout does not
cancel an already READY/inspecting cycle.

Use one thread-safe monotonic clock across collection and inspection. Milestones:

| Milestone | Field |
| --- | --- |
| Cycle started | `result.started_at` |
| Collection finalized | `result.collection_finished_at` |
| Inspection queued | `inspection_queued_at` |
| Inspection claimed | `inspection_started_at` |
| Inspection finished | `result.finished_at` (last frame outcome) |
| Result ready | `result_ready_at` (immediately before Future delivery) |

Queue/start timestamps are None for incomplete, uninspected results. Exceptions
do not manufacture an inspection/timing envelope. These timestamps are diagnostic
milestones, not acceptance thresholds or a benchmark.

## Shutdown

Stop revokes public admission immediately and wakes both owned threads. The owner
rejects unprocessed commands with `CoordinatorStoppedError`, finalizes incomplete
cycles through collector shutdown (SHUTDOWN or already TIMED_OUT), and rejects
unclaimed READY cycles with `CoordinatorStoppedError`. Claiming a READY cycle and
requesting stop share a lock: whichever happens first defines whether it is active.

The **whole active inspection cycle** finishes all remaining frames, including
after ordinary per-frame errors. Only then does the owner stop and join the worker.
Engine cleanup executes on that worker. The owner joins the serial inspector and
worker before reporting STOPPED (or FAILED if a fatal error occurred). A fatal
worker/runtime error may abort the active cycle with an explicit exception.

Blocked native construction, inference or cleanup is never force-cancelled. A
caller join timeout returns False, leaves threads owned/observable and does not
claim successful shutdown. Repeated stop calls are harmless. Stopping before
startup creates no threads or engine; stopping during startup waits for native
construction before cleanup. No new cycles can enter a stopped session.

## Validation and remaining decisions

Focused fake runtime tests include 200 cycles / 1,200 frames with overlapping
collection and reversed camera arrival, identity checks, exactly-once completion,
queue high-water bounds and no residual runtime threads. Events gate active
inference, startup, command processing and cleanup; no sleep-based race tests.

```powershell
uv run --no-sync pytest tests/unit/test_runtime_coordinator.py --tb=short
uv run --no-sync pytest tests/integration/test_coordinator_vimo.py --run-gpu --require-gpu -s --tb=short
```

The optional test sends six deterministic reference Frames through the coordinator
and real worker/engine, checks golden per-image semantics and confirms one engine
owner and clean shutdown. This is wiring validation, not model accuracy. No full
400-image regression, long stress run or benchmark is needed for this composition.

Real hardware must establish trigger/session IDs, counter restart/wrap handling,
camera-to-trigger association, buffer leases, clock synchronization, deadline and
capacity settings, upstream overload signaling, consumer retention and machine
shutdown policy. Future camera/PLC adapters supply/consume these contracts; they
must not move collection ownership into Qt or redefine technical failure as NG.
