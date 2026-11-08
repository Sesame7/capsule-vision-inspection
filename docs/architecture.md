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

## Runtime diagnostics

The normal launcher accepts `--diagnostics standard` or `--diagnostics detailed`
for one process lifetime. Omitting the option creates no diagnostic thread,
directory or file. Standard records startup and Close, Recipe preparation,
Start/warm-up, Run activation, Stop and faults. It also records acquisition and
inspection failures, eligible four-cycle groups and reporting outcomes, alarm
monitor outages/recovery and History, Audit or PNG persistence problems.
Detailed adds frame admission, host retrieval and camera timing, per-Cycle
and per-camera inspection stages, group membership, counter reads, write/ACK
timings and successful PNG publication.

Owners hand small scalar observations to one non-daemon writer with 2,048
waiting slots. The writer serializes UTF-8 JSON Lines under entry-relative
`data/logs/` as `capsule-runtime-v1-<session_id>-<segment>.jsonl`.
It rolls at 10 MiB and retains up to ten owned segments across
launches. A directory lease protects concurrent writers. On normal exit, the
launcher drains accepted observations and joins the writer after the application
and Web owners settle. A full queue drops observations and produces a counted
`diagnostics.loss` record when writing permits. A writer failure disables
further publication for that launch and reports the exception type on standard
error. Launch from a console to see this feedback if no file appears.

### Field troubleshooting

1. Start with Standard. Keep the segments for the incident before rotation
   removes them. Each row carries the schema `capsule.runtime.v1`, UTC time
   (`utc`), process monotonic time (`monotonic_ns`), event type, PID and a unique
   launch `session_id`. Use the activation row to locate the Recipe, generation
   and `run_id`. Pre-activation events can have a generation without a Run.
2. For a timing investigation, reproduce with Detailed. Join frames using the
   Run/generation, CycleId, configured CameraId and frame sequence. Occupancy,
   traditional, ViMo and mapping times come from the terminal inspection
   evidence. Durations ending in `_ms` are elapsed milliseconds. Owner stage
   clocks ending in `_at_s` are monotonic seconds. The SDK device timestamp has
   its own raw units. These clocks do not establish physical trigger time.
3. For four-cycle delivery, follow the observed member IDs and the group's
   first CycleId within that generation. Compare eligibility, retained PLC
   counter, bitmap ACK, sequence ACK and commit uncertainty. Group timing ends
   on the last distinct terminal arrival. The counter-read and write detail
   uses the owner's original stage times and is published after settlement.
   A null ACK time means the owner recorded no confirmed ACK for that stage.
   A protocol ACK does not establish PLC scan consumption or ejection.
4. Check for loss records and writer errors before interpreting gaps.
   History, Audit Trail and published PNGs retain their existing evidence
   roles. Diagnostic records contain no image pixels, model outputs, account
   credentials or arbitrary exception text.

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

Account changes, Batch information edits, Recipe publication/deletion, Model staging/publication/rename/
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

