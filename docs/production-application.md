# Production application — Phase 2C

`vimo_demo.application` composes the existing runtime and resolved configuration.
It has no Qt, camera, PLC, archive or SDK-object dependency. Software Stop is
graceful inspection stop, **not machine safety or emergency-stop logic**.

## Ownership and API

```text
ProductionApplication (one lifecycle thread; locked commands/snapshots)
  -> fresh ProductionRuntimeSession per Start
    -> RuntimeCoordinator (collection owner + serial cycle runner)
      -> InferenceWorker (only engine owner)
        -> VimoSegmentationEngine
```

Configuration parsing/resolution and `prepare_configuration(resolved,
base_directory=...)` are blocking preparation work; perform them outside a future
GUI thread. Preparation verifies the artifact. Startup verifies the resolved
artifact again before engine construction; neither check is a filesystem lease.
Keep local model files immutable while in use.

Composition example (the caller supplies prepared configuration and owned BGR
warm-up arrays; no implicit machine path or synthetic production frame):

```python
from vimo_demo.application import ProductionApplication, ProductionRuntimeSession

app = ProductionApplication(
    lambda prepared, generation: ProductionRuntimeSession(
        prepared, generation,
        warmup=lambda _: warmup_images,
    )
)
app.configure(prepared)
app.start_batch("batch-001")  # Optional; otherwise batch_id is None.
started = app.start()        # Future[None]; no model work on this caller.
# After started succeeds:
# cycle = app.begin_cycle(cycle_id)
# cycle.accepted: collector admission, not completion or pixel-buffer return.
# app.submit_frame(frame): admission acknowledgement, same ownership distinction.
# cycle.result: Future[ProductionInspectionResult].
# app.expire(): explicit monotonic deadline observation, owned by the source loop.
# stopped = app.stop()
# closed = app.close()
# Outside GUI/callback threads: closed.result(); assert app.join(timeout).
```

`RuntimeSession` is a small injectable Protocol for deterministic tests, not a
plugin framework. Factories must return an unstarted session or raise without
retaining resources. Every Start uses a fresh generation, coordinator and worker.
All engine construction, warm-up, inference and close occur on the worker thread.
The application lifecycle thread waits for runtime readiness/termination; callers
do not. Future callbacks must be short and nonblocking, must not wait/join these
owners, and must not modify widgets. Future handles are observations, not promises
callers may settle or cancel. Exceptions retain their causes; immutable snapshots
retain only compact issue type/message data, never exception tracebacks.

## Local image-set ingress — Phase 3A

`LocalCycleIngress` is a small synchronous admission helper in `application/`.
It starts no thread, queue, source plugin or lifecycle. Supply the configuration
layout used by the running session and one ordered set of decoded BGR arrays:

```python
from vimo_demo.application import LocalCycleIngress

# After app.start() succeeds; non-GUI source context only.
ingress = LocalCycleIngress(app, prepared.configuration.layout)
cycle = ingress.submit(images_bgr)  # One array per configured camera, in layout order.
# The external arrays can now be reused, even while inference is still running.
result = cycle.result.result()     # Existing ProductionInspectionResult, no wrapper.
```

Create one ingress per runtime session and use it serially as that session's sole
cycle-ID source. Each valid iteration gets local `CycleId(1), (2), ...`; admission
rejections can leave gaps and never trigger retries. This identity is correlation
only, not a physical-product/PLC counter or persistent ID. Invalid input detected
before begin consumes no ID and creates no partial cycle. Camera count/order comes
from the supplied layout (six for this workstation), not hard-coded camera paths.
`sequence=0` and local `monotonic()` capture metadata do not determine membership
or collector deadlines. The caller decodes local files; ingress never changes BGR
meaning, dtype, dimensions or orientation.

Before beginning a cycle, all arrays are checked and copied into independently
owned C-contiguous uint8 H×W×3 storage, then wrapped in the existing `Frame` type.
Even already-owned arrays are copied because external aliases/reuse are allowed.
Keep source arrays and the input sequence stable during `submit()`. No leases or
zero-copy path are introduced. Only the current input set is held locally; any
admitted frames/results follow existing bounded application/runtime ownership.

`submit()` waits for the existing cycle-admission Future and each frame-admission
acknowledgement, then returns the existing `ProductionCycle`. It does not wait for
inspection; observe `cycle.result` for the result or an explicit terminal error.
Neither these waits nor waiting for results belongs on the GUI or runtime callback
thread. Native inference still executes exclusively on the existing worker.

