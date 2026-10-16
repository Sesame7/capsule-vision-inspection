# Runtime architecture

The normal entry point is [app.py](../app.py). Current implementation locations
describe responsibilities, not a framework that must be expanded for future work.

## Execution and ownership

```text
saved Model / Recipe / MachineLayout / CameraSources
  -> stopped prepared candidate -> Start / warm-up -> active session
  -> passive Basler frames or Folder Replay
  -> explicit identified Frames / cycle admission
  -> bounded runtime -> one InferenceWorker / ViMo engine
  -> configured channel inspection -> compact terminal result / accounting
       -> bounded serial first-station PLC reporting
       -> independent latest display copies -> Qt and read-only Web
```

| Package | Responsibility |
| --- | --- |
| `configuration/` | Strict definitions, pure resolution and atomic persistence; no engine/session |
| `inspection/` | Frame/cycle identity, synchronous collection and channel evidence/judgment |
| `inference/` | Typed synchronous API, SDK boundary, staging and owned outputs |
| `runtime/` | Engine worker, serialized collector, bounded dispatch and terminal delivery |
| `application/` | Candidate activation, sessions, sources and counters |
| `ui/` | Normal composition, presentation, editing and queued notifications |
| `web/` | Latest-only publisher and serial cached HTTP delivery |
| `communication/` | FINS clients, result projection/report owner and UDP simulator |

Only the inference adapter imports SmartMore. pypylon is isolated in the lazy
Basler SDK backend. Domain/runtime layers do not import Qt. The GUI never calls
ViMo or blocks on unfinished Futures/joins; `FutureDelivery` crosses to queued Qt
slots. `QtProductionBridge` commands/presents the application, not its lifecycle owner.

## Inference

`VimoSegmentationEngine(model_path, device_id=0, module_id=...)` loads once and
reuses one GPU segmentation module with `max_batch_size=1`. In worker execution,
an engine factory constructs, calls and closes it on the sole worker thread;
CLI callers may own it in a serial context. Instances are not shared concurrently.

Inputs are nonempty uint8 H x W x 3 OpenCV BGR arrays. Validation rejects other
representations, normalizes non-contiguous inputs when necessary, and never
resizes/reorients or guesses RGB. All aliases of a borrowed worker input must
remain immutable until its task Future completes/cancels. Owned `Frame` pixels
are immutable while inspection/snapshots retain them.

The model is SHA-verified and staged under `outputs/vimo_cache/<sha256>/`; runtime
cache writes never target source models. Module selection follows explicit model
configuration, or the engine's verified unique-segmentation selection. Existing
area/dimension thresholds, ROI and mask operation remain as exported. Mask and
region information are required; region flags include area, long/short sides,
bounding/min-area rectangles and outer/inner contours; score maps remain disabled.
No `vimo_params.json` overlay or environment repair is performed.

`ImageResult` contains our typed rectangles/regions, label mappings and an owned
read-only mask. `(H,W,1)` is squeezed only at the final singleton channel to `(H,W)`;
incompatible geometry/dtype fails. Integer IDs and original coordinates survive.
`label_map` is distinct from `regions_per_label`; `has_defects` means returned
regions, not production acceptance. Retained outputs survive subsequent inference
and SDK cleanup. Initialization/inference/normalization errors use the small
`VimoError` boundary and preserve original exception chaining. Cleanup follows the
SDK's actual lifecycle; failures remain observable rather than being suppressed.

## Cycles and sources

`CycleId`, `CameraId` and complete `FrameId` identify membership explicitly.
Captured timestamps are source metadata, not a proximity-based association rule.
Configured camera/slot order determines output order regardless of frame arrival.
Duplicate/unknown/late frames fail explicitly; finalized cycles never reopen.

`CycleCollector` has one serialized runtime owner. Admission acknowledgements,
worker task Futures and cycle terminal results are different observations; none
implicitly returns Frame storage. Every admitted cycle gets one terminal result
or explicit runtime exception, without unbounded completed history. Commands,
active collection, inspection backlog and worker jobs are bounded; overload is
observable rather than silently dropped or converted into a model judgment.
Deadlines use host monotonic time and explicit runtime expiration, not Qt polling.

Folder input uses independent deterministic per-camera cursors. Saved Loop sources
preload an immutable application-lifetime RAM dataset before engine creation;
source metadata changes invalidate reuse on the next Start. Memory checks retain
a system/runtime reserve; insufficient telemetry/budget falls back observably to
on-demand decoding. Ingress still produces owned Frames. Non-Loop/fixed inputs
remain on demand. Stop preserves the reusable cache; safe Close releases it.
Pause/Step gates whole groups: an already claimed group finishes, PAUSING becomes
PAUSED at the safe boundary, and Step produces one formal result. This is still
application RUNNING, with editing locks retained. End performs actual Stop.

Saved Basler mode first uses Folder references for worker warm-up, then opens/arms
six explicitly mapped cameras once. **RUNNING is not ARMED.** An idle armed source
produces no cycles, counters, timeouts or reports and never generates W10.7 writes.
Each accepted frame is copied before SDK buffer Release and immediately enters
early serial inspection without waiting for its peers.