Engineer and Administrator sessions use the shared Records date range and Batch
selection on the Audit tab, with one searchable account selector starting at All
Accounts. Choices come from distinct recorded actor IDs and names across the Audit
history, including older and disabled accounts. Search and scrolling read bounded
pages on the existing Audit owner; admission, execution and delivery remain
revision-fenced. Selecting an account matches its recorded ID and name exactly.
The UI has no action-type filter; action codes and their descriptions remain in
the evidence and table. The table shows time, user, action,
target and result. Target names come from the recorded operation. An action-specific
category or a dash stands in for an opaque or unknown target. Selecting an operation
shows its additional recorded changes. A successful edit shows before and after.
An unsuccessful edit shows the prior and requested values and any recorded failure
explanation. Routine entries with nothing to add use the full table height.
The SQLite owner pages by stable insertion order. Qt shows up to 50
rows and clears private details on session change. Review access is checked by the
service as well as the tab. Batch-scoped review alternates bounded reads on the
Audit and History owners without either worker waiting for the other. It includes
successful Batch selection/end with a matching UUID, metadata edits with their
recorded immutable unique Batch label, and successful Start or observed Stop with
an established Run identity. Failed Start targets are not Run evidence. Admission
denials and unrelated account/configuration actions are not attributed by time.
Unbatched review can use a persisted unbatched Run, an explicitly unbatched Cycle
for that Run, or a successful unbatched-statistics clear. Missing associations stay
excluded. Metadata targets at the audit truncation limit cannot establish a Batch
identity and are excluded. This is limited associated evidence, not complete Batch
audit coverage.
Cancellation and session changes stop further reads; every page and final delivery
remain revision-fenced. Ordinary navigation, refreshes, per-exposure results
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
Engineer and Administrator use the shared Records date range and Batch selection
on the Alarms tab, with an additional signal-category filter. UI Batch selection
matches the stored Batch UUID, never a label or a nearby timestamp. The explicit
no-associated-Batch choice matches an absent stored Batch identity. The table shows observation time, source,
alarm label, state and Batch. The recorded source distinguishes Software from PLC.
A captured PLC word and bit appear alongside PLC when available. The state distinguishes
a witnessed transition from a state found on a read. Selection shows additional
explanatory text only when the event supplies it. The original endpoint, signal,
station, definition and identifiers remain in the stored event. Pages contain up to 50
observations and private rows are cleared on identity change. Close waits for the actual monitor
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
The Production Batch form includes optional product name, specification and notes.
An exact label lookup on the History owner distinguishes a new Batch from a
continuation and loads existing metadata. Selection and submitted metadata commit
together, without replacing a Batch UUID, its counters or Run associations.
The owner compares the displayed identity/information before writing; a conflict
or unavailable store is a failure, not a new Batch or a partial successful selection.
Operator admission, idle-state checks and the initiating session's metadata audit
intent/completion remain at the Application/History boundary. Start or another
Batch change waits until this command settles. Closing the form without submitting
does not write; End Batch and unbatched Clear Statistics remain separate actions.

Each successful Start has its own Run UUID. After warm-up the lifecycle owner
requires a committed Run for a selected, durable Batch before publishing RUNNING.
Basler also opens a Run without a Batch, with a null Batch reference. Its
Cycle detail and eligible PLC groups use that Run UUID and cannot accrue to a
later Batch. The Run records its start observation, active Recipe/Model identity
and reporting mode, base and endpoint when enabled. Its **Start Account** stores
the account ID and username captured at Start admission in the same transaction
as the Run. A subsequent login during warm-up cannot change the recorded actor.
An admitted anonymous replay records Guest when it opens a Run. Identity-free
launches and historical Runs without an actor show a dash. An unactivated Run
and its provisional account association are removed together.
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
enter this owner through bounded non-waiting handoffs. Each eligible completed group
updates its Run's durable position summary and, when named, its Batch summary in
the same transaction. Both use the existing final-channel projection, including
OK, NG, EMPTY and UNKNOWN, and count the group once. The owner retains deduplication
keys for the lifetime of the open Run so detail expiry cannot allow a duplicate. The
eligible group stores its Run and first
Cycle identity, PLC counter, four actual member CycleIds in slot order, timing
and final channel outcomes. Joining on Run/Cycle works in either arrival order.
PLC ACK has no effect on these totals. Separate per-Run flags record possible
loss and expired detail. When storage becomes writable, a dropped or failed
fact marks that Run uncertain. A crash with an open Run also prevents a clean
completeness claim. Startup loads the saved summaries directly.

Run position summaries are initialized with Run admission, removed with an
unactivated Run, and retained independently of Cycle/group detail. Unbatched
Runs keep their own summary without accruing to a later Batch. A known summary
records committed eligible facts even when the Run is interrupted or uncertain;
the existing loss flag qualifies those counts rather than implying completeness.
Four-cycle Runs can have a known zero; modes without attributable groups do not
present exposure counts as capsule quantities.

For supported older stores, the History owner adds a companion summary table
before retention. A missing legacy Run summary is recovered only from valid
30/36-channel final outcomes and four distinct stored members with slots 1–4,
without expired-detail, loss or interrupted/open-Run evidence. All contributions
in its named Batch must reconcile exactly, including group and EMPTY/UNKNOWN
counts, to the independent durable Batch summary. Existing known Run summaries
can contribute to this check. Otherwise the legacy summary remains unavailable;
no residual Batch count is assigned to a Run. Old unbatched Runs have no such
independent checkpoint and remain unavailable. Recovery never changes Batch
totals or reconstructs physical groups from Cycle observations.

