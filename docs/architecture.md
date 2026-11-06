# Runtime architecture

The normal entry point is [app.py](../app.py). Current implementation locations
describe responsibilities, not a framework that must be expanded for future work.

## Execution and ownership

An unconfigured normal window retains the same deferred session factory,
Application, Qt bridge and display store as a configured window. Vision can save
source definitions independently and initialize new ROI drafts only from actual
dimensions. A complete saved layout and explicit Production Recipe selection
make that existing composition usable without restarting. No source or engine starts on
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
Recipe Save only persists the editor definition. Production selects a saved Recipe
independently, resolving current saved dependencies on the existing bounded IO
worker. It never reads or replaces the editor draft. The saved definition, stopped
candidate and active session are distinct; only successful Start promotes a candidate.

Prepared candidates retain immutable saved Vision definitions and their paths.
Vision has one Save action with independent MachineLayout/CameraSources outcomes;
it never enters Production preparation or emits a candidate-installation event.
Successful Save/reload observations mark a differing candidate as requiring
re-preparation without replacing its Recipe identity or an active session. Identical
parsed definitions leave a current candidate valid. Production alone selects and
prepares saved Recipes, including explicit re-selection of the same ID.
The existing Application lifecycle worker verifies persisted Vision dependencies
again before invoking the session factory. Changed or unreadable definitions reject
Start before engine/session side effects and retain the previous candidate/active
values. Session creation consumes the candidate's captured sources and layout.
Snapshot presence is authoritative: missing captured CameraSources reject Start
without a fallback disk read. Fixed-image launchers capture their own source mapping;
its absent file path denotes launcher ownership, not missing captured values.
No filesystem validation is added to Qt dispatch or the Application command lock;
no watcher, automatic preparation or cross-file transaction is introduced.
Fixed-image engineering/demo launchers retain their explicit source ownership.

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
candidate installation still delivers its prepared value for preference persistence
and feedback,
including when reselecting unchanged contents produces no snapshot change.

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

## Local identity ownership

`application/identity.py` owns an anonymous-by-default immutable session and one
bounded serial account/credential worker (16 waiting requests plus one active).
SQLite and PBKDF2 execute on that worker; normal consumers use Futures and cheap
session/storage observations. Logout advances the session revision immediately.
Starting authentication supersedes the previous pending attempt without changing
the active session. The service tracks that attempt separately from the admission
revision; both must still match at commit. `cancel_login` invalidates the specific
pending attempt under the same lock as Last Login persistence/session publication,
without undoing an already committed login. Admission revisions prevent stale
administrative work from replacing a newer session decision. Password work stays
outside the session lock; short authorized transactions and their session
publication are serialized together.

The private account store validates v1/v2 records and migrates them to v3 in one
transaction, preserving IDs, credentials and Last Login. LocalIdentity owns both
first setup and subsequent online mutations under its existing lease and audit
owner. Setup leaves the session anonymous. A verified temporary password commits
Last Login and publishes a restricted revision. All role admissions, including
Guest replay, reject that revision. Only logout and a revision-bound password
rotation are available. PBKDF2 runs outside the lock. The final password update,
rotation-flag clearance and session publication share the service commit boundary.
Cancellation invalidates uncommitted rotation and cannot undo a completed change.