Current Basler membership uses per-session SDK ImageNumber ordinals and validates
consecutive BlockID/ImageNumber per serial. A new ordinal cannot start before the
previous capture group completes. Observed gaps, duplicates, unsupported/late
metadata stop the source; no shifted reassociation or software-trigger fallback.
These counters are **not independent hardware trigger IDs**: undetectable missed
electrical triggers remain a commissioning limit.

The early result deadline is 100 ms from first valid frame admission. Completed
camera inspections survive; missing/unfinished cameras get explicit collection
ERROR/NG on their channels, without invented model findings. Late native results
are discarded. Full-group/Folder collection instead uses its normal complete-cycle
path and 30-second composition deadline; incomplete groups do not run algorithms.
Reporting eligibility and ambiguity checks are in [communication](communication.md).

## Inspection and results

Full-camera ViMo runs at most once per camera when enabled, then findings are
assigned against saved channel ROIs with original global ChannelIds. Per-channel
label filtering is a consumption policy, not a change to SDK evidence. Unassigned
camera findings remain distinct and never fabricate channel findings.

Occupancy always measures the centered `max(1, width*70//100)` portion of the saved
ROI, split into upper/lower views; a pixel is foreground if any BGR component is
greater than 20. Each view is EMPTY at <=1%, PRESENT at >=5%, otherwise UNKNOWN.
Only equal EMPTY/EMPTY or PRESENT/PRESENT pairs fuse to that state. Geometry stays
unchanged. Valid EMPTY explicitly skips enabled Size/Color, preserving provenance;
it does not suppress ViMo or erase its NG evidence.

Enabled Size/Color operate on the original ROI and its two views. Size measures
detected body width/height; Color uses usable saturated/bright HSV pixels and
circular Hue statistics. Only requested capabilities execute. Measurement is
separate from target/tolerance judgment. These checks require product/lighting
calibration; integration tests are not accuracy certification.

Technical status is SUCCESS / ERROR / DISABLED; product judgment is OK / NG /
UNKNOWN. Definite NG survives a simultaneous error. Missing required work, or all
enabled checks failing, yields ERROR; disabled is never a passing check. A normal
cycle containing check ERROR is a result, not automatically application FAULT.
Infrastructure/invariant failure can fault the application. Business status is
derived in priority order: existing NG -> NG; valid Occupancy EMPTY -> EMPTY;
Occupancy PRESENT plus SUCCESS/OK -> OK; otherwise UNKNOWN.

## Application and shutdown

Startup restores explicit IDs from entry-relative `data/`, verifies artifacts and
prepares a stopped candidate. Start creates a disposable session and warm-up with
zero production results/counters. Only successful Start promotes the candidate;
failed activation preserves last-known-good active identity and returns READY if
cleanup is safe. Stop/Start or a Recipe change retains counters; explicit Batch
resets them. Accounting describes cycle/channel occurrences, not physical products.

Stop prevents admission, finalizes incomplete collection and explicitly rejects
unclaimed inspection work. A claimed native call finishes naturally. Owners are
retained until source/runtime/worker/report threads actually exit and SDK cleanup
is safe. Notification or timeout is not successful termination. Unsafe cleanup
remains visible as ownership/failure; native calls are never forcibly cancelled.
Qt Close is asynchronous; the final joins occur outside GUI dispatch.

## Display and Web

The shared Qt shell uses one layout with window-scoped dark/light palettes and
DPR-aware vector icons. Normal startup maximizes the native framed window, keeping
taskbar clearance; portrait Production retains two columns and three rows. Theme
switching is presentation-only and is not persisted. Language retains its existing
preference behavior. The fixed bottom strip presents application state, the most
recent PLC reporting observation (ACK is not connectivity/consumption), Web owner
availability and current feedback. It reuses bridge refresh/notifications without
another timer or network request.

Rendering is not result completion or accounting. The display store owns separate
bounded latest-per-camera copies with generation, cycle, complete FrameId,
dimensions and source key. Only matching evidence may overlay those pixels.
Vision Runtime references are frozen snapshots; explicit refresh/camera selection
chooses a new eligible reference rather than tracking every live frame.

Normal startup owns one Web publisher and one serial HTTP thread across Stop/Start.
It keeps one current publication plus a replacement being built; JPEGs are at
most 640 pixels on the longest side, quality 70, with unchanged frame bytes reused.
Application/display snapshots are sampled separately, not atomic six-camera capture.
Publisher ID, revision and source identities prevent old bytes/new evidence aliasing.

Default server/browser interval is 500 ms (CLI alternatives 1000/2000). The publisher
waits after each publication; the browser never overlaps polls, fetches JPEGs
sequentially and owns at most six displayed object URLs plus one decode. Requests
have a five-second poll deadline; 410 waits for the next observation, without an
immediate retry loop. Publication age beyond six seconds is stale, separate from
application FAULT. HTTP uses one serial handler, backlog four and one-second I/O
timeouts. All service threads/socket ownership must exit on process Close.

Routes are `/`, `/observer.js`, `/observer.css`, `/api/snapshot`, exact returned
`/api/cameras/<CameraId>.jpg?publisher=...&revision=...`, and `/health`. Snapshot is
503 before first publication; superseded JPEG identity is 410. There are no write
routes, CORS, authentication or HTTPS. Explicit assigned IPv4 binding is for a
trusted isolated LAN; startup failure is optional and does not invalidate Production.

Verification commands and retained developer entry points are in [README](../README.md).
