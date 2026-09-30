# Inspection cycle contract

`vimo_demo.inspection` is a Qt/SDK-independent, single-owner lifecycle layer.
It imports only our `ImageResult` and uses an injected inspection callable. It
does not start threads, construct engines, dispatch GUI events or own PLC logic.
Collection and inspection are separate so a collection deadline is never confused
with cancellation of synchronous native inference.

```text
begin(cycle_id) -> bounded active collection
  accept(frame) -> READY snapshot when every camera is present
  expire()     -> TIMED_OUT snapshot for each due incomplete cycle
  shutdown()   -> SHUTDOWN (or already TIMED_OUT) snapshots

CollectedCycle -> inspect_cycle(snapshot, callable) -> InspectionCycleResult
```

## Public data and identity

- `CycleId`: caller-supplied nonnegative integer, strictly increasing at `begin()`
  within one collector session. Gaps are allowed; reuse/reopening is rejected.
  Only the highest admitted ID is retained, not an unbounded finalized-ID history.
  Session persistence, PLC counter wrap/restart and real trigger association remain
  future integration decisions. Do not feed stale frames from an earlier session
  to a new collector with reused IDs; drain/isolate the source when replacing it.
- `CameraId`: nonempty exact string, configured once in an ordered tuple. There
  are normally six cameras, but count/order are data, not six duplicated paths.
- `FrameId`: `(cycle_id, camera_id, sequence)`. Sequence is nonnegative source
  metadata, not a timing-based cycle association or global uniqueness claim.
  Different cycles/cameras may use the same sequence; one camera contributes
  exactly one frame to a cycle. A new sequence does not permit replacement.
- `Frame`: identity, source `captured_at` timestamp and owned BGR pixels.
- `ReceivedFrame`: accepted frame plus host `arrived_at` timestamp.
- `CollectedCycle`: immutable terminal collection snapshot with expected cameras,
  received frames, missing cameras, start/deadline/finalization times and
  `CollectionStatus` (`READY`, `TIMED_OUT`, `SHUTDOWN`). Snapshot validation rejects
  mixed identities, reordered cameras and a false READY with missing frames.
- `FrameInspectionResult`: identity, capture/arrival/inspection times, shape and
  either `ImageResult`, `InspectionFailure`, or neither when not inspected.
- `InspectionCycleResult`: cycle identity, expected cameras, ordered per-frame
  outcomes, missing cameras, collection/finish times and technical `CycleStatus`.

`captured_at` is finite, nonnegative source-clock metadata. It is not compared to
host time because camera clocks are not yet synchronized. `arrived_at` means host
admission time at `accept()`, not an independently measured transport timestamp.
The collector and inspector use the same monotonic time domain (seconds), defaulting
to `time.monotonic`. Inject a callable clock for tests; backwards/nonfinite readings
fail explicitly. Deadlines and durations must never use camera capture timestamps.

## Ownership, admission and bounds

`Frame` accepts nonempty uint8 H x W x 3, C-contiguous arrays that own their storage.
It retains that exact array and marks it read-only, without copying. The producer
transfers exclusive pixel ownership at construction and must not mutate pixels,
shape or flags through any alias while a frame/snapshot/inspection still uses it.
NumPy read-only flags do not revoke existing writable aliases. For a borrowed
camera buffer, view, or buffer that the producer must reuse immediately, explicitly
copy before constructing `Frame`. Unsupported dtype/shape/storage is rejected.
Different cameras may have different image dimensions; no resizing occurs.

`CycleCollector(expected_cameras, timeout=..., max_active=2, clock=...)` accepts
only explicitly opened cycles. `active_cycle_ids` is an ordered diagnostic view.
All calls belong to one owning context; no concurrent-call safety is claimed.

- At most `max_active * len(expected_cameras)` frames are retained internally.
  `begin()` raises `CycleCapacityError` without eviction or consuming the new ID.
- Unknown cameras raise `UnknownCameraError`. A second frame for a camera raises
  `DuplicateFrameError`, even if sequence differs; the first frame stays intact.
- Unopened/finalized cycles and operations after shutdown raise `CycleStateError`.
  Late input never opens a cycle or gets reassigned to the next one.
- Every terminal snapshot leaves the collector exactly once; the collector keeps
  no result queue or history. Snapshot/result ownership transfers to the caller.
  Consumers must bound any downstream queues/history; this layer does not bound
  caller-held objects, per-image byte sizes or future pipeline backlog.

Snapshots retain their source frames until released by the caller. Final inspection
results retain metadata and inference results, not source images. A producer may
reuse original storage only after all frame/snapshot users and active inspection
have finished/released it; there is no automatic buffer-pool return protocol.

