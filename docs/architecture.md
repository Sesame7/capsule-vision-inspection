# Runtime architecture

The normal entry point is [app.py](../app.py). Current implementation locations
describe responsibilities, not a framework that must be expanded for future work.

## Execution and ownership

An unconfigured normal window retains the same deferred session factory,
Application, Qt bridge and display store as a configured window. Vision can save
source definitions independently and initialize new ROI drafts only from actual
dimensions. A complete saved layout and explicit Recipe Prepare/Apply make that
existing composition usable without restarting. No source or engine starts on
Save; display camera identities are installed on candidate configuration.

```text
saved Model / Recipe / MachineLayout / CameraSources
  -> stopped prepared candidate -> Start / warm-up -> active session
  -> passive Basler frames or Folder Replay
  -> explicit identified Frames / cycle admission
  -> bounded runtime -> one InferenceWorker / ViMo engine
  -> configured channel inspection -> compact terminal result / accounting
       -> bounded serial first-station PLC reporting
       -> bounded SQLite Production history
       -> bounded original-frame PNG archive (opt-in Basler Recipe)
       -> independent latest display copies -> Qt and read-only Web
```

| Package | Responsibility |
| --- | --- |
| `configuration/` | Strict definitions, saved preferences, pure resolution and atomic persistence; no engine/session |
| `inspection/` | Frame/cycle identity, synchronous collection and channel evidence/judgment |
| `inference/` | Typed synchronous API, SDK boundary, staging and owned outputs |
| `runtime/` | Engine worker, serialized collector, bounded dispatch and terminal delivery |
| `application/` | Startup/composition, stopped editing/preparation, activation, sessions, sources, counters and history persistence |
| `localization/` | Exact language identities, native ordering, static catalogs and text resolution; no Qt or Configuration dependency |
| `ui/` | Qt entry/window wiring, visual styling, widgets, presentation and queued notifications |
| `web/` | Latest-only publisher and serial cached HTTP delivery |
| `communication/` | FINS/Modbus TCP clients, one first-station report owner and UDP simulator |

Only the inference adapter imports SmartMore. pypylon is isolated in the lazy
Basler SDK backend. Domain/runtime layers do not import Qt. The GUI never calls
ViMo or blocks on unfinished Futures/joins; `FutureDelivery` crosses to queued Qt
slots. `QtProductionBridge` commands/presents the application, not its lifecycle owner.

Configuration owns the application and Communication preference definitions/codecs
in `preferences.py` and `communication_preferences.py`. Its private `_io.py` shares
duplicate-key JSON decoding and closed, flushed/fsynced same-directory file
publication. Codecs retain schema validation, contextual errors, parent-directory
policy and missing-file behavior; startup owns recovery and live service selection.
Recipe creation uses exclusive publication, ordinary Save replaces one target, and
multi-file saves remain independent. Failed publication and temporary-file cleanup
remain observable; no filesystem retry or cross-file transaction is implied.

`configuration/catalog.py` owns Recipe file operations and Model discovery;
`configuration/drafts.py` owns the independent saved/working copies. Shared ROI
rounding/bounds live in Configuration, while gestures and image transforms remain
in Qt presentation. `application/editing.py` coordinates Recipe selection, Save,
Delete, resolution and preparation without importing UI. Qt dispatches explicit
actions and queued completions; candidate installation and Start remain separate.

Production composition is Qt-independent: `application/startup.py` owns entry-relative
paths, persisted loading/validation and service assembly; `composition.py` builds
the normal/deferred and explicit-path applications without starting inspection.
`production_application.py` owns stopped source connections, admission and frozen
reporting/replay selections. `production_session.py` owns each generation's source,
inspection coordination, report/archive handoff and actual joins. Existing worker,
SDK and connection owners remain authoritative; these modules add no service owner.
Composition/startup return prepared Application configuration. Its snapshots capture
the immutable active/candidate prepared values and compact identities under the same
lock, without IO or copying definitions. Pending means both resolved configurations
exist and differ; artifact-path binding and the managed flag alone do not imply a
pending Recipe change. Qt derives names and card layouts from these observations,
without a configuration registry or a cache of previously observed activation.
RUNNING cards use active configuration; other states use candidate where available.
A saved startup layout remains the initial fallback when no Recipe is selected.
Qt bootstrap creates windows and queued source/preference notifications, binds the
observer, and retains the existing event-loop/Close wiring. Explicit successful
Apply still delivers its prepared value for preference persistence and feedback,
including when reapplying unchanged contents produces no snapshot change.

