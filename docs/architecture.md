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
Recipe Save publishes the definition and audit evidence without scheduling
preparation or PLC process application. Publication of a materially changed
Recipe matching both the selected and candidate IDs makes the candidate ineligible
for Start before releasing the stopped file-management slot, even if audit
confirmation fails after publication. Unchanged or unrelated Saves preserve
candidate eligibility. Save retains the selected identity, old candidate evidence
and active configuration. An explicit Production selection, including the same
ID, prepares the current saved dependencies on the existing bounded worker.
Preparation uses saved definitions; only successful Start promotes a candidate.

Prepared candidates retain immutable saved Recipe and Vision definitions and paths.
Cameras Save validates unapplied coordinates for the displayed ROI before either
file can be written. MachineLayout and CameraSources then publish independently.
Save never enters Production preparation or emits a candidate-installation event.
Successful Save/reload observations mark a differing candidate as requiring
re-preparation without replacing its Recipe identity or an active session. Identical
parsed definitions leave a current candidate valid.
The existing Application lifecycle worker verifies persisted Recipe and Vision
dependencies again before invoking the session factory. Changed or unreadable
definitions reject Start before engine/session side effects and retain the previous
candidate/active values. Session creation consumes the candidate's captured sources
and layout.
Snapshot presence is authoritative: missing captured CameraSources reject Start
without a fallback disk read. Fixed-image launchers capture their own source mapping;
its absent file path denotes launcher ownership, not missing captured values.
No filesystem validation is added to Qt dispatch or the Application command lock;
no watcher or cross-file transaction is introduced.
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
lock, without IO or copying definitions. Application also retains the validated saved
Recipe selection's ID/name independently of candidate evidence. The existing bounded
editing worker validates the saved catalog entry before accepting that selection,
then prepares dependencies. Both acceptance and installation enforce the request's
session revision, generation and supersession ticket. A valid explicit selection
awaiting preparation is UNCONFIGURED; failure retains the choice and old evidence,
but Start cannot use an older or ineligible candidate. Selection itself performs no
PLC work and does not update startup preferences. Invalid catalog requests leave the
prior valid selection intact. Close and authorization loss reject late acceptance
or installation. Ordinary setup feedback stays on Production/the relevant editor.
Pending means both resolved configurations
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

The private account store validates v1/v2/v3 records and migrates them to v4 in one
transaction, preserving IDs, verifiers, Last Login and rotation flags. The v4
store has one optional slot for an authorized Operator account ID. LocalIdentity
owns first setup and subsequent online mutations under its existing lease and
audit owner. Setup leaves the session anonymous. A verified temporary password
commits Last Login and publishes a restricted revision. Role-based commands
reject that revision. Logout and revision-bound password rotation remain
available. PBKDF2 runs outside the lock. The final password update,
rotation-flag clearance and session publication share the service commit boundary.
Cancellation invalidates uncommitted rotation and cannot undo a completed change.