The account service protects every existing Administrator from administrative
mutations and requires confirmation for granting Administrator access. The store
also guards the last enabled Administrator transactionally. The offline GUI and
CLI use the same OS-trusted maintenance operations and exclusive file lease. Each
records its provenance through the existing audit writer when available and warns
on audit loss. See [local accounts](configuration.md#local-accounts).

Startup acquires identity ownership before creating application resources and
passes that owner into the existing Application lifetime. Close drains it and
joins the actual worker after the existing source/History cleanup. Missing account
storage remains Guest without creating a database. Unavailable storage remains
explicit. The window presents Login, Logout, Switch User, Change Password and
Administrator account management through this same service. The forced-change
form is window-modal. Credential forms clear secrets on submission or dismissal.
The service worker handles authentication and account IO. Qt receives results
through its destruction-safe queue and ignores superseded sessions. The existing shell refresh observes role
changes. Manage Users presents a read-only four-column table and captures account
IDs in separate role and disable-confirmation dialogs. Switch User retains the
current login until successful replacement. Dismissing its pending credential form
invalidates authentication at the service boundary as well as queued Qt delivery.
Dismissing pending first setup or forced rotation fences the initiating revision.
Dismissing an ordinary account-management dialog stops delivery to that form.
The service completes accepted work under its authorization checks. The shell
disables account UI on confirmed Close. Application retains worker stop/join
ownership. Login/logout never controls an inspection session.

Production checks the session at command admission under the Application lock.
Start freezes automatic reporting and Guest demonstration provenance for the
admitted generation. Saved Recipe installation additionally validates the
initiating session revision and request identity. Startup supplies restored
settings and candidate during construction. Normal startup always supplies a
LocalIdentity, including when account storage is unavailable. Explicit identity-free
engineering compositions remain trusted. Runtime delivery and shutdown keep their
existing owners. See the [Production policy](configuration.md#local-accounts).

The Qt shell refreshes permission-sensitive controls when the observed session
revision changes. The Recipe page can present an immutable catalog entry while
the editing owner retains its working copy. Its existing metadata worker reads
the displayed Model and checks that Model again before delivering labels.
Production exposes its Guest replay eligibility and generation provenance for
the player. Command owners still validate each operation at admission.

Engineering commands use the same LocalIdentity and revision as Production.
A pending confirmation, staged Model import or Vision validation retains its
initiating revision. Each subsequent publication checks that revision. The
Model file and cache owners release the identity lock before SDK work. The
Operator Recipe selection path shares the file ownership fence and retains its
own selection ticket. Import cancellation and actual worker shutdown are
independent of the current account.

## Audit Trail

Normal Application composition attaches one serialized audit owner to LocalIdentity.
It accepts up to 128 waiting requests plus one active request. Its SQLite connection
stays on that worker. History, accounts and JSON files retain their respective
transaction owners. Explicit identity-free engineering compositions have no local
account audit owner.

Account changes, Recipe publication/deletion, Model staging/publication/rename/
deletion/cache preparation, each Vision file save, communication settings and
manual PLC writes require a committed intent. An intent freezes the actor, target,
requested values, UTC time and a unique operation ID. The business worker waits
for that commit outside the session lock, checks the initiating revision again,
and executes the existing command. Successful publication and failed or uncertain
execution append separate terminal evidence. Repeating the same fact for the same
operation is idempotent. A crash between the intent and completion leaves the
operation unresolved. An audit finalization error leaves the business result intact.

Authentication, logout, actual Start/Stop, Batch changes, unbatched statistics
clearing, saved Recipe selection and important security or persistence failures
submit observations without waiting.
The audit writer exposes a separate storage issue and count of unconfirmed writes
for its lifetime. An abrupt process exit can lose queued best-effort observations.
Inspection, automatic reporting and Stop continue through audit loss. Close drains
account work and accepted audit records and joins their owners.

History includes an Inspection History tab and an Audit Trail tab. Engineer and Administrator
sessions can query audit records by UTC date, exact username or account ID, and
action. The SQLite owner pages by stable insertion order. Qt shows up to 50 rows
and clears private details on session change. Review access is checked by the
service as well as the tab. Ordinary navigation, refreshes, per-exposure results
and automatic PLC transactions do not produce entries.

## Alarm monitoring and history

Normal Application composition owns one independent, read-only monitor for
the [saved alarm selection](communication.md#plc-alarm-monitoring). Its worker
captures definitions at activation and holds one observation and one connection
at a time. A first active signal is recorded as observed active. Subsequent
successful reads record changes only. During communication loss the last known
active set remains available and is marked stale. The first valid reading after
a gap reconciles differences as observed active or observed recovered. All
timestamps are host observations. Pulses entirely between reads can be missed.
A new activation starts a fresh observation baseline.

One nonblocking handoff carries all transitions from a word to the existing
History SQLite owner. Its bounded queue retains the lifecycle slot for Run
closure. An additive alarm table is created on the first event. Each row freezes
the signal ID, required Chinese and English labels, any configured translations,
category, endpoint, station, word/bit,
definition origin/version/digest and the selected Batch UUID and label when
available. Actual Production FAULT entry and explicit fault reset use a
separate application source and carry no PLC address. Storage loss is visible
in Alarm History. Bounded retention removes alarm observations older than 30
days and leaves Batch totals and the independent Audit Trail intact.

Existing bilingual alarm tables can be reviewed immediately. The History owner
adds one nullable translations column on the next alarm write. Earlier rows
retain their labels and cursors. Changing the UI language selects from the
labels captured with the observation, with English fallback. Reading History
never consults the current equipment file.

All roles can see the PLC alarm status in the existing 58-pixel strip. Operator,
Engineer and Administrator can review the third History tab by UTC date, signal
category and exact Batch label or UUID. Pages contain up to 50 observations and
private rows are cleared on identity change. Close waits for the actual monitor
worker before draining History.

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

Vision's explicit Connect all and Production Start from DISCONNECTED open/arm the
required five or six saved Basler mappings on one application-owned SDK thread.
Start coordinates connection on the existing Application lifecycle worker, after
saved-dependency validation and before session construction. It uses the prepared
candidate's captured sources, never unsaved Vision drafts. CONNECTING,
DISCONNECTING and source FAULT reject another Start connection attempt; mismatched
connected mappings require explicit recovery. There is no automatic replacement,
reconnection or retry. Connection survives page/Recipe/Batch changes and successful
Production Stop. CONNECTED/ARMED is independent of inspection RUNNING. Idle connected
frames are retrieved, validated and released without cycles, counters, display
publication or PLC reports. One optional
idle capture request copies only the selected camera's next matching frame into a
Future for Vision; it does not enter the Production path.
Acquisition runs through `ContinuousBaslerSource` on this connection owner;
`application.basler` provides read-only discovery, serial validation and shared SDK
observations.

Basler Start establishes or reuses a matching CONNECTED source, checks every saved
ROI against current camera Width/Height and warms one ViMo worker with black uint8
BGR inputs, twice per distinct shape. Folder references are optional. Only after
warm-up and an idle,
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

The Basler Production player exposes Start/Stop and applicable Fault Reset;
Pause/Resume/Step remain internal admission controls, not Production UI actions.
Internal Basler Pause closes new inspection admission while keeping all configured
cameras grabbing and the same worker/engine alive. The SDK owner continues retrieving,
validating and releasing paused frames without publishing images, cycles, counters
or reports. PAUSING becomes PAUSED only after the admitted capture boundary,
terminal inspection, actual camera-task return and accepted reporting settle.
Reporting settlement may be ACK or an explicit failure; it is not PLC consumption.

The explicitly enabled [four-cycle reporting mode](communication.md#four-cycle-reporting)
uses an alternative serial communication worker under the same generation/session
owner. Camera admission queues scalar CycleIds; terminal callbacks hand off the
existing reject projection. Only the communication thread reads the selected
W48/W148 counter or writes bitmap/commit words derived from W49/W149. Four observed
cycles are associated under external phase assumptions, without changing
scheduling, judgments, occurrence accounting or History.
At the existing successful pre-write attribution check, the worker publishes one
immutable four-result completion to Application. Separate `PilotCounters` classify
each ChannelId once using those business statuses and the already merged reject
words: every set reject bit means final NG (including conservative rejection),
otherwise all verified EMPTY means EMPTY, otherwise OK with only OK/verified EMPTY
observations. Original per-Cycle UNKNOWN/ERROR evidence remains intact. These conditional
physical-position totals never divide or replace `ProductionCounters`.
Application consumes the completion outside the reporting lock and without Qt;
one generation-local CycleId high-water mark prevents duplicate/stale accounting.
Incomplete or initially unattributable batches do not count. A subsequent bitmap
or commit failure does not retract an already eligible inspection completion.
Four-cycle reporting rejects Pause/Step/Resume at the Application boundary;
Stop/Close and actual worker cleanup retain their existing ownership. Disabled reporting has
no automatic owner, counter reads or group attribution, while per-Cycle inspection
and occurrence counting continue. New normal installations select disabled
four-cycle/FINS-TCP/W49; explicit engineering launchers retain per-cycle defaults.
Per-cycle mode retains the internal admission/draining semantics described here.

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

Full-camera ViMo runs at most once per eligible camera frame when enabled, then
findings are assigned against saved channel ROIs with original global ChannelIds.
After label filtering, each PRESENT or UNKNOWN channel consumes its assigned
findings, including a finding shared with another channel. Confirmed EMPTY
channels omit those assignments and their defect reasons from the effective
judgment. Unassigned findings remain available as diagnostic evidence. The
camera aggregates the channel judgments and collection status, and its defect
overlays show the findings and reasons consumed by those channels. See the
[PLC projection contract](communication.md#first-station-registers) for reject bits.

Occupancy always measures the centered `max(1, width*70//100)` portion of the saved
ROI, split into upper/lower views; a pixel is foreground if any BGR component is
greater than 20. Each view is EMPTY at <=1%, PRESENT at >=5%, otherwise UNKNOWN.
Only equal EMPTY/EMPTY or PRESENT/PRESENT pairs fuse to that state. Geometry stays
unchanged. Valid EMPTY explicitly skips enabled Size/Color with recorded
provenance. Enabled full-camera inference and result validation still run.
Execution errors, collection failures and forced rejects retain their own reasons.

Size thresholds the complete Channel ROI once (any BGR channel >20), then labels
8-connected foreground once. The divider at `height // 2` assigns logical views;
it does not crop Size bodies. Each component belongs to at most one view. Its
predominant foreground side and centroid position must agree. The existing view
minimum area, `max(12, floor(view_area * 0.005))`, and quarter-dominant competition
rule remain: a crossing component with minority support meeting that side's
minimum and at least 25% of its majority support is ambiguous in both views.
This conservatively rejects significant cross-view merges without reconstructing
objects. Smaller crossing tails stay attached to the complete selected component.
Within each assigned view, a qualifying runner-up at least 25% of the dominant
component's full area rejects that view. External Channel ROI edge contact is
truncated; internal-divider contact alone is valid. Missing, competing, ambiguous,
externally truncated and degenerate geometry remain explicit NG evidence.

Size reuses selected component labels to fit the minimum-area rotated rectangle
from transient external boundary vertices. Its longer side is Length, spanning
pixel centers (30 occupied axis-aligned pixels span 29 px). Scalar bounds, major
and minor lengths and original-image axis endpoints survive measurement; contours
and label arrays do not. Production, frozen Recipe tuning and the offline evaluator
use this same implementation. Thresholds do not affect measurement validity.
Each logical view independently requires `abs(length - target_length) <=
length_tolerance`. The other view never receives a duplicate or passing placeholder.

Color retains independently cropped upper/lower views, usable saturated/bright
HSV pixels and circular Hue statistics. Only requested capabilities execute;
Color-only does not label or measure Size bodies. Occupancy, ViMo and confirmed-EMPTY
skip/reporting remain unchanged. These checks require product/lighting calibration;
geometric tests cannot certify half-capsule detection or physical rejection.

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
Recipe Save accepts incomplete definitions without activation; Production preparation
requires resolved identities and verified artifacts before preparing a stopped candidate. Start creates a disposable session and warm-up with
zero production results/counters. Only successful Start promotes the candidate;
failed activation preserves last-known-good active identity and returns READY if
cleanup is safe. Stop/Start or a Recipe change retains the selected Batch's totals.
`ProductionCounters` describe cycle/channel occurrences, not physical
products; enabled four-cycle reporting's separate conditional channel-position totals are
described above.

Stop prevents inspection admission, finalizes incomplete collection and explicitly rejects
unclaimed inspection work. A claimed native call finishes naturally. Owners are
retained until source/runtime/worker/report threads actually exit and SDK cleanup
is safe. Stop during an unfinished automatic connection cancels that Start and
requests connection cleanup; successful Stop requires actual SDK-owner termination.
Unsafe cleanup remains observable as source/application FAULT with the original
owner retained for explicit recovery. Cancellation is ordered against CONNECTED
publication: once the connection is valid, Stop retains it even if later warm-up
or activation fails. Basler session shutdown detaches its callbacks and drains
reports but keeps the SDK connection grabbing/discarding. Vision Disconnect is
available only with inspection stopped; it releases cameras and joins their actual owner.
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
restores the selected Batch UUID and its cumulative totals before Start. In READY
an Operator can select a trimmed, nonempty, case-sensitive label or confirm End
Batch. The same label always resumes its original Batch on this IPC. Switching
labels deselects the previous Batch. End records an event and clears selection.

Each successful Start has its own Run UUID. After warm-up the lifecycle owner
requires a committed Run for a selected, durable Batch before publishing RUNNING.
Basler also opens a Run without a Batch, with a null Batch reference. Its
Cycle detail and eligible PLC groups use that Run UUID and cannot accrue to a
later Batch. The Run records its start observation, active Recipe/Model identity
and reporting mode, base and endpoint when enabled.
If unbatched Run recording fails, inspection continues with a visible warning
that totals and detail may be incomplete. Individual Cycle records are still
offered to History on a best-effort basis. Unbatched Folder Replay continues to
record individual Cycles without opening a durable Run.
Stop records termination after session cleanup, including for zero-Cycle Runs,
and preserves the Batch selection. Exclusive owner startup marks any abandoned
open Run interrupted and uncertain, with the recovery time as its termination
observation. Warm-up and preview contribute no production evidence.

One background owner handles every access to the live History database, along
with projection, compression and retention. The normal inbox admits up to 128
pending facts or queries, with one additional slot for a Batch/Run command.
Only the lifecycle worker waits for
Run confirmation. Stored Cycle rows have independent persistent IDs and unique
`(run_id, cycle_id)` identities. Compact details retain configured camera/slot/
channel identities, business/technical/judgment states and existing abnormal
reason codes. Images, masks, raw outputs, measurements and full snapshots are
excluded. History reads each stored judgment and reason as recorded under the
inspection policy in effect at the time. Neither judgments nor PLC delivery
depend on storage success.

Accepted Cycle admissions, terminal results and eligible four-Cycle completions
enter this owner through bounded non-waiting handoffs. For a named Batch a
transaction updates its summary once per source fact. The owner retains deduplication
keys for the lifetime of the open Run so detail expiry cannot allow a duplicate. The
eligible group stores its Run and first
Cycle identity, PLC counter, four actual member CycleIds in slot order, timing
and final channel outcomes. Joining on Run/Cycle works in either arrival order.
PLC ACK has no effect on these totals. Separate per-Run flags record possible
loss and expired detail. When storage becomes writable, a dropped or failed
fact marks that Run uncertain. A crash with an open Run also prevents a clean
completeness claim. Startup loads the saved summaries directly.

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

Retention removes at most 256 Cycle and 256 group rows strictly older than 30 days before each
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
Query failures remain explicit. For each visible Cycle the same query request
loads committed archive paths by Run/Cycle/Camera. Selecting a camera or one of
its channels enables **Open Image** only when that camera has a link. One click
opens the recorded PNG in the system viewer. History reports a missing file or
a rejected desktop open. History has no Web API.

Operators and higher roles can manually export any saved named Production Batch,
including an ended Batch, from History. The picker pages through durable Batch
identities independently of the Cycle filters. The SQLite owner captures one
dated snapshot of the selected Batch, its Runs and retained group detail. A
single export worker streams two Excel sheets from that private copy. Production
continues during export. Close requests cancellation and joins the worker.
Publication requires the initiating session revision and atomically replaces
the destination only after the workbook is complete.

Production Summary shows cumulative completed groups, Passed (final OK),
Rejected (final NG) and Empty. Inspected is Passed plus Rejected. Both rates
use Inspected as their denominator and show `—` when it is zero. Separate Cycle
observations include per-cycle and reporting-disabled Runs. Run rows record
start/end observations, outcome, Recipe and reporting mode. Data status flags
active Runs, uncertain totals and expired detail. Group Results contains one
row per retained eligible group with its recorded completion observation, Run,
PLC counter, four stored CycleIds in exposure order, duration and channel
outcomes. When the retained count differs from the durable completed total, the
detail sheet also shows the two counts above its table.

## Image archiving

An enabled Basler session owns one `application.image_archive.ImageArchive` thread.
The source retains the original immutable full BGR Frame before runtime submission;
the terminal camera result must match its session generation, CycleId, CameraId,
complete FrameId and dimensions. Generation is session-bound, not guessed from
timestamps. Missing, unfinished or ambiguously attributed frames are skipped;
display copies are never a substitute. Borrowed aliases remain immutable until
archival write/drop as well as inference completion. No additional full-image copy
is made at handoff.

Each camera's six channels determine its code. Any definite channel NG takes
precedence (`1`), followed by channel technical ERROR (`2`). When every channel
is explicitly OK or confirmed EMPTY, at least one OK gives archive OK (`0`) and
six EMPTY give archive EMPTY (`3`). UNKNOWN prevents EMPTY/OK classification.
EMPTY requires confirmed absence in an inspected frame. The matching
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
No completed pixels are retained in memory. Stop/Close drain accepted writes after
runtime callbacks settle and wait for the actual writer exit on the application
lifecycle thread. A blocked writer keeps shutdown pending; no timeout abandons it.

The selected root receives `YYYY-MM-DD/<CameraId>/<code>_HHmmssSSS_<CycleId>_<CameraId>_<RunId>.png`.
Start binds the full RunId to the session archive before opening Cycle admission.
If recording the Run was unavailable, PNGs still use its session RunId, without
claiming a durable History association.
Configured `CAM1`..`CAM6` names remain unchanged; filesystem-unsafe camera bytes
are escaped without assigning a different camera identity. One local wall-clock
observation timestamp captured at frame retention supplies both date and filename
(it is not a claimed camera exposure time). PNG preserves all original pixels and
dimensions, without overlays/crops. Same-directory `.part` write/fsync followed by
atomic no-replace publication avoids completed-looking partial PNGs. On collision
the new file gets `_1`, `_2`, etc.; exhaustion at 999 collisions is an observable failure.

Only after publication does the writer offer the absolute final path, including
any suffix, to the History owner for a durable Run. Its separate bounded queue
(128 paths by default) yields to Run, Cycle, group and query work. A link can
arrive before or after its Cycle. A dropped or failed link is visible in History
without invalidating the PNG or marking production totals uncertain. Close drains
accepted paths. Cycle retention also drops its links; unpaired paths expire
incrementally after 30 days. History never backfills links or deletes image files.

## Display and Web

The shared Qt shell uses one layout with window-scoped dark/light palettes and
DPR-aware vector icons. Normal startup maximizes the native framed window, keeping
taskbar clearance; portrait Production retains two columns and three rows. Theme
switching is presentation-only and is not persisted. Language retains its existing
preference behavior. The fixed bottom strip preserves source identification and
connection counts, summarizes observed PLC worker, alarm monitor and Web states, and
retains current feedback. The three service groups have no runtime tooltips;
actionable failures use the existing visible feedback. Reporting retains its
visible lifecycle and ACK/success/ERR summary without runtime diagnostic tooltips;
detailed observations stay with their existing owners.
ACK is not connectivity, PLC consumption or physical rejection. It
reuses bridge refresh/notifications without another timer or network request.
The persistent groups reserve compact role widths measured across each
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

Top navigation measures each page's current localized caption and font, using
natural, nonuniform widths and modest gaps. It first fits horizontal icon/caption
buttons beside the right-aligned current-user/theme/language controls. If that
does not fit, icons stack above captions while utilities remain on the same row.
If the stacked buttons and utilities still cannot share a row, utilities move to
a second, right-aligned row. Ordinary Chinese/English labels and short usernames
fit one row at 1080 and 1920 Qt logical pixels; neither width mandates a mode.
Larger fonts or translations can select a stacked row or two-row fallback.
After choosing the mode from intrinsic content requirements, buttons share spare
width toward bounded comfortable sizes. Longer captions retain more space;
short captions do not stretch across the bar. Surplus remains between the page
group and the right-aligned utilities. Width growth does not influence mode
selection, so resizing does not oscillate between presentations.

Mode selection uses actual available logical width and content/font budgets,
not screen orientation or physical resolution. Long captions have a font-scaled
width limit and elide, retaining complete localized tooltips and accessible names.
Each mode reserves stable height across selected pages, themes and ordinary
identity/status updates; language and font changes can affect the selected mode.
Resizing reuses the same five buttons, selection group and native language popup.
There is no sixth page in normal operation.
The current-user control has a fixed width; long usernames elide while retaining
the full username and role in its tooltip and accessible name. User/language
controls reserve enough height for the current font while retaining touch targets.
Account actions use a menu, without another navigation page or navigation selection state.
Navigation's minimum width uses touch targets and utilities, independently of its
last allocated row width. The shell reserves its content's minimum height when
two navigation rows or larger text need more vertical space.

The minimum supported window width is 1080 Qt logical pixels. UI validation uses
1080-wide portrait at 100% Windows scaling and 1920-wide landscape; wider cases
are retained where they exercise responsive allocation. Sub-1080 windows are not
acceptance targets. This support boundary does not change Qt's existing minimum
size constraints or the image canvas coordinate system.

Folder Replay keeps Start/Resume, Pause/Step and Stop in fixed player positions.
Basler shows only Start and Stop in a stable right-anchored pair, retaining the
separate applicable Fault Reset slot. Connecting uses the localized connection
caption before the existing warm-up presentation.
Their icons always sit above their captions, independently of navigation's mode.
The visible native player buttons have equal widths and a common height of about
60 logical pixels at ordinary fonts. Their shared width follows a comfortable
font-metric budget, normally 120 logical pixels, rather than the longest translated action
across all languages. Slots stay stationary across ordinary language, theme and
action-state changes; exceptional captions elide with complete localized names
and guidance retained in tooltips/accessibility. Actual larger fonts can increase
width and height within the bounded horizontal allocation.
Reset remains a separate icon control in its reserved, state-dependent slot.
Batch has its localized label above a prominent value, with Edit to their right.
Its text budget follows ordinary content and is capped for long values; elision
never changes the underlying Batch identity or its full tooltip/accessibility text.
The editor shows the current Batch ID and a separate input. Enter or **Set Batch ID**
submits a nonempty value. Escape and the window close button discard the edit.
A named Batch offers **End Batch** with **Keep Open** as the confirmation default.
In READY without a named Batch, the same slot offers **Clear Statistics** to an
Operator or higher, with **Cancel** as the confirmation default. Dismissing
either confirmation returns to the editor with its input intact.
The stacked player and Batch fit the existing ordinary-font header height, leaving
the metrics row and camera grid geometry unchanged. The Production identity has two
rows: state with conditional inline active/requested identity, then the saved-Recipe
selector and Refresh. The selector shows the installed candidate, including startup
restoration; synchronization never prepares. During preparation it retains that
candidate while the first row identifies the request. Divergent active identity
and full candidate/request details remain available inline and in accessible
names/tooltips. Refresh never relabels a prepared definition from newer saved data.
The selector prefers 170 logical pixels; long names elide only in its closed label,
with full text retained in the model, tooltips and accessibility.
Header space after Batch and selector width can shrink while preserving the player
slots and common Batch IDs. Save actions reserve
localized group budgets, borrowing flexible feedback/summary space. Conventional
utilities use touch-sized icons with full localized tooltips/accessibility:
History paging/refresh, Recipe color sampling, Vision numeric ROI Apply, and
Communication group creation and advanced-settings disclosures.
Watch removal, single read and automatic refresh form equal compact icon/text
slots budgeted across languages and refresh states; Add remains a separate square.
Automatic refresh keeps its one-second interval visible, a loop icon, a checked state and an explicit
Stop caption while active, distinct from Production Stop. Manual writing retains
a localized verb. Save and service power controls retain visible verbs.
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
The Folder Replay middle control becomes Step while paused. Folder and internal
Basler admission retain distinct source-boundary semantics. Reset is a separate
fault-recovery action. Basler control only affects software admission, never
equipment or PLC mode. Empty canvases and routine acquisition waits stay quiet.
Read-only channel symbols retain static
ChannelId tooltips and accessible short states, without expanding runtime evidence.
Only identity-matched results draw defect boxes.

Production shows localized Cycles, `OK`, `NG`, `EMPTY` and `Δt` captions for completed
cycles, channel occurrences and last inspection duration. Counts come directly
from the application business counters; unavailable
timing displays `--`. Whole metric groups wrap when their text cannot fit one row.
These are cumulative occurrences, retained by Stop/Start and Recipe changes and
scoped to the selected Batch.

An enabled four-cycle session instead presents completed **Batches** and
separate cumulative channel-position OK, NG and EMPTY totals. Final NG includes
every authoritative reject bit, including conservative rejection. The Batches
tooltip exposes the original raw inspection-cycle count. **Batch Duration** runs
from the first Cycle admission to the arrival of all four terminal results at the reporting
owner, excluding later attribution checks, writes and ACK. Unavailable timing is
`--`; per-Cycle timing remains separate in camera diagnostics. The five metric
groups and their wrapping behavior are unchanged. Chinese/English four-cycle captions
use the existing English fallback in other languages.

The last successfully activated session determines the displayed counting unit,
including after Stop; editing future reporting settings or a failed Start cannot
relabel retained facts. A successful per-cycle/reporting-disabled/Folder Start uses
the original occurrence counters. Both cumulative stores survive Stop/Start and
Recipe changes and remain separate across mode changes. Selecting a Batch loads
its summaries and last eligible group duration and clears current result overlays
and group progress. Retained pixels do not carry the cleared judgments.
Confirmed unbatched clearing resets both counter sets, recent result indications
and group duration in memory. It preserves the last successful Start's counting
mode and any persistence warning. It appends a routine Audit Trail action when
available and leaves historical records and PLC registers intact.

The History Cycle table shows Time, `#`, Group, Batch, Recipe and Result. Group
shows the first CycleId and the Cycle's stored slot (for example, `2265 · 2/4`),
or `—` when there is no persisted association. Its tooltip and accessible
description include the full Run identity. The Time heading tooltip retains clock semantics, and elided
Batch/Recipe names retain their full-value tooltips. Result filters show the original status codes with
their independent product/technical/business meanings in localized tooltips.
Selecting a History channel shows its details below the matrix; cells do not
duplicate that text in tooltips. Camera tooltips identify the camera only; their
detailed diagnostic evidence remains in the stored record.

Rendering is not result completion or accounting. The display store owns separate
bounded latest-per-camera copies with generation, cycle, complete FrameId,
dimensions and source key. Only matching evidence may overlay those pixels.
In four-cycle mode the same store accepts only the reporting owner's actual fourth
member: at most one candidate fourth image and one eligible display image per
camera, plus their matching fourth result evidence. Eligibility promotes only
matching fourth pixels and clears cameras whose fourth frame is absent or invalid.
An exact fourth callback arriving late may fill its missing image; an earlier or
later frame cannot substitute. During the next incomplete group, the previous
eligible image retains its own fourth result/overlays and identity tooltip. New
generation/configuration boundaries invalidate compatibility; pending copies are
released on session cleanup. No four-exposure image cache is created. Independent
per-exposure archiving and per-Cycle/Folder presentation are unchanged.

The Qt camera header centers six four-cycle channel groups in configured order
between camera identity and timing. Each 28×28 logical-pixel slot holds a 2×2
array of 11×11 lamps with 2-pixel gaps. At ordinary font sizes the header takes
30 logical pixels, leaving the recovered height for the image viewport. Qt and
Web show only the lamps within each group. Tooltips and accessible names identify
the camera, channel and all four exposure states.
Admission order is top-left, top-right, bottom-left, bottom-right. Red means an
authoritative per-Cycle reject request, green non-reject OK, grey verified EMPTY,
and neutral pending/unavailable or invalidated unconfirmed progress. These scalar
indicators are independent of image availability and never become image overlays.
The reporting owner publishes one bounded latest progress observation; Application
includes it in its snapshot alongside cumulative accounting and group duration.
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
Web four-cycle metadata carries the same group membership, scalar channel lamps,
metrics and eligible fourth-image identities. Each 28×28 CSS-pixel slot centers
the 24×24 lamp grid with 2 pixels of padding. The browser displays lamps independently
of JPEG delivery, including blank cameras, and neutralizes them on stale/unavailable
metadata. Image failure does not invalidate a fresh eligible group. Missing fourth
images retire prior encoded delivery URLs through the existing invalidation path.
Installing a candidate with a changed camera set clears retained display pixels and advances its
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
