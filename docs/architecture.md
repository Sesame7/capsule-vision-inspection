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
       -> bounded original-frame PNG archive (opt-in Basler Recipe)
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

`ProductionApplication.prepare_model_cache(ModelPreparationRequest(...))` admits
one immutable Model/path/module/device identity while READY or UNCONFIGURED.
Its existing lifecycle thread waits for a disposable `InferenceWorker`; hashing,
construction, optional inference and close stay off Qt. The real factory uses the
same default SHA staging/cache location and SDK settings as Production. GPU module
creation lets the SDK generate/reuse opaque TensorRT artifacts. No source, Recipe,
counter, cycle or reporting session is created. No image/dimensions are required;
an explicitly supplied known `sample_shape` adds two immutable black BGR calls.

Preparation and Production Start are mutually exclusive under the application's
command lock, including construction and cleanup. Its noncancellable result Future
completes after worker join, reporting initialization separately from sample calls.
The application retains only the latest preparation observation. Clean failures
release admission; incomplete SDK cleanup retains the owner, faults the application
and blocks reset/Start. Close requests worker stop and waits for native work and
actual termination; a GUI timeout does not release ownership. No native operation
is forcibly cancelled. A terminal native cleanup failure can require process-level
recovery; no second thread retries SDK close. Production still does its actual-shape
warm-up and does not require prior preparation or cache-file checks.

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
application RUNNING, with editing locks retained. The Stop control requests actual
shutdown.

Vision's explicit Connect all opens/arms six saved/applied Basler mappings on one
application-owned SDK thread. Connection survives page/Recipe/Batch changes and
Production Stop. CONNECTED/ARMED is independent of inspection RUNNING; there is no
automatic connection/retry. Idle connected frames are retrieved, validated and
released without cycles, counters, display publication or PLC reports. One optional
idle capture request copies only the selected camera's next matching frame into a
Future for Vision; it does not enter the Production path.

Basler Start requires CONNECTED, checks every saved ROI against current camera
Width/Height and warms one ViMo worker with black uint8 BGR inputs, twice per
distinct shape. Folder references are optional. Only after warm-up and an idle,
complete SDK boundary is the session bound; production admission additionally
requires RUNNING. Start inspection before external triggering: mid-stream takeover
is unsupported. Buffered/partial pre-session frames are never retroactively admitted.
Each admitted frame is copied before SDK buffer Release and immediately enters
early serial inspection without waiting for its peers. Production never writes W10.7.

Current Basler membership uses connection-lifetime SDK ImageNumber ordinals and validates
consecutive BlockID/ImageNumber per serial. A new ordinal cannot start before the
previous capture group completes. Observed gaps, duplicates, unsupported/late
metadata stop the source; no shifted reassociation or software-trigger fallback.
These counters are **not independent hardware trigger IDs**: undetectable missed
electrical triggers remain a commissioning limit.

Normal Basler Pause closes new inspection admission while keeping all six cameras
grabbing and the same worker/engine alive. The SDK owner continues retrieving,
validating and releasing paused frames without publishing images, cycles, counters
or reports. PAUSING becomes PAUSED only after the admitted capture boundary,
terminal inspection, actual camera-task return and accepted reporting settle.
Reporting settlement may be ACK or an explicit failure; it is not PLC consumption.

Step and Resume first drain available SDK results and require a complete common
ImageNumber boundary. Buffered paused groups remain discarded. Step admits one
subsequent attributable ordinal, including an attributable partial deadline result,
then returns PAUSED after settlement; Resume restores continuous admission without
reloading resources. Cycle IDs retain SDK ordinals, so discarded groups create
intentional gaps, not fabricated production counts. BlockID/ImageNumber continuity
is still checked for every received frame, including discarded frames.

A partial Step's late same-ordinal peers are discarded. Until its SDK boundary is
complete, Step/Resume fails explicitly; a newer ordinal before missing peers is
an ambiguity, never a shifted association. Empty-queue observation is not a shared
electrical trigger identifier and cannot prove the timing of unobservable in-flight
exposures or arbitrary trigger loss. Control Futures and diagnostic counts are
bounded; Stop interrupts idle Step and follows normal native/resource cleanup.

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

Stop prevents inspection admission, finalizes incomplete collection and explicitly rejects
unclaimed inspection work. A claimed native call finishes naturally. Owners are
retained until source/runtime/worker/report threads actually exit and SDK cleanup
is safe. Basler session shutdown detaches its callbacks and drains reports but
keeps the SDK connection grabbing/discarding. Vision Disconnect is available only
with inspection stopped; it releases cameras and joins their actual owner.
Application Close first settles inspection, then disconnects application-owned
sources on the lifecycle thread. Partial connection failures remain source FAULT
with explicit ownership/error observations; a failed application Close remains
application FAULT. Unresolved SDK cleanup retains its
original owner for an explicit Disconnect/Close retry. There is no automatic
reconnection. Source loss stops the bound session and remains visible in Vision;
it does not fabricate results. Notification or timeout is not termination, and
native calls are never forcibly cancelled. Qt Close stays asynchronous.

