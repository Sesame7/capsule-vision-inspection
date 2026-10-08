# Production inspection contract — Phase 2A

`vimo_demo.inspection.production` adds synchronous, typed production evidence and
judgment to the Phase 1 foundation. It imports our inference models, never the
SDK, Qt, a camera library or PLC code. It creates no engines, threads, queues,
timers or result history. Production criteria here are initial domain rules,
not calibrated thresholds or an accuracy/acceptance claim.

## Composition with Phase 1

```text
owned Frame + CameraLayout -> TraditionalInspector -> TraditionalCameraResult

six full Frames -> existing CycleCollector / inspect_cycle / InferenceWorker
                -> InspectionCycleResult (or coordinator envelope.result)
                                      |
                  ProductionPipeline.from_cycle(..., traditional_results)
                                      |
                          ProductionInspectionResult
```

`ProductionPipeline.inspect_traditional(frame, inspector)` invokes a synchronous
typed protocol with immutable source pixels and explicit layout. It produces
lightweight results keyed by the complete existing `FrameId`. The original protocol tests use deterministic fakes. Phase 3E-1 additionally supplies
the [capsule demonstration](capsule-inspection.md) in configured application execution.

`camera_result(layout, frame_outcome, traditional_result)` consumes a single
existing `FrameInspectionResult`. `from_cycle()` consumes the unchanged cycle
result and joins traditional evidence by exact cycle/camera/sequence identity.
No membership is inferred from ordering or timing. Mismatched/duplicate identities
or missing configured channel entries in supplied traditional results raise
`ValueError`. Unavailable required evidence instead produces explicit ERROR/UNKNOWN.

The existing runtime remains responsible for collection, deadlines, bounded
dispatch, worker ownership and shutdown. Its nominal path still makes **six
full-camera-image ViMo calls**, never 36 crop calls. Production conversion performs
zero additional inference calls. The package can consume a completed coordinator
Future without modifying its envelope or scheduling policy.

An ordinary non-GUI composition context can use:

```python
from vimo_demo.inspection import inspect_cycle
from vimo_demo.inspection.production import ProductionPipeline

pipeline = ProductionPipeline(layout)
# Given a READY CollectedCycle, a started worker and a traditional inspector:
traditional = tuple(
    pipeline.inspect_traditional(entry.frame, traditional_inspector)
    for entry in collected.frames
)
raw_cycle = inspect_cycle(
    collected, lambda image: worker.submit(image).result()
)
production = pipeline.from_cycle(raw_cycle, traditional)
print(production.assessment.technical_status, production.assessment.judgment)
```

Alternatively, pass `coordinator_future.result().result` to `from_cycle()` with
separately obtained traditional outcomes. Tests demonstrate both paths. This is
consumer-side composition. The [Phase 2C application](production-application.md)
additionally uses an optional coordinator cycle-inspector hook to schedule resolved
checks on the serial cycle runner, outside Qt. Do not run algorithm work in a
blocking worker/coordinator callback.

## Layout and identity

- Reuse `CycleId`, `CameraId`, `FrameId` and `Frame` without changing their contracts.
- `ChannelId` is a positive stable integer. `ChannelROI` binds an explicit slot,
  channel ID and `BoundingRect` in original image coordinates.
- `CameraLayout` requires slots 0..5 exactly once and unique channel IDs. Slots
  are canonicalized into slot order. Positions and channel IDs are independent:
  mirroring is expressed by changing data, not inference or mapping algorithms.
- `InspectionLayout` contains ordered, uniquely identified cameras and requires
  channel IDs 1..N exactly once. Camera count is data; six cameras yield 36 channels.
  Configure CAM1 with 1–6, CAM2 with 7–12, …, CAM6 with 31–36. Camera names and
  image orientation are not parsed to invent physical mappings.
- Rectangles have integer, nonnegative origins and positive dimensions. Layout
  ROIs must fit the supplied image shape; invalid configuration fails explicitly.
  ROI overlap is allowed and meaningful. There are no default physical ROI values.