## Completion, timeout and shutdown

`accept()` returns None until all cameras arrive, then one READY snapshot. Frames
and results always follow configured camera order, never physical arrival order.
Cycles may overlap and complete out of ID order, without mixing frames. `expire()`
and `shutdown()` return multiple snapshots in increasing cycle-ID order.

The collection interval is `[started_at, deadline)`. At the exact deadline, input
is rejected with `CycleDeadlineError`; it does not fill a missing slot. The cycle
remains active until `expire()`/`shutdown()` returns its timeout snapshot. There
is no silent timeout eviction during unrelated calls. Invoke `expire()` from the
owner's deadline/event handling; no internal sleep or scheduler exists. Even with
zero frames, expiration produces explicit missing-camera information. Observation
may happen after the deadline: `finalized_at` records observation, `deadline` the
cutoff. A source cannot backdate admission by supplying an old capture timestamp.

`shutdown()` closes admission and finalizes partial cycles once. Already-expired
cycles remain TIMED_OUT; other partial cycles become SHUTDOWN. Repeated shutdown
and expiration afterward return empty tuples. Snapshots already handed off are
outside the collector; the owner must separately finish/drain inspection work and
stop its worker. Shutdown does not cancel native calls or revoke external arrays.

## Inspection and technical results

`inspect_cycle(cycle, inspect, clock=...)` is synchronous. Run it outside the GUI
thread, using an engine in its existing owning context or an appropriate callable
that waits on the existing `InferenceWorker` from a non-GUI context. No new engine
or worker is constructed here. The [runtime coordinator](runtime-coordinator.md)
now uses the latter route with a dedicated serial inspector while its collection
owner stays responsive. This package remains synchronous; Qt cycle integration
is not implemented.

| CycleStatus | Meaning |
| --- | --- |
| COMPLETE | All expected frames received and all inspections succeeded |
| FAILED | Collection was complete; one or more inspections raised an Exception or returned a mismatched shape |
| TIMED_OUT | Missing camera frames at the collection deadline |
| SHUTDOWN | Collection stopped before all frames arrived and before its deadline |

Only READY snapshots invoke the callable, once per frame in configured order.
Ordinary exceptions become `InspectionFailure` with type, message and formatted
traceback/cause chain; later frames are still inspected, without retries. Exception
objects/traceback frames are not retained, avoiding hidden source-array references.
Process-control `BaseException` such as KeyboardInterrupt propagates; the caller
still owns cleanup. Invalid clock behavior is a caller error, not a defect result.

Incomplete snapshots produce uninspected entries for their received frames and
explicit `missing_cameras`; partial images are not inspected. On those entries,
result, failure and inspection start/finish times are None. Collection timeout does
not become an inference timeout: a READY snapshot remains ready if inspection
starts or ends later than its collection deadline. Timing fields are descriptive,
not benchmark measurements.

`InspectionCycleResult.is_complete` expresses technical completion. Returned
defects do not change COMPLETE to FAILED. There is no cycle OK/NG or production
acceptance rule; per-image `has_defects` keeps its existing meaning. Repeated calls
to `inspect_cycle` rerun inspection: exactly-once dispatch of each emitted snapshot
is the consuming owner's responsibility, not an unbounded deduplication cache.

## Small fake-source example

```python
import time

import numpy as np

from vimo_demo.inspection import CameraId, CycleCollector, CycleId, Frame, FrameId

cameras = tuple(CameraId(f"camera-{i}") for i in range(6))
collector = CycleCollector(cameras, timeout=1.0)
collector.begin(CycleId(1))
for sequence, camera in enumerate(reversed(cameras)):
    snapshot = collector.accept(Frame(
        FrameId(CycleId(1), camera, sequence),
        time.monotonic(),
        np.zeros((2, 3, 3), dtype=np.uint8),
    ))
    if snapshot is not None:
        # In a non-GUI owner, pass a fake inspector or an owned engine's infer:
        # result = inspect_cycle(snapshot, inspector)
        print(snapshot.cycle_id, snapshot.missing_cameras)
```

Focused verification (no SDK/GPU):

```powershell
uv run --no-sync pytest tests/unit/test_inspection_cycle.py tests/integration/test_cycle_worker.py --tb=short
```

Future hardware work must establish trigger-to-frame association, session/counter
rules, camera timestamp synchronization, transport arrival/drop behavior, deadline
values, buffer ownership and upstream backpressure. Algorithm/channel rules and
consumer output policy remain outside this phase. See [architecture](architecture.md).