The equipment-free UI demo seeds ordinary isolated definitions and fixed image
files, then reuses this explicit-path composition. It supplies fake inference,
preparation and label-reading dependencies; normal defaults remain real. Its
source belongs to each production generation, with no launcher-owned acquisition
thread. Fixture ROI initialization uses the same MachineLayout draft geometry;
existing saved geometry is never reseeded. Normal and demo entry points retain
ownership through Qt construction failures and drain the window's asynchronous
Close path after event-loop exit, before final Application/service joins.

`MainWindow` owns Recipe and Vision as peer workspaces. Its
`ui/editing.py` coordinator shares the existing `RecipeEditorSession` and owns
one bounded setup/Recipe-preparation IO slot, queued completion and actual thread
join. Model controls submit directly to that same owner; neither workspace owns
the other's construction, navigation or shutdown. Vision save/reload policy lives
in Application editing, with independent Machine/Source outcomes. Request tokens,
current selection and closing state guard late results. Close drains setup/import
cleanup and the independent label/Preview work before releasing presentation.

## Inference

Recipe Model management reuses this window-owned slot. A short Application
admission context fences management work against Start, preparation and Close, without filesystem work
under the command lock. No second engine, manager thread or GPU scheduler is
created. SDK metadata inspection loads a hashed memory snapshot and closes its
Solution on that worker; module creation remains exclusive to the existing
InferenceWorker. Unpublished imports stay outside catalog discovery. Configuration
owns scoped file operations and saved Recipe references; Application additionally
protects candidate/active identities and unfinished resource owners. Model actions
do not activate a Recipe or change counters; explicit protected Model deletion
also removes that Model's own cache.

`VimoSegmentationEngine(model_path, device_id=0, module_id=...)` loads once and
reuses one GPU segmentation module with `max_batch_size=1`. In worker execution,
an engine factory constructs, calls and closes it on the sole worker thread;
CLI callers may own it in a serial context. Instances are not shared concurrently.

Inputs are nonempty uint8 H x W x 3 OpenCV BGR arrays. Validation rejects other
representations, normalizes non-contiguous inputs when necessary, and never
resizes/reorients or guesses RGB. All aliases of a borrowed worker input must
remain immutable until its task Future completes/cancels. Owned `Frame` pixels
are immutable while inspection/snapshots retain them.

Normal composition explicitly marks catalog-owned Models for
`VimoSegmentationEngine.from_managed_model`: the serial SDK owner verifies the full
configured SHA-256, directly loads `data/models/<model_id>/model.vimosln`, and checks
its hash again after initialization. No redundant runtime model is staged. The SDK
generates/reuses adjacent `vimo_trt_cache/`; authoritative Model content and
`model.json` remain unchanged. Linked source/cache paths and adjacent
`vimo_params.json` overlays fail instead of changing the verified input/settings.
The general-purpose constructor and explicit-path engineering workflows retain
isolated `outputs/vimo_cache/<sha256>/` staging, with no path-based autodetection,
automatic cache migration or deletion. Module selection follows explicit model
configuration, or the engine's verified unique-segmentation selection. Existing
area/dimension thresholds, ROI and mask operation remain as exported. Mask and
region information are required; region flags include area, long/short sides,
bounding/min-area rectangles and outer/inner contours; score maps remain disabled.
No `vimo_params.json` overlay or environment repair is performed.

The SDK can additionally extract a provider and write a TensorRT timing cache in
its process temporary directory. These normal SDK writes are permitted.
Model-relative engine artifacts and auxiliary temporary data are distinct; neither
file presence establishes initialization or future readiness.

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
construction and close stay off Qt. The real factory uses the
same managed Model path/adjacent cache and SDK settings as Production. GPU module
creation lets the SDK generate/reuse opaque TensorRT artifacts. No source, Recipe,
counter, cycle or reporting session is created. Preparation initializes the Model
without inference; no image or dimensions are required.