`CameraInspectionResult.channels` follows configured slot order. The production
`channels` property derives the stable ascending ChannelId view from camera
results; it references the same immutable channel objects, without duplicated
mutable storage. Result arrival order does not determine either view.

## ViMo findings and overlap

`extract_vimo_findings()` converts each actual `DefectRegion` into `ViMoFinding`:
frame identity, source region index, label, area, bounding rectangle and module ID.
The source index is correlation metadata, never a channel number. No class-ID
mapping is guessed. Bboxes supply existing lightweight geometry, so extraction
does not inspect masks or retain contour arrays. Invalid labels/geometry/shape
are explicit invalid-result technical errors in camera conversion.

`OverlapPolicy.minimum_intersection_area` defaults to **1 pixel²**: any positive
integer bounding-box intersection qualifies. Rectangles use half-open intervals;
touching edges have zero intersection. The criterion is explicit and configurable,
not a hidden industrial tolerance. Assignment records intersection area and the
fraction of the finding's **bounding-box area**, not segmented region area.

A finding is attached to **every** qualifying ROI. Multiple assignments reference
the same immutable finding, and every affected channel receives ViMo NG evidence.
Bounding-box overlap is deliberately coarse: it may include empty space around
an irregular region. Contour/mask intersection and calibrated minimum overlap
criteria remain later work, not claims made by this policy.

Findings with no qualifying ROI remain in `camera.unassigned_findings`. They never
invent a channel, but force camera and production judgment to NG. Thus six channel
OK judgments can coexist with camera NG and an explicit unassigned finding.
The production `unassigned_findings` view preserves source camera/frame identity.
PLC response to this situation is intentionally undefined.

## Execution and product judgment

`Assessment` keeps two independent fields:

| TechnicalStatus | Meaning |
| --- | --- |
| SUCCESS | Required enabled execution succeeded |
| ERROR | Required execution/result unavailable or failed; also invalid cycle lifecycle |
| DISABLED | No check was enabled |

`ProductJudgment` is OK, NG or UNKNOWN. Core `ImageResult.has_defects` is unchanged;
the production layer now explicitly treats a valid returned defect as NG evidence
when the ViMo check is enabled.

Each channel contains a `CheckResult` for TRADITIONAL and VIMO, including explicit
disabled entries. `aggregate()` applies this order:

1. Any enabled definite NG evidence wins product judgment.
2. Otherwise any required enabled error/unavailable result produces UNKNOWN.
3. Otherwise successfully inspected enabled checks produce OK.
4. If all checks are disabled, return DISABLED + UNKNOWN.

Technical status is calculated independently. Optional errors remain visible in
their check/reasons but do not invalidate another successful required check.
If every enabled check fails, even when optional, the result is ERROR + UNKNOWN
(or NG if evidence exists); there was no successful inspection to justify OK.

ERROR + NG is valid: traditional NG can coexist with ViMo failure, or ViMo NG
with traditional failure. Both reasons survive. Technical failure alone never
creates a product defect. ERROR + OK and DISABLED + OK/NG are rejected. Individual
successful checks require a definite judgment; at camera/production level,
SUCCESS + UNKNOWN can represent successful work alongside disabled/uninspected
channels. An uninspected channel cannot make the whole camera appear fully OK.

Camera and production aggregation preserve any NG evidence, including unassigned
findings, while aggregating required child technical validity separately. Existing
cycle TIMED_OUT/SHUTDOWN/FAILED remains recorded as `cycle_status` and makes
production technical status ERROR; it is not renamed or converted to six NG
channels. Missing/uninspected frames carry structured COLLECTION reasons and
unknown checks. Independent existing NG evidence remains NG despite another error.

`CheckPolicy(enabled, required)` is explicit construction-time data for each source,
not a Recipe system. Both sources default to enabled/required: an absent traditional
implementation must not silently pass. Explicitly disable an uncalibrated source
when appropriate. A protocol implementation cannot silently disable a source that
the pipeline policy requested; this is treated as unavailable evidence. Policies
govern consumption, **not the unchanged coordinator's call scheduling**. Disabling
ViMo here does not suppress Phase 1 calls or erase a recorded cycle failure.