Recipe display names come from the application's immutable resolved configuration:
Production presents the active name separately from the prepared candidate, and
each History submission freezes that active name without changing its Recipe ID.
Older records without a captured name show a dash in the Recipe column. Its
tooltip includes the stored Recipe ID. Rendering uses only historical evidence.

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

Records opens on **Batches**, followed by **Inspection**, **Audit** and **Alarms**.
All roles can browse Batches and Inspection. Operators and above use the shared
Export action: Batches exports the selected Batch's production summary. Inspection
exports the entire exact selected Batch's defect classification, or its effective
filtered defect summary when no named Batch is selected. Engineers and above export
Audit Records; Operators and above export Alarm Records using each tab's effective
filters.
Each tab keeps paging and Refresh together at the right of its filter row.
A single shared filter bar contains two clearable dates and a searchable Batch
selector. Both dates start empty (no bounds), with Start Date / End Date
placeholders; clearing restores that empty value. Native calendar popups omit
week-number headers and use Qt's available-screen placement. Opening an empty
date shows the current calendar month without selecting a query bound. Start-only
and end-only ranges are valid. Bounds use host-local calendar days: inclusive start midnight and exclusive
midnight after the inclusive end date, including DST changes. Inspection uses
Cycle completion, Audit operation time, and Alarms event observation time. Selections
persist across tabs and filter changes reset paging. Time cells expose the full
local timestamp and UTC offset.

The selector starts at All Batches. Its popup searches persisted labels across
all matching Batches, with bounded 50-choice pages and explicit navigation. Choices
require an overlapping recorded Run and sort by the latest matching Run's recorded
activity; later Runs outside the interval do not reorder them. Labels remain full
in tooltips and accessibility; opaque identities never become captions. A date
change clears an excluded selection after the owning query confirms exclusion.
An unavailable query shows failure rather than guessing membership. A no-associated-
Batch choice is offered only from stored unbatched Run/Cycle evidence or, for an
authorized alarm reviewer, unassociated alarm observations. It does not synthesize
a Batch or cumulative totals. Batches has no separate search, Inspection retains
its result filter, Audit retains its compact historical-account selector, and
Alarms its category.
The lists size columns from Qt font metrics and representative business
values. At a given viewport and font, selecting a row, paging, refreshing and
switching languages keep the column boundaries stable. Longer captions and
values can elide. If even the short samples exceed the viewport, columns share
the available width down to Qt's minimum section size. Tooltips and accessible
text provide their full meanings.

Batches lists up to 50 named Batches per page. A date range matches any individual
Run that started within the interval or continued into it. A Run ending exactly at midnight
belongs to the preceding day, and a zero-duration Run belongs to its start day.
An open Run can overlap days through the time of the query. Gaps between Runs
remain inactive. Batches without Runs are visible in the list when both bounds
are empty, but do not appear among the Run-based selector choices. An unbatched
selection shows no Batch rows; it never aggregates unrelated Runs.

One hierarchical table shows Batch / Run, Start, Recent / End, Account,
Inspected, OK, NG and Pass Rate. Batch rows are emphasized and initially collapsed;
native branch interaction expands at most one Batch. Indented Run rows include
their ordinal; interrupted, unfinished and fault states remain visible, while
routine Stopped is retained in tooltips rather than repeated in the title. Batch Start is the earliest recorded
Run start. Recent uses the latest observed termination, including recovery of an
interrupted Run. For an open Run it uses the latest of its recorded start,
retained Cycle completions and observed PLC groups, with Running in its tooltip.
This observation orders the list. A Batch without a Run sorts by creation and
displays a dash in both Run time columns. Run End remains unavailable until a
termination is recorded. Compact time captions omit the year; tooltips and
accessible text retain the complete local observation and UTC offset. Account
shows the stored initiating username on Run rows, without inventing a single
account for the Batch.
Inspected is durable four-cycle OK + NG, and pass rate is OK / Inspected.
Without attributable groups the quantities show a dash. A zero denominator
leaves the rate unavailable.