Input errors and synchronous/asynchronous admission errors propagate unchanged.
For example, READY-backlog overflow remains an exception on `cycle.result`, never
a manufactured product judgment. Submission is not transactional: if a later frame
is rejected, already accepted frames/cycle remain application-owned. The caller
must use existing expiration or Stop/Close cleanup, which may produce a technical
incomplete result. The helper does not silently rollback, retry, stop the machine,
schedule deadlines or redefine failure semantics.

Ingress binds the current running generation and rejects later use while stopped
or after restart. Stop/drain the source before restarting/replacing the session;
construct a new ingress afterward. Application begin/frame commands do not accept
an atomic generation token, so snapshot checks are **not** protection against a
concurrent Stop/Start between checking and submission. This phase keeps that
existing single-source/lifecycle coordination requirement rather than redesigning
application admission. The existing UI simulation is unchanged.

Focused equipment-independent verification:

```powershell
uv run --no-sync pytest tests/integration/test_local_ingress.py --tb=short
```

## State and transactional activation

| From | Action/outcome | To |
| --- | --- | --- |
| UNCONFIGURED | Install prepared candidate | READY |
| READY | Replace candidate / explicitly start batch | READY |
| READY | Start | STARTING |
| STARTING | Model + warm-up ready, no stop requested | RUNNING |
| STARTING | Startup fails, cleanup succeeds | READY |
| STARTING / RUNNING | Stop | STOPPING |
| STOPPING | Actual runtime termination and safe release | READY |
| STARTING / RUNNING / STOPPING | Lifecycle integrity/cleanup failure | FAULT |
| FAULT | Explicit reset, ownership safely released | READY (or UNCONFIGURED without configuration) |
| Any nonterminal state | Close and safe runtime termination | CLOSED |

Invalid commands raise typed `ApplicationStateError`. CLOSED cannot restart.
The prepared candidate and last-known-good active configuration are separate.
Only successful startup promotes the candidate. Failure keeps the previous active
identity and exposes a startup issue; first startup may have no active identity.
Configuration changes are allowed only in UNCONFIGURED/READY. There is no hot
replacement. Start clears the previous latest result, but never resets counters.

Warm-up defaults to **two serial calls per distinct input shape per session**;
`warmup_iterations` is an explicit positive setting. The injected provider runs
on the worker and supplies nonempty uint8 H×W×3 BGR arrays. Duplicate shapes are
skipped; empty/invalid inputs or result-shape mismatch fail startup. Provider
pixels/aliases must remain immutable until startup finishes. No resize, 36-channel
warm-up, CycleId, production result or counter increment is performed. Stop during
warm-up allows the active native call to return, skips remaining iterations and
prevents activation. A blocked native call remains observable as STARTING/STOPPING;
an observer's Future/join timeout never fabricates successful cleanup.

Stop immediately revokes new production admission. Existing coordinator policy
finishes the active whole cycle, finalizes incomplete cycles as SHUTDOWN and rejects
queued READY cycles not yet started. Stop completion means all session threads
have joined and ownership is released. Repeated pending stops share completion.
Close also stops the application lifecycle thread; `join()` confirms its exit
after the close notification (callbacks still run on that exiting thread).
Uncertain engine cleanup retains the session in FAULT; reset/close cannot discard
it. An explicit later cleanup attempt is allowed, never automatic recovery.

A normal per-image/check Exception becomes technical result evidence, not global
FAULT. Worker termination, invariant failure or unsafe close enters FAULT and
revokes admission. Reset requires no live/unsafe session and no pending cycles;
it does not Start, reset counters or manufacture a recovered runtime.

## Snapshots, counters and bounded completion

`ProductionApplicationSnapshot` contains state/generation, candidate and active
recipe/model/hash identities, optional batch ID, frozen counters, latest lightweight
production result, `runtime_owned` and last structured issue. State + ownership +
issue describe runtime health; there is no event/result history or image/mask cache.

| Counter | Exact meaning |
| --- | --- |
| cycles_started | Collector accepted the cycle, not merely queued its begin command |
| cycles_completed | A formal production result was received and accounted once |
| channel_ok / channel_ng / channel_unknown | Completed non-disabled channel judgments |
| channel_disabled | Completed channel has TechnicalStatus.DISABLED, counted first |
| production_ng_cycles | Completed result judgment is NG, including unassigned evidence |
| technical_error_cycles | Completed result technical status is ERROR |