The account service protects every existing Administrator from administrative
mutations and requires confirmation for granting Administrator access. The store
also guards the last enabled Administrator transactionally. The offline GUI and
CLI use the same OS-trusted maintenance operations and exclusive file lease. Each
records its provenance through the existing audit writer when available and warns
on audit loss. See [local accounts](configuration.md#local-accounts).

Startup acquires identity ownership before creating application resources and
passes that owner into the existing Application lifetime. Missing account storage
remains Guest without creating a database. Unavailable storage remains explicit.
After restoring the saved configuration, normal startup queues one
auto-login attempt on the same identity worker. The attempt shares manual
authentication's revision and supersession checks. The store rechecks the saved
ID and eligible Operator inside the Last Login transaction. Publication creates
the ordinary Operator revision.

At Close admission, Application advances the session to a new Guest revision and
blocks new identity admissions under the same lock that orders account commits.
Pending authentication, first setup, password and account-management decisions
lose their authorization to commit. Committed transactions retain their results
and audit attribution. The identity worker continues to settle requests during
resource cleanup. After application resources settle, final identity shutdown
drains the worker and Audit and waits for actual termination. A recoverable
resource cleanup failure before final identity shutdown reopens admission for
fresh authentication after the Close result reports failure. The session stays
Guest until a new login succeeds.

The window presents Login, Logout, Switch User, Change Password and
Administrator account management through this same service. The forced-change
form is window-modal. Credential forms clear secrets on submission or dismissal.
The service worker handles authentication and account IO. Qt receives results
through its destruction-safe queue and ignores superseded sessions. The existing
shell refresh observes role changes. Manage Users presents a read-only four-column
table and captures account IDs in separate role and disable-confirmation dialogs.
The Administrator also authorizes or revokes auto login through the worker's
audited account mutation path. Account changes revoke a matching authorization
in the account transaction. Switch User retains the current login until successful
replacement. Dismissing its pending credential form invalidates authentication at
the service boundary and fences queued Qt delivery.
Dismissing pending first setup or forced rotation fences the initiating revision.
Dismissing an ordinary account-management dialog stops delivery to that form.
The service completes accepted work under its authorization checks. The shell
disables account UI on confirmed Close. Application retains worker stop/join
ownership. Login/logout never controls an inspection session.

Production checks the session at command admission under the Application lock.
Start and source Pause, Resume and Step require Operator or higher. Start freezes
the reporting selection and initiating account for the admitted generation.
Identity changes leave admitted operations running. Stop and Close remain
available independently of role. Saved Recipe installation additionally validates
the initiating session revision and request identity. Startup supplies restored
settings and candidate during construction. Normal startup always supplies a
LocalIdentity, including when account storage is unavailable. Explicit identity-free
engineering compositions remain trusted. See the [Production policy](configuration.md#local-accounts).

The Qt shell refreshes permission-sensitive controls when the observed session
revision changes. The Recipe page can present an immutable catalog entry while
the editing owner retains its working copy. Its existing metadata worker reads
the displayed Model and checks that Model again before delivering labels.

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
An account mutation rejected as stale at initial admission records System when
only a superseded revision is available. Work admitted before a session change
keeps its captured actor and follows the existing transaction admission checks.
Manual PLC write intent retains the validated request values in order through
the protocol limits of 997 FINS words and 123 Modbus registers.

Audit schema version 2 additionally retains complete configuration revision evidence
for Recipe JSON (stable Recipe ID), MachineLayout (`machine.tsv`), CameraSources
(`camera_sources.json`) and the entire `communication.json`. The Configuration codec
prepares one immutable byte sequence for both publication and revision storage;
the Audit owner commits its SHA-256, artifact identity and complete content together
with the initiating operation intent in one SQLite transaction. Revision records
reference that operation's actor and UTC evidence and prohibit content updates or
deletion. A changed definition produces a pending revision; unchanged saves still
follow the existing command admission but create no duplicate revision. External
edits and read/reload observations are not attributed as user publications.

The business worker waits for durable intent/content before the existing atomic
single-file publication. For a tracked revision it also waits for the completion
transaction, outside Application and identity locks. A successful completion
confirms the revision; failed/stale attempts are not confirmed, and missing
completion remains unresolved. If confirmation fails after publication, the file
and durable pending evidence remain intact and Save explicitly reports incomplete
audit confirmation. There is no automatic rollback, retry or inferred recovery.
An affected candidate remains ineligible until explicit selection prepares it
from the published definitions. Vision files retain independent outcomes;
Communication retains its existing publication/next-Start admission boundary.
A confirmed revision is evidence of publication, not Production activation or Run usage.

`AuditTrail.revisions` provides bounded, revision-fenced Engineer review by artifact
identity; `revision_content` retrieves the exact bytes and verifies their hash before
delivery. Routine status and customer reports do not include these snapshots.
Recipe deletion does not remove its earlier revisions. Revision evidence has no
automatic expiry or restore action. Run capture can reference an exact confirmed
publication through the existing Audit owner; it does not create a Save event.
Incompatible Audit schemas
are rejected visibly without migration or replacement; existing databases must be
preserved for an explicitly authorized maintenance decision.

Authentication (including `session.auto_login`), logout, actual Start/Stop, Batch
changes, unbatched statistics clearing, saved Recipe selection and important
security or persistence failures submit observations without waiting.
Successful explicit Recipe selection, including identity-equipped direct
configuration, records the stable ID and display name of the installed Candidate.
PLC process application and Start have separate Audit outcomes.
Rejected Start observations use the selected Recipe ID when present. An admitted
Start keeps its captured actor and Recipe until activation, when its success
records the Run ID and Recipe ID. A pre-activation Stop or Close produces a
cancelled outcome when the startup owner observes the interruption and cleanup
settles safely. A startup or cleanup failure still records failure when a stop
was requested concurrently.
Unsuccessful manual authentication records the initiating actor, outcome and
failure classification with an empty target. Local Audit review and customer
reports show a generic target and safe failure captions for these operations,
including historical records. Historical evidence remains in the database.
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
target and result. Target names come from evidence recorded with the operation.
An action-specific category or a dash stands in for an opaque or unknown target.
Successful Recipe selections show the approved selection-time name as the target
and tooltip. Legacy records and withheld names use the generic Recipe caption.
The name is target evidence and adds no change comparison or detail.
The Simplified Chinese and English views use approved account names from
successful account changes and show Production Run for a successful Start.
Selecting an operation shows its additional recorded changes. Reliably paired
changes use Field, Before and After columns. The last column shows Requested
value for unsuccessful or unresolved operations. Pairing uses exact recorded
field paths, including indices, in prior field order. Recipe Save/Delete, Vision
layout/sources and Communication Save show unpaired fields in separate Field
and Recorded value columns under Before, After or Requested as appropriate.
Missing sides supply no values. Explicit empty values have a caption and recorded
dashes retain their spelling. A recorded missing-file or unreadable-file observation
provides prior context for Communication Save. Ambiguous sides use a text fallback.
An ellipsis, a value at the usual 96-character limit or a side at the 2,048-character
limit can indicate an incomplete summary. Review shows the possible truncation
and the existing summary limits of 16 fields, 96 characters per value and 2,048
per side while retaining the recorded evidence. Exact publication bytes and
Production activation have separate evidence. Long values wrap within the
scrollable details area. Simplified Chinese and English use Audit-specific
captions for actions, results and recognized reasons. **No completion recorded**
identifies an operation without a recorded completion at review time.
**Execution uncertain** reflects a recorded uncertain result. **Denied** covers
authorization and other admission refusals. A recorded protected-address refusal
has its own reason. The local view keeps available engineering details.
Routine entries with nothing to add use the full table height.
The SQLite owner pages by stable insertion order. Qt shows up to 50
rows and clears private details on session change. Review access is checked by the
service as well as the tab. Batch-scoped review alternates bounded reads on the
Audit and History owners without either worker waiting for the other. It includes
successful Batch selection/end with a matching UUID, metadata edits with their
recorded immutable unique Batch label, and successful Start or observed Stop with
an established Run identity. Rejected, failed and cancelled Start observations
have no Run or Batch association. Admission denials and unrelated
account/configuration actions are not attributed by time.
Unbatched review can use a persisted unbatched Run, an explicitly unbatched Cycle
for that Run, or a successful unbatched-statistics clear. Missing associations stay
excluded. Metadata targets at the audit truncation limit cannot establish a Batch
identity and are excluded. This is limited associated evidence, not complete Batch
audit coverage.
Cancellation and session changes stop further reads; every page and final delivery
remain revision-fenced. Ordinary navigation, refreshes, per-exposure results
and automatic PLC transactions do not produce entries.

## Alarm monitoring and history

Normal Application composition owns one auxiliary serial FINS/TCP status service
for the [saved status selection](communication.md#plc-status-service-and-alarm-monitoring).
The existing alarm worker writes a fresh software heartbeat/Application FAULT
pair, then reads the alarm word, with one connection and no periodic backlog.
Application state is copied under its lock before each write, never during I/O.
The Production reporting owner remains independent. Read/write health and last
successful observations are separate; a partial failure never fabricates fresh
alarm values or changes Application FAULT. Saved selection replacement and Close
retire the socket on its owning thread; the old status-write range remains
reserved until retirement. Invalid definitions prevent activation. This service
uses no new database, audit stream or connection-sharing abstraction.
Its worker captures definitions at activation and holds bounded observations
and one connection at a time. An active signal on the first read of a fresh
baseline is recorded as observed active. Subsequent successful reads record
changes only. During communication loss the last known active set remains
available and is marked stale. The first valid reading after a gap reconciles
differences as observed active or observed recovered. All timestamps are host
observations. Pulses entirely between reads can be missed.

A reactivation for heartbeat or fault output addresses or transport timeout
uses the last successful alarm read as its comparison baseline when both
selections are enabled, the host, port and alarm word match, and the newly
loaded definitions match the prior frozen definitions. Its first valid read
reconciles differences as after a gap. A different alarm input, changed or
invalid definitions, disable/re-enable and a process restart discard the
previous baseline.

The saved Running address is a separate read-only target definition (default
W16.1, fixed bit 1); this worker neither samples it nor writes it. A Running-only
Save preserves the connection and observations. Its strict address constraints
and editable disabled-service behavior are defined in
[Communication preferences](configuration.md#communication-preferences).

The immutable monitor snapshot also carries its applied selection, frozen
definitions, last successful raw word and observation time. The Communication
card presents that same evidence without additional PLC reads. A failed alarm
read retains the word and time as stale. Selection replacement clears the
published observations before activation. A preserved baseline is used for
event comparison until a successful read publishes new evidence. Disabled-monitor
definition previews are separate read-only file jobs on the Communication
page's existing bounded worker/delivery path. They never substitute for active
definitions, issue PLC requests or change History.

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

All roles can see the PLC alarm status and expand the Communication monitoring
card to inspect signal names, bit positions, raw values and interpreted states;
full word.bit addresses remain in tooltips and accessibility descriptions.
Only Engineer and Administrator can edit its settings. The global status strip
remains independent of the expanded card. Operator,
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
frames are retrieved, validated and released without cycles, counters, Production
display publication or PLC reports. At most one idle capture request copies the
selected camera's next matching frame into a Future for Vision; it never enters
the Production path.
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

The explicitly enabled [single-/four-cycle PLC group modes](communication.md#plc-group-reporting)
reuse the alternative serial communication worker under the same generation/session
owner. Camera admission queues scalar CycleIds; terminal callbacks hand off the
existing reject projection. Only the communication thread reads the selected
W48/W148 counter or writes bitmap/commit words derived from W49/W149. One or four
observed cycles, according to the frozen reporting mode, are associated under external phase assumptions, without changing
scheduling, judgments, occurrence accounting or History.
At the existing successful pre-write attribution check, the worker publishes one
immutable one-/four-result completion to Application. Separate `PilotCounters` classify
each ChannelId once using those business statuses and the already merged reject
words: every set reject bit means final NG (including conservative rejection),
otherwise all verified EMPTY means EMPTY, otherwise OK with only OK/verified EMPTY
observations. Original per-Cycle UNKNOWN/ERROR evidence remains intact. These conditional
physical-position totals never divide or replace `ProductionCounters`.
Application consumes the completion outside the reporting lock and without Qt;
one generation-local CycleId high-water mark prevents duplicate/stale accounting.
Incomplete or initially unattributable batches do not count. A subsequent bitmap
or commit failure does not retract an already eligible inspection completion.
PLC group reporting rejects Pause/Step/Resume at the Application boundary;
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
Each channel applies the Recipe label policy and suppresses assignments when
Occupancy confirms EMPTY. For each remaining exact label in each optical view
of this exposure, Inspection keeps the region with the greatest SDK `region.area`.
Equal areas use the earliest region in the SDK result. The original bounding-box
center selects the view relative to the channel ROI's horizontal divider
(`roi.y + roi.height // 2`). A center on the divider belongs to the lower view.
The whole Mask Area travels with the finding, including when it spans the divider
or overlaps another channel. Each channel selects its representatives independently.

A representative is effective when its Mask Area meets or exceeds the Recipe
minimum for its label. Zero or omission preserves ViMo presence, channel
judgments and PLC reject bits for the same inputs. A failed Occupancy measurement
leaves the assignments available. Unassigned findings remain camera diagnostics.
Inspection keeps each otherwise eligible representative below its minimum as
separate area-filtered evidence for four-exposure archiving and review. It carries
the original FrameId, channel assignment, label, rectangle and whole SDK area.
Inspection, PLC reporting and Batch classification consume effective channel
results. Live defect overlays show the retained rectangles with captions such as
`Dent · 423 px²`. See the
[PLC projection contract](communication.md#first-station-registers) for reject bits.

Occupancy always measures the centered `max(1, width*70//100)` portion of the saved
ROI and retains foreground and total pixel counts for each upper/lower view.
A pixel is foreground if any BGR component is greater than 40. A valid view is
PRESENT when more than 5% of its pixels are foreground and EMPTY otherwise.
After both views are measured successfully, either PRESENT view makes the
channel PRESENT. Two EMPTY views make the channel EMPTY. Missing or invalid
measurements record a technical failure and leave the Occupancy classification
unset. Confirmed EMPTY skips enabled Size/Color with recorded provenance.
Enabled full-camera inference and result validation still run. Execution errors,
collection failures and forced rejects retain their own reasons.

Size thresholds the complete Channel ROI once using `max(B, G, R) > 40`. The
combined Size and Color path uses the equivalent HSV `V > 40`. Size then labels
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

Length is the selected component's vertical pixel-center span in the original
image. A bounding height of 30 pixels gives 29 px between the top and bottom
pixel centers. The complete component bounds are retained for the owning view.
Tune and the offline evaluator draw the endpoints at those Y coordinates and
the horizontal midpoint of the bounds. Component statistics also reject
degenerate geometry, including straight single-pixel diagonals.

Production, frozen Recipe tuning and the offline evaluator use the same
measurement. Each logical view supplies its own valid body evidence. The Size
judge applies the saved Recipe values using
`abs(length - target_length) <= length_tolerance` for each view.

Color measures independently cropped upper/lower views using pixels with HSV
saturation and Value of at least 40 and circular Hue statistics. Only requested
capabilities execute. Color-only skips Size body labeling and measurement.
Occupancy, ViMo and confirmed-EMPTY
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
products; enabled PLC group reporting's separate conditional channel-position totals are
described above.

The Application snapshot exposes the primary current Start blocker. It uses
in-memory observations of the lifecycle, Operator access, selected Recipe and
candidate, preparation, pending Batch changes, PLC process verification, source
ownership and reporting settings. An eligible Basler source can Start while
disconnected. Group reporting requires Basler when enabled. Disabled reporting
permits ordinary inspection. Start still takes the existing session admission and
rechecks eligibility. The lifecycle worker settles initial Batch restoration and
performs saved dependency and resource preflight before activation.

`QtProductionBridge` supplies the initial and refreshed Production snapshots.
Before dispatch, the player checks the displayed action and generation against
current observations. It rejects stale clicks and refreshes through the bridge.
Start availability uses the Application blocker and Qt's editing, unsaved Vision
draft and Close guards. Localized hints describe those reasons.

Recipe process overrides remain immutable definition metadata in Configuration
and Run evidence. Configuration resolves one explicit DM starting address per
parameter and validates flat ID/value overrides. A stopped candidate with overrides
requires separate PLC application and register readback before Start is eligible.
The existing Application lifecycle thread serializes this work: at most one active
attempt and one latest pending candidate. It owns its private bounded FINS/TCP
connection; Qt dispatch, the Status service and the reporting owner do not execute
or lend a client to this operation. Start warm-up does not apply parameters.

An Operator-or-higher explicit saved Recipe selection/configuration admits
application at the captured session revision. Startup restoration is read-only and
leaves overridden candidates ineligible until authorized re-selection. Empty
overrides require neither a target nor PLC I/O. Recipe Save admits no process
application.
The candidate stays available as diagnostic evidence on failure; there is no
fallback, retry or rollback. Supersession, saved dependency invalidation, cancellation
and Close fence subsequent operations and eligibility. Already admitted I/O finishes
under its deadline and the lifecycle owner closes it before shutdown completes.
The initiating identity is rechecked before each operation and final eligibility.
External saved Recipe/catalog/Vision changes retain the ordinary pre-Start freshness
checks. A changed/unreadable saved Status target also blocks a later Start until
re-selection, without writing during Start.

Audit records the prepared Recipe identity and captured Recipe hash for an application
attempt; each parameter has linked address/value intent and verified/failed/uncertain
completion. Recipe Save records its own event; explicit selection records
`production.recipe`.
Configured Run overrides are not rewritten as proof of PLC application.
See [process definitions](configuration.md#process-parameters) and
[application wire semantics](communication.md#process-parameter-application).

The separate hold-to-view PLC observation captures the saved Status endpoint and
configured DM addresses, revalidates the editor's catalog, and admits each
bounded DM read with the initiating Engineer session revision. The Application
read function is Qt-independent. Communication's existing single diagnostic/file
job owns temporary clients, queued completion and actual thread termination;
the live Status connection is never borrowed. Qt keeps observations separate
from the original editor widgets and cancels presentation on release, navigation,
focus loss, session change or Close. Cancellation stops unadmitted reads and
fences late results; already admitted I/O settles under its deadline before the
owner joins. Recipe editing/Save retain their independent worker and never wait
for this observation. View PLC never writes or feeds readings back into the draft.

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
After Close admission, the lifecycle thread settles inspection, then disconnects
application-owned sources. Partial connection failures remain source FAULT
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
Identity-free launches and historical Runs without an actor show a dash. An
unactivated Run and its provisional account association are removed together.
If unbatched Run recording fails, inspection continues with a visible warning
that totals and detail may be incomplete. Individual Cycle records are still
offered to History on a best-effort basis. Unbatched Folder Replay continues to
record individual Cycles without opening a durable Run.
Stop records termination after session cleanup, including for zero-Cycle Runs,
and preserves the Batch selection. Exclusive owner startup marks any abandoned
open Run interrupted and uncertain, with the recovery time as its termination
observation. Warm-up and preview contribute no production evidence.

History schema 4 records an immutable configuration manifest in the same transaction
as each durable Run and its Start Account, before RUNNING is published. Preparation
captures Recipe, MachineLayout and CameraSources bytes and parses those same bytes
against its frozen definitions. Start's existing parsed-value freshness checks are
unchanged. The lifecycle owner captures the effective resolved inspection values,
source mapping/base, configured process overrides/catalog and frozen reporting
selection (including disabled reporting). It does not read editor drafts or reload
files after warm-up. Model identity retains the verified Model ID/SHA-256, without
copying its binary. Process overrides describe configuration, not PLC-applied values.

Resources retain stable identities, content hashes, their saved-byte or effective-value
origin and optional revision IDs. Saved Communication bytes are retained when the
selection is restored, published or validated on reload, then frozen at Start
admission. They supply publication context only; the separate frozen reporting
selection specifies what this Run uses, not the file's independent Web/status settings.
One bounded read on the Audit owner matches artifact, resource, SHA-256 and exact
content against successful confirmations only. A newer unmatched, pending or failed
revision is never substituted. Missing matches are explicit null references; an
unavailable Audit owner is separately recorded. These historical references do not
imply an atomic transaction across Audit and History, a user selection event or PLC
acceptance. Later edits, deletions and confirmations never relabel a recorded Run.

`ProductionHistory.run_configuration` retrieves one captured manifest on the existing
History owner and verifies resource content hashes. Normal composition requires
Engineer admission with session fencing before, during and after the queued read;
trusted identity-free compositions retain their existing ownership. Missing launcher
inputs or directly submitted engineering Runs are explicitly incomplete, never
reconstructed from current files. Unactivated Run cleanup removes its manifest in
the same transaction. Manifest retention follows the Run, independently of 30-day
Cycle/group expiry. Failure to commit rolls back the whole Run transaction; named
Batch admission still fails, while unbatched inspection may continue with its existing
visible non-durable/uncertain observation. Older History schemas are rejected without
migration, reset or configuration backfill.

One background owner handles every access to the live History database, along
with projection, compression and retention. The normal inbox admits up to 128
pending facts or queries, with one additional slot for a Batch/Run command.
Only the lifecycle worker waits for
Run confirmation. Stored Cycle rows have independent persistent IDs and unique
`(run_id, cycle_id)` identities. Compact details retain configured camera/slot/
channel identities, business/technical/judgment states and existing abnormal
reason codes. When effective ViMo findings exist, the same Cycle transaction
records their labels, original image rectangles, effective channel associations
and SDK Mask Areas in pixels squared under the observed camera. A shared region
records only the channels that retained it. Earlier records keep their original
finding multiplicity and may lack areas. Unassigned labels remain separate
camera observations. History reads each stored judgment and reason as recorded under the
inspection policy in effect at the time. Neither judgments nor PLC delivery
depend on storage success. Image pixels and raw inference outputs stay outside
the History payload.

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
In Inspection, older records without a captured name show a dash in the Recipe
column. Its tooltip includes the stored Recipe ID. Rendering uses only historical evidence.

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

Records opens on **Batches**, followed by **NG**, **Cycles**, **Audit** and
**Alarms**. The NG tab pairs a capsule outline with the same caption in every
language. Its tooltip and accessible name give the full localized name for NG
Capsules. Both normal and portable Records center each icon and caption in a
132-logical-pixel tab slot, including a four-pixel gutter. Each rounded tab has
its own border, with the page background visible in the gutters. The slots
share a smaller width when space is tight, and long captions elide.
All roles can browse the first three tabs. Cycles identifies Inspection History
in its tooltip and accessible name. The upper-right Records action pairs the
export icon with **PDF**. Its tooltip and accessible name identify the report and
its scope. Batches exports the selected Batch's complete Production Report. NG
exports the complete Defect Classification Report for the selected final-NG
capsule's recorded Batch, independent of gallery sorting and filters. Filtered
entries, unbatched capsules and empty selections disable NG export. Cycles has
no export action. Audit and Alarms export their effective record filters at
their respective roles.

The button keeps the same size and position across languages and states on the
four export tabs. During export it shows a localized **Cancel** action. Its
tooltip and accessible name continue to identify the initiating report and
scope as the operator browses or switches language.

Each tab keeps paging and Refresh together at the right of its filter row.
A single shared filter bar contains two clearable dates and a searchable Batch
selector. Both dates start empty (no bounds), with Start Date / End Date
placeholders; clearing restores that empty value. Native calendar popups omit
week-number headers and use Qt's available-screen placement. Opening an empty
date shows the current calendar month without selecting a query bound. Invalid
typed dates mark the field and describe its retained effective date or unset
bound in the tooltip and accessibility text. Start-only
and end-only ranges are valid. Bounds use host-local calendar days: inclusive start midnight and exclusive
midnight after the inclusive end date, including DST changes. Cycles uses
Cycle completion, NG Capsules uses PLC group observation, Audit uses operation
time and Alarms uses event observation time. Selections
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
a Batch or cumulative totals. Batches has no separate search, Cycles retains
its result filter, Audit retains its compact historical-account selector, and
Alarms its category. The closed Cycle result and Alarm type filters use the
shell's 10-logical-pixel horizontal padding. Long captions elide inside the
text field, and tooltips retain the full selection descriptions.
The lists size columns from Qt font metrics and representative business
values. At a given viewport and font, selecting a row, paging, refreshing and
switching languages keep the column boundaries stable. Longer captions and
values can elide. Audit outcomes and Alarm states reserve width for their
longest shaped captions across supported languages. If the minimum width
budget exceeds the viewport, columns share the available width down to Qt's
minimum section size. Tooltips and accessible text provide their full meanings.

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
The existing Run reporting-mode value determines one- or four-member group
coverage; membership is never inferred from however many detail rows remain.
Before `single_cycle`, every persisted group fact had four members, including
trusted compositions whose Run had no reporting selection. Those facts retain
four-member interpretation; only explicit `single_cycle` Runs use one member.
This does not grant per-cycle runtime reporting any physical-group attribution.
Both modes update Run and Batch totals from the same deduplicated eligible fact.
They reuse the existing tables without a schema migration or historical rewrite.
Inspected is durable attributed-group OK + NG, and pass rate is OK / Inspected.
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
as the metadata and whole-Batch Export context. A new Batch PDF requires a
selected row from the latest completed list for the current filters and page.
Clearing the selection or requesting another list disables new exports until
the current list has a selected row. An open Save dialog keeps its admitted
Batch through subsequent browsing.
Run quantities use the durable Run summary: Inspected is OK + NG, and Pass Rate
is OK / Inspected, unavailable for a zero denominator.
Unavailable summaries show dashes, with localized evidence feedback; uncertain
recorded totals retain their warning. The date filter applies to discovery;
both totals and Run children cover the whole Batch. All roles review metadata
read-only here; editing belongs to Production Batch setup. There is no separate
information dialog or View inspections action. Shared dates/Batch selection still
filter Cycles. Reviewing a Batch never selects it for Production. Loss/expiry
feedback remains in the stored-evidence tooltips. Unspecified information stays
empty, without fabricated product values.

Inspection queries run on the Production History owner. The Qt page retains at most
50 cycles with keyset Newer/Older navigation. Named Batch filtering uses the exact
persisted Batch association across its Runs. No-associated-Batch filtering uses an
absent persisted Batch label, without combining those observations into Batch totals.
The Cycles table shows time to milliseconds, Cycle, attributed group, Batch,
Recipe and result. Cycles without an established group show a dash. Date and
record-specific filters precede paging and refresh controls. The Cycles
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
stored aggregate judgment or status precedence. The Cycles page applies the filter
before keyset paging and resets paging when the selection changes.
Page entry, filters and Refresh request data; the scalar warning refresh
does not poll the database. The read-only detail matrix uses stored camera order
and slot/ChannelId mappings, with one row per camera. Each channel shows its business
status and an independent technical ERR when present. The cell tooltip and
accessible text explain technical errors in the current language. The selected
channel shows one compact summary of recorded defect labels and localized reasons. Repeated
labels and matching reasons from both capsule views appear once, and the summary
wraps when needed. The generic model-defect reason remains available for older
details. Selecting a camera can show its recorded unassigned labels separately.

Result summaries always show OK then NG, including zeros. EMPTY, UNKNOWN and
technical ERR follow only when nonzero. A channel can contribute to both a
business count and ERR. A recorded Cycle technical ERROR without a channel ERR
count adds a compact Cycle ERR marker. Nonroutine completion and other Cycle
outcomes absent from the counts appear in the selected record's details and full
Result tooltip. The Cycle explanation has its own line above any selected camera
or channel reasons. Full details remain available through tooltips and accessible
descriptions. Routine COMPLETE/SUCCESS is implicit in the presentation only.
The six columns use compact count spacing and reserve room for ordinary Cycle and
Group identifiers. Longer Batch and Recipe names use most of the spare width.

Query failures remain explicit. Audit and Alarm History indicate when a new query
is pending, including when rows from the previous query remain visible. Independent
storage and query failures retain their own details. For each visible Cycle the
same query request loads committed archive paths by Run/Cycle/Camera. Selecting a camera or one of
its channels enables **Open Image** only when that camera has a link. The action
reserves space for the longest shaped caption across the supported languages
and retains the linked path in its tooltip. One click opens the PNG in a native
read-only dialog. Its initial size fits the image at
its original aspect ratio, allowing for the window frame, margins and caption
within about 92% of the screen's available width and height. Resizing refits the
complete image. The dialog pins the selected record and camera, and paints only their
stored effective ViMo rectangles with matching image dimensions. Captions include
the recorded Mask Area when available. Cycles and NG use the same representative
for each exposure, channel, optical view and exact label. The rectangle center
locates the view in the frozen channel ROI, with the divider assigned to the lower
view. A recorded SDK area takes priority. For unmeasured legacy boxes the largest
rectangle wins and its caption remains label-only. Equal display ranks retain
stored order. Without trustworthy view geometry, legacy candidates share one
channel/label selection. A pair of previously recorded measured representatives
retains its original evidence when the Run layout is unavailable.
Coincident rectangles are drawn once. Stored reasons remain available for images without
geometry. Missing, unreadable, invalid and dimension-mismatched images produce
feedback in the Records detail area. An open error follows the selected Cycle,
Camera and linked path across language and theme changes. A different image
selection or a new open attempt clears that error. The native Close control and
Esc dismiss the dialog. History has no Web API.

NG Capsules opens on **NG**, with one row per stored final-NG ChannelId in an
attributed four-member PLC group. **Filtered** lists final-OK channels with a
recorded Mask Area exclusion on at least one member. **All** combines the two
sets. Each Run, group and channel appears once. A final NG always takes precedence.
History requires four distinct member Cycles in slots 1–4, with the group's first
Cycle in slot 1 and a complete 30- or 36-channel final outcome. Retained four-member
facts from trusted Runs without a reporting selection also qualify. The list uses
the Run's exact Batch association and shows up to 50 capsules per page. The
default **Time** order shows the newest group observation first and channels
ascending within each group. A capsule stays
listed even when all four photos are unavailable. The table shows Time, Batch,
Recipe, Channel and Group. Batch displays the associated historical label or a
dash, and Group displays the group's first Cycle. Recipe uses the display name
frozen at Run start. The Channel tooltip includes the camera mapped to that
channel in the frozen layout. If either field is unavailable, History uses the
group's retained member Cycles when their recorded values agree. An unavailable
Recipe name displays as an em dash. Batch and Recipe share the spare column
width and expose their complete values in tooltips and accessibility text.

The NG sort button immediately before Previous Page offers **Time** and the
exact defect labels with measured evidence in the current date, Batch and result
scope. Choosing a label orders matching capsules by their largest recorded Mask
Area across all four exposures and both optical views, smallest first. NG rows
use effective findings and Filtered rows use recorded exclusions. A missing
photo does not erase a recorded measurement. Capsules without a measurement
for that label remain accessible through Time. Equal areas follow the Time
order, with Run and group identity breaking ties between simultaneous groups.
History applies this order to the entire scope before keyset pagination. A sort
change selects the first matching capsule, and the gallery arrows follow that order.

Selecting a row reads the channel's camera, slot and ROI from its frozen Run
layout. This limited History query is available to Guest. Full Run configuration
review requires Engineer or Administrator. Each numbered exposure shows the whole
channel ROI from its member's linked PNG, with both mirror views in their original
orientation and aspect ratio. Effective ViMo rectangles assigned to the channel
are clipped to the ROI and translated to its origin when image dimensions match.
Their captions use the recorded label and any measured Mask Area of the whole region.
Filtered rows show `OK` beside the channel. Their excluded representatives use
dashed amber rectangles and amber measured captions. Exposure tooltips identify
the exclusion even when the PNG is missing.
A conservative final NG supplies no additional findings. A missing ROI, unusable
link or incompatible image leaves that exposure unavailable.

The four photographs fit side by side at their frozen ROI proportions. A single
row of numbers 1–4 sits directly below them, each centered beneath its exposure.
In portrait the table fills the space below the photos. In landscape it fills
the space to their right. Arrows at the edges of the group select the adjacent
capsule in filtered table order, crossing a page boundary when needed and
scrolling the selected row into view. At the first or last capsule the
corresponding arrow is disabled.

Exposure tooltips and accessibility text retain Cycle identity, recorded channel status,
reasons and missing-image details. With the table focused, Shift+F1 shows those
details for all four exposures of the selected capsule. The shared Qt worker pool
decodes one selection at a time and the page discards superseded results.

The optional [`--history DIRECTORY`](configuration.md#portable-history-review)
startup argument opens a separate read-only window with Batches, NG,
Cycles and Alarms. It shares their presentation, filters and language
selector with the normal window. Audit, Export and equipment sign-in are absent.
Only a bounded SQLite reader is created. It verifies the supplied copy's version,
review schema, integrity and foreign keys before opening the window. The reader
uses an immutable, read-only connection and the same Records queries and stored
evidence decoding as the production owner. It does not create the normal
Application, identity, History writer or equipment and Web services. The reader
skips recovery, retention and checkpoints. Retained Cycles, groups and Alarms of
any age are browsable, and an open Run stays open in the evidence. The window
detaches query delivery on Close, and the reader releases SQLite outside Qt dispatch.

Portable Alarms uses the stored row labels, states and details with the same
date/category/Batch filters and 50-record pages. It reads all retained dates
without the normal 30-day query cutoff. A copy with no alarm table or rows shows
an empty Alarms page; a bilingual table is read without adding translations.
The shared Batch picker also accepts exact Alarm associations to stored Batches
with no overlapping Run, and offers unassociated Alarm observations where
present. No live monitor or storage status is inferred. Normal Alarm review
still requires Operator or higher and keeps its existing query cutoff and retention.

For archived images in this mode, Records displays and opens the path beneath
the supplied root using the date, matching archive CameraId directory and exact
PNG filename from the stored absolute link. Invalid suffixes, missing files and
destinations resolving outside the root are unavailable. Records does not use
the original location, search for alternative files or reconstruct links.
Stored finding evidence and the production archive writer are unchanged.

On the same History writer and in the Cycle transaction, new records capture
distinct effective ViMo labels and traditional NG reasons per channel. ViMo
identity includes its saved Model and artifact digest. The pipeline has already
applied the label and Occupancy gates, selected representatives per view and
applied the Recipe Mask Area minima. Unassigned regions, execution
errors, UNKNOWN and forced collection rejection supply no product category.
This classification evidence contains scalar identities and categories.

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

Operators and higher roles can export Production, Defect and Alarm PDFs. Audit
requires Engineer or Administrator. Export opens the native PDF Save dialog
directly. The action captures the report identity or effective event filters,
initiating session and UI language before opening Save. Simplified Chinese
(`zh`) produces a Simplified Chinese PDF. All other UI languages produce English.

- **Batch production report.** One statistics table covers the selected Batch,
  independent of Records discovery filters. Its emphasized Batch Total row uses
  the persisted Batch position summary. Every associated Run follows in start
  order with its own durable summary. A Batch with no Runs still has a total row.
  Each row shows Inspected (OK + NG), OK, NG and Pass Rate (OK / Inspected) from
  attributed group outcomes. Empty positions are excluded. A zero denominator
  leaves Pass Rate unavailable (`—`).
  A Batch without attributable groups or a Run with a missing historical summary
  shows unavailable statistics. Only Run rows have a start time, observed end,
  and recorded start account. The Batch / Run cell normally shows just the Run
  number. In-progress, interrupted and faulted Runs show their outcome beside the
  number. Any recorded uncertainty appears directly beneath it.
  The header retains the recorded inspection span, known Run-start accounts and
  available product, specification and notes.
  Recorded uncertainty, interrupted, faulted or active Runs, missing accounts or
  statistics and technical errors can qualify the summary. The report includes
  paper signature and date fields.
- **Defect classification report.** NG resolves the selected rejected capsule's Run to its
  persisted Batch UUID. The matrix starts with an emphasized Batch Total and lists
  every associated Run in start order. Inspected and NG precede the recorded
  category columns and come from the corresponding Batch or Run position summary.
  With complete recorded coverage for zero groups, Batch NG is zero and Batch
  Inspected is unavailable. The Run column includes the recorded start time.
  Complete coverage shows numeric zero for an absent category. Incomplete coverage
  shows an unavailable marker. The matrix uses durable Run categories, independent
  of gallery filters, pagination and detail retention. Batch category totals sum complete
  Run coverage only when all Run position summaries reconcile to the independent
  Batch totals. Missing coverage is unavailable, never zero. Interrupted, active
  and uncertain Runs remain identified. Production OK/NG/Inspected totals are not
  recomputed from classifications.
  A final NG position can contribute to multiple categories. ViMo labels keep
  their recorded spelling, and Length/Color use short Chinese or English captions.
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
  only when relevant by approved changes, recorded result evidence and safe
  failure captions. The report uses the same Simplified Chinese and English
  Audit terminology as local review. It omits generic failure text when there
  is no additional disclosable classification. Reliably paired values use Field,
  Before and After columns beneath the event, with Requested value for unsuccessful
  or unresolved operations. Approved unpaired configuration values, including a
  value whose counterpart is withheld, use Field and Recorded value columns
  under the recorded side. The report identifies omitted values and potentially
  incomplete summaries. Potentially cut values are withheld. Ambiguous configuration
  sides use only individually approved text lines. Each action has its own
  disclosure rules. Configuration changes show approved fields and value types,
  with the action and result available for other changes. Reports omit credentials,
  internal IDs and hashes, network endpoints and routes, camera serials and file
  paths. Approved business names retain their recorded spelling. Recipe selection
  targets use the approved recorded name up to 32,767 UTF-16 code units. Longer
  names use the generic Recipe caption.
  A manual PLC write shows its recorded register range and up to the first 16 requested values
  with an explicit count of any additional recorded values omitted. A successful
  manual write identifies the PLC acknowledgment. Manual write and individual
  Recipe process-value details label the recorded register request as Requested
  value regardless of outcome. Matching register readback appears separately
  when the successful Recipe process operation's completion records it. Historical
  records require reliable send-stage evidence to describe a request as unsent.
  Local Engineer review shows the full recorded request and retains engineering details
  subject to the unsuccessful login rule. Both views flag incomplete historical
  write evidence and omit any potentially cut final value. PDF uses landscape A4
  and repeats event and continuing detail-table headings.

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
  A4 with repeated headings. Export leaves alarm state and acknowledgment unchanged.

Classification columns follow a consistent category order. Distinct categories
with colliding display names use a short ViMo suffix when needed. Only recorded
customer defect categories contribute to the matrix.

Business headers use two balanced label/value groups and retain required scope
fields. They omit unavailable optional metadata. All four reports use landscape
A4, approximately 14 mm margins, embedded Windows Chinese TrueType fonts, 17 pt
titles and 10 pt body/table text. A compact product name and report title repeat
on each page; the footer carries the actual local export time with UTC offset and localized
Page X of Y. The snapshot's data-as-of time remains in the information header.
White backgrounds, subtle grey separators and restrained dark-blue text keep
printing readable. Numeric columns align right, and blank signature/review lines
include room for handwritten dates.
Table headings repeat on continuation pages. Audit events and their change details
stay together when they fit on a page. A comparison row stays together when it
fits below the repeated headings. Oversized details continue without losing
lines or reducing the font size. Wide defect matrices split into consecutive
column groups. Each group repeats Run, Inspected and NG and paginates the full
Run list at the same text size. Ordinary reports fit approximately
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
existing destination. Close cancels and joins the actual export worker.

## Image archiving

An enabled Basler session owns one `application.image_archive.ImageArchive` thread.
The source retains the original immutable full BGR Frame before runtime submission;
the terminal camera result must match its session generation, CycleId, CameraId,
complete FrameId and dimensions. Generation is session-bound, not guessed from
timestamps. A missing original, mismatched identity or ambiguous source cannot
supply that exposure. Borrowed aliases remain immutable until
archival write/drop as well as inference completion. No additional full-image copy
is made at handoff.

Each camera's six channels determine its code. Any definite channel NG takes
precedence (`1`), followed by channel technical ERROR (`2`). When every channel
is explicitly OK or confirmed EMPTY, at least one OK gives archive OK (`0`) and
six EMPTY give archive EMPTY (`3`). UNKNOWN prevents EMPTY/OK classification.
EMPTY requires confirmed absence in an inspected frame. The individual camera
code controls `save_ok`, `save_error`, `save_empty` and, outside four-cycle reporting,
`save_ng`. Collection errors and unassigned findings retain their recorded meanings.

In four-cycle reporting, `save_ng` uses the reporting owner's completed group.
A camera qualifies when one of its channels is final NG or a final-OK channel
has an otherwise eligible representative below its Recipe minimum on any member.
The frozen layout maps channels to cameras. The archive requests the original
for each of the four actual member CycleIds on each qualifying camera. Multiple
channels and switches share one write per Run/Cycle/Camera. The filename keeps
the individual camera code, or `group` for a matching original without an
individual classification. The persisted final outcomes and excluded
representatives identify the capsule's review category independently of that code.

The session registers PLC submission first. Individual switches may publish
at terminal inspection. The archive holds remaining originals until attribution
and releases them when the group is skipped or stopped. Missing originals and
capacity losses produce diagnostics. Encoding and filesystem work run on the
archive worker, independently of accounting, reporting ACKs and inspection
deadlines. Limits are 8 Cycles collecting or awaiting attribution, 24 queued or active writes,
32 MiB per frame and 256 MiB of combined retained, queued and active raw pixels.
With no existing archive backlog, one burst of four Cycles with up to six eligible
images each fits these defaults even while the writer is temporarily blocked,
provided the combined raw pixels are within the byte budget. Encoding uses
additional transient storage for one image beyond that budget.
Overflow drops archival work with scalar diagnostics; disk/encoding/path failures
record a bounded last issue and continue subsequent images. Bridge observation
shows an archive warning/detail without changing judgments, counters or PLC state.
Once resolved, only queued and active writes retain pixels. Stop/Close drain accepted writes after
runtime callbacks and reporting settle and wait for the actual writer exit on the application
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
connection counts, summarizes observed production-reporting and alarm-monitor
states, and retains current feedback, including Web failures. Source state uses
its observed symbol. Unconfigured and routine Status observations use neutral
symbols. Preparation uses the clock, and faults retain warning symbols. Reporting retains its
visible lifecycle and ACK/success/ERR summary without runtime diagnostic tooltips;
detailed observations stay with their existing owners.
ACK is not connectivity, PLC consumption or physical rejection. It
reuses bridge refresh/notifications without another timer or network request.
The persistent groups reserve compact role widths measured across each
language's concise states, recalculated on language/font changes rather than status
updates. This avoids both English-only clipping and reserving other languages'
longest captions at the expense of feedback. Longer source observations elide with
full accessible text; the event/error group receives all remaining horizontal
space. The event tooltip and accessible description retain actionable details,
including unresolved configuration, service and archive issues alongside
higher-priority feedback. Routine preparation does not hide these issues.
On Inspect, the player header names the Application fault and the status strip
keeps the fault icon in its reserved slot. Other pages show the state caption
beside that icon. The feedback area names the observed failure, such as
**Startup failed** or **Cleanup failed**. Its tooltip and accessible description
retain the exception type and point to Reset only when the current user can
perform it and the owner permits recovery.
Start availability and its localized tooltip and accessibility guidance use the
Application's current blocker. The existing Production feedback strip can show
the reason while stopped, with operational errors retaining priority. Qt also
guards Start during unsaved Vision edits, active editing work and window Close.
Command feedback keeps its owning operation and ignores superseded results.
A matching recovery clears the corresponding error, including earlier player
failures still queued for display. Independent issues retain their priority.
Only the event group stretches when the window grows; status changes do not
move the persistent groups.

Compact captions use their surrounding page/card context: service cards show
Reporting, Web and Status; the bottom strip also labels production reporting
as Reporting rather than PLC. Service-card dots, state captions and power actions
share measured width/spacing budgets. Title slots use the widest current localized
title measured with its Qt font and update only when captions/font metrics change.
Stacked cards align their indicators; side-by-side cards retain local header
coordinates and right-aligned actions. Runtime observations do not move the dots.
Reporting's compact Merge count dropdown uses 0/1/4 for per-cycle/single-group/four-group mode
choices, with unsupported counts visible but disabled; its independent power
action and Save/next-Start lifecycle are unchanged.
Field labels use IPv4, units and short localized names.
Full localized meanings remain in tooltips and
accessibility text. Routine disabled services show localized Off captions; waiting for the next
Start, active transitions, failures and ACK observations remain distinct. Idle
diagnostics omit no-request prose. A successful Watch pass updates the table
values and their last-read times. A failed manual read shows its cause. A failed
automatic pass also reports that refresh stopped. Failure feedback identifies any
retained timestamp as the last successful read. A diagnostic draft that differs
from the effective endpoint reveals the saved target above the operations.

The main window title is `Capsule Vision Inspection` on every page. The five
existing destinations (Production, Recipe, Communication, History and Vision)
use the visible English captions Inspect, Recipe, Connections, Records and Cameras.
Connections (连接 in Simplified Chinese) and Cameras (相机) use their localized
destination names on the buttons, in tooltips and for assistive technology.
Each page button centers its icon and localized caption together. The five
slots use the same widths on both workstation profiles, ranging from 110 to
142 logical pixels. Changing page, language or account preserves their
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

Native numeric inputs share compact, vertically paired chevrons on the right,
with themed hover, pressed, disabled and focus states. Their existing ranges,
precision, steps, validation and permission behavior remain field-owned; only
Color target Hue wraps. Optional process-parameter text, Vision FPS/ROI fields
with deferred validation, register lists and other non-spin inputs retain their
existing editing and commit behavior.

The [Qt workstation profiles](field-ui-validation.md#native-screenshots-and-geometry)
are portrait 1080×1920 and landscape 1920×1080 logical pixels at normal
application fonts. The native frame and taskbar reduce the maximized client area.

Folder Replay keeps Start/Resume, Pause/Step and Stop in fixed player positions.
Start remains visible in the operation slot in UNCONFIGURED and READY, including
while eligibility is blocked.
Basler shows only Start and Stop in a stable right-anchored pair, retaining the
separate applicable Fault Reset slot. Connecting uses the localized connection
caption before the existing warm-up presentation.
The player icons always sit above their captions.
The native player buttons occupy equal 120×60 logical-pixel slots across
languages, themes and action states. Captions elide when needed and retain
their complete localized names and guidance in tooltips and accessible text.
Inspect shows the Reset icon in its reserved slot when Application reports FAULT
and both the runtime and model-preparation owners have released their resources.
Operators and higher roles can activate it.
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
selector and Refresh. The selector normally reflects the accepted saved Recipe,
including startup restoration. An incomplete selection keeps that identity and
shows setup guidance with Start disabled. Request and active details remain
available in the accessible text and tooltips. Refresh lists saved definitions
without preparing them or relabeling the selected identity. When Recipe Save
invalidates the candidate, the selector shows the unselected placeholder and the
dropdown lists current saved names. The placeholder remains until explicit selection
installs a prepared candidate. The generic reselection instruction stays available
in tooltips and accessible feedback while the bottom strip shows other relevant
warnings or faults. Camera cards replace the empty-canvas guidance when a prepared
layout becomes available.
The selector prefers 170 logical pixels; long names elide only in its closed label,
with full text retained in the model, tooltips and accessibility.
Header space after Batch and selector width can shrink while preserving the player
slots and common Batch IDs. Save actions reserve
localized group budgets, borrowing flexible feedback/summary space. Conventional
utilities such as History paging/refresh, Recipe color sampling, Communication
group editing and advanced-settings disclosures use touch-sized icons with full
localized tooltips and accessibility.
Cameras pairs the coordinate check icon with a visible Apply ROI caption.
The Watch toolbar starts with the group dropdown and square Add and Remove
buttons. The selector uses the shared chevron. Read and Auto sit beside these
controls and reserve their own localized caption widths. During automatic refresh
the same slot shows a checked Stop action with the loop icon and one-second
interval. The toolbar stays on one row in both workstation profiles, and the
table stays in place between reads.
Long group names retain their full tooltip and accessible description.
Write has an explicit localized verb beside the register address and values.
Save and service power controls retain visible verbs. Compact captioned actions
keep stable widths across languages and states, with four-pixel horizontal
padding and a two-pixel focus allowance.
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
Read-only channel symbols show camera, channel and the short observed state in
tooltips and accessibility, without expanding runtime evidence.
Only identity-matched results draw defect boxes.

Production shows localized Cycles, `OK`, `NG`, `EMPTY` and `Δt` captions for completed
cycles, channel occurrences and last inspection duration. Counts come directly
from the application business counters; unavailable
timing displays `--`. Whole metric groups wrap when their text cannot fit one row.
These are cumulative occurrences, retained by Stop/Start and Recipe changes and
scoped to the selected Batch.

An enabled single-/four-cycle session instead presents **Groups / 组数** and
separate cumulative channel-position OK, NG and EMPTY totals. Groups counts
completed inspection groups attributable to a PLC counter. The **Batch ID / 批号**
identifies the operator's production Batch separately. Final NG includes every
authoritative reject bit, including conservative rejection. The Groups tooltip
also exposes the original raw inspection-cycle count. **Group time / 组耗时** runs
from first Cycle admission to the last required terminal result at the reporting
owner. It is published for eligible groups and excludes later attribution checks,
writes and ACK. Unavailable timing is `--`; per-Cycle timing remains available in
camera diagnostics. Qt and Web use the same group definitions. Qt captions follow
the shared localization coverage and its explicit translation-review fallbacks.

The effective mode defaults to per-cycle. A successful Start uses the admitted
reporting mode, with occurrence counting for per-cycle, reporting-disabled and
Folder sessions. Stop, a failed Start and edits to future reporting settings
retain the effective mode. Selecting or restoring a Batch adopts its recorded
mode when available and retains the current mode otherwise. Both cumulative stores
survive Stop/Start and Recipe changes and remain separate across mode changes.
Selecting a Batch loads its summaries and last eligible group duration and clears
current result overlays and group progress. Retained pixels do not carry the
cleared judgments.
Confirmed unbatched clearing resets both counter sets, recent result indications
and group duration in memory. It preserves the effective counting mode and any
persistence warning. It appends a routine Audit Trail action when available and
leaves historical records and PLC registers intact.

The History Cycle table shows Time, `#`, Group, Batch, Recipe and Result. Group
shows the first CycleId and the Cycle's stored slot (for example, `2265 · 2/4`
or `2265 · 1/1`), with group size read from the persisted Run reporting mode,
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
Single-cycle PLC groups use the ordinary identity-matched single-Cycle image and
single-result indicators, with authoritative physical-group counters. Group
counting and four-exposure display are separate snapshot facts.
In four-cycle mode the same store accepts only the reporting owner's actual fourth
member: at most one candidate fourth image and one eligible display image per
camera, plus their matching fourth result evidence. Eligibility promotes only
matching fourth pixels and clears cameras whose fourth frame is absent or invalid.
An exact fourth callback arriving late may fill its missing image; an earlier or
later frame cannot substitute. During the next incomplete group, the previous
eligible image retains its own fourth result/overlays and identity tooltip. New
generation/configuration boundaries invalidate compatibility; pending copies are
released on session cleanup. The display store keeps only the fourth-exposure
candidate and eligible image. The [archive worker](#image-archiving) owns the
originals retained for group archiving.

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
pixels; no separate persisted reference library exists. Recipe presents one shared
Size/Color tuning workspace within its bottom inspection card, outside the compact
summary scroll area. Side-by-side Size/Color controls have independent enablement
and expansion; mode switches retain frozen-image ownership, cached measurements
and viewport geometry. Explicit length/color sampling edits only Recipe draft
targets from that cached evidence, as described in
[configuration](configuration.md#editing-and-persistence). Vision's Capture button
toggles passive preview of the selected connected Basler camera. The existing SDK
owner supplies the next matching validated idle frame after arming each request;
preview does not wait for peer cameras or expire while idle. Qt requests another
frame only after accepting the previous one, updating the canvas without resetting
the ROI interaction for frames in the same image coordinates. Stop keeps the last
image. Camera/source changes, disconnect, Production Start, loss of admission,
page exit and Close cancel preview without automatic resumption. Qt rejects stale
deliveries and each new request checks Operator admission and session revision.
The separate timed single-capture path and Production attachment still require
an empty, complete SDK boundary. An SDK observation is not proof of physical
exposure time. Preview never opens another camera, sends a software/PLC trigger,
or creates an inspection, archive or accounting record.

Normal startup owns a Qt-independent `WebRuntime` control thread and at most one
Web publisher/serial HTTP pair. Production Stop/Start leaves Web independent.
Missing communication preferences leave Web and PLC reporting disabled; valid
saved settings restore independently of camera source mode.
On Connections, the Web header power action persists only Web enablement, then starts or
stops using saved network settings. When the running configuration differs, the
conditional action restarts Web with enabled saved settings or stops it with a
saved disabled selection. Drafts never become runtime inputs implicitly. Admission
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
Web exposes physical-group metrics for both attributed modes. Its `four_cycle`
presentation flag applies only to four-member groups; `group_counting` governs
metrics independently of the image/indicator presentation.
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

The publisher waits a fixed 500 ms after each publication and reports this
interval in the snapshot's `refresh_interval_ms` field. The browser starts a
poll every 500 ms or as soon as the previous poll finishes if it takes longer.
It fetches changed JPEGs sequentially and owns at most six displayed object
URLs plus one decode. Requests
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
bounded expiry/410 behavior and independent per-camera commits.

Group metrics, application fault and connection feedback share a compact row
above the camera grid. Messages and metric pairs wrap within the viewport.
When all three are absent, the row and its gap take no space. A change in row
height or window size refits the grid to maximize contain-fitted image area
while keeping the same camera cards, images and six channel indicators.

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