Preparation and Production Start are mutually exclusive under the application's
command lock, including construction and cleanup. Its noncancellable result Future
completes after worker join and reports initialization and cleanup outcomes.
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

Normal persisted machines support exactly CAM1–CAM5/CH1–CH30 or
CAM1–CAM6/CH1–CH36, six slots per camera. `data/machine.tsv` explicitly stores
ROI coordinates by ChannelId; fixed channel arithmetic derives canonical camera
and slot identities. Camera-keyed `data/camera_sources.json` matches sources by
identity; file order never identifies hardware. Incomplete definitions remain
editable, while connection/Start enforce complete usable sources. A missing
configured camera remains a missing result, never an automatic count reduction. One Folder/Basler
runtime serves both configurations. Count edits require stopped/disconnected
sources and take session effect only on successful Start; existing active identity
and cumulative counters are retained until their ordinary lifecycle/reset actions.

`CycleCollector` has one serialized runtime owner. Admission acknowledgements,
worker task Futures and cycle terminal results are different observations; none
implicitly returns Frame storage. Every admitted cycle gets one terminal result
or explicit runtime exception, without unbounded completed history. Commands,
active collection, inspection backlog and worker jobs are bounded; overload is
observable rather than silently dropped or converted into a model judgment.
Deadlines use host monotonic time and explicit runtime expiration, not Qt polling.

Configured Folder inspection delivers one typed terminal completion containing
both raw frame evidence/timings and production judgments. The session forwards
that production outcome directly; it keeps no separate CycleId result lookup.
Every coordinator caller supplies an explicit inspection callback; the coordinator
does not select an inspection algorithm. Production assembly uses configured Recipe
execution. Standalone `inspect_cycle` and raw evidence types remain independent
inspection contracts. Application accounting and actual worker/resource release
remain separate boundaries.

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

Vision's explicit Connect all opens/arms all five or six saved/applied Basler mappings on one
application-owned SDK thread. Connection survives page/Recipe/Batch changes and
Production Stop. CONNECTED/ARMED is independent of inspection RUNNING; there is no
automatic connection/retry. Idle connected frames are retrieved, validated and
released without cycles, counters, display publication or PLC reports. One optional
idle capture request copies only the selected camera's next matching frame into a
Future for Vision; it does not enter the Production path.
Acquisition runs through `ContinuousBaslerSource` on this connection owner;
`application.basler` provides read-only discovery, serial validation and shared SDK
observations.

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

Normal Basler Pause closes new inspection admission while keeping all configured cameras
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

Startup reads versionless definitions from entry-relative `data/`. Optional
preferences select only a last-applied Recipe and language; each Recipe selects
its own Model explicitly. Managed Model IDs come from immediate child directories
(`model.json` and `model.vimosln`); Recipe IDs come from JSON filename stems.
Discovery isolates invalid entries with diagnostics, and only the selected
Recipe's dependency chain determines readiness. A missing catalog is empty,
never a first-entry fallback. A missing preference file leaves selection empty.
Recipe Save accepts incomplete definitions without activation; Prepare/Apply
requires resolved identities and verified artifacts before preparing a stopped candidate. Start creates a disposable session and warm-up with
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

## Production history

Normal startup owns one `application.history.ProductionHistory` for
`data/history.sqlite3`, including when the shell is UNCONFIGURED. Application
accounting hands off each authoritative terminal Production result with its host
wall-clock completion observation, active Recipe/Model identity, Run and Batch
segment. A successful Start creates a fresh Run UUID; Stop/Start retains the
segment. Every successful explicit Batch creates a new segment UUID, even for
the same label. A new application starts fresh identities and counters; browsing
old records never restores current state. Warm-up, previews, idle captures and
manual communication requests create no records.

One background owner performs all SQLite access, projection, compression and
retention. Its non-waiting inbox holds at most 128 pending writes/queries, plus
one active operation. Stored rows have independent persistent IDs and unique
`(run_id, cycle_id)` identities. Compact details retain configured camera/slot/
channel identities, business/technical/judgment states and existing abnormal
reason codes. Images, masks, raw outputs, measurements and full snapshots are
excluded. Neither judgments nor PLC delivery depend on storage success.