The single table scrolls all expanded Runs. A read-only metadata card below it
reserves about 72 logical pixels at ordinary fonts for the selected Batch's
available product, specification and notes. Empty fields stay blank; long values
elide with full tooltips and accessibility text. It does not repeat statistics or
Run details, and its height does not depend on the number of Runs.
Batch pagination is independent of child rows. Expansion reads every associated
Run summary through the existing anchored pages of 50, with one outstanding read
and only one Batch's accumulated children. It does not load Cycle/group evidence.
Stale completions cannot replace a newer list or expansion. A failed child-page
read remains visible and requires explicit refresh or re-expansion to retry.
Expansion does not change selection; selecting a child retains its parent Batch
as the metadata and whole-Batch Export context.
Run quantities use the durable Run summary: Inspected is OK + NG, and Pass Rate
is OK / Inspected, unavailable for a zero denominator.
Unavailable summaries show dashes, with localized evidence feedback; uncertain
recorded totals retain their warning. The date filter applies to discovery;
both totals and Run children cover the whole Batch. All roles review metadata
read-only here; editing belongs to Production Batch setup. There is no separate
information dialog or View inspections action. Shared dates/Batch selection still
filter Inspection. Reviewing a Batch never selects it for Production. Loss/expiry
feedback remains in the stored-evidence tooltips. Unspecified information stays
empty, without fabricated product values.

Inspection queries run on the Production History owner. The Qt page retains at most
50 cycles with keyset Newer/Older navigation. Named Batch filtering uses the exact
persisted Batch association across its Runs. No-associated-Batch filtering uses an
absent persisted Batch label, without combining those observations into Batch totals.
The Inspection table shows time to milliseconds, Cycle, attributed group, Batch,
Recipe and result. Cycles without an established group show a dash. Date and
record-specific filters precede paging and refresh controls. The Inspection
toolbar wraps when space is limited.
Full Cycle/Run/Batch/Recipe identities remain in the stored record rather than a
repeated detail row. The compact result dropdown toggles OK, NG, EMPTY, UNKNOWN
and ERR independently, with a plain right-aligned ✓ for enabled items and blank
space for disabled items. The native popup has no checkbox square or persistent
selected-row fill; neutral hover and keyboard focus remain visible. It stays open
for multiple mouse/keyboard toggles; the closed caption summarizes the selection.
All five start enabled and leave the query unfiltered. None enabled matches
nothing. Otherwise a Cycle matches **any** enabled recorded channel business
state, or its independent technical ERROR for ERR. In particular, selecting OK
includes a Cycle containing both OK and NG. Existing compressed channel evidence
provides OK/NG membership on the History owner; this does not change the schema,
stored aggregate judgment or status precedence. Filtering precedes keyset paging
and applies to the full defect-report scope, with paging reset on selection change.
Page entry, filters and Refresh request data; the scalar warning refresh
does not poll the database. The read-only detail matrix uses stored camera order
and slot/ChannelId mappings, with one row per camera. Each channel shows its business
status and an independent technical ERR when present. Selection shows only additional
defect or failure reasons, with localized captions for known codes. Selecting a camera
can also show its recorded unassigned labels. List summaries always show OK then NG,
including zeros. EMPTY, UNKNOWN and independent technical ERR follow only when nonzero. A channel may
contribute to both a business count and ERR. Cycle-level anomalies absent from those counts
remain explicit; routine COMPLETE/SUCCESS is implicit in the presentation only.
Query failures remain explicit. For each visible Cycle the same query request
loads committed archive paths by Run/Cycle/Camera. Selecting a camera or one of
its channels enables **Open Image** only when that camera has a link. One click
opens the recorded PNG in the system viewer. History reports a missing file or
a rejected desktop open. History has no Web API.

On the same History writer and in the Cycle transaction, new records capture
distinct effective ViMo labels and traditional NG reasons per channel. ViMo
identity includes its saved Model and artifact digest. The pipeline has already
applied the Recipe label filter and EMPTY gate. Unassigned regions, execution
errors, UNKNOWN and forced collection rejection supply no product category.
The stored evidence contains scalar identities and categories only.

Either a Cycle or the eligible PLC group can arrive last. Once all four distinct
members have complete classification records covering the group's channels, the
writer uses the stored final NG positions and counts each channel/source/category
once across the four exposures. It commits the group marker and named Batch
aggregates together, along with durable per-Run category counts and coverage.
Customer categories collapse traditional Size reasons into Length and Color
reasons into Color, deduplicating upper/lower views and reason variants on the
same group/channel before aggregation. ViMo identity retains the recorded Model,
artifact digest and effective label. Original per-Cycle/group evidence and existing
raw Batch aggregates remain unchanged. An NG position with two categories increments each category
and contributes one explained NG. Coverage records distinguish such positions,
NG with no category in fully documented groups, and NG in groups lacking full
evidence. Group and Cycle detail expire after 30 days. Batch and Run category
totals, covered groups and explained counts survive with the existing counters.
Run coverage is initialized at admission and removed with an unactivated Run.
Unbatched Runs retain their own categories without accruing to a later Batch.