## Image archiving

An enabled Basler session owns one `application.image_archive.ImageArchive` thread.
The source retains the original immutable full BGR Frame before runtime submission;
the terminal camera result must match its session generation, CycleId, CameraId,
complete FrameId and dimensions. Generation is session-bound, not guessed from
timestamps. Missing, unfinished or ambiguously attributed frames are skipped;
display copies are never a substitute. Borrowed aliases remain immutable until
archival write/drop as well as inference completion. No additional full-image copy
is made at handoff.

Each camera's six channels determine its code: any definite channel NG -> `1`;
otherwise technical ERROR -> `2`; otherwise at least one confirmed EMPTY with all
remaining channels explicitly OK/EMPTY -> `3`; otherwise all six explicitly OK ->
`0`. UNKNOWN prevents EMPTY/OK classification. EMPTY means confirmed absence in an
inspected frame, never absence of a frame. The matching
Recipe switch controls one image per camera/cycle, independently of other cameras.
Unassigned findings do not invent channel judgments. Collection-forced rejection
does not turn an unfinished frame into an inspected archive image.

Handoff is bounded and performs no encoding/filesystem work. The session registers
PLC submission first; image writing never gates accounting, reporting ACKs or
inspection deadlines. Limits are 8 collecting cycles, 12 queued/active writes,
32 MiB per frame and 128 MiB combined retained/queued/active raw pixels. Encoding
uses additional transient storage for one image, so this is not an RSS cap.
Overflow drops archival work with scalar diagnostics; disk/encoding/path failures
record a bounded last issue and continue subsequent images. Bridge observation
shows an archive warning/detail without changing judgments, counters or PLC state.
No completed-image history is retained. Stop/Close drain accepted writes after
runtime callbacks settle and wait for the actual writer exit on the application
lifecycle thread. A blocked writer keeps shutdown pending; no timeout abandons it.

The exact selected root receives `YYYY-MM-DD/<CameraId>/<code>_HHmmssSSS_<cycle-id>.png`.
Configured `CAM1`..`CAM6` names remain unchanged; filesystem-unsafe camera bytes
are escaped without assigning a different camera identity. One local wall-clock
observation timestamp captured at frame retention supplies both date and filename
(it is not a claimed camera exposure time). PNG preserves all original pixels and
dimensions, without overlays/crops. Same-directory `.part` write/fsync followed by
atomic no-replace publication avoids completed-looking partial PNGs. Existing files
get `_1`, `_2`, etc.; exhaustion at 999 collisions is an observable failure.
There are no sidecars, indexes, retention rules or automatic deletion. Recording
is best-effort, not guaranteed evidence delivery or storage-capacity management.

## Display and Web

The shared Qt shell uses one layout with window-scoped dark/light palettes and
DPR-aware vector icons. Normal startup maximizes the native framed window, keeping
taskbar clearance; portrait Production retains two columns and three rows. Theme
switching is presentation-only and is not persisted. Language retains its existing
preference behavior. The fixed bottom strip presents application state, the most
recent PLC reporting observation (ACK is not connectivity/consumption), Web owner
availability and current feedback. It reuses bridge refresh/notifications without
another timer or network request.

Production keeps Start/Resume, Pause/Step and Stop in fixed player positions.
The middle control becomes Step while paused; Folder and Basler retain distinct
source-boundary semantics. Reset is a separate fault-recovery action. Basler control
only affects software admission, never equipment or PLC mode. Empty canvases and routine acquisition waits stay quiet, with
acquisition progress available in the state tooltip. Read-only channel symbols
retain full ChannelIds and technical evidence in tooltips; only identity-matched
results draw defect boxes.

Rendering is not result completion or accounting. The display store owns separate
bounded latest-per-camera copies with generation, cycle, complete FrameId,
dimensions and source key. Only matching evidence may overlay those pixels.
Vision uses temporary browsed/imported/captured images or eligible frozen Runtime
pixels; no separate persisted reference library exists. Recipe preview retains its
internal frozen-image contract. Capture uses the same SDK owner, accepts one pending
request with a monotonic deadline and first requires an empty complete SDK boundary.
It never sends software/PLC triggers or opens another camera. Camera/source changes,
Disconnect, Start and Close cancel pending capture; a Qt request token rejects stale
delivery. A frame already buffered at request processing is not substituted for a
new capture. The boundary is an SDK observation, not proof of physical exposure time.
Capture success is temporary canvas data, never inspection admission or accounting.

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