Recipe display names come from the application's immutable resolved configuration:
Production presents the active name separately from the prepared candidate, and
each History submission freezes that active name without changing its Recipe ID.
The optional name extends the existing detail payload; older records retain an
unknown historical name and show their ID, without consulting current Recipe files.

Only committed inserts increment the owner's persisted count. Overflow rejects
the record explicitly; storage failures and dropped/failed counts remain visible
in History without faulting Production. This is best-effort evidence, not a
completeness guarantee. Normal Close settles inspection first, then drains
accepted history requests and joins the actual persistence thread off Qt. A
blocked write keeps Close pending; no timeout abandons the owner or force-cancels
I/O. Failed database initialization remains unavailable until a new owner is
created; it does not replace the database or retry automatically.

Retention removes at most 256 rows strictly older than 30 days before each
operation and on interruptible 60-second idle observations. Backlogs expire
incrementally. This uses recorded host wall-clock times, so clock adjustments
affect ordering and retention. SQLite can reuse deleted space; there is no
automatic VACUUM or image deletion.

History queries run on that same owner. The Qt page retains at most 50 cycles
with keyset Newer/Older navigation and at most 100 recent Batch-segment choices
within the date scope. Segment labels show the earliest retained cycle completion
for that segment, not its Batch creation time; retention can advance this observation.
Identical timestamps gain a short segment ID, while filtering always uses the full ID.
Filters and upper-right pagination size for translated content and wrap on narrow pages.
Full Cycle/Run/Batch/Recipe identities remain in the stored record rather than a
repeated detail row. OK/NG filter the stored product judgment, ERROR the technical
assessment, and EMPTY/UNKNOWN channel business states; a cycle can match several
filters. Page entry, filters and Refresh request data; the scalar warning refresh
does not poll the database. The read-only detail matrix uses stored camera order
and slot/ChannelId mappings, with one row per camera. Channel selection exposes
the independent business/technical/judgment states and reason codes; NG and ERROR
can coexist. List summaries count channel OK/NG/EMPTY, adding UNKNOWN and independent
technical ERR counts when present. Cycle-level anomalies absent from those counts
remain explicit; routine COMPLETE/SUCCESS is implicit in the presentation only.
Query failures remain explicit. There is no image association, export or Web history API.

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
preference behavior. The fixed bottom strip preserves source identification and
connection counts, summarizes observed PLC worker and Web lifecycle states, and
retains current feedback. The three service groups have no runtime tooltips;
actionable failures use the existing visible feedback. Reporting retains its
visible lifecycle and ACK/success/ERR summary without runtime diagnostic tooltips;
detailed observations stay with their existing owners.
ACK is not connectivity, PLC consumption or physical rejection. It
reuses bridge refresh/notifications without another timer or network request.
The three persistent groups reserve compact role widths measured across each
language's concise states, recalculated on language/font changes rather than status
updates. This avoids both English-only clipping and reserving other languages'
longest captions at the expense of feedback. Longer source observations elide with
full accessible text; the event/error group receives all remaining horizontal
space. Its tooltip
retains full actionable failure/recovery text when the fixed row elides it, plus
specific configuration/file recovery detail. It does not expand routine telemetry.
Only the event group stretches when the window grows; status changes do not
move the persistent groups.

Compact captions use their surrounding page/card context: navigation shows
`PLC / Web`, service cards show Reporting and Web, and field labels use IPv4,
units and short localized names. Full localized meanings remain in tooltips and
accessibility text. Routine disabled services show `OFF`; waiting for the next
Start, active transitions, failures and ACK observations remain distinct. Idle
diagnostics omit no-request prose; watch success uses `OK` with its observation
time, while read/write failures remain visible. The diagnostic target stays
explicit and is not evidence that an edited preference was saved or applied.

The minimum supported window width is 1080 Qt logical pixels. UI validation uses
1080-wide portrait at 100% Windows scaling and 1920-wide landscape; wider cases
are retained where they exercise responsive allocation. Sub-1080 windows are not
acceptance targets. This support boundary does not change Qt's existing minimum
size constraints or the image canvas coordinate system.

