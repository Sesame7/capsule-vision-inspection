# Application

This document owns the **Current** application contract: persistent startup,
candidate activation, disposable runtime sessions, production accounting, and
local source execution. `ProductionApplication` is Qt independent. The normal
entry currently composes it through `ui/bootstrap.py` and `ui/local_production.py`;
these implementation locations and thread names are descriptive, not permanent
Target architecture requirements.

Neighboring authorities are [configuration](configuration.md) for persisted
definitions and pure resolution, [inspection](inspection.md) for identities,
collection and judgments, and [inference/runtime](inference-runtime.md) for
engine/worker/coordinator execution and resource ownership. Presentation remains
in [Production UI](ui-shell.md#production), [Recipe editing](ui-configuration.md#recipes)
and [machine/source editing](ui-configuration.md#machine-roi).

## Startup recovery

### Persistent inputs and discovery

Normal startup uses root `app.py`. It anchors `data/` beside that entry, independently
of the process working directory:

```text
data/
  app.json
  machine/machine.json
  sources/camera_sources.json
  models/*.json                 model definitions
  models/<referenced artifact>   locally provisioned model
  recipes/*.recipe.json
```

`data/` is ignored persistent configuration, not generated scratch space.
Root `models/` and `images/` remain supported local CLI/reference inputs;
`outputs/` contains generated work and retained local evidence/manual setups.
Ignored status does not authorize deleting any of these inputs or evidence.
The external ViMo SDK, license and GPU runtime prerequisites are unchanged;
see [inference verification](inference-runtime.md#verification).

`app.json` is a separate application manifest. It requires integer
`schema_version: 1`, `default_model_id`, and `last_applied_recipe_id`. Both IDs
may be null, meaning unselected, but readiness requires explicit valid selections.
Only `language` is optional (`zh` or `en`, omitted value `zh`). For example:

```json
{
  "schema_version": 1,
  "default_model_id": "general",
  "last_applied_recipe_id": "local",
  "language": "zh"
}
```

These are illustrative IDs, not fallback identities. Duplicate keys, unknown
fields, missing identity fields, invalid identity values, wrong types and
unsupported versions invalidate the manifest. If language alone is invalid,
startup preserves the valid IDs, uses `zh` **in memory**, and issues a warning.
An unreliable manifest supplies no guessed production identities. Startup never
rewrites the manifest or other saved configuration.

Discovery is nonrecursive: model definitions are `*.json` files in `data/models/`,
Recipes are `*.recipe.json` files in `data/recipes/`. Strict codecs and
duplicate-ID rejection apply; invalid/duplicate catalog entries block startup
preparation even if another selected entry is valid. The manifest default Model
ID must exist even when the selected Recipe overrides it. Only the explicit
last-applied Recipe ID is restored; there is no first-file/first-Recipe selection.

Relative model artifacts resolve against each model metadata file's directory.
The startup registry holds absolute paths without changing the metadata on disk.
Relative Folder paths resolve against the source JSON's directory. Saved machine
camera order, mappings and ROIs remain authoritative. Normal Production requires
six configured cameras and matching source identities. Bootstrap never creates,
calibrates, clamps or repairs machine geometry, substitutes a model, or falls back
to fake inference. Detailed definition/resolution rules belong to
[configuration](configuration.md#definitions).

### Preparation and availability

On normal entry, catalog/configuration IO, resolution, model SHA-256 verification
and one Folder reference-group decode/bounds check happen **before QApplication
is created**. The reference group is discarded; it is not a production cycle.
No runtime session, engine, warm-up or activation occurs. A stopped application
owner and bounded display store are composed for the shell.

After Qt starts, saved Recipe Apply captures clean saved data, then hashes and
verifies configuration in the existing single background preparation slot.
Normal-entry preparation also checks the Recipe catalog and saved source
schema/camera identities. Folder availability is acquisition preflight, not
candidate validity. Setup/reference verification uses the same bounded job slot;
it is not a new scheduling service. Manifest notification writes are currently
synchronous Qt signal handling, separate from this background preparation.

| Startup condition | Application outcome |
| --- | --- |
| Missing/unreliable manifest or absent/unknown explicit production ID | UNCONFIGURED; no selection fallback |
| Missing, malformed, unreadable or duplicate Model/Recipe definitions | UNCONFIGURED; no catalog bypass |
| Missing/unreadable artifact or SHA-256 mismatch | UNCONFIGURED; no alternate artifact/engine |
| Missing/invalid machine or source definition, wrong camera count or source identities | UNCONFIGURED; no generated assignments or geometry |
| Valid candidate/source definition, but Folder missing, empty, unreadable or incompatible with saved ROIs | READY with actionable warning; Start preflight fails before session/engine construction |
| Omitted language only | Use `zh`; no language warning |
| Invalid language only, otherwise valid inputs | Preserve IDs/candidate, use `zh` in memory with warning; protect manifest writes |

Permission/IO failures follow the same boundary: required configuration/artifact
failure blocks preparation; unavailable image acquisition does not invalidate an
otherwise valid candidate. A valid startup has candidate identity but **no active
configuration**. No session, cycle, Batch, counters or previous results are restored.
The shell stays navigable; unavailable machine data produces an unavailable
workspace, not synthetic images or ROIs. Shell camera placeholders are not a
configured inspection layout.

### Preference persistence and feedback

Only successful preparation **and candidate installation** through the existing
Recipe Apply signal requests an update of `last_applied_recipe_id`. Selection,
draft editing, Save alone and failed preparation do not. Language changes request
their own preference write. The manifest stores neither active configuration nor
runtime/counter/draft state.

Candidate Apply and preference persistence are independent outcomes. A write
failure can leave the new candidate successfully installed while the previous
manifest remains on disk. It does not roll back Apply or imply activation. The UI
keeps successful Apply feedback and separate localized `app.json` repair/restart
guidance, with diagnostic details in tooltips/stderr.

The preference writer creates missing parent directories, writes UTF-8 JSON with
Chinese text preserved and two-space indentation, flushes/fsyncs/closes a
same-directory temporary file, then replaces the target. Failed replacement
preserves the previous target; failed temporary cleanup is noted on the exception.
It does not use the domain writer's sorted-key/explicit-LF formatting convention.
This is single-file atomic replacement, not an Apply/preferences transaction,
backup system or filesystem lease; see the distinct
[domain persistence contract](configuration.md#persistence).

Normal-entry notifications protect invalid-language, malformed, unsupported or
unreadable manifests for the session. They refuse to overwrite them even after a
successful Apply or language change. Before each permitted write they reread and
compare preference values; changed values or a newly invalid manifest are refused.
This is a value check, not locking against concurrent external writes. A definitely
missing manifest may be created by an explicit language change with null IDs;
that does not rediscover or select configuration.

Unresolved feedback clears only with relevant evidence:

- A startup Folder warning survives arbitrary Recipe Apply, a reference read or
  path edit. Successful Start preflight/readiness, or successful verification of
  all sources/bounds followed by source Save and Apply, clears it.
- Manifest/recovery guidance survives unrelated Apply, Start and Stop. A successful
  preference write clears its own error, without clearing another command's error.
- Runtime/source errors remain distinct from product NG. Actual runtime faults
  retain their visibility; presentation rules belong to the UI documents.

There is no Folder monitor or in-process configuration rediscovery guarantee.
Repair invalid Model/machine/manifest/source schema or artifacts externally and
restart for fresh discovery. Where supporting definitions are already valid,
explicit Recipe selection/Save and apply or Vision source tools provide their
existing recovery paths. Folder-path repair never replaces saved geometry.
Read the affected file/identity in the tooltip or stderr, verify READY and the
intended candidate, then explicitly Start when acquisition is usable. No automatic
repair, rollback or recovery wizard is implemented.

## Activation

### Application API and states

`ProductionApplication` provides locked commands/snapshots and one lifecycle
thread. `configure(prepared)` installs already resolved/artifact-checked data;
`prepare_configuration()` performs blocking artifact verification outside GUI
dispatch. The supplied session factory must return an unstarted session or raise
without retaining session/native/thread resources. An application-lifetime source
dataset has a separate local-composition owner, described under [Folder replay](#folder-replay).

| State | Allowed work and transitions |
| --- | --- |
| UNCONFIGURED | Install prepared candidate -> READY; Stop is an idle completion; Close |
| READY | Replace candidate; explicit Batch reset; Start -> STARTING; idle Stop; Close |
| STARTING | No production admission/configuration replacement; successful readiness -> RUNNING; Stop/Close -> STOPPING; recoverable startup failure -> READY after cleanup |
| RUNNING | Begin cycles, submit frames, observe expiration; Stop/Close -> STOPPING; infrastructure/lifecycle failure -> FAULT |
| STOPPING | No new production admission; repeated pending Stop shares completion; Close; safe termination -> READY (or UNCONFIGURED without configuration), unsafe cleanup -> FAULT |
| FAULT | No Start/admission/configuration replacement; explicit Stop/Close cleanup attempt; Reset only after session ownership and outstanding cycles are released |
| CLOSED | Terminal; no restart; repeated Close returns its existing notification |

Invalid commands raise `ApplicationStateError`; outstanding-capacity overflow
raises `ApplicationCapacityError`. `start()`, `stop()` and `close()` return
noncancellable `Future[None]` observations. Callers must not settle these promises.
Lifecycle exceptions retain their original causes; snapshots keep compact issue
type/message data rather than exception objects or tracebacks.
Callbacks must be short/nonblocking, must not join owners or wait for unfinished
work, and must not mutate widgets. Qt delivery is described in
[Qt integration](ui-shell.md#qt-delivery).

Candidate and active are separate immutable prepared configurations:

1. Save persists configuration; preparation verifies it; configure/Apply installs
   the stopped candidate. These steps do not activate inference.
2. Each Start increments generation and creates a fresh session, coordinator and
   worker. Start clears the previous latest result and application issue, but
   retains counters, Batch identity and last-known-good active configuration.
3. Artifact verification runs again on the worker before engine creation. These
   checks are point-in-time; model files must remain immutable while in use.
4. Only successful runtime readiness/warm-up, with no Stop or terminal session
   notification, promotes the candidate and enters RUNNING.
5. If candidate B fails, previously active A stays active; B stays candidate.
   Safe cleanup returns READY with a startup issue. Unsafe ownership/cleanup enters
   FAULT. No automatic rollback, retry or reactivation of A is performed.

Configuration replacement is restricted to UNCONFIGURED/READY, never hot-swapped.
Machine Apply prepares the saved Recipe against saved geometry; it does not
change Recipe identity or reset counters. Snapshot IDs are not geometry revisions.
[Configuration resolution](configuration.md#resolution) produces the immutable
per-channel checks actually consumed by the configured pipeline, rather than a
UI-only summary. ViMo runs once per full camera image when any channel needs it;
traditional checks consume the resolved settings. Evidence assignment, label
filtering, optional/required failures and Size/Color judgment remain authoritative
in [configured inspection](inspection.md#configured-execution).

### Readiness warm-up

`ProductionRuntimeSession` uses a supplied warm-up provider on its owning worker.
Default readiness is **two serial calls per distinct input shape per session**;
`warmup_iterations` must be positive. Arrays must satisfy the BGR input contract;
duplicate shapes are skipped, no input is resized, and an empty/invalid provider
or result-shape mismatch fails startup. Keep provider pixels and aliases stable
until startup completes.

Warm-up creates no CycleId, production result, channel judgment or counter
increment. It is engine readiness work, not a 36-channel inspection, and runs even
before production admission is available. Local composition supplies its preflight
image group. Stop during warm-up lets the active native call return, skips remaining
iterations and prevents candidate promotion. A blocked call remains observable in
STARTING/STOPPING; an observer timeout is not cancellation or cleanup success.

## Counters

### Results and bounded receipts

`begin_cycle(cycle_id)` returns a frozen `ProductionCycle` containing two distinct
noncancellable observations:

| Observation | Meaning |
| --- | --- |
| `accepted: Future[None]` | The collector admitted the cycle, not merely a queued begin command |
| `result: Future[ProductionInspectionResult]` | Terminal formal result or explicit runtime/admission exception |
| `submit_frame(frame): Future[None]` | Collector frame-admission acknowledgement, not inspection completion or buffer return |
| `expire(): Future[None]` | Explicit deadline-observation command, not a periodic scheduler or terminal cycle result |

Cycle identity/order and frame ownership remain defined by
[inspection](inspection.md#identity-and-frames); underlying admission and timeout
guarantees are in [runtime](inference-runtime.md#admission-and-futures).

Application outstanding receipts are bounded (`max_pending_cycles`, default 8),
separately from collecting/READY/command capacities. Locked bookkeeping records
generation, CycleId and a unique receipt token, accounts admission once, removes
the token at terminal completion, and ignores duplicate/stale callbacks. Old
session-health callbacks also check session identity. The session hands off compact
production results through a transient map and removes them on delivery; there is
no unbounded completed-result/deduplication history. Application latest result is
one lightweight record; caller-retained Futures/results are the caller's responsibility.

Rejected admission settles both observations exceptionally without incrementing
started. Admitted work can later fail exceptionally without a completed-result
increment. Healthy overload/rejected queued work is observable, not a fabricated
inspection result or automatic FAULT. A wrong result CycleId, terminal worker/runtime
failure or lifecycle invariant failure enters FAULT and revokes admission. Any
unresolved receipts at termination are settled exceptionally and treated as a
lifecycle failure, never silently lost.

### Factual accounting

| Counter | Meaning |
| --- | --- |
| `cycles_started` | Collector-admitted cycles |
| `cycles_completed` | Formal production results accounted exactly once |
| `channel_ok`, `channel_ng`, `channel_unknown` | Completed non-disabled channel judgment occurrences |
| `channel_disabled` | Completed channels with technical DISABLED, counted before judgment |
| `production_ng_cycles` | Completed result's product judgment is NG |
| `technical_error_cycles` | Completed result's technical status is ERROR |

For one channel, SUCCESS + OK increments `channel_ok`; SUCCESS + NG increments
`channel_ng`; ERROR + UNKNOWN increments `channel_unknown`; ERROR + NG increments
`channel_ng` only; DISABLED + UNKNOWN increments `channel_disabled` only. The four
channel counters partition the result's configured channels (36 in the current
layout). Cycle NG and technical ERROR counters can both increment. Unassigned
NG evidence can make a cycle NG without inventing a channel NG occurrence.

Formal TIMED_OUT/SHUTDOWN results count as completed; exceptional runtime outcomes
do not. Started and completed can legitimately differ after overload/Stop.
A normal result containing ERROR + UNKNOWN leaves the application RUNNING;
technical failure evidence is distinct from an infrastructure FAULT. Detailed
judgment rules belong to [inspection](inspection.md#judgment).

These are **admitted-cycle/channel-judgment occurrences**, not physical capsule,
product, camera-trigger or valid PLC-trigger counts. Only explicit
`start_batch(nonempty_id_or_None)` in READY resets counters and latest result.
Batch ID is None until supplied; no timestamp invents it. Start/Stop, fault Reset,
Recipe/model/layout changes preserve counters. Start alone clears latest result,
so latest-result clearing must not be confused with counter reset.

`ProductionApplicationSnapshot` is frozen and contains state/generation,
candidate/active Recipe/Model/hash identities, optional Batch ID, frozen counters,
latest compact production result, `runtime_owned`, and last structured issue
(stage/code/type/message). It stores no image, mask, traceback or event history.
Interpret state together with ownership and issue; retained active identity while
READY/FAULT does not mean inference is running. UI display pixels are separate.

## Local ingress

`LocalCycleIngress(application, layout)` is a synchronous admission helper with
no thread, queue or lifecycle ownership. Create it after successful Start, use
the running configuration's camera order, and keep it as the sole serial CycleId
source for that generation. The caller decodes a complete ordered image set.

Before begin, ingress validates every input and copies it into independently
owned C-contiguous uint8 H x W x 3 BGR storage, even if already owned. Keep the
input sequence/pixels stable during `submit()`; afterward external arrays may be
reused while inference continues, because submitted Frames own independent copies.
It does not reinterpret color, resize or change orientation.

Each valid submission uses local `CycleId(1), (2), ...`, configured CameraIds and
`FrameId(cycle_id, camera_id, sequence=0)`. Local monotonic capture metadata is not
camera exposure, membership or a collector deadline. Pre-admission invalid input
consumes no ID; admission rejection may leave gaps without retry. IDs are local
correlation, not persistent product/PLC identity.

`submit(images)` waits for cycle acceptance and each frame acknowledgement, then
returns `ProductionCycle`; it does **not** wait for inspection. Observe `cycle.result`
for the terminal outcome. All these waits belong to a non-GUI source context.
Input/admission failures propagate unchanged. Submission is **not transactional**:
if a later frame fails admission, earlier accepted state remains application-owned.
Use existing explicit expiration or Stop/Close cleanup; no implicit rollback,
retry, deadline scheduling or fake OK/NG is added.

Ingress rejects use while stopped or after a generation change. Stop/drain the
source before Stop/Start or session replacement, then create a new ingress.
Snapshot checks cannot atomically protect a concurrent Stop/Start between check
and submission: application commands do not accept a generation token. Do not
mix independent cycle-ID producers in one session.

## Folder replay

`FolderReplay` is a single-owner reader, not a camera adapter. Its on-demand path
remains in use for Loop off and memory fallback. It
snapshots file paths in each configured camera's Folder, nonrecursively, supporting
PNG/JPG/JPEG/BMP/TIF/TIFF case-insensitively. Natural numeric filename ordering
uses case-folded text and original spelling to break ties. Camera association is
explicit layout/source identity, never inferred from filenames or arrival time.

One group contains one image per camera in configured order. Each image
must satisfy BGR and saved ROI bounds; subsequent shape changes within a camera's
replay fail, without resize. Different cameras need not share one image shape.
Only a complete valid group advances indices. Corrupt files are errors, not
silently skipped or substituted. With Loop on, cameras wrap independently; with
Loop off, replay ends when any camera exhausts its list. No partial group is
submitted and no directory watcher refreshes the path snapshot.

Normal local session creation runs on the application lifecycle thread. Each
Start reloads saved source definitions, checks identities and creates a fresh
replay cursor **before constructing a runtime session/engine**. For saved Loop
sources, the local application first loads or validates its decoded dataset cache;
the uncached path instead decodes the first group.
Readiness uses that group; the first production cycle uses it again only after
RUNNING. Every restart begins replay at the start, never at a saved cursor.

### Application-lifetime decoded dataset

`FolderReplayCache` belongs to the local application composition. On the first
Start with saved `loop=true`, it enumerates and decodes every supported file for
every configured camera, validates uint8 BGR shape and saved ROI bounds, and
requires consistent dimensions within each camera. Cameras may have different
dimensions. It publishes one complete dataset only after checking the file
inventory/metadata again. Any detected change during loading fails Start; there
is no partial production admission or automatic retry.

Stop retains a successfully loaded dataset. Later Starts validate identity and
reuse its readonly arrays without decoding. Fresh sessions, generations, replay
cursors, CycleIds and FrameIds still apply. Every cycle executes the configured
inspection pipeline; results, masks, classifications and derived HSV/ROI data
are never cached. Original source ordering, independent wrapping, complete-group
advancement and remaining-budget pacing are unchanged.

Identity includes the saved source definition (including Loop/pacing), configured
camera order, naturally ordered resolved file paths, and each file's size,
nanosecond modification/ctime metadata, device/inode and mode. There is no image
content hashing on every Start. Ordinary additions/removals/replacements and
metadata changes trigger retirement and full rebuilding of this one dataset.
It is not a per-camera multi-dataset cache. Current ROI bounds are revalidated on
a hit; geometry is never repaired or resized. Unsaved source drafts are ignored.

This fingerprint is a point-in-time check, not a filesystem transaction or
continuous monitor. Content changes that preserve all observed metadata can
escape detection, and concurrent filesystem changes cannot be ruled out between
checks. During RUNNING, cached pixels remain the frozen dataset even if disk files
change. No hot reload occurs. A detected source change or failed rebuild leaves
no stale/partial cache to reuse. Cancellation of validation of an otherwise
unchanged existing cache retains it for a **new validation on the next Start**.

Decoded bytes are actual array `nbytes`; no arbitrary 8 GiB cap applies. Before
each decode and after allocation, current available physical RAM must leave
`max(2 GiB, 10% of physical RAM)` for the system/runtime. The preceding image's
size estimates the next same-camera allocation; the first image has no known
decoded size until decoding completes. These observations are not an allocator
reservation or guarantee against concurrent allocations. Cached hits also check
the reserve. Missing memory telemetry, insufficient reserve, `MemoryError` or
OpenCV's explicit out-of-memory status discard partial/old cache and select the
original on-demand reader. This **observable memory fallback** is reported through
the compact cache status callback and stderr, with its reason; first-group
preflight and subsequent source errors keep their original behavior. An actual
corrupt/invalid image is a source error, never a memory-fallback reason or a file
to silently skip. Files not reached before memory fallback are validated by the
original on-demand path when read.

Bulk loading stays on the application lifecycle thread, outside Qt and before any
native engine exists. Start remains asynchronous; the shell uses its existing
STARTING/STOPPING presentation. Metadata-only progress/status callbacks report
loading counts, bytes, population time, hits and fallback (default: stderr).
Callbacks must be short/nonblocking and must not mutate widgets. Stop/Close is
observed before/after each decode and at enumeration/metadata boundaries. An
active filesystem/decode operation finishes naturally; it is not force-cancelled.
Incomplete cancelled loading never constructs the native runtime. Its Start
Future fails through the existing startup boundary; successful cleanup returns
READY or completes Close. `runtime_owned=false` while loading does not prove
lifecycle-thread termination: Stop/Close Futures and actual join remain necessary.

The cache stores readonly views over readonly decoded bases, without another
full-image copy. Ingress still makes independently owned Frames. Display
publication copies identified source pixels directly into the existing byte-backed
display store; borrowed cache views are not misrepresented as owned domain Frames.
Source aliases remain immutable until their consumer is finished. After actual
safe joins, disposable sessions drop their dataset references; application cache
ownership survives Stop. Successful Close releases the cache after safe runtime
shutdown, and application join confirms owner-thread exit. Failed/unsafe Close
retains ownership and cache. Chained preload errors discard bulk traceback locals
as well as partial cache storage.

`loop=false`, explicit fixed-image CLI inputs, and independent Vision Folder
reference browsing remain uncached with their prior behavior. Normal startup's
pre-Qt first-group availability check is also unchanged; full residency begins
only with explicit Start. No source JSON/Recipe fields or persistent cache files
were added.

Current `LocalRuntimeSession` composes one non-daemon local source thread with the
existing serial runtime. It waits for its matching RUNNING generation, submits
through ingress, and waits for each terminal production result before proceeding.
It retains no result history and publishes a separate display copy using the actual
admitted frame identity/generation/source key. Display delivery is not accounting.
Local composition uses one collecting and one queued READY slot with a 30-second
collection deadline; these are current demo settings, not hardware requirements.

Saved `target_fps` supplies a start-to-start source-iteration budget. The loop
uses the absolute deadline of the previous iteration's monotonic start plus
`1 / target_fps`. B12 waiting reserves 12 ms from the remaining budget for fine
waiting: the existing Stop Event directly interrupts the coarse wait, then
deadline-aware `sleep()` requests of at most 0.5 ms finish the interval, checking
Stop between slices. An active sleep cannot be interrupted; scheduling can extend
it, so this is not a cancellation-latency upper bound. Stop checks before new
source work and actual source/runtime joins remain required.

The first iteration has no pacing wait; the readiness wait before RUNNING is
unchanged. Work that reaches or exceeds the deadline adds no pacing delay, fixed
post-result wait or catch-up burst. This policy covers cached Loop replay,
on-demand non-Loop replay and explicit fixed images (which retain the 8 FPS
default). Iterations include decode/copy, admission, result waits and display
publication. This is serial software pacing, not guaranteed camera FPS, physical
throughput, PLC synchronization or a hard deadline. Collector timeout observation
remains explicit; replay does not introduce an autonomous expiration scheduler.

Normal non-loop exhaustion requests application Stop and settles `source_done`
normally. A later source/decode/admission failure settles `source_done`
exceptionally, reports source feedback and requests Stop to clean partial work;
it does not fabricate NG or by itself redefine application FAULT. An underlying
terminal runtime failure still faults the application. Expected Stop-related
admission errors are suppressed during shutdown; other errors stay observable.
The source completion Future is notification, not proof of source-thread exit.

## Shutdown

Software Stop is graceful inspection shutdown, **not emergency/safety stopping**.
It immediately revokes production admission. The current coordinator policy
finishes the already claimed whole cycle, finalizes incomplete collection as
SHUTDOWN (or already due TIMED_OUT), and rejects queued READY work not begun.
It cannot force-cancel an active native call; see
[runtime shutdown](inference-runtime.md#shutdown) for lower-layer details.

Application lifecycle cleanup requests session Stop, joins its owned runtime
threads, verifies `cleanup_safe`, and resolves outstanding observations before
release. Local composition must also join its source; READY cannot precede its
exit. Stop success establishes session termination/ownership release, while the
application lifecycle thread remains alive for another Start. A pending repeated
Stop shares completion; idle READY/UNCONFIGURED Stop needs no session.

Unsafe engine close or uncertain thread/resource release retains session ownership
in FAULT. Fault Reset requires no session and no outstanding receipts, clears the
application issue, and returns READY (or UNCONFIGURED); it never starts a runtime
or resets counters. A later **explicit** cleanup attempt is allowed; no automatic
recovery discards unsafe ownership.

Close prevents further commands and drains/releases the session before CLOSED.
Its Future is delivered from the exiting application thread; callbacks can still
be running when it becomes done. `join(timeout)` separately confirms actual
lifecycle-thread exit and cannot run on that same thread. A failed Close retaining
unsafe ownership leaves FAULT and permits a later explicit attempt. Observer
Future/join timeouts never force cancellation or pretend successful termination.
The GUI uses its existing nonblocking close workflow; blocking fallback cleanup
in launchers happens outside Qt event dispatch.

## Developer entry points

Use the verified environment with `uv run --no-sync`. The following commands are
from the repository root; they are supported entry forms, not actions required
on every development cycle.

| Entry | Meaning |
| --- | --- |
| `app.py` | Normal real-inference shell; persistent manifest/catalogs/sources under entry-relative `data/` |
| `scripts/run_local_production_ui.py` | Explicit-path real Production composition; independent of manifest preference restoration/writing |
| `scripts/run_production_ui.py` | Equipment-free shell with explicitly fake engine, including when local pixels are supplied |
| `python -m vimo_demo.ui.app` | Separate one-image inference/worker/Qt diagnostic, not production configuration or accounting |

```powershell
uv run --no-sync python app.py
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python -m vimo_demo.ui.app models/model_trt_fp16.vimosln
```

Root `app.py` has no language/size CLI; language comes from preferences. The
one-image diagnostic optionally accepts `--image <path>`. Fake shell scenarios
are developer simulation, not alternative normal-entry recovery.

An explicit real launch using illustrative existing local files is:

```powershell
uv run --no-sync python scripts/run_local_production_ui.py --model-config data/models/model.json --machine-config data/machine/machine.json --recipe data/recipes/local.recipe.json --sources-config data/sources/camera_sources.json --language zh --size 1080x1920
```

These filenames must exist and validate; they are not universal catalog filenames
or a first-file fallback. Omitted `--sources-config` means `camera_sources.json`
beside the supplied machine file. Missing sources in this developer entry can
produce an unsaved empty Folder draft; that does not weaken normal startup's
required persisted-source validation. Start still needs usable inputs.

Mutually exclusive `--frames-dir images` (first six naturally sorted files) or
`--images <six paths>` bind fixed images in configured camera order for this
process, repeated serially. They do not save sources or become normal persistent
Folder configuration. Artifact paths still resolve relative to model metadata;
there is no runtime/model substitution. Operator persistence and draft workflows
remain in the existing UI documents.

For headless composition, supply prepared configuration and owned warm-up arrays;
the example performs readiness only, no production admission:

```python
from vimo_demo.application import ProductionApplication, ProductionRuntimeSession

# prepared and warmup_images are supplied/validated outside GUI dispatch.
application = ProductionApplication(
    lambda candidate, generation: ProductionRuntimeSession(
        candidate, generation, warmup=lambda _: warmup_images,
    )
)
try:
    application.configure(prepared)
    application.start().result()  # Blocking example: non-GUI caller only.
    snapshot = application.snapshot()  # RUNNING; warm-up added no counters.
    application.stop().result()
finally:
    application.close().result()
    assert application.join()
```

## Verification

Current deterministic tests use fake sessions/engines, controlled Futures/events
and isolated temporary configuration. They verify failed-candidate retention,
warm-up exclusion, ordinary ERROR versus fatal FAULT, bounded/stale receipts,
counter reset, ingress copying/nontransactional admission, saved-source replay,
manifest recovery and independent feedback outcomes.

```powershell
uv run --no-sync pytest tests/unit/test_production_application.py tests/unit/test_configured_execution.py tests/integration/test_application_runtime.py --tb=short
uv run --no-sync pytest tests/unit/test_application_bootstrap.py tests/integration/test_application_bootstrap_ui.py tests/integration/test_feedback_corrections.py --tb=short
uv run --no-sync pytest tests/unit/test_folder_replay.py tests/integration/test_local_ingress.py tests/integration/test_camera_setup.py tests/integration/test_local_production_ui.py --tb=short
uv run --no-sync pytest tests/unit/test_folder_cache.py tests/integration/test_folder_cache_lifecycle.py --tb=short
```

Optional real-GPU checks require explicit opt-in and local prerequisites. These
short readiness/local-composition checks are not full-dataset or stress tests:

```powershell
uv run --no-sync pytest tests/integration/test_application_vimo.py tests/integration/test_local_production_vimo.py --run-gpu --require-gpu -s --tb=short
uv run --no-sync pytest tests/integration/test_folder_cache_vimo.py --run-gpu --require-gpu -s --tb=short
```

The cache GPU smoke reads the saved Loop-enabled `data/` configuration and folders,
preloads once, executes a few real cycles, then verifies reuse through a fresh
Start/session/engine. It preserves assets/settings and records load bytes/time,
actual inference signatures and cleanup under its pytest temporary directory.
It is a wiring observation, not a pacing/performance qualification.

Normal quality commands remain in [AGENTS](../AGENTS.md). Markdown-only reconstruction
requires static link/path/example/command/whitespace checks, no runtime testing.
Historical SDK/dataset/stress/visual findings remain in
[verified evidence](archive/validation-evidence.md) and the tracked golden fixture; they are
not continuous guarantees or model-accuracy certification. Historical local
migration provenance/backups remain preserved separately from current startup
instructions.

**Target / deferred:** real camera buffer leases and trigger-to-cycle association,
PLC outputs, physical counting, safety stopping, calibrated production acceptance,
hardware deadlines/throughput, History/Communication execution and archival
consumers are unimplemented. Folder replay and current thread topology do not
decide those designs. No automatic retries, hot activation, cross-file transaction,
configuration watcher or persistent accounting recovery is promised here.
