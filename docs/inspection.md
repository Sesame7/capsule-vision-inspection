# Inspection

This document owns **Current** frame/cycle identity, collection, channel evidence
and technical/product judgment. The inspection domain is synchronous and independent
of Qt, SmartMore internals, camera/PLC libraries and runtime scheduling. It uses
our [typed inference results](inference-runtime.md#input-and-results), injected inspection callables
and [resolved configuration](configuration.md). The
[runtime coordinator](inference-runtime.md#cycle-admission-and-terminal-delivery) owns dispatch/backpressure/thread
termination; the [application](application.md) owns activation and
execution composition. No production accuracy or physical-product counting claim
follows from the demonstration algorithms below.

## Identity and frames

| Type | Identity/data contract |
| --- | --- |
| `CycleId` | Caller-supplied nonnegative integer; strictly increasing at `begin()` within a collector session; gaps allowed |
| `CameraId` | Exact nonempty string in the configured ordered camera tuple |
| `FrameId` | Explicit `(cycle_id, camera_id, sequence)`; nonnegative sequence is source metadata |
| `Frame` | FrameId, capture timestamp and exclusively owned nonempty uint8 H×W×3 BGR ndarray |
| `ReceivedFrame` | Frame plus host-monotonic admission timestamp |
| `CollectedCycle` | Immutable terminal collection snapshot; configured camera identities, collection times/status and received frames |
| `FrameInspectionResult` | Identity, capture/arrival/inspection times, shape, typed result or explicit failure |
| `InspectionCycleResult` | Cycle identity, expected/missing cameras, ordered outcomes, timing and technical CycleStatus |

Membership never follows timing proximity, arrival order, filename or camera-name
parsing. One frame per camera/cycle is admitted; a different sequence does not
replace an earlier frame. Reused/older CycleIds cannot reopen a finalized cycle.
The collector retains only the highest admitted ID, not an unbounded deduplication
history. A new collector is a new session: sources must be drained/isolated so
stale frames cannot enter reused IDs. Real trigger association/wrap rules are deferred.

`Frame` requires owned, C-contiguous pixels, accepts them **without a copy**, and
sets that array read-only. Views/borrowed camera buffers require an explicit
producer copy first. Read-only flags do not revoke pre-existing aliases or form a
buffer-return protocol: every alias must remain immutable while frames/snapshots
or inspection still use it. Only after all users release/finish may the producer
reuse storage. Runtime frame-admission acknowledgement is not that notification.
Different cameras may supply different image dimensions; no resize, RGB reinterpretation
or orientation change is performed by collection.

`captured_at` is finite nonnegative source-clock metadata. It is not the deadline
clock and cannot backdate admission. Arrival and inspection timestamps use the
same finite, nonnegative, nondecreasing host-monotonic time domain; invalid/backward
clock behavior raises rather than becoming product evidence.

## Collection

`CycleCollector(expected_cameras, timeout=..., max_active=2, clock=...)` has one
owning context. It starts no thread, timer or scheduler and claims no concurrent-call
safety. Timeout must be finite/positive; capacity must be a positive integer.

```text
begin(cycle_id) -> active collection with fixed deadline
accept(frame)  -> None, or one READY snapshot after all cameras arrive
expire()       -> due incomplete TIMED_OUT snapshots
shutdown()     -> remaining TIMED_OUT / SHUTDOWN snapshots, then closed
```

Expected cameras are unique and ordered. Frame/snapshot/result order follows
that tuple. Overlapping cycles may complete out of CycleId order without mixing;
multi-cycle `expire()`/`shutdown()` outputs are sorted by CycleId.

| Boundary | Explicit behavior |
| --- | --- |
| Active capacity reached | `CycleCapacityError`; no eviction or consumption of the proposed new ID |
| Unknown camera in an active, unexpired cycle | `UnknownCameraError` |
| Second frame for the same camera/cycle | `DuplicateFrameError`; first frame remains intact |
| Unopened/finalized cycle or admission after shutdown | `CycleStateError`; no reopen/reassignment |
| Arrival at/after deadline | `CycleDeadlineError`; reject input, leave the cycle active for explicit expiration |

The interval is **`[started_at, deadline)`**. Expiration is observed by the owner
through `expire()`, not by sleeps, capture timestamps or silent eviction during
another operation. Even zero received frames yields a timeout with explicit missing
cameras. `deadline` is the cutoff; `finalized_at` records observation and may be later.

`shutdown()` finalizes incomplete cycles once. Already-due cycles remain TIMED_OUT;
other incomplete cycles become SHUTDOWN. Repeated shutdown or expiration after
shutdown returns empty tuples. Already-emitted snapshots are outside the collector:
their owner separately finishes/drains work. Shutdown does not cancel native inference.

At most `max_active × expected_camera_count` frames are held internally. Finalized
snapshots are removed exactly once; no result queue/history remains. The bound
does not constrain caller-held objects, image byte sizes or downstream backlog.
The runtime must bound those handoffs separately.

A small fake-source example, with no sleeps or inference:

```python
import numpy as np

from vimo_demo.inspection import CameraId, CycleCollector, CycleId, Frame, FrameId

cameras = tuple(CameraId(f"CAM{i}") for i in range(1, 7))
collector = CycleCollector(cameras, timeout=1.0, clock=lambda: 10.0)
collector.begin(CycleId(1))
for sequence, camera in enumerate(reversed(cameras)):
    snapshot = collector.accept(Frame(
        FrameId(CycleId(1), camera, sequence),
        10.0,
        np.zeros((2, 3, 3), dtype=np.uint8),
    ))
    if snapshot is not None:
        assert tuple(row.frame.id.camera_id for row in snapshot.frames) == cameras
        assert snapshot.missing_cameras == ()
```

### Technical cycle outcomes

`inspect_cycle(snapshot, inspect, clock=...)` inspects READY snapshots serially
through the injected callable; it creates no engine/worker. Execution belongs
outside Qt, with the existing engine owner or non-GUI worker integration.
The current Production shell already integrates cycles; domain independence
does not mean application cycle integration is absent.

| CycleStatus | Meaning |
| --- | --- |
| COMPLETE | Every expected frame received and each invoked inspection succeeded |
| FAILED | Complete collection, but one or more inspections raised an ordinary Exception or returned the wrong image shape |
| TIMED_OUT | Missing frames at the collection cutoff |
| SHUTDOWN | Collection stopped incomplete before its deadline |

Only READY snapshots invoke `inspect`, once per received frame in camera order.
Ordinary exceptions become `InspectionFailure` with qualified type, message and
formatted traceback/cause chain. Later frames still run, without retry; exception
objects/traceback frames are not retained. Process-control `BaseException` propagates.
Incomplete snapshots never invoke inspection, even for received frames: those
entries have result/failure/start/finish all None, plus explicit `missing_cameras`.
A READY cycle is not retroactively timed out if inspection starts/finishes after
the collection deadline.

`is_complete` is technical, independent of returned defects. There is no raw-cycle
production OK/NG. Configured execution may intentionally skip disabled ViMo calls
while preserving collection status and explicit disabled checks below. Repeated
calls to `inspect_cycle()` rerun work; exactly-once dispatch belongs to the runtime
owner, not an inspection deduplication cache.

## Channel results

`ChannelROI` binds a stable positive `ChannelId`, slot and original-image
`BoundingRect`. `CameraLayout` has slots 0..5 exactly once, canonicalized in slot
order; `InspectionLayout` preserves explicit camera order and maps channel IDs
1..N exactly once. Six cameras give 36 channels. Names/orientation do not imply
physical wiring; mirrored mapping is explicit data.

ROIs have integer nonnegative origins and positive sizes and must fit the actual
camera image. Overlap is allowed; no cropping/clamping repairs invalid geometry.
`CameraInspectionResult.channels` uses configured slot order;
`ProductionInspectionResult.channels` derives the ascending ChannelId view from
the same immutable channel objects.

### ViMo evidence and assignment

`extract_vimo_findings()` converts actual returned regions into lightweight
`ViMoFinding`: FrameId, source-local region index, label, area, bounding rectangle
and module ID. The index is correlation metadata, never a channel number.
Invalid labels/geometry/out-of-image boxes are invalid-result errors. Extraction
does not inspect masks, guess class-ID mappings or retain contours.

`OverlapPolicy.minimum_intersection_area` defaults to **1 pixel²**. For half-open
rectangles, intersection is the product of positive overlapping width and height;
touching edges do not qualify. Each finding attaches to **every** qualifying ROI.
`FindingAssignment` records intersection area and
`intersection_area / (finding_bbox.width × finding_bbox.height)`.
This fraction uses bounding-box area, not segmented region area or IoU.

Assigned findings provide channel ViMo NG evidence. Findings without any qualifying
ROI remain camera-level `unassigned_findings`; retained unassigned evidence makes
camera/production judgment NG without inventing a channel NG. All channel judgments
can therefore be OK while the camera is NG. The configured filter rule below still
applies. Bbox overlap is coarse and can include empty space around an irregular
region; neither contour overlap nor industrial tolerances are implied. PLC response
to unassigned evidence is deferred.

### Configured execution

`ProductionPipeline.camera_result()` / `from_cycle()` map existing inference/cycle
outcomes without additional inference. Mapping-only conversion explicitly supplies
a DISABLED traditional check; it does not claim Size/Color ran.

Current `ConfiguredProductionPipeline` composes the
[resolved policies](configuration.md#resolution) with the same mapping/judgment:

- For READY collection, evaluate Occupancy for every channel regardless of Recipe
  Size/Color/ViMo enablement. Confirmed EMPTY skips enabled traditional work with
  explicit provenance; otherwise measure/judge enabled Size/Color once on the
  full original ROI. Both capabilities disabled still skip traditional measurement.
  One combined TRADITIONAL check results.
- Call serial inference **once per full camera** when any channel enables ViMo,
  not once per ROI. A six-camera cycle needs at most six calls, never 36 crop calls.
- Filter assigned ViMo evidence independently per channel by exact disabled labels.
  Retain unassigned findings only if any enabled channel in that camera accepts
  their label. Shared raw evidence and SDK parameters remain unchanged.
- Incomplete collection executes neither algorithm. Enabled checks are unavailable;
  disabled checks stay explicit. Collection reasons and missing cameras survive.

Normal per-frame inference exceptions retain type/message in the configured raw
outcome (without a formatted traceback), mark raw status FAILED, and permit later
cameras to run. Normal capsule exceptions yield `capsule.execution_error`. Scheduling,
engine ownership and candidate activation remain application/runtime responsibilities.

### Always-on Occupancy

`measure_occupancy(image, roi)` borrows full-resolution uint8 BGR pixels and keeps
only immutable scalar evidence. It first validates the **original** saved ROI,
including two nonempty views. Measurement width is `max(1, roi.width * 70 // 100)`;
left offset is `(roi.width - measurement_width) // 2`. An odd unused margin leaves
one extra pixel on the right. Height/y are unchanged and split at `roi.height // 2`.
For a 176×960 ROI this measures 123×480 pixels in each view, offset 26 pixels.
Saved geometry and the Size/Color region are never replaced by this center window.

Every pixel with **any BGR channel >20** is foreground. No sparse sampling, HSV,
components or morphology is used. Each view classifies independently:

| Foreground ratio | OccupancyState |
| --- | --- |
| ≤1% | EMPTY |
| ≥5% | PRESENT |
| Between those bounds | UNKNOWN |

Only EMPTY/EMPTY fuses to EMPTY and PRESENT/PRESENT to PRESENT. All other valid
pairs fuse to UNKNOWN. PRESENT/UNKNOWN execute the unchanged enabled Size/Color
capabilities on the original ROI; there is no persisted Occupancy switch or setting.

Configured `ChannelInspectionResult.occupancy` carries an `OccupancyResult`:
`evidence` has the measurement rectangle, upper/lower integer foreground/total
counts, derived ratios/view states and fused state. Failure instead carries
`error_code`, optional `exception_type` and message; its state is UNKNOWN with no
fabricated zero counts. Codes are `occupancy.result_unavailable` for incomplete/
missing inputs and `occupancy.execution_error` for ordinary execution exceptions.
Incomplete cycles execute neither Occupancy nor other algorithms. Occupancy is
required independently of Recipe: failure forces channel technical ERROR,
preserving any definite NG. Raw cycle status still describes full-camera inference/
collection; an ordinary Occupancy error is a returned result, not application FAULT.
Mapping-only `ProductionPipeline` has no pixels and leaves `occupancy=None`.

When Size/Color is enabled but Occupancy confirms EMPTY, `traditional_skip` is
`TraditionalSkipReason.CONFIRMED_EMPTY`. The TRADITIONAL check is neutral,
DISABLED/UNKNOWN, with **no** measurements or executed reasons. Its `required`
flag retains the configured enabled requirement, explicitly exempted by this skip.
Recipe-disabled traditional work instead has `required=False` and no skip provenance.
This distinction does not fabricate a passing check or change Recipe configuration.
The channel invariant requires EMPTY evidence and a neutral check for this skip.

Successful Occupancy does not contribute OK/NG evidence to `aggregate()`.
EMPTY with no enabled executed checks remains DISABLED/UNKNOWN; an executed
successful ViMo check without accepted defects can still yield SUCCESS/OK through
the existing aggregation. **Occupancy=EMPTY and ProductJudgment=NG may coexist**:
accepted assigned ViMo findings/reasons survive unchanged. Full-camera inference,
disabled-label filtering, assignment and unassigned camera-level NG are untouched.
These original judgments stay separate from the [business interpretation](#channel-business-status)
below. Occupancy is not a ViMo filter or PLC mapping.

This center-region rule is approved only for the current normally centered capsule,
same-capsule upper/lower views and dark-background demonstration. The E1 1% EMPTY
band is an explicit integration choice; E0-R's historical preferred review baseline
was strict zero. Seven external EMPTY neighbor cases support interference exclusion;
local reviewed shortlist labels provide additional limited evidence, with their own
provenance limits. Neither establishes industrial accuracy. Extreme displacement,
other lighting/products and certified false-EMPTY limits remain outside that evidence.

## Judgment

`Assessment` has independent `TechnicalStatus` and `ProductJudgment` axes:

| Technical status | Meaning |
| --- | --- |
| SUCCESS | Successful enabled evidence; required execution is satisfied |
| ERROR | Required enabled work failed/unavailable, all enabled checks failed, or a collection/lifecycle error applies |
| DISABLED | No active check evidence |

Product judgment is **OK / NG / UNKNOWN**. A valid consumed defect is NG evidence;
technical failure alone is not a defect. Inference `has_defects` still only means
returned regions exist, not a physical-product acceptance decision.

Every channel carries explicit VIMO and TRADITIONAL `CheckResult` entries, with
source, assessment, required flag, scalar measurements and structured reasons.
For enabled checks, `aggregate()` computes technical validity independently of
product judgment:

1. Any definite NG wins product judgment, including from optional checks.
2. A required enabled error makes technical ERROR; absent NG, judgment UNKNOWN.
3. An optional error remains visible but does not invalidate another successful
   enabled check. If **all** enabled checks fail, technical ERROR/UNKNOWN still
   applies even when all were optional; NG remains possible if evidence exists.
4. Otherwise successful enabled checks yield SUCCESS/OK. All disabled yields
   DISABLED/UNKNOWN, never OK.

ERROR/NG is valid and preserves both kinds of evidence; ERROR/OK and
DISABLED/OK-or-NG are invalid. Individual SUCCESS checks require a definite
judgment. Disabled checks carry no executed reasons/measurements. At camera and
production level, all active child results are required and disabled children are
technically neutral but prevent asserting whole-parent OK: SUCCESS/UNKNOWN can
represent successful children alongside disabled channels. Any child NG or retained
unassigned finding still yields parent NG.

### Raw failure versus configured policy

`ProductionInspectionResult.cycle_status` always retains the raw status.
Mapping-only results default `frame_failures_in_checks=False`: raw FAILED forces
production technical ERROR regardless of a downstream consumption policy.
Configured results set it to True: enabled/required check results account for
frame failures, so optional failed ViMo plus successful capsule evidence can yield
production SUCCESS while raw status remains FAILED. This is intentional, not a
renaming of technical failure. TIMED_OUT/SHUTDOWN always force production ERROR.

A completed cycle with technical ERROR is a returned inspection result, not
automatically an application FAULT. Infrastructure/runtime terminal exceptions
belong to the [application fault contract](application.md#application-api-and-states); do not
convert overload or lost runtime ownership into model NG.

`Reason` records source (VIMO/TRADITIONAL/COLLECTION), stable code and optional
label/rectangle/exception type, not localized operator prose. Typical codes are
`defect_region`, `result_unavailable`, `execution_error`, `invalid_result`,
`capsule.execution_error`, `cycle_timed_out` and `cycle_shutdown`.
Malformed identity/layout/result contracts may raise; ordinary check errors are
represented as above. There is no retry/recovery policy here.

## Channel business status

`ChannelInspectionResult.business_status` derives the Qt-independent
`ChannelBusinessStatus` without mutating Occupancy, checks, reasons, findings or
the original assessment. It is additional scalar interpretation, not a replacement
for technical/product judgment or industrial accuracy certification.

Apply these rules in order:

| Priority | Existing channel evidence | Business status |
| --- | --- | --- |
| 1 | Channel ProductJudgment=NG, including technical ERROR or unavailable Occupancy | NG |
| 2 | No NG and valid Occupancy evidence=EMPTY | EMPTY |
| 3 | Valid Occupancy=PRESENT and channel SUCCESS/OK | OK |
| 4 | All other cases | UNKNOWN |

EMPTY+NG and UNKNOWN+NG remain NG. Valid EMPTY with a failed/unavailable ViMo
check may be business EMPTY **and** technically ERROR; the error remains visible.
Failed/unavailable Occupancy has no valid evidence and can never qualify EMPTY
or OK. UNKNOWN Occupancy with original SUCCESS/OK is business UNKNOWN. A disabled
or mapping-only channel without Occupancy is UNKNOWN, not a fabricated business OK.
Unassigned camera-level findings never create channel NG: the camera/cycle may
be NG while channel business states remain OK/EMPTY/UNKNOWN.

The derived property is available through the existing compact result and application
snapshot; it owns no pixels/history and performs no inspection. Occurrence accounting
belongs to [application](application.md#factual-accounting), and its icons/details
belong to [UI presentation](ui-shell.md#production). Neither domain repeats these rules.
This adds no physical capsule identity, rotation-based deduplication, reject policy
or PLC result mapping.

## Capsule screening

Current Size/Color screening is an **uncalibrated demonstration**, inspected with
yellow capsules on black backgrounds, not accuracy certification, physical presence
or a foreign-object classifier. Settings/defaults/ranges are owned by
[configuration](configuration.md#capsule-settings).

`measure_capsule(image, roi, settings)` borrows BGR pixels without mutation,
resize or retained arrays. The saved ROI splits at `height // 2`: upper has that
many rows; lower has the remainder. At least two ROI rows are required for two
nonempty views. Boxes/dimensions stay in original full-image coordinates.
`judge_capsule()` uses the measured evidence and current targets/tolerances.

| Size | Color | Measured capability |
| --- | --- | --- |
| off | off | Production skips measurement; explicit disabled check, no fabricated evidence |
| on | off | Size geometry only; no HSV conversion, Hue or Color sampling mask |
| off | on | Independent view Color only; no components/body boxes/dimensions |
| on | on | Independent Size/Color using one shared HSV conversion |

`CapsuleMeasurement` records size/color capability flags and upper/lower view
evidence. Targets/tolerances do not affect measurement; requesting an enabled
capability that was not measured raises a technical contract error. Valid body
geometry, Size invalid reason and Color evidence are independently optional.

### Size

For each view, foreground is **HSV Value >20**, equivalently any BGR channel >20.
Size-only uses the equivalent BGR range mask; combined mode thresholds shared
HSV Value. Eight-connected components smaller than
`max(12, floor(view_area × 0.005))` pixels are ignored. Remaining candidates are
ordered by descending component area, then component index.

- No candidate: `capsule.<view>.missing_body`.
- Second candidate area at least 25% of the dominant: `.competing_bodies`.
- Dominant bbox touching any view edge, including the split midpoint:
  `.truncated_body`.
- Either bbox dimension below 3 pixels: `.invalid_geometry`.

These checks run in that order. Valid body bbox width/height, in pixels, must each
satisfy **`abs(actual - target) <= tolerance`** in **both** views. Exceeded limits
produce `capsule.size.<view>.width` / `.height` with the body rectangle. Scalar
measurements are `capsule.<view>.width` and `.height` (`px`). No dimensions are
invented for an invalid body. Small disconnected particles do not expand the
dominant bbox; connected contamination can change it.

### Color

Color independently screens **the whole upper/lower view**, selecting pixels with
**S ≥40 and V ≥40** in OpenCV HSV. There is no connected-component/body selection
or erosion. Each view requires at least
**`max(12, ceil(view_area / 20))`** usable pixels: an inclusive 5% requirement
with a 12-pixel floor. This internal provisional constant is not a Recipe field
or calibrated presence criterion.

Hue uses a period of 180. The current 180-bin histogram computes a weighted
circular mean, rounds its center, unwraps Hue into `[-90, 90)` around that center,
then takes the median and wraps back. Circular mean magnitude **<0.5** is ambiguous.
At the magnitude boundary (within `1e-12`) or a center rounding tie (within `1e-10`),
the implementation falls back to pixel-order computation to preserve the numerical
rule, not to relax acceptance. Circular distance is
`min(abs(h - target) % 180, 180 - (abs(h - target) % 180))`.
Both views require usable Hue and distance **≤ hue_tolerance**.

| Condition | Reason suffix after `capsule.color.<view>.` |
| --- | --- |
| Too few usable pixels | `insufficient_foreground` |
| Ambiguous/unavailable Hue after sufficient pixels | `unusable` |
| Hue outside inclusive tolerance | `tolerance` |

Color measurements record `capsule.<view>.usable_pixels` (`px`) and, only when
available, `capsule.<view>.hue` (`opencv_hue`). Reasons carry no invented body bbox.
Size invalidity does not suppress Color; a truncated/competing scene can pass
Color while failing Size. Contamination/multiple objects can alter whole-view Hue.

**Historical:** earlier Color sampled an eroded body interior. That is superseded,
not an alternative Current mode or equivalence oracle for whole-view screening.
The [performance record](archive/inspection-performance-records.md#selective-execution-and-fast-color--phase-3f-2b)
retains that change's provenance; this document does not repeat historical timings.

Both enabled capabilities and both views contribute to one required TRADITIONAL
check. Valid execution with any content/limit rejection is **SUCCESS/NG**; otherwise
SUCCESS/OK. Execution/ROI/evidence-contract exceptions are **ERROR/UNKNOWN** at
the configured boundary. Independent ViMo evidence can still make the combined
channel ERROR/NG. No legacy Presence/Blob or unparameterized Color path exists.
The [tuning preview](ui-configuration.md#tuning-preview) uses
these semantics without creating production cycles or activating draft settings;
UI cache/reference ownership is outside this domain contract.

## Ownership, timing and limits

Production result/evidence dataclasses are frozen with tuple snapshots and retain
no Frame, ImageResult, ndarray, mask, contour arrays or vendor object. Bbox/scalar
evidence remains valid after engine cleanup. Raw inspection results may still
retain ImageResult masks; converting to production evidence does not clear a
caller-held raw result/Future. Release those separately.

Per-camera timing records nullable `occupancy_ms` / `traditional_ms` / `vimo_ms`, `mapping_ms`,
and a sum of available stages. Disabled configured traditional work can measure
fast-path overhead; None means not measured/run. `occupancy_ms` separately measures
the six scalar channel measurements and failure wrapping, outside traditional/ViMo
timing, and is None for incomplete collection or mapping-only conversion.
ViMo interval may include worker
wait/normalization, not just GPU work. Collection/queue latency is not included in
that stage sum. Production records inspection start/result readiness/aggregation;
runtime milestones remain in its envelope. Timings are descriptive, share the host
monotonic domain, and impose no thresholds. Overlapping stage boundaries and
performance methodology are owned by [performance](inspection-performance.md).

Real camera/PLC work must decide session/trigger association, buffer leases,
clock synchronization, capacity/deadlines and machine responses to UNKNOWN or
unassigned NG. Calibration and industrial acceptance for geometry, Hue, contamination
and other lighting/products remain unverified; local frames do not settle them.

## Verification

Current code pointers: [identity/results](../src/vimo_demo/inspection/models.py),
[collection/serial inspection](../src/vimo_demo/inspection/cycle.py),
[layout/overlap](../src/vimo_demo/inspection/production/layout.py),
[judgment/evidence](../src/vimo_demo/inspection/production/models.py),
[mapping](../src/vimo_demo/inspection/production/pipeline.py),
[capsule algorithms](../src/vimo_demo/inspection/production/capsule.py),
[Occupancy](../src/vimo_demo/inspection/production/occupancy.py) and
[configured composition](../src/vimo_demo/application/pipeline.py).
These are navigation to Current code, not a permanent internal class-layout mandate.

Relevant GPU-independent tests cover identity/ownership/deadlines, out-of-order and
overlapping collection, bbox assignment to multiple ROIs, required/optional errors,
unassigned NG, configured filtering, selective capabilities and numerical boundaries:

```powershell
uv run --no-sync pytest tests/unit/test_inspection_cycle.py tests/unit/test_production_inspection.py tests/unit/test_configured_execution.py tests/unit/test_occupancy.py tests/unit/test_configured_occupancy.py tests/unit/test_capsule.py tests/unit/test_capsule_selective.py tests/unit/test_capsule_optimization.py tests/integration/test_production_pipeline.py --tb=short
```

[Cycle-to-Production integration](../tests/integration/test_production_pipeline.py) and
[configuration integration](../tests/integration/test_configuration_pipeline.py)
check composition without making every domain test load ViMo. Historical model
regression/visual parity in [validation findings](archive/validation-evidence.md) validates SDK
integration, not annotation quality or these traditional algorithms' accuracy.

The optional [saved-source E1 smoke](../tests/integration/test_occupancy_vimo.py)
reads local `data/` without saving it, runs a few real six-camera Folder cycles,
checks scalar Occupancy/ViMo evidence and actual Stop/Close termination:

```powershell
uv run --no-sync pytest tests/integration/test_occupancy_vimo.py --run-gpu --require-gpu --tb=short -s
```