Production keeps Start/Resume, Pause/Step and Stop in fixed player positions.
Each role reserves a shaped-text budget across the supported languages; switching
languages or action states does not move its slot. Header space after Batch can
shrink to retain the active/pending Recipe identity. Save/apply actions reserve
localized group budgets, borrowing flexible feedback/summary space. Conventional
utilities use touch-sized icons with full localized tooltips/accessibility:
History paging/refresh, Recipe color sampling, Vision numeric ROI Apply, and
Communication group creation and advanced-settings disclosures.
Watch removal, single read and automatic refresh form equal compact icon/text
slots budgeted across languages and refresh states; Add remains a separate square.
Automatic refresh keeps its one-second interval visible, a loop icon, a checked state and an explicit
Stop caption while active, distinct from Production Stop. Manual writing retains
a localized verb. Save/apply and service power controls retain visible verbs.
These compact action groups use four-pixel horizontal style padding and a
two-pixel focus allowance beyond their shaped caption/icon budget. Watch peers
use three-pixel gaps; their surrounding selector/Add spacing remains unchanged.
Touch heights, fonts and translations are unchanged; widths remain content-based
and stable across languages and action states.
Action buttons never stretch into surplus row space;
the Watch selector receives the remaining space. A single selector/Add/action row
is preferred whenever the actual available logical width accommodates the controls.
Dynamic group names do not enlarge its minimum or force wrapping; long values may
elide, with their full text in tooltips/accessibility. When space is insufficient,
the selector/Add stay above the three actions, leaving surplus space after that
narrow action group. Reflow retains the same controls and Watch operations.
Watch columns reserve language-stable heading/value budgets before sharing surplus width.
History wraps pagination below filters when their compact icon group cannot share
a row. Date and filter budgets use that row's available space, retaining equal
pagination buttons and equal bounded Batch/Result selectors. Dynamic values may
elide with full tooltips; localized date digits retain their measured width.
The middle control becomes Step while paused; Folder and Basler retain distinct
source-boundary semantics. Reset is a separate fault-recovery action. Basler control
only affects software admission, never equipment or PLC mode. Empty canvases and
routine acquisition waits stay quiet. Read-only channel symbols retain static
ChannelId tooltips and accessible short states, without expanding runtime evidence.
Only identity-matched results draw defect boxes.

Production shows localized Cycles, `OK`, `NG`, `EMPTY` and `Δt` captions for completed
cycles, channel occurrences and last inspection duration. Counts come directly
from the application business counters; unavailable
timing displays `--`. Whole metric groups wrap when their text cannot fit one row.
These are cumulative occurrences, retained by Stop/Start and Recipe changes and
reset only by explicit Batch, not physical capsule counts.

History uses short Time, `#`, Batch and `CAM` headings. Full identities remain in
stored records. The Time heading tooltip retains clock semantics, and elided
Batch/Recipe names retain their full-value tooltips. Result filters show the original status codes with
their independent product/technical/business meanings in localized tooltips.
Selecting a History channel shows its details below the matrix; cells do not
duplicate that text in tooltips. Camera tooltips identify the camera only; their
detailed diagnostic evidence remains in the stored record.

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

Normal startup owns a Qt-independent `WebRuntime` control thread and at most one
Web publisher/serial HTTP pair. Production Stop/Start leaves Web independent.
Missing communication preferences leave Web and PLC reporting disabled; valid
saved settings restore independently of camera source mode.
Communication's header power action persists only Web enablement, then starts or
stops using saved network settings. Conditional Apply changes restarts differing
saved/running settings; drafts never become runtime inputs implicitly. Admission
allows one operation, with no restart backlog or completed-result history. The
control owner constructs/binds off Qt; Stop interrupts HTTP I/O and joins both
actual service threads/socket before releasing ownership or rebinding. Close
fences new operations, waits for in-progress startup/cleanup and terminates the
control thread before the native window closes. A caller's timeout is not release;
incomplete cleanup retains ownership and prevents replacement. No thread is
forcibly cancelled. On failed replacement, one restoration of the previous running
configuration is attempted only after failed-owner cleanup; success/failure remains
explicit and does not change saved settings or Production state.