The four channel counts sum to configured channels per completed cycle (36 for
the current layout). ERROR + NG increments channel_ng only, and both cycle-level
NG/error counters. A cycle NG caused solely by unassigned findings adds no invented
channel NG. Timeouts/SHUTDOWN with formal results count as completed; runtime
exceptions/rejected work do not. Rejected collector admission does not count as
started. Thus started and completed can legitimately differ after overload/stop.
None of these counters counts physical capsules, products or valid PLC triggers.

Only `start_batch(nonempty_id_or_None)` in READY resets counters and latest result.
Start/Stop, fault reset and Recipe/model changes preserve counters. Batch ID stays
None until explicitly supplied; timestamps never invent batch identity.

Each outstanding cycle has a generation and unique token. Locked admission/result
bookkeeping admits/counts once, removes the token at terminal completion and ignores
duplicate or stale callbacks. Old-session health callbacks also check session
identity. No unbounded deduplication history is retained. Application outstanding
capacity defaults to 8; runtime defaults remain 2 collecting, 2 queued READY and
32 commands, with one worker task at a time. Overflow is explicit, never a fake
inspection result or silent eviction. Callers must observe exceptional Futures.

## Configured execution

`ConfiguredProductionPipeline` consumes already resolved per-channel configuration;
there is no JSON or inheritance resolution in the hot path. It runs on the existing
serial cycle runner. Product-level [capsule settings](capsule-inspection.md) directly
control the sole traditional path: both disabled skips measurement; either enabled
runs one shared Size/Color measurement per channel. Valid content that is missing,
competing, truncated or outside limits produces NG; execution failure produces
ERROR/UNKNOWN. Incomplete cycles run no measurement. No legacy dispatch/injected
inspector exists, and no extra scheduling thread is introduced.

ViMo runs once on the full camera image if any channel enables it, otherwise zero
times. It never performs per-channel native inference. Phase 2A bbox assignment is
reused. Each channel then filters assigned findings with its resolved label policy;
ignored evidence is omitted without mutating ImageResult, regions or mask. An
unassigned finding remains camera-level NG if any ViMo-enabled channel on that
camera accepts its label; otherwise it is filtered. No fabricated channel exists.

Inference failure reaches each enabled channel using that channel's `required`
policy. Disabled checks stay disabled; optional failure does not invalidate another
successful required result. Raw CycleStatus.FAILED remains visible. The additive
`ProductionInspectionResult.frame_failures_in_checks=True` tells aggregation that
configured check policies already account for those frame errors. The default
False preserves Phase 2A unconditional FAILED-cycle ERROR. TIMED_OUT/SHUTDOWN always
remain technical errors. ERROR + NG and ROI overlap semantics are unchanged.

Incomplete cycles execute no traditional or native inference. Their missing/error
evidence is still assembled deterministically. Production records contain compact
evidence/measurements, not arrays or masks. Stage timings describe traditional,
worker inference and mapping/assembly work; no performance conclusions are drawn.

## Verification and boundaries

```powershell
uv run --no-sync pytest tests/unit/test_production_application.py tests/unit/test_configured_execution.py tests/integration/test_application_runtime.py --tb=short
uv run --no-sync pytest tests/integration/test_application_vimo.py --run-gpu --require-gpu -s --tb=short
```

Tests use controlled Futures/events and real coordinator/worker composition with
fake engines. The optional GPU check only loads, warms twice and stops the verified
model; no production counters, full regression or soak run. Existing Phase 1/2A/2B
tests remain part of normal gates. Production UI is Phase 2D; archive/events/history
are Phase 2E. Camera/PLC trigger gating, physical counting, buffer leases, calibrated
ROIs/CV, machine handling of unassigned findings and field throughput/FAT/SAT require
Phase 3 hardware knowledge. This application state is not a machine safety system.

## Machine geometry preparation

MachineLayout is the sole channel-ROI source. Machine Save only persists geometry;
explicit Apply resolves the selected saved Recipe with that saved layout and
prepares a new immutable candidate. No running configuration is mutated, and
successful Start remains the activation boundary. Geometry changes do not reset
counters or create a new Recipe identity. Snapshot IDs are not geometry revisions.
The UI drops previously prepared choices with obsolete layouts after Apply;
other Recipes can be selected and prepared again against the current machine.
