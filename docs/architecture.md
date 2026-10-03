# Architecture and direction

## Checkpoint status

| Status | Phase / responsibility |
| --- | --- |
| Implemented | 1A — typed inference core and SDK boundary |
| Implemented | 1B — serial worker and Qt integration demo |
| Implemented | 1C — inspection-cycle collection and technical results |
| Implemented | 1D — bounded runtime coordinator |
| Implemented | 2A — production inspection domain and result model |
| Implemented | 2B — Recipe/model/layout persistence and pure configuration resolution |
| Implemented | 2C — production application state and configured runtime composition |
| Implemented | 2D-1 — PySide Production shell, bounded display frames and simulated source |
| Design reference | Production UI direction; physical workstation tuning remains |
| Next, not implemented | 2D-2 — remaining production UI work |
| Future | 2E — Archive, events and diagnostics |
| Future | 3 — Camera, PLC and field integration |

Phase 2C composes existing layers: candidate activation after model warm-up,
fresh runtime sessions, factual cycle/channel counters and heterogeneous checks.
Recipe/model changes require stopped inspection; no running-engine hot replacement.

## Currently implemented

The Qt demo runs in one process. Its application entry supplies an engine factory;
the existing Python worker owns engine construction, synchronous calls and cleanup.

```text
GUI thread:     DemoWindow -> QtInferenceBridge
                                   |
                              InferenceWorker
                                   |
Worker thread:            VimoSegmentationEngine -> SmartMore SDK
                                   |
Result path:        ImageResult -> Future -> queued Qt signal -> GUI slot
```

Dependencies flow **ui -> runtime -> inference**. Higher layers may also import
lower-layer result types or supply the engine factory; no reverse dependency is
allowed. Runtime and inference are independent of Qt. Direct SDK objects stay
inside the inference adapter; callers receive our typed `ImageResult`.

One worker means serial, single-image inference with a bounded pending queue.
It has one lifecycle and is not restarted. The CLI tools can own an engine
directly in a single serial context without running Qt or a worker.

The independent `inspection/` layer now collects explicitly identified frames
into bounded cycles and converts snapshots to `InspectionCycleResult` through an
injected serial inspection callable. It depends on inference result models, not
Qt, SDK objects or camera/PLC libraries. Its [cycle contract](inspection-cycle.md)
covers duplicate/late frames, missing cameras, deadlines and technical failures.
The default scenario is six cameras; camera count/order are configured data.
The pure-Python `RuntimeCoordinator` now owns collection on one serialized thread
and dispatches READY snapshots through a bounded queue to a serial cycle inspector.
That inspector calls `inspect_cycle()` using the existing worker's Futures; the
worker remains the sole engine owner. Collection stays responsive while inference
runs. A cycle Future returns a technical result with runtime timing, or an explicit
runtime error. See the [coordinator contract](runtime-coordinator.md) for admission,
explicit deadline ticks, overload and shutdown.

```text
simulated sources -> bounded commands -> CycleCoordinator / CycleCollector
                                              |
                                       bounded READY queue
                                              |
                                       CycleInspector / inspect_cycle
                                              |
                                       InferenceWorker -> engine
                                              |
                         InspectionCycleResult + timing -> cycle Future consumer
```

Fake/local frames validate overlapping cycles; one opt-in six-frame GPU test checks
real wiring. The Qt demo remains single-image and does not orchestrate cycles or
share a worker with a coordinator. No real camera/PLC composition is implemented.

Phase 2A adds `inspection.production`: explicit camera/slot/channel layouts,
lightweight ViMo findings, a synchronous traditional-inspector Protocol and
`ProductionInspectionResult`. It consumes existing cycle results plus independent
traditional evidence; no worker, coordinator or lifecycle contract changes.
ROI overlap maps full-image findings to every affected channel and preserves
unassigned camera-level defects. Technical execution and product judgment remain
independent, including ERROR + NG. See the
[production inspection contract](production-inspection.md) for the initial rules,
ownership and timing. Phase 2C schedules configured traditional checks; calibrated
production algorithms and thresholds remain hardware/commissioning work.

Phase 2B adds the pure `configuration/` layer above `inspection.production`:
separate model metadata, machine layout and product Recipes resolve once into
immutable per-channel configuration. It reuses existing layout/identity/policy
types and provides strict JSON persistence and optional artifact verification.
It owns no engine, active configuration or runtime lifecycle. See the
[configuration contract](inspection-configuration.md) for inheritance, ROI
overrides, explicit label filtering and the pipeline-composition boundary.

