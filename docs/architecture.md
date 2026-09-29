# Architecture and direction

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
Fake/local frames validate this layer; the Qt demo is still single-image and does
not orchestrate cycles. No production inspection pipeline/scheduler is provided.

The GUI observes readiness, results, exceptions and shutdown. It does not own
inspection decisions, and result availability does not wait for rendering.
Native calls cannot be forcibly cancelled: a blocked call can delay shutdown,
and a timeout retains ownership rather than claiming termination. This is an
integration demo, not a production workstation interface.

## Planned direction — not implemented

```text
camera adapters / simulation or folder sources
    -> owned frames with stable identity
    -> cycle collection
    -> inspection pipeline (traditional CV + ViMo)
    -> InspectionCycleResult
    -> communication/output adapter

local UI / future read-only web display <- application/runtime state and results
```

Frame/cycle types, collection and serial callable inspection are implemented;
`InspectionCycleResult` contains per-frame `ImageResult` outcomes and technical
status, not acceptance policy. Real camera acquisition, trigger association,
application scheduling, algorithm/channel rules, PLC and web modules are future
work. The diagram describes their intended composition, not existing wiring.

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
| [Inspection cycle](inspection-cycle.md) | Identity, owned frames, bounded collection and technical cycle results |
| [Qt integration](qt-integration.md) | GUI-thread delivery, object lifetime and deferred close |
