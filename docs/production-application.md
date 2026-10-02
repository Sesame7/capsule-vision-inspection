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
        traditional=channel_inspector,
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
serial cycle runner. For each camera it invokes enabled traditional checks using
`ChannelInspector.inspect(frame, resolved_channel, TraditionalCheck)` for presence,
blob and color. This passes exact ROI, thresholds, enabled and required policies.
No calibrated CV implementation is supplied. Missing implementations and ordinary
exceptions become explicit check ERROR; disabled checks are never called.

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