## Traditional evidence and errors

The `TraditionalInspector.inspect(frame, layout)` Protocol returns one
`TraditionalChannelCheck` per configured channel in any order. Each wraps a
`CheckResult` with structured `Reason` codes and optional named numeric
`Measurement` values/units. The legacy protocol has no built-in Blob/presence implementation. Phase 3E-1 adds
separate product-level capsule Size/Color measurement and judgment. Phase 2C combines
injected legacy presence/blob/color checks using this
evidence-first aggregation before returning its traditional source result.

Reasons identify source (VIMO/TRADITIONAL/COLLECTION), stable code, optional defect
label/rectangle and exception type. Codes include `defect_region`,
`result_unavailable`, `check_unavailable`, `execution_error`, `invalid_result`,
`cycle_timed_out` and `cycle_shutdown`. Labels remain actual model labels; no
localized operator prose is embedded. Reasons are not a large defect taxonomy.

Ordinary traditional execution exceptions become ERROR/UNKNOWN results with an
`execution_error` code and exception type. No exception object/traceback frames are
retained. Malformed identities/channel mappings are programming-contract errors
and raise; process-control BaseExceptions propagate. There are no retries.

## Ownership and timing

Production dataclasses are frozen; collections are tuples, including snapshots
of supplied collections. They retain neither `Frame`, `ImageResult`, ndarray,
mask nor SDK object. Immutable bbox/scalar data remains valid after engine cleanup.
Source pixels follow the existing Frame ownership contract, with no new copies.
Traditional inspectors must not mutate or retain frames. A caller retaining an
original coordinator Future/cycle result still retains its masks; conversion
does not clear caller-owned Phase 1 objects. Release those separately.

All new clock readings use an injectable monotonic clock in the existing host
time domain; negative/nonfinite/backward intervals fail explicitly. Per camera:

- `traditional_ms`: measured traditional stage, or None when not supplied/disabled.
- `vimo_ms`: existing frame inspection interval, including worker wait/normalization,
  or None when not measured/disabled; it is not a raw GPU kernel measurement.
- `mapping_ms`: extraction, assignment and channel assembly duration.
- `total_ms`: sum of available stage durations, excluding collection/queue latency;
  inspect nullable fields before treating it as a complete measurement.

Production records earliest available inspection start, result readiness and
aggregation duration. Existing coordinator milestones remain in its envelope.
No timing thresholds, parallel execution, benchmark or speedup claims are added.

## Verification and next boundaries

```powershell
uv run --no-sync pytest tests/unit/test_production_inspection.py tests/integration/test_production_pipeline.py --tb=short
uv run --no-sync pytest tests/integration/test_production_vimo.py --run-gpu --require-gpu -s --tb=short
```

Synthetic tests cover stable/mirrored mapping, spanning/unassigned findings,
independent ERROR/NG, disabled and unavailable checks, ownership, invalid mappings
and injected-clock timing. Integration tests consume actual Phase 1 collector,
worker and coordinator outputs while asserting six full-image calls and unchanged
cleanup. The optional one-image GPU check preserves real region evidence after
engine cleanup without retaining its mask. Synthetic ROIs do not validate physical
channel accuracy. Existing Phase 1 tests and behavior remain unchanged.

Recipe persistence and pure resolution now live in the additive
[Phase 2B configuration layer](inspection-configuration.md). Deferred: calibrated
ROIs/thresholds and calibrated algorithms; Phase 2D production Qt UI and configuration
editor; Phase 2E archive/events/history; Phase 3 cameras, buffer leases, PLC mapping/handshake,
unassigned-finding machine policy and measured hardware throughput/FAT/SAT.

Phase 2C now implements activation, configured check execution and counters. Its
additive `frame_failures_in_checks` result flag applies enabled/required policies
to frame failures while preserving raw FAILED cycle status. The default remains
False for existing Phase 2A consumers; timeout/shutdown still force technical ERROR.
