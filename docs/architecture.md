# Architecture

## Purpose and scope

This overview connects the maintained contracts of the local ViMo capsule-inspection
application. Detailed behavior and verification remain in the owning domain pages.

**Current** means implemented behavior supported by the code and existing tests.
**Target / Not implemented** means deferred direction with unresolved decisions.
**Historical** means dated integration or measurement evidence, not a continuing
performance, accuracy or machine-safety guarantee.

Current Production uses six ordered cameras with six channels each (36 channels).
Their identities/geometry are configuration, not an immutable hardware architecture.
Inputs are local; real camera acquisition and commissioned Production PLC integration
are unimplemented. A FINS/UDP subset and localhost simulator support manual operations
and opt-in automatic first-station reporting through normal startup's Communication
page or explicit developer composition. Reporting preferences are memory-only.

## Current capabilities

| Capability | Implemented scope |
| --- | --- |
| Configuration | Strict Model, Recipe, MachineLayout and camera-source definitions; atomic file persistence, pure resolution and separate artifact verification. Saved MachineLayout owns ROI geometry. |
| Inference | Typed synchronous ViMo on validated uint8 BGR arrays, serial ownership, preserved geometry/raw mask IDs and owned outputs without vendor objects. |
| Collection and runtime | Explicit frame/cycle identity, ordered collection, bounded admission and READY inspection backlog, terminal results/errors and observable shutdown. |
| Inspection | Full-camera ViMo evidence assigned to configured channel ROIs, always-on scalar Occupancy with explicit EMPTY traditional skips, selective Size/Color checks, and independent technical status and product judgment. |
| Application | Stopped candidate preparation, successful-Start activation, readiness warm-up, disposable runtime sessions, bounded observations and separate factual judgment/business-occurrence accounting. |
| Local sources | Owned image-set ingress, independent per-camera Folder replay (application-lifetime RAM dataset for saved Loop sources), explicit fixed images, and session-local Pause/Continue/Step for supported Folder sources in normal startup and opt-in developer composition. |
| Qt shell and editing | Chinese/English Production, Recipe, Vision and Communication pages, including session-local automatic-reporting controls/observations; identity-matched display, saved drafts, isolated tuning/reference preview and nonblocking close. History remains a placeholder. |
| Engineering verification | Unit and opt-in GPU regression, fake UI review, wrapper/Production measurement and profiling tools, with distinct measurement boundaries. |
| Communication | Qt-independent FINS/UDP DM/WR words, first-station projection/two-write submission, localhost simulator, independent manual operations/register watches and opt-in bounded local Production reporting; no commissioned hardware. |
| Web observation | Optional read-only snapshot service, default loopback with explicit assigned IPv4 binding on a trusted test LAN; independent publisher encodes bounded latest-camera JPEGs, serial HTTP serves cached bytes and a minimal responsive observer. No runtime commands, authentication or HTTPS. |

Size/Color is uncalibrated. ViMo `has_defects` describes returned regions, separately
from configured channel judgment. SDK/golden regression verifies integration,
not model accuracy or physical product acceptance.

### Entry points

These workflows coexist; they are not interchangeable startup recovery paths.

| Entry | Current purpose |
| --- | --- |
| [`app.py`](../app.py) | Normal real-inference Production shell; discovers strict persistent configuration under entry-relative `data/` and prepares a stopped candidate. |
| [`scripts/run_local_production_ui.py`](../scripts/run_local_production_ui.py) | Explicit-path real Production composition, using saved Folder sources or process-local fixed images; does not restore/write manifest preferences. |
| [`scripts/run_production_ui.py`](../scripts/run_production_ui.py) | Equipment-free Production review with a fake engine, including when real local image pixels are supplied. |
| [`python -m vimo_demo.ui.app`](../src/vimo_demo/ui/app.py) | Separate one-image engine/worker/Qt diagnostic; does not own Production cycles, configuration activation or accounting. |