Before retention, the History owner adds the Run classification companion tables
to supported stores. A legacy Run is backfilled only when valid retained groups
and all four distinct memberships reconcile exactly to its independently known
durable position summary, and every group has complete recorded classification
of its final NG channels. This covers the accepted facts; existing uncertainty
and interruption flags still qualify production completeness. Missing Run totals,
expired or missing group evidence, or incomplete classification leave coverage
unavailable. Raw Batch categories cannot establish per-Run attribution or collapse
overlapping traditional reasons after detail expiry; no residual counts are assigned.

Operators and higher roles manually export Batch, Defect and Alarm customer reports;
Audit export requires Engineer or Administrator. PDF is the default, with Excel
and independently selected Simplified Chinese or English also available. One Export
action beside the Records tabs uses the selected tab's context; its compact
dialog does not repeat the report type or Batch picker. Inspection offers counting
bases only when no named Batch is selected. Audit exports only Audit Records;
its dialog shows the effective dates, Batch association scope and account, with
no counting-basis choice. Alarms exports only Alarm Records, showing effective dates,
Batch scope and signal category without another report or Batch selector.

- **Batch production report:** the entire selected Batch, independent of Records
  discovery filters. The report uses durable attributable position totals for
  Inspected (= OK + NG), OK, NG and Pass Rate (OK / Inspected). A zero denominator
  shows `—`; a Batch without any attributable groups shows unavailable position
  metrics, never quantities inferred from exposure counts. There is no standalone
  EMPTY metric. Recorded product/specification/notes are included only when present.
  The recorded inspection span and Run-start account names come from persisted
  evidence, not Batch creation, Run termination or the exporting account. Multiple
  Runs add a compact start/end-observation, account and outcome table with the
  same durable Run Inspected/OK/NG/Pass Rate as Batch review. Missing legacy
  summaries remain unavailable even when Batch cumulative totals are known. Recorded
  uncertainty, interrupted/faulted/active Runs, missing accounts, unavailable Run
  statistics and technical errors can qualify the summary. Blank paper signature fields
  do not represent electronic approvals.
- **Defect classification report, exact selected Batch:** one table shows Batch
  Total followed by every associated Run in start order, with concise Run/start
  identification, authoritative NG count and one numeric column per recorded
  category. Batch Total is emphasized; numbers align right. Complete coverage
  shows numeric zero for an absent category, while incomplete coverage shows an
  unavailable marker. It uses durable Run categories, independent of discovery dates, ANY result
  selections, pagination and detail retention. Batch category totals sum complete
  Run coverage only when all Run position summaries reconcile to the independent
  Batch totals. Missing coverage is unavailable, never zero. Interrupted, active
  and uncertain Runs remain identified. Production OK/NG/Inspected totals are not
  recomputed from classifications.
- **Defect classification report, no selected Batch:** all retained Cycles matching
  the effective shared Records date/unbatched and Inspection result filters,
  ignoring pagination.
  Affected capsule-position counting is offered and defaults only when the entire
  nonempty scope is a union of complete, valid, attributable four-cycle groups.
  Every group must have its four distinct stored members, exposure slots and valid
  stored channel outcomes; a partial group or an unattributed Cycle makes the
  entire scope Detection Count only. This rule is checked again in the export
  snapshot; a changed scope fails explicitly instead of silently changing basis.
  Position counts use persisted per-group classification of final NG channels.
  Detection Count counts each recorded channel/category once per Cycle, collapsing
  traditional reason variants within that Cycle, never across exposures.
  Classification completeness remains separate from physical attribution: missing
  historical classification is disclosed, never reconstructed. A position can
  contribute to multiple categories, so category sums are not total NG. ViMo labels
  retain their recorded spelling; Length/Color use short Chinese/English captions.