It keeps one current metadata publication plus a replacement being built; JPEGs
remain at most 640 pixels on the longest side, quality 70. Per configured camera,
the publication retains only current and at most one previous compatible encoded
image. Metadata-only revisions reuse the same bytes and image URL. The URL binds
publisher identity, an opaque monotonic image epoch, generation, full FrameId,
display frame revision and source geometry. The epoch changes after observed
source-key, generation or geometry boundaries, missing/invalid image evidence,
encoding failure or camera removal; retired URLs cannot revive after recovery.
Previous encoded bytes are shared, never copied/re-encoded for delivery grace.
At most 12 encoded images are lookupable; building a replacement can temporarily
retain six additional encodings. A serial response pins just its chosen immutable
JPEG (or metadata bytes), which may finish after invalidation without changing
its identity. Confirmed service-thread exit releases the publication/cache; an
incomplete join retains ownership and cache. There is no disk cache or image history.
Application/display snapshots are sampled separately, not atomic multi-camera capture.
Publisher ID, revision and source identities prevent old bytes/new evidence aliasing.
Applying a changed camera set clears retained display pixels and advances its
revision; the existing publisher follows that configured set without another Web
owner. History stores each cycle's own five/six-camera identities and order, so
older 36-channel records remain unchanged beside new 30-channel records.

Default server/browser interval is 500 ms (saved Communication preferences also
allow 1000/2000 ms; Save is separate from explicit runtime application). The publisher
waits after each publication; the browser never overlaps polls, fetches JPEGs
sequentially and owns at most six displayed object URLs plus one decode. Requests
have a five-second poll deadline; 410 waits for the next observation, without an
immediate retry loop. After a superseded batch, the same serial queue starts with
the next camera to avoid fixed queue-tail starvation; configured DOM order is
unchanged. Publication age beyond six seconds is stale, separate from
application FAULT. HTTP uses one serial handler, backlog four and one-second I/O
timeouts. Each replacement gets a fresh publisher identity/revision sequence;
old URLs cannot alias new observations, even at the same address. STARTING,
RUNNING, STOPPING, STOPPED and FAILED describe service observations, not Production
readiness. All service threads/socket ownership must exit on process Close.

The browser preserves camera nodes while their configured set/order is unchanged.
The last compatible complete image and its own six-channel judgments stay visible
without Pending/Retained pulses during normal refresh, delayed matching results,
decoding and temporary 410 responses. Each camera commits decoded pixels and
matching judgments together; an incomplete replacement never receives or changes
the displayed image's judgments. Without a complete observation the view stays
neutral; reference/retained pixels can still appear without inferred judgments.
Only an authoritative matching observation renews a displayed result's freshness:
the existing six-second rule is checked on polls/completions, without another
timer. Fresh metadata about an incomplete or undelivered replacement cannot keep
old judgments fresh indefinitely. Disconnects, invalid results and genuine stale
observations neutralize judgments; application FAULT remains separate.
Publisher, camera, frame-generation/shape and changed generation-zero reference
boundaries invalidate incompatible pixels. Missing/invalid image evidence also
clears them. Genuine server-retained/reference and stale views retain their subtle
markers. Unchanged captions, symbols and attributes are not rebuilt. Business
status and independent technical ERROR remain distinct. This preserves the
bounded expiry/410 behavior and independent per-camera commits. One small
viewport/aspect-ratio grid selection maximizes contain-fitted
image area, without a fixed desktop width or camera remounts on resize. Normal
presentation is just images, camera identities and six compact channel symbols;
connection/stale/fault feedback appears only when needed.

Routes are `/`, `/observer.js`, `/observer.css`, `/api/snapshot`, exact returned
`/api/cameras/<CameraId>.jpg?publisher=...&image=...`, and `/health`. The returned
image key encodes `image_epoch-generation-cycle-sequence-frame_revision-height-width`;
clients validate it against `image_identity`, not the global metadata revision.
Snapshot is 503 before first publication; expired/incompatible JPEG identities
are 410, unknown camera identities are 404 and malformed queries are 400. An image
two replacements behind may expire even on a healthy service. Serial delivery
does not guarantee simultaneous cameras or freshness on arbitrarily slow links.
There are no write routes, CORS, authentication or HTTPS. Explicit assigned IPv4 binding is for a
trusted isolated LAN; startup failure is optional and does not invalidate Production.

Verification commands and retained developer entry points are in [README](../README.md).