[Application entry points](application.md#developer-entry-points) own commands, path
bases and prerequisites. Invalid normal-startup configuration leaves the shell
UNCONFIGURED, without first-file selection, fake fallback or invented geometry.
A stopped candidate is not an active inference session.

## Responsibilities and dependencies

| Domain | Responsibility and boundary |
| --- | --- |
| [Configuration](configuration.md) | Definitions, persistence and immutable inspection resolution; no engine, activation or runtime state. |
| [Inspection](inspection.md) | Identity, owned frames, synchronous collection, evidence and technical/product judgment; no source or scheduling ownership. |
| [Inference and runtime](inference-runtime.md) | SDK boundary, typed results, worker engine ownership, serialized collection, bounded dispatch and terminal delivery. |
| [Application](application.md) | Candidate/active configuration, readiness, sessions/source composition, admission, factual accounting and coordinated shutdown. |
| [UI shell](ui-shell.md) | Application commands, nonblocking notifications, localized presentation and independently owned display pixels. |
| [UI configuration](ui-configuration.md) | Recipe/Vision drafts, reference/tuning previews and persistence/preparation workflows, under configuration/application authority. |
| [Performance tooling](inspection-performance.md) | Workloads, timing scopes, reports and interpretation; no runtime admission policy or machine-capacity specification. |
| [Communication](communication.md) | Protocol/socket ownership, business-status projection, per-generation bounded reporting execution and loopback simulation; no judgment changes or Qt dependency. Application/session composition owns reporting lifecycle. |

Presentation/composition depends on the Qt-independent application, configuration,
inspection, runtime and inference domains, never the reverse. Configuration resolves
inspection types; inspection uses typed inference results; runtime dispatches through
its worker. This is not one exhaustive linear import chain. Only the inference
adapter imports SmartMore and exposes no vendor objects to callers.

The following distinctions remain architectural constraints for Current changes:

- **Resolution, preparation, activation:** pure resolution creates no session.
  Saving/preparing/installing a stopped candidate is not activation; successful
  Start/readiness promotes it. Failure preserves last-known-good active configuration.
- **Engine ownership:** the worker factory constructs, infers and cleans up in one
  serial context. The coordinator owns that worker; the one-image diagnostic owns
  another. Workers are not restarted; CLI tools may own an engine directly.
  No SDK thread-safety claim is made.
- **Identity/pixels:** explicit cycle membership and configured camera order do not
  depend on arrival proximity. Owned Frames/borrowed aliases stay stable while used;
  admission acknowledgement is neither terminal completion nor pixel-buffer return.
- **Admission/delivery:** admission acknowledgements, task Futures and terminal cycle
  results are distinct. Queues/observations are bounded; admitted cycles have one
  terminal result or explicit error, without silent loss or completed-result history.
  A permanently blocked native call or completion callback can delay delivery.
- **Failures:** frame/check technical errors, product NG/UNKNOWN and application FAULT
  are independent; ERROR + NG is valid. Ordinary failed checks can complete normally
  in RUNNING, unlike infrastructure/invariant failures.
  Derived channel business status uses NG → valid EMPTY → PRESENT/SUCCESS/OK →
  UNKNOWN without replacing those axes; it does not define PLC/reject behavior.
- **Display:** rendering does not complete inspection/accounting. Raw results may own
  masks; compact production results retain no images/masks/contour arrays. Separate
  bounded latest-per-camera copies require generation, cycle, full FrameId and
  dimension matching for overlays; they do not shorten original frame lifetimes.
- **Shutdown:** stop admission, finalize incomplete cycles, reject unclaimed work
  explicitly and let active native work finish. Stop notification/timeout does not
  prove termination. Owners remain until threads actually exit; Qt never waits on
  unfinished work.

Precise borrowing, failure, cancellation, timeout and cleanup guarantees belong to
[inference/runtime](inference-runtime.md), [inspection](inspection.md),
[application](application.md) and [UI shell](ui-shell.md).

## Current execution and data flow

The diagram shows the implemented local Production path, not a real camera/PLC path.
Its branches describe responsibilities, not parallel GPU execution.

```text
Persisted Model / Recipe / MachineLayout / camera sources
    -> strict validation / pure resolution / artifact verification
    -> prepared stopped candidate
    -> Start: fresh runtime session + worker readiness warm-up
    -> active configuration / RUNNING

Local Folder replay or explicit fixed image inputs
    -> LocalCycleIngress: owned Frames with CycleId / CameraId / FrameId
    -> ProductionApplication admission
    -> RuntimeCoordinator / CycleCollector
    -> READY CollectedCycle -> configured serial inspection
         -> per-channel Occupancy -> enabled Size/Color except confirmed EMPTY
         -> enabled full-camera ViMo: InferenceWorker -> engine -> ImageResult
         -> configured channel evidence / technical and product judgments
    -> InspectionCycleResult + ProductionInspectionResult + timing
    -> application terminal result / factual accounting / bounded observations
    -> Qt snapshot and completed-Future notifications -> Production presentation

Eligible terminal Production result (explicit local opt-in only)
    -> scalar generation/CycleId + 36 channel statuses
    -> bounded serial first-station report owner -> localhost FINS simulator
    -> independent compact ACK/failure/skip/rejection observations

Identified source Frames
    -> separate latest-per-camera display copies
    -> Qt image presentation / identity-matched result overlays

Optional normal-process Web observation
    -> existing application + display snapshots (not atomic acquisition)
    -> independent latest-only JPEG publisher
    -> bounded read-only loopback / explicit trusted-LAN HTTP cache delivery
    -> responsive browser observation, not inspection completion or control
```

The collector remains responsive during serial inspection in configured camera
order. Occupancy evaluates every valid channel; Size/Color runs where enabled and
not confirmed EMPTY. ViMo runs at most once per full camera
frame when needed, then channel policy/ROI assignment consumes its evidence.
Incomplete collection finalizes technically without running algorithms.

Explicit runtime expiration observes host-monotonic deadlines, without autonomous
coordinator scheduling or captured-timestamp/GUI deadlines. Overload is an explicit
admission/terminal error, not model OK/NG. Collection completion order may differ
from cycle-ID order; identity and per-cycle camera ordering remain deterministic.

Readiness warm-up produces no production data or counters. Accounting measures
admitted cycles/channel judgments, not capsules; only explicit Batch resets it,
not Stop/Start or Recipe changes. The local source waits for RUNNING and each cycle
before advancing. Overlapping-collection support is not physical-throughput evidence.

Normal startup carries the validated Folder composition's optional replay command
capability in `Startup.local_replay` and passes it to the existing Qt bridge. The
developer launcher opts into the same source gate. Control presentation follows
that capability, not the entry-point name; missing/unsupported sources and generic
compositions retain generic operations. No additional controller or runtime is
created by this wiring.

These local sources can gate subsequent groups with Pause/Continue/Step while the
application remains RUNNING. This creates no general Production PAUSED
state, additional inference owner or real-camera semantics. The
[application contract](application.md#local-replay-controls) owns group claiming,
formal-result observation, cursor/pacing and genuine shutdown; Qt only commands
and presents that optional boundary. Persistent source/configuration selection and
successful-Start activation remain unchanged.

Optional reporting performs no network work on the inference, completion-callback
or Qt threads. Ordinary communication errors do not change inspection/accounting;
backlog rejection explicitly requests Application Stop. Session release additionally
requires reporting-thread exit and safe socket cleanup. The
[communication contract](communication.md#local-production-reporting) owns sequence,
eligibility and shutdown details; the manual workspace remains independent.
Normal composition captures the selected immutable reporting settings at Start;
the Communication page only commands/presents that optional boundary through the
existing Qt bridge. Its manual dispatch protects W49–W52 until the reporting owner
exits, without blocking reads or changing the low-level client API.

## Implementation navigation

These locations explain the Current code. They do not mandate that Target
packages, classes, composition hooks or thread topology retain this arrangement.

| Location | Navigation |
| --- | --- |
| [`configuration/`](../src/vimo_demo/configuration/) | Definition models, pure resolution, strict persistence and camera sources. |
| [`inspection/`](../src/vimo_demo/inspection/) | `Frame`, `CycleCollector`, `CollectedCycle`, `inspect_cycle()` and technical results; [`production/`](../src/vimo_demo/inspection/production/) holds layout, evidence/judgment and capsule screening. |
| [`inference/`](../src/vimo_demo/inference/) | Public `VimoSegmentationEngine` and result types; [adapter](../src/vimo_demo/inference/vimo_adapter.py) and model staging stay behind that API. |
| [`runtime/`](../src/vimo_demo/runtime/) | `InferenceWorker` and `RuntimeCoordinator`; sole collector owner, serial cycle runner and worker engine owner. |
| [`application/`](../src/vimo_demo/application/) | `ProductionApplication`, `ProductionRuntimeSession`, `ConfiguredProductionPipeline`, `LocalCycleIngress` and `FolderReplay`. |
| [`ui/`](../src/vimo_demo/ui/) | Shell, bridges, display copies and configuration/preview widgets; [bootstrap](../src/vimo_demo/ui/bootstrap.py) and [local composition](../src/vimo_demo/ui/local_production.py) host Current desktop startup/source wiring. |
| [`communication/`](../src/vimo_demo/communication/) | Typed FINS/UDP DM/WR client, first-station projection/submission, optional local reporting owner and finite loopback simulator. |
| [`web/`](../src/vimo_demo/web/) | Optional snapshot publisher, serial HTTP cache delivery and read-only browser assets; normal bootstrap owns its process lifetime. |
| [`tests/`](../tests/) and [`scripts/`](../scripts/) | Maintained verification, optional real regression, CLI tools and fake UI review. |

## Target / Not implemented

These directions need separate design/validation. No frameworks, protocols,
adapter designs or scheduling structures are selected here.

| Direction | Unresolved design or validation boundary |
| --- | --- |
| Real cameras/adapters | Hardware/SDK choice, acquisition mode and failure behavior; local inputs do not implement cameras. |
| Trigger/cycle association | Explicit hardware/frame association, buffer leases, captured/arrival clocks and synchronization. |
| Hardware capacity/deadlines/overload | Measured limits, deadline events and machine responses; software bounds/pacing do not establish field capacity. |
| PLC integration and counting | Actual equipment/configuration, output association, delivery acknowledgement and physical-product accounting; the standalone FINS/UDP subset does not implement these or a capsule counter. |
| Machine stopping/commissioning | Safety, emergency-stop responsibilities and hardware recovery; graceful software Stop/close is not machine safety. |
| Calibrated inspection/acceptance | Lighting/geometry, calibration, thresholds, annotations and field acceptance criteria remain unverified. |
| History/archive/remote deployment | Retention, result/image delivery, authentication/HTTPS and commissioned network access remain unimplemented. Current trusted-test-LAN Web observation is best-effort and provides neither control nor reliable result delivery. |
| Performance/concurrency | Measure target workloads and ownership/hardware behavior before choosing concurrency; Current serial execution is not a permanent mandate. |

Future architecture may differ substantially from today's internal topology.
An intentional change to an existing external behavior contract still requires
separate design approval and suitable validation.

**Historical evidence** remains in [validation evidence](archive/validation-evidence.md), the tracked
[golden fixture](../tests/reference/phase0.json) and
[performance records](archive/inspection-performance-records.md). Integration,
stability and software P95 do not establish accuracy or machine throughput.
Ignored `outputs/` may contain protected evidence; it and persistent `data/`, local
models/images are outside this migration.

The archives preserve dated evidence separately from Current contracts. The
original documents remain recoverable at Git checkpoint
`8fad326aaaf5e0e981224963607a7e000eb95604`; local evidence and assets stay in place.

## Documentation map

| Maintained document | Authority |
| --- | --- |
| [Architecture](architecture.md) | Current capability/boundary overview, implementation navigation and deferred Target direction. |
| [Configuration](configuration.md) | Definition schemas, machine/source ownership, resolution, persistence and artifact paths. |
| [Inspection](inspection.md) | Frame/cycle identity, collection, channel evidence, technical/product judgment and Size/Color semantics. |
| [Inference and runtime](inference-runtime.md) | Typed input/results, SDK boundary, engine/worker/coordinator ownership, admission, Futures and shutdown. |
| [Application](application.md) | Startup recovery, candidate activation, sessions, readiness, counters, local sources and lifecycle. |
| [UI shell](ui-shell.md) | Presentation, display identity, feedback, Qt delivery, closing and review workflows. |
| [UI configuration](ui-configuration.md) | Recipe/Vision drafts, references, ROI/tuning workflows and distinct Save/Apply/activation stages. |
| [Inspection performance](inspection-performance.md) | Measurement workloads/boundaries, reports, interpretation, historical results and verification. |
| [Communication](communication.md) | FINS/UDP subset, socket lifecycle/errors, local reporting/simulation, session-local UI controls/observations, manual workspace and deferred commissioned hardware integration. |

[README](../README.md) owns setup, usage and verification commands; [AGENTS](../AGENTS.md)
owns durable engineering safeguards and proportional checks. Temporary migration
reviews are evidence, not maintained specifications.

Frozen Historical records: [validation evidence](archive/validation-evidence.md),
[performance records](archive/inspection-performance-records.md) and
[UI exploration](archive/production-ui-exploration.md). They preserve observations
and provenance, not additional Current requirements.