- **Audit Records report:** all recorded operations matching the effective shared
  local-date bounds, Batch selection and exact historical account selection,
  independent of UI pagination. Date bounds apply to the operation's recorded UTC
  timestamp; displayed times are local. Stable descending operation cursors retain
  the review order without duplicates. Historical/disabled accounts remain eligible.
  Batch and unbatched filtering use the existing [Audit association rules](#audit-trail),
  never temporal proximity. The header identifies a named scope as Batch-linked
  records; it does not claim complete Batch operational coverage. Missing legacy
  associations remain excluded. An empty scope gives short feedback without
  publishing a file. Columns are Time, Account, Action, Target and Result, followed
  only when relevant by recorded before/after values (before/requested for an
  unsuccessful operation) and additional failure reasons. Known fields, actions
  and outcomes use concise localized captions; recorded business names remain
  unchanged. Opaque targets use an accurate object category. Credential payloads,
  internal IDs/hashes and transport descriptions are omitted. A manual PLC write
  can retain its recorded register range and requested values. PDF uses landscape
  A4 and repeats column headings; Excel keeps typed local timestamps, literal text
  cells and a frozen table header. Neither format changes Audit evidence.

- **Alarm Records report:** every retained event matching the shared local-date
  bounds, exact stored Batch UUID (or no associated Batch) and signal category,
  independent of UI pagination. Date bounds apply to recorded observation times.
  The History owner freezes matching rows in one private snapshot; events sort by
  observation time descending with stored row identity as the deterministic tie-break.
  Columns are local Time, Alarm, State and a concise PLC/Software Source. An all-Batch
  scope adds recorded Batch labels when associations exist; a selected Batch appears
  in the business header. Missing names remain unavailable, never guessed from IDs.
  Recorded Chinese/English alarm names and meaningful additional details are retained.
  Activation/recovery transitions remain distinct from active/clear states found on
  a read; communication gaps and unknown states do not become recovery events. No
  pairing, duration or missing timestamp is synthesized. Connection URIs, PLC addresses,
  bit numbers, definition hashes and internal IDs are not customer fields.
  An empty scope gives short feedback without publishing a file. PDF uses landscape
  A4 with repeated headings. Excel uses one sortable row per event, typed local
  timestamps and a frozen header. Neither format acknowledges or clears alarms.

Classification tables use a consistent category order, with no Source, View,
Camera or Channel column. Without an exact Batch, a compact Category/Count table
retains the effective-filter scope. Visible label collisions retain distinct
internal identities and use a short ViMo suffix where needed.
Neither classification scope consults the currently installed Model or treats
EMPTY, UNKNOWN or technical ERROR as a defect category.

Both formats share this content and statistical basis. Business headers use two
balanced label/value groups and omit unavailable optional metadata; required
scope fields remain. Excel has one worksheet with typed counts, dates and rates;
the defect matrix keeps all category columns, freezes the header and Run/NG
columns, and never concatenates counts into text. All four PDF reports use landscape
A4, approximately 14 mm margins, embedded Windows Chinese TrueType fonts, 17 pt
titles and 10 pt body/table text. A compact product name and report title repeat
on each page; the footer carries the actual local export time with UTC offset and localized
Page X of Y. The snapshot's data-as-of time remains in the information header.
White backgrounds, subtle grey separators and restrained dark-blue text keep
printing readable. Production metrics remain prominent, numeric columns align
right, and blank signature/review lines include room for handwritten dates.
Table headings repeat on continuation pages. Audit events and their change details
stay together when they fit on a page; oversized details continue without losing
lines or reducing the font size. Many
categories split into consecutive column groups repeating Run/NG; each group
paginates the full Run list at readable size. Ordinary reports fit approximately
one page. Numerous Runs, categories or long business
content can add pages; all Runs are retained without shrinking the report to one
page. Customer wording uses concise headings, count units and factual exceptions;
it omits internal accounting explanations and general methodology disclaimers.
Unavailable values and incomplete classification remain explicit. Cycle/group/channel
detail is never appended. UUIDs,
hashes and inference timings are not customer report fields. Missing
fonts, unrenderable characters or a recorded heading too tall to leave a data row
on A4 fail PDF export before publication.

PDF headers also repeat an export workstation label and one ephemeral export
reference. The workstation label reads the exporting Windows installation's
`HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` through the native 64-bit
registry view. A validated non-null GUID's canonical bytes are SHA-256 hashed with
the domain prefix `capsule-vision-inspection/report-workstation/v1` and a NUL
separator; the first 12 hexadecimal characters form `WS-XXXX-XXXX-XXXX`.
Raw registry values are never returned to presentation, logged or added to PDF
metadata. A missing, denied or invalid value displays localized Unavailable;
there is no fallback identity, Windows modification or separately persisted ID.
This labels the exporting installation, not a historical inspection device or
verified equipment serial number. The reference combines the PDF's actual export
timestamp and a fresh three-byte random suffix, reused on every page. It is not a
globally unique report number or a claim of authenticity, approval or equipment
traceability. PDF metadata retains the localized title and identifies Capsule
Vision Inspection as creator. Historical event/snapshot timestamps are unchanged.
The first-page footer centers one compact, underlined software identity with
localized **System Generated** text. It identifies the generation process only,
not personal approval, a digital signature or verification. The separate Production
preparation/review and handwritten-date fields remain unchanged.

The low-level PDF renderer accepts an explicit, default-off `sample=True` option
for engineering sample generation. It paints a light grey, low-opacity diagonal
**SAMPLE** behind content on every page without reserving space or changing
pagination. Existing isolated PDF validation scripts may opt in; the normal
Application export path does not expose or pass this option. It never derives
sample status from names, record contents or source databases. No sample
provenance, setting or control is added to production snapshots or the UI.

The existing History owner captures a dated, consistent SQLite snapshot for
production reports. Audit export first freezes matching operations and completions
on the Audit owner; when Batch filtering is requested, the History owner then
copies the minimal persisted association evidence. These are separate owner
snapshots, not an atomic transaction spanning both databases. Filtering uses
immutable Batch/Run identities and the same bounded association helper as review;
later Audit events/completions cannot enter the frozen export. Neither owner waits
on the other or holds the identity lock during the copy. No second database writer
is introduced.
The same single cancellable export worker reads these private copies; it does not
read live configuration or infer missing evidence. Audit and Alarm reads are revision-fenced,
and the initiating session must still authorize atomic replacement at the report's
required role. Failure/cancellation preserves an
existing destination. Close cancels and joins the actual export worker. Report
format, language and counting-basis choices do not alter stored facts.

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

Compact captions use their surrounding page/card context: service cards show
Reporting and Web, and field labels use IPv4, units and short localized names.
Full localized meanings remain in tooltips and
accessibility text. Routine disabled services show `OFF`; waiting for the next
Start, active transitions, failures and ACK observations remain distinct. Idle
diagnostics omit no-request prose; watch success uses `OK` with its observation
time, while read/write failures remain visible. The diagnostic target stays
explicit and is not evidence that an edited preference was saved or applied.

The main window title is `Capsule Vision Inspection` on every page. The five
existing destinations (Production, Recipe, Communication, History and Vision)
use the visible English captions Inspect, Recipe, PLC, Records and Cameras.
Each page button centers its icon and localized caption together. The five
slots use the same widths on both workstation profiles, ranging from 116 to
132 logical pixels. Changing page, language or account preserves their
positions. Extra window width goes into the gap before the right-aligned
user, theme and native language controls. Exceptionally long captions elide
when painted. Accessibility retains the full caption and localized destination,
and the tooltip identifies the destination.

The user menu has a 192-logical-pixel slot; longer usernames elide while the
tooltip and accessible name retain the full username and role. Short dropdown
lists show all choices when space permits. Longer lists scroll and retain
keyboard selection; the language selector keeps its native 64-language popup.
Pointer activation preserves focus and checked or selected state without
leaving a keyboard focus outline. Keyboard navigation uses Qt's visible focus
cue, independently of selection.

The supported workstation profiles are portrait 1080×1920 at 100% Windows
scaling and landscape 3840×2160 at 200% (about 1920×1080 Qt logical pixels).
The native frame and taskbar reduce the maximized client area. The supported
minimum width of 1080 logical pixels does not change Qt's window minimum or
the image canvas coordinate system.

Folder Replay keeps Start/Resume, Pause/Step and Stop in fixed player positions.
Basler shows only Start and Stop in a stable right-anchored pair, retaining the
separate applicable Fault Reset slot. Connecting uses the localized connection
caption before the existing warm-up presentation.
The player icons always sit above their captions.
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
pagination buttons and bounded shared date/Batch and tab-specific selectors. Dynamic values may
elide with full tooltips. Date fields reserve space for a full ISO date and both
calendar and clear actions.
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