`application/` is the Qt-independent owner above configuration and runtime.
`ProductionApplication` has one lifecycle thread, immutable snapshots and bounded
outstanding cycle receipts. Each Start creates a fresh `ProductionRuntimeSession`;
it uses the existing coordinator and worker, with engine warm-up on the worker.
`ConfiguredProductionPipeline` runs resolved checks on the existing cycle runner,
reuses Phase 2A assignment/aggregation and publishes lightweight production results.
No engine ownership moves to application/Qt. See the
[application contract](production-application.md) for transactional activation,
state transitions, exact counter semantics, explicit Batch resets and shutdown.

```text
Production UI/source -> ProductionApplication -> ProductionRuntimeSession
                             |                       |
                    snapshots/counters        RuntimeCoordinator
                                                     |
                                      ConfiguredProductionPipeline
                                        |                     |
                                injected checks       InferenceWorker -> engine
                                        +----------+----------+
                                         ProductionInspectionResult
```

The GUI observes readiness, results, exceptions and shutdown. It does not own
inspection decisions, and result availability does not wait for rendering.
Native calls cannot be forcibly cancelled: a blocked call can delay shutdown,
and a timeout retains ownership rather than claiming termination.

Phase 2D-1 adds a separate six-camera Production shell:

```text
ProductionApplication -> snapshot / command Future -> QtProductionBridge -> widgets
accepted source Frame -> bounded LatestDisplayFrameStore -> CameraCard
```

The bridge polls changed snapshots/display revisions; lifecycle Futures cross
queued Qt signals. The independent latest-frame store owns immutable display
copies. Generation/cycle/frame/shape matching prevents stale overlays. Widgets
do not control runtime internals or duplicate judgments/counters. The same six
cards rearrange between portrait and landscape; other pages are placeholders.
The launcher uses real application/runtime composition with a fake engine and
synthetic inputs. See the [Production UI contract](production-ui.md) for ownership,
nonblocking shutdown, localization, layout and physical-screen limitations.

## Planned direction — not implemented

```text
camera adapters / simulation or folder sources
    -> owned frames with stable identity
    -> cycle collection
    -> inspection pipeline (calibrated traditional CV + ViMo)
    -> ProductionInspectionResult
    -> communication/output adapter

local UI / future read-only web display <- application/runtime state and results
```

Frame/cycle types, collection, runtime coordination and serial inspection are
implemented. `InspectionCycleResult` retains per-frame `ImageResult` outcomes and
technical status; the additive production layer derives channel judgments and
lightweight evidence without retaining images/masks. Real camera acquisition,
trigger association, hardware deadline-event integration, calibrated algorithms,
PLC and web modules are future work. Recipe activation and simulated configured
runtime execution are implemented; the diagram describes future hardware wiring.

- Isolate camera SDKs behind adapters; support simulation/folder sources for
  equipment-free tests. Frames need owned pixels and stable identity.
- Keep inspection/channel rules independent of UI and communication. Represent
  channel count through configuration/data rather than duplicated implementations.
- Compose traditional CV and ViMo inside inspection logic. PLC/communication
  consumes an inspection result through an adapter and never waits for UI rendering.
- Keep desktop display local without unnecessary HTTP/base64 transport. A future
  read-only web display consumes state; it does not own inspection execution.
- Choose GPU concurrency only after measurement on the target workload. Real
  camera timing, dropped-frame behavior and field throughput require hardware tests.

Historical SDK/regression checks establish integration behavior, not model
accuracy, annotation quality or production acceptance thresholds.

## Documentation map

| Document | Responsibility |
| --- | --- |
| [README](../README.md) | Setup, usage, maintained tools and validation commands |
| [AGENTS](../AGENTS.md) | Durable engineering rules and change-validation policy |
| [Phase 0 findings](phase0-findings.md) | Frozen experimental evidence and golden provenance |
| [Inference core](inference-core.md) | Engine/input/result contract and SDK boundary |
| [Runtime worker](runtime-worker.md) | Ownership, queue, Futures, lifecycle and failure |
| [Runtime coordinator](runtime-coordinator.md) | Serialized collection, bounded cycle dispatch, result Futures and coordinated shutdown |
| [Inspection cycle](inspection-cycle.md) | Identity, owned frames, bounded collection and technical cycle results |
| [Production inspection](production-inspection.md) | Channel layout, ROI evidence assignment, traditional boundary and independent technical/product judgments |
| [Inspection configuration](inspection-configuration.md) | Separate Recipes/models/layout, strict JSON, deterministic inheritance and immutable per-channel policies |
| [Production application](production-application.md) | Transactional activation, warm-up, runtime sessions, configured execution, factual counters and Batch lifecycle |
| [Qt integration](qt-integration.md) | GUI-thread delivery, object lifetime and deferred close |
| [Production UI](production-ui.md) | Implemented shell/bridge, display ownership, identity matching, controls and review |
| [Production UI direction](production-ui-direction.md) | Agreed visual direction and flexible review details; not an exact implementation specification |
