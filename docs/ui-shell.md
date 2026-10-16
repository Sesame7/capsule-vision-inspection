# UI shell

This document owns the **Current** Qt shell, Production presentation, display
ownership/identity, shared feedback, notification delivery and safe closing.
Named widgets, timers and layout measurements describe the implemented/reviewed
baseline; they are not permanent Target architecture requirements.

The [application](application.md) owns activation, sessions, results and accounting;
[inspection](inspection.md) owns identities, evidence and judgments;
[configuration](configuration.md) owns persisted definitions/resolution; and
[inference/runtime](inference-runtime.md) owns execution and resource lifecycle.
Qt presents these contracts. It does not construct a Production engine, schedule
inspection, decide product acceptance or update counters on behalf of painting.
Operator editing remains in the existing [Recipe](ui-configuration.md#recipes),
[machine/source](ui-configuration.md#source-reference) and [tuning](ui-configuration.md#tuning-preview)
documents; this page covers their interaction with the shell, not their controls.

## Shell

`ProductionWindow` hosts five destinations in this order:

| Operator label, Chinese / English | Current content | Internal route |
| --- | --- | --- |
| 生产 / Production | Summary and six configured camera cards | `production` |
| 配方 / Recipe | Saved/draft Recipe workspace and Tune preview | `parameters` |
| 通信 / Communication | Session-local automatic reporting, manual D/W operations and register watches | `diagnostics` |
| 历史 / History | Unimplemented placeholder | `history` |
| 视觉 / Vision | Camera sources, reference images and Machine ROI workspace | `settings` |

**Vision** is the visible page name. Legacy `settings`/`diagnostics` identifiers
are implementation routes, not extra Settings/Diagnostics features. Communication
provides explicit manual reads/writes on configured PLC endpoints and optional one-second register
watch refresh, without persistent online state. Normal startup also presents a
compact automatic-reporting bar with endpoint, enablement, ACK/counts and last-error
details. Low-frequency automatic/manual connection editors are in one nonmodal
settings dialog, with separate manual/automatic UDP/TCP selection (default UDP).
TCP disables UDP address editors without clearing them; negotiated nodes are used
by the owning background client. The page emphasizes register
watches; their editor, manual operations and logs start collapsed. Empty tables are
hidden; useful rows grow to bounded viewports before internal scrolling. These
presentation choices retain observations and do not initiate/cancel requests.
Reporting preferences are editable only in READY/UNCONFIGURED; the existing bridge polls bounded H2B observations for
display, without driving reporting or accounting. Ordinary reporting errors stay
in that bar rather than changing shared event priorities or application FAULT.
The [Communication contract](communication.md#normal-application-reporting-controls)
owns activation, status meanings and reserved manual-write protection. Leaving the
page stops future refresh/unstarted reads while preserving the active owner until
actual exit; its controls/ownership belong to
[communication](communication.md#communication-workspace). History does not archive
cycles. Navigation, Qt-drawn icons, language switching,
selection, tooltips/accessibility names and keyboard/touch controls share the
Chinese/English style catalog in `production_style.py`. The language button names
the language it switches to. Camera headers use the same monochrome Qt camera
icon + configured number as Vision, retaining full localized names and CameraIds
in tooltips/accessibility. No emoji or bundled font is required.

Navigation does not clear backend results/counters or release runtime ownership.
Leaving a tuning/setup workspace goes through its existing draft/job-lifetime
guard; changing pages is not implicit activation or shutdown. The fixed shared
status strip stays outside the page stack, including while preview is visible.
The shell remains navigable when startup is UNCONFIGURED. Without a saved machine
layout, Production shows an unavailable workspace, never generated camera/ROI
geometry. Startup/recovery policy belongs to
[application startup](application.md#startup-recovery).

## Production

### Summary and commands

The summary has two compact rows. In the first, Batch ID/edit action is left,
application/replay state and Recipe identity centered, and the operation group right
(two context-sensitive controls with local replay capability; one otherwise).
Equal flexible side columns center the compact state/Recipe group
across the row; the operation area stays stable across states. The second row
shows completed cycles, **CH OK**, **CH NG**, and available latest-inspection elapsed ms.
CH OK/NG read the application's separate business-occurrence counters, never legacy
judgment totals. Aggregate EMPTY/UNKNOWN counts are not displayed. The four metrics
occupy equal-width columns with left-aligned secondary captions and modestly
emphasized values; their definitions, timing scope and missing-value semantics
are unchanged.

State uses one short localized text label without a duplicate state icon/badge:
neutral READY `就绪` / `Ready`, accent STARTING `预热中` / `Warming up`, restrained
green RUNNING `运行` / `Running`, amber local PAUSED `暂停` / `Paused`, amber STOPPING
`停止中` / `Stopping`, and violet FAULT `故障` / `Fault`. Closing is also compact.
The full existing state explanation remains in tooltip/accessibility text; issue
details retain the shared feedback policy. PAUSED is still application RUNNING.
These labels do not introduce states, activate configuration or drive commands.

Production buttons replace native focus outlines with a visible two-pixel rounded
focus border (light on the primary action, accent on other targets). Hover and
disabled styles are scoped to Production; keyboard navigation, accessible names
and touch targets remain intact. Other pages/navigation keep their existing styles.

Recipe presentation uses the Recipe navigation icon followed by a read-only name:

- Candidate only, before the first successful Start: show its name with no pending
  marker. This is configuration information, not a claim of an active engine.
- Identical active/candidate configurations: show one name.
- Different active/candidate configurations: retain the active name and append
  localized `Pending Start` / `待启动激活` for the prepared candidate. The remembered
  active display name remains distinct even for same-ID edits until successful Start.
- Neither identity: show `--`.

Recipe selection/editing belongs to Recipe. UI names do not promote a candidate;
the [successful-Start boundary](application.md#activation) remains authoritative.
Long names elide when needed, with full Recipe/pending information in tooltips and
accessibility text.

Batch is labeled `批号` / `Batch`, with neutral `—` when unset and no extra identity
icon. Its edit icon stays adjacent. Width is allocated from available space and
text metrics; ordinary
IDs such as `12313123`, `1231312213123` and `LOT-20261009-001` remain visible at the
reviewed portrait/landscape sizes. Unusually long IDs elide only when necessary.
Long Batch/Recipe identities share available width through Qt layout constraints
and text metrics, retaining readable Pending Start text. The Batch label stays
enabled for full-value tooltip/accessibility access even when editing is disabled
in RUNNING.

For compositions without local replay-control capability:

| Application state | Presented primary operation | Batch edit |
| --- | --- | --- |
| UNCONFIGURED | Disabled Start with configuration feedback | Disabled |
| READY | Start, subject to current preparation/dirty-setup guards | Enabled |
| STARTING / RUNNING | Stop | Disabled |
| STOPPING | Disabled Stopping progress | Disabled |
| FAULT | Reset only when runtime ownership is released; application still validates safety | Disabled |
| CLOSED | Disabled Closed | Disabled |

### Local replay operations

Validated Folder composition in normal startup and the explicit developer launcher
wire the same `QtProductionBridge.local_replay` boundary. The optional source
capability determines presentation; generic/fake modes without it retain the table
above. It presents two compact Qt-drawn Play/Pause/Stop-square/Step-forward operation
buttons, with localized labels, tooltips and accessibility names:

In normal startup this capability survives saved Folder/Basler mode changes, while
`replay_enabled` follows the saved mode. Basler hides Step/Pause/Continue and uses
ordinary Start/Stop; the existing operation-group allocation remains stable for
switching back to Folder. An unsaved Vision mode selection cannot change execution.

Both replay targets are 132 x 44 logical pixels, with equal icon sizes/strokes.
Their positions remain stable across Start/Pause/Continue/Step/End and disabled
progress; an unavailable secondary action is hidden but retains its layout space.
Primary/secondary treatments remain distinct and keyboard focus remains visible.
Without replay capability the existing single 176 x 44 operation is retained.

| Local presentation | Primary | Secondary |
| --- | --- | --- |
| READY | Start / 开始 | Step / 单步 |
| RUNNING automatic source | Pause / 暂停 | End / 结束 |
| PAUSED source (application still RUNNING) | Continue / 继续 | Step / 单步 |
| PAUSING / STEPPING | Disabled progress | End / 结束 |
| STARTING / STOPPING | Disabled progress | Unavailable |

FAULT/Reset and close safeguards remain authoritative. There is no End button
while PAUSED; Continue followed by End, application Stop or window Close still
performs genuine shutdown. End invokes the existing application Stop. Pause/Step
do not release resources, unlock editing, reset counters or imply a PLC ACK.
The replay-paused state label is page-scoped presentation with explanatory tooltip,
never `ApplicationState.PAUSED`. Persistent configuration/source selection and
reporting/Recipe/Vision editing restrictions remain authoritative while paused.

The bridge observes a small replay snapshot separately from the application
snapshot and delivers operation Futures through the existing queued gate. It
never owns source scheduling or blocks on unfinished operations. Until a claimed
cycle's formal result is observed, Pause remains progress and Step is not reusable;
rapid duplicate commands cannot queue extra permits. Stale action/generation checks
prevent an old displayed action being reinterpreted after a lifecycle transition.
See [application replay controls](application.md#local-replay-controls) for timing,
cursor, source exhaustion and shutdown semantics.

Closing disables operational controls. Commands go through `QtProductionBridge`
to `ProductionApplication`, with authoritative state validation even if a displayed
button has become stale. Prepared selection, Reset and Batch are cheap locked
application operations; Start/Stop/Close are observed asynchronously. Heavy model
hashing/preparation uses the existing background slot. This does not claim all
configuration reads/writes are asynchronous; their actual contexts are documented
in [application](application.md#preparation-and-availability).

Only READY permits the compact Batch dialog. Explicit confirmation calls the
application Batch operation and resets its counters/latest result; Cancel changes
nothing. The warning says previous statistics are **not archived**. Start/Stop,
Recipe changes and fault recovery do not reset counters. CH OK/NG count channel
business detection occurrences, **not physical capsules/products**; repeated
observations count again. Detailed factual meanings
are in [application counters](application.md#counters).

Summary timing uses the latest result's inspection-start to result-ready interval,
not frame collection, camera exposure, GUI refresh latency or physical throughput.
Camera headers show available measured inspection-stage timing; unavailable values
use `--`. Fake timings describe fake work, not GPU performance or acceptance limits.

### Camera cards and reviewed layout

Cards follow configured camera order; six indicators follow that camera's configured
slot mapping. Identity is never inferred from arrival order or global channel-number
sorting. Headers put camera identity left, the fixed indicator group centered and
available timing right. Abnormal/expanded IDs do not shift indicator slots.

| Channel presentation | Treatment |
| --- | --- |
| No matching result | Pending outline circle |
| Business OK | Restrained green check |
| Business NG | Red cross on a subdued red background |
| Business EMPTY | Neutral gray outlined empty slot, distinct from pending dot/disabled dash |
| Business UNKNOWN | Amber warning |
| ERROR + NG | Red NG treatment/background with amber border and `!`; both meanings retained |
| ERROR + EMPTY | Gray EMPTY treatment with amber border and `!`; valid Occupancy and technical failure coexist |

Abnormal channels show ChannelId; clicking an indicator toggles its ID display.
The primary state comes from the [domain classification](inspection.md#channel-business-status),
not a widget-specific judgment rule. DISABLED remains an original technical status
in details; without valid EMPTY or NG it presents as business UNKNOWN. The old
assessment-only indicators used outside Production retain their disabled dash.
Localized tooltips/accessibility names include business status, original Occupancy/
view ratios, ProductJudgment, technical status, stable reason codes/types and
confirmed-EMPTY traditional skip provenance. Raw SDK exception messages/paths are
not exposed. A camera
technical error has amber header/border treatment; application FAULT has distinct
text/color. Neither creates invented product NG evidence. The image area avoids
generic technical-error text; headers/indicators and specific shared feedback retain
the evidence. Current [judgment semantics](inspection.md#judgment) stay unchanged.

Current layout/review baselines, in **logical pixels**:

| Baseline | Current behavior |
| --- | --- |
| 1080 x 1920 and 900 x 1200 | 2 camera columns x 3 rows |
| 1920 x 1080 | 3 camera columns x 2 rows |
| Grid switch | Three columns when width >= 1.2 x height, otherwise two; reuse the same cards |
| Minimum window | 880 x 720; not a promise that every minimum-size orientation is an operator target |
| Indicator slots | 44 x 44, 2-pixel gap |
| Operation targets | 176 x 44 without replay capability; two equal 132 x 44 targets with replay |
| Shared status strip | Fixed 33-pixel height, one row |

Qt layouts/DPI handle logical coordinates. Icons have common DPR variants and
do not depend on font glyphs; text uses installed Microsoft YaHei UI / Segoe UI
fallbacks. No font is copied or installed. These sizes preserve the current baseline
until deliberately revised; they do not establish physical-screen ergonomics.

The minimum 880×720 opens with the existing three-column grid, but those narrow
card headers do not guarantee room for all six fixed 44px targets; overlap/clipping
also exists in the pre-business-status layout. Summary metrics fit that baseline.
The reviewed full channel-header baselines are 1080×1920 and 1920×1080. No camera
placement, touch-size or grid redesign is implied by this known minimum-width limit.

## Display identity

Business results remain image-free. `LatestDisplayFrameStore` is a Qt-independent,
thread-safe display channel with one latest frame per configured camera (six in
the shell), no masks, SDK objects or history. Source pixels enter it separately
from terminal inspection results; rendering never gates result delivery/accounting.

`publish(frame, generation=..., source_key=...)` copies BGR pixels into immutable
bytes-backed uint8 storage. Consumers cannot re-enable ndarray writes. Producer
aliases stay stable during this copy **and** throughout any independent inference
borrow; a frame-admission acknowledgement is not inspection completion or buffer
return. Display copying does not shorten that borrow. Input/Frame contracts remain
in [inspection ownership](inspection.md#identity-and-frames).

Unknown display cameras and invalid generations are rejected. For each camera,
older/equal `(generation, cycle_id, sequence)` publications are ignored. Store
snapshots enumerate configured camera order and carry a revision; callers retaining
snapshots extend old copies' lifetime and must not accumulate an unbounded history.
`source_key` distinguishes reference-source provenance; it does not replace
production identity.

Each viewport converts only a changed frame revision to `QImage` using
`Format_BGR888` and `.copy()`. The Qt image owns independent painting pixels;
there is no BGR/RGB swap or access to recycled producer storage. Retained store
frames and QImages have separate lifetimes. Unchanged revisions avoid conversion.

Indicators and overlays require all of:

- Display generation equals the application generation.
- Display CycleId equals the latest production result's CycleId.
- Complete FrameId matches the camera result, including CameraId and sequence.
- Camera result image shape equals the display ndarray shape.

A newer frame suppresses old indicators/boxes until its matching result arrives;
same camera identity alone is insufficient. A matching retained result uses its
**own** camera/slot layout even after a different candidate is prepared.

Generation-zero launcher/Folder references remain available for Vision but are
hidden on Production. A viewport without a displayed QImage shows a centered,
restrained `暂无检测图像` / `No inspection image` placeholder and pending indicators.
This depends on actual display availability (including generation suppression),
never brightness, Occupancy or missing inference evidence. A valid dark image
removes the placeholder. Stop retains latest production pixels and any
still-matching evidence; READY signals the stopped state without a global frozen
label. During a later STARTING/RUNNING/STOPPING generation, previous-generation
pixels are hidden locally and old results do not match. A current-generation
RUNNING image without its result may show the localized pending message.
This presentation does not reset the store, results or counters.
Local replay PAUSED retains the last pixels, matching results and overlays; only
the summary state label and existing operation buttons describe the paused source.

`ImageTransform` preserves image orientation, aspect ratio and centered letterboxing
and maps source-image boxes to logical display coordinates. QPainter draws clipped
hollow red rectangles independently of the bitmap, using existing assigned/unassigned
findings and NG reason boxes, with duplicate boxes removed. Production adds no
colored mask, contour geometry or invented region to domain results. ROI editing
and frozen-reference transforms remain in the machine/source document.

## Read-only snapshot publication

Normal `app.py --web-snapshots [--web-host 127.0.0.1] [--web-port 8765]` optionally
starts a process-owned, Qt-independent `ProductionSnapshotService`. It is disabled
by default, retains `127.0.0.1` as the default host, adds no persistence/Qt controls
and never commands the application or
participates in inspection completion, counters or PLC reporting. `ui` defers its
public Qt bridge import so the existing pure display/identity modules and Web
publisher do not load Qt. The bridge export/API itself is unchanged.

**Current Web-2 extends Web-1's loopback-only contract:** explicit host selection
accepts a specific assigned local unicast IPv4. A read-only Windows
[GetIpAddrTable](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getipaddrtable)
query supplies addresses and actual subnet masks; no DNS/interface configuration is
performed. Wildcard, broadcast/subnet addresses, multicast, reserved addresses,
IPv6 and hostnames are rejected. Non-default loopback aliases are not accepted.
Addresses that are unassigned or become unavailable before bind fail observably.
Host validation/bind errors are optional-service startup errors on stderr; normal
Production still opens. The default loopback path requires no interface query.

The publisher samples the existing locked application/display snapshots separately;
this is not one atomic six-camera acquisition. Camera rows follow saved configured
order (none when normal startup has no valid machine layout). Each available source
identity includes generation, CycleId, CameraId, sequence, source dimensions,
monotonic capture timestamp and display-frame revision. JSON adds application state,
latest formal CycleId, process-specific publisher ID, publication revision and UTC
observation/publication timestamps. A new publisher ID prevents old-process URLs
from aliasing a restarted process's revision numbers; it is not an authentication key.

The same `matching_camera()` rules authorize channel business/technical statuses:
generation, CycleId, complete FrameId and original dimensions must all match.
Unmatched current pixels are pending during RUNNING; missing evidence while stopped
is unavailable. Retained older-generation pixels explicitly carry their old identity
and stale/unavailable judgments. Generation-zero references and older-generation
pixels during STARTING/RUNNING/STOPPING are not served as Production JPEGs.
Stop/PAUSE can retain already observed pixels; HTTP availability does not imply
RUNNING. No brightness/Occupancy heuristics or new judgment/acceptance aggregation
is introduced. A failed encode removes that camera's published JPEG/status rather
than substituting old bytes under a new identity.

One non-daemon publisher thread samples nominally every two seconds, using OpenCV
BGR directly, preserving orientation/aspect ratio and limiting the largest dimension
to 640px at JPEG quality 70. Unchanged frame revisions reuse encoded bytes. It holds
one current observation while building one replacement, never a queued history or
catch-up workload; the next sample reads the latest store. Encoding/copying runs
outside inference callbacks, the inference worker and GUI dispatch. The cached
publication owns only compact JSON and at most one latest JPEG per configured
camera: no masks, contour arrays, native inference references or full result history.
An in-flight HTTP read may retain its old immutable publication until it finishes.

| GET endpoint | Meaning |
| --- | --- |
| `/` | Responsive read-only observer; no controls or additional Production callbacks |
| `/observer.js`, `/observer.css` | Packaged same-origin observer resources; no CDN/build system |
| `/api/snapshot` | Latest compact JSON; 503 before the first successful publication |
| `/api/cameras/<CameraId>.jpg?publisher=<publisher_id>&revision=<publication_revision>` | Cached JPEG for that exact publisher/publication; 410 if superseded, 404 if unavailable/unknown, 400 without valid identity parameters |
| `/health` | Publication availability, publisher liveness and latest publication failure; not Production readiness or connectivity proof |

Use the JPEG URL returned by JSON, including escaped CameraId, publisher ID and revision. A newer
publication never substitutes new pixels under an old URL. Responses disable cache;
there is no CORS enablement, write/configuration/runtime endpoint or
extra FINS request. A single HTTP thread processes one connection at a time with a
four-connection listen backlog and a one-second socket I/O timeout; slow readers do
not block publisher/Production. No per-request thread or unbounded task queue exists.

The service spans application Stop/Start generations. Bind/start/publication failure
is isolated from Production; startup reports optional service errors to stderr and
continues normal composition. Per-camera encode errors appear in compact metadata;
whole-observation errors retain the last timestamped publication and appear in
health/stderr. It makes no zero-performance-impact claim: encoding and HTTP still
use shared CPU/memory, and no real-GPU performance qualification was performed.

After the Qt event loop returns, the normal entry requests service stop: reject new
requests, interrupt any active socket, stop future publication, and join outside GUI
dispatch. An active encoder is allowed to finish; `join(timeout)` returns false
while any owned thread remains alive and retains ownership. Successful join means
actual publisher/HTTP termination and socket release, not merely notification.
Application/native cleanup keeps its existing ownership guarantees; no native call
is forcibly cancelled. The window's close notification alone is not process/Web exit.

#### Browser observation and resource ownership

The self-contained observer uses three/two/one columns as the viewport narrows,
preserves image orientation/aspect ratio with contained images, and follows
configured camera order. Its normal header contains only the compact Observer
identity and an observation dot. Camera cards contain the CameraId, JPEG and six
compact SVG indicators in the returned configured channel order, without numerical
sorting. Generation, CycleId, FrameId, dimensions, timestamps and diagnostic status
strings remain in JSON rather than permanent page text. No details panel, desktop
summary, camera/cycle acceptance judgment or overlay is added.

Channel symbols are a green check for OK, red cross for NG, neutral outlined square
for EMPTY and amber warning triangle for UNKNOWN. A separate amber border and `!`
mark technical ERROR without replacing the business symbol, including ERROR + NG
and ERROR + UNKNOWN. Pending uses a neutral dashed circle; unavailable uses a dash.
Known ChannelIds and full business/technical descriptions remain in tooltips and
accessible names. If matching channel metadata is absent, the six neutral slots
are described by CameraId/slot position; no ChannelId mapping is invented.

Missing images use neutral placeholders. Retained images have a subtle pause mark;
stale observations have a clock mark and neutral channel indicators. Chinese/English
concise feedback appears for disconnection, stale/unavailable observations and empty
configuration. Application FAULT is shown separately from connection failure,
using the authoritative last observed state; after loss/staleness it is explicitly
marked as last observed. Neither HTTP connectivity nor an individual channel ERROR
is interpreted as Production health or Application FAULT.

One poll starts approximately every two seconds, after the preceding work settles;
there are no overlapping polls or catch-up requests. JPEGs are fetched sequentially,
so each observer has at most one active fetch. Only source identity changes trigger
image replacement: publisher ID, generation, CycleId, complete FrameId, display-frame
revision and original shape. A new publication revision alone does not redownload
an unchanged displayed image; matching status can still update from fresh metadata.
Every changed/hidden frame loses its previous pixels/status before any download.
Only successfully decoded matching bytes receive that observation's business/technical
indicators; otherwise the six slots remain neutral.

Exact returned JPEG URLs are used once per poll. A 410 is an ordinary publication
race: the camera waits for fresh metadata, without an immediate retry loop. Other
missing JPEGs remain unavailable; connection failures replace current channel evidence
with neutral indicators and mark retained pixels as last observations. A five-second
poll deadline aborts network/decoding work; invalidated late responses cannot replace
newer observations.
Temporary failures recover on later polls. Older revisions of the same publisher
cannot overwrite newer metadata; a new publisher ID permits process restart safely.
At most six cards/object URLs and one in-progress decode exist; replaced/disposed
URLs are revoked. Page disposal aborts work and clears timers/URLs; restoration can
resume observation.

Connection status and publication freshness are separate from the last observed
Application state. A publication older than six seconds is labeled stale and its
current channel evidence replaced with neutral indicators; this is a browser
observation indicator, not a Production deadline or acceptance limit. The age indicator
uses the client's wall clock; significant clock skew can produce a stale warning.
Fresh HTTP/publication timestamps do not prove that new camera frames are arriving.
Local replay pause remains RUNNING; the observer invents no PAUSED application state.
The six cards need not be one cycle.

The serial HTTP model is unchanged. CPU tests exercise three concurrent clients
with 420 successful JSON/JPEG requests while encoding only the initial six JPEGs;
this is small local compatibility evidence, not physical-LAN capacity qualification.
Slow links can occupy the single server slot until its one-second I/O timeout and
delay other observers. No threaded/async server, second publisher or request-driven
encoding has been introduced.

#### Trusted-LAN deployment boundary

Explicit LAN binding does **not** authenticate clients or restrict their source subnet.
There is no login, HTTPS or internet/shared-network qualification. Use only an
isolated trusted internal test network with external access restricted by network
configuration and Windows Firewall. The application never changes interfaces,
firewall rules or access policy. Physical computer/phone connectivity and Production
responsiveness require manual verification; localhost and same-machine LAN-IP
tests cannot establish them.

Manual verification with existing valid persistent `data/` and local Folder sources:

1. Launch `uv run --no-sync python app.py --web-snapshots --web-port 8765` from the
   repository root. The application opens stopped; inspect `/health` and
   `/api/snapshot` using `Invoke-RestMethod http://127.0.0.1:8765/api/snapshot`.
2. Use Start or Step. After publication, fetch a camera's returned URL with
   `Invoke-WebRequest ("http://127.0.0.1:8765" + $snapshot.cameras[0].image_url)`,
   where `$snapshot` is a fresh `Invoke-RestMethod` result. Verify original camera
   identity/order, reduced dimensions/colors and matching channel statuses.
3. Pause/Stop and inspect retained identity/state; Start again and check the new
   generation. Reusing a superseded URL should return 410, not a newer image.
4. Close normally. Confirm the command exits and the local port stops accepting
   connections. Launch without flags to confirm the service stays disabled.

LAN verification, without changing persistent configuration:

1. Identify the machine's assigned preferred IPv4 with
   `Get-NetIPAddress -AddressFamily IPv4`. Confirm the isolated network and externally
   administered access restrictions; do not use a wildcard or guess an address.
2. Launch `uv run --no-sync python app.py --web-snapshots --web-host <LAN-IP> --web-port 8765`.
   Open `http://<LAN-IP>:8765/` on one computer and one phone on that network.
3. Use Start/Step in Qt; confirm six camera identities/order, aspect/color and six
   matching channel indicators, including separate technical ERROR marks. Inspect
   detailed identity in JSON when needed. Observe up to three browser clients and
   check that Production remains responsive. This does not measure zero impact.
4. Temporarily disconnect/reconnect a client. Its connection warning must remain
   separate from Application state; images/status should recover without reload.
   Pause/Stop/Start retain the existing application/display identity rules.
5. Close normally and confirm actual command exit. `Test-NetConnection <LAN-IP> -Port 8765`
   should no longer connect. Repeat without Web flags to confirm default-disabled startup.

CPU/Fake regressions cover matching/mismatching identity, image encoding, bounded
replacement, IPv4 validation, static/read-only routes, concurrent reads, default/
opt-in/error normal startup and native process exit. The normal pytest suite runs
deterministic tests of the actual browser JavaScript through an existing Node.js;
if Node is absent those checks skip explicitly, with no automatic installation.
Run them directly with `node --test tests/web_observer_checks.cjs`. They cover
source-image reuse, identity/revision changes, empty/pending/stale data, 410,
connection recovery, deadline cancellation and delayed decode disposal, plus the
minimal presentation, configured indicator order, combined ERROR/business symbols,
localized accessibility and separate Application FAULT feedback. Browser
viewport inspection complements these tests but is not physical phone/LAN evidence.
Real ViMo/PLC/camera performance, authenticated deployment and hardware commissioning
remain unqualified.

## Feedback

### Shared strip and priority

One fixed **33-logical-pixel** strip is reserved across all five pages and Tune
preview. Text is plain, single-row and right-elided; its full short summary is
available in tooltip/accessibility text. Severity icon/color and padding are shared.
Navigation or message changes never resize the workspace or clear backend evidence.
Normal states and product-only NG leave the strip empty; NG stays in Production
camera/channel evidence. A latest technical ERROR still appears, including ERROR + NG.
For saved Basler sources the strip also shows neutral preparation, explicit ARMED
notices during STARTING/RUNNING (the explicit developer override also has single-cycle processing). They are queued from
source observations, generation-checked, and subordinate to errors/FAULT/closing;
RUNNING by itself does not prove cameras are armed. One bridge refresh batches
snapshot/pixel notifications before presenting camera cards once; accounting and
terminal delivery remain independent of rendering. Camera/button styles are applied
only when changed, avoiding repeated Qt repolishing on unchanged live status. No new lifecycle state is added.
Historical technical counters alone do not create a current warning.

The current message precedence is:

1. Approved window closing / continued close waiting.
2. Application FAULT, using its actual issue when available.
3. Relevant cached command failure or source failure.
4. Application issue (startup/shutdown/runtime stage).
5. Latest production result's technical ERROR.
6. Unresolved startup/configuration guidance, or the Production-only UNCONFIGURED hint.
7. Empty strip for ordinary state/product judgment.

This describes message selection; FAULT coloring remains distinct during close.
FAULT is violet, technical failures amber, ordinary closing progress neutral.
Latest technical summaries use available CameraId, source/reason code and exception
type; application issues use actual stage/type. Missing details are not guessed.
Runtime/source summaries do not expose raw SDK text, paths or tracebacks; their
tooltips retain the short summary, not an invented diagnostics log.

Startup/configuration and preference diagnostics are different: localized operator
summaries link to full configuration detail in existing tooltip/workspace feedback
and stderr. Source handlers print original source exceptions; backend Futures
retain their original exception chains. There is no universal logging/history or
new diagnostic viewer. Complete configuration detail is not inserted into the
single-row operator summary.

### Scope and recovery

Lifecycle, source, preference and relevant operational failures stay visible across
navigation. Cached `recipe` command feedback is scoped to Recipe/Vision; Batch
feedback is scoped to Production. Recipe/Vision validation and save feedback stay
in their workspaces, with their existing full diagnostic tooltips. Startup source/
manifest guidance can also appear globally; it is not an ordinary page-local
Recipe validation error.

The implemented recovery rules preserve independent outcomes:

| Feedback | What clears/supersedes it |
| --- | --- |
| Startup Folder availability warning | Successful Start, or all-source/bounds verification followed by successful source Save and Apply |
| Manifest/language recovery guidance or preference-write failure | Its permitted successful preference write; protected/unreliable files need external repair/restart |
| Cached runtime/source and ordinary command summaries | Successful Start or authoritative completed FAULT Reset supersedes obsolete summaries; existing command-success rules update the bounded command summary |
| Latest per-cycle technical ERROR | A later healthy result supersedes that latest evidence; historical error counters remain |
| Actual application issue/unsafe ownership | Only application lifecycle/recovery evidence, never navigation or an arbitrary success signal |

Arbitrary Recipe Apply, one reference read and a Folder path edit do not clear a
startup source warning. Verified source Save/Apply clears that warning; it is not
a blanket reset of runtime-source errors or manifest guidance. Unrelated Recipe,
Start or Stop success cannot erase unresolved preference feedback. A successful
preference write clears its own error without clearing an existing Recipe command
error. Generic command success can supersede ordinary cached command summaries
under the current rules; this is not a persistent event history.

Recipe Apply can install a candidate while its independent preference update fails.
Show successful Apply locally **and** unresolved localized `app.json` repair/restart
feedback with full details; do not relabel either outcome or roll back the candidate.
Persistence/protection and readiness remain owned by
[application recovery](application.md#preference-persistence-and-feedback).

While tuning a frozen image, an earlier product NG is not a description of the
current preview. The reserved strip stays quiet for ordinary NG while actual
technical errors, application issues, relevant command failures, FAULT and shutdown
remain visible. Preview measurements/local validation belong to the workspace.
This changes no result, counter, candidate or preview isolation semantics.

## Qt delivery

### Completed Futures and QObject lifetime

Widgets and public bridge operations belong to the GUI thread. `FutureDelivery`
callbacks can run on a backend thread, a cancellation caller or immediately when
registered on an already-complete Future. They read only `result(timeout=0)` from
that completed Future, preserve the original value/exception, and emit a private
signal. An explicit `QueuedConnection` delivers it to a GUI-thread QObject slot.
Slots narrow `Signal(object)` payloads at runtime. Callbacks do not inspect masks,
serialize, render, wait for unfinished work or touch widgets.

Callbacks retain a small Python gate, not a window/bridge. Its separate unparented
emitter is protected by a lock: detach revokes emission before `deleteLater()` on
the sender. Bridge destruction also detaches through a direct destruction
connection; destroyed receivers are disconnected by Qt. Live detached bridges
discard queued notifications. This prevents late callbacks emitting on a deleted
sender or updating a destroyed receiver.

Bridge `detach()` is idempotent GUI-thread delivery revocation, **not** worker,
session or application shutdown. An owner deleting a bridge early must independently
retain, stop and join backend ownership. Normal close retains the window/bridge
until its specific actual-termination condition. Future completion precedes GUI
notification; lost/suppressed painting cannot invalidate a terminal backend result.

### Production bridge

`QtProductionBridge` observes `ProductionApplication` and the display store. Its
default **100 ms** GUI timer emits changed snapshots and changed display revisions;
it does not emit a history of every cycle/state transition. Start/Stop/Close Futures
use the queued gate. Prepared selection/Reset/Batch wrap cheap application commands
in the same command-notification path.

`snapshot_changed` and `frames_changed` carry presentation snapshots;
`command_finished(name)` / `command_failed(name, original_error)` describe command
observations. Cycle acceptance and terminal results are separate application
receipts, not these command signals. The application/source consumes them
independently of polling or painting. `shutdown_finished` is emitted only after
successful application-close notification **and** nonblocking `application.join(0)`.
No GUI call waits on an unfinished Future or performs native inference.

### One-image diagnostic bridge

The separate `QtInferenceBridge` observes one supplied `InferenceWorker`, not a
ProductionApplication or cycle coordinator. `start()`, `submit(image_bgr)` and
`request_stop()` run on the GUI thread. Submission borrows the same input under
the [worker ownership contract](inference-runtime.md#worker-tasks-and-borrowing),
without a bridge copy. The owning worker factory still creates/uses/closes the engine;
Qt does not replace it with a QThread-owned engine.

Startup success/failure, typed result/task failure, submission rejection and
worker-state notifications remain distinct. Errors retain identity/causes, including
cancellation; an initialization error may appear both as startup and terminal
worker failure. `shutdown_failed` reports timeout or terminal failure;
`shutdown_finished` reports observed thread exit, **including failed cleanup**.
The `stopped` property means actual exit, not merely a terminal WorkerState.

Readiness/task notifications are event-driven. A **250 ms** timer observes lifecycle
state and only `worker.join(timeout=0)`; it never polls task results. State snapshots
may omit transient states. The standard-widget diagnostic retains its decoded
array until the task completes, disables repeat Run, and ignores successful late
results while closing. Its single-file decode currently runs on the GUI thread;
it is not a large-file asynchronous loader or Production acquisition path.

## Closing

### Production shell

The existing workspace guards handle dirty Recipe/machine/source decisions and
asynchronously wait for active preview/preparation/reference/device-discovery jobs before approving
application close. Cancelled draft decisions leave the window open. Late preparation
cannot activate a candidate while closing; job delivery is detached after those
jobs finish. Detailed editing choices remain in the UI configuration documents.

Once approved, the initial close event is ignored, controls are disabled and
application close is requested once. Qt remains responsive while the application
releases its session/source/runtime. Successful close Future delivery is followed
by `application.join(0)` observation; only actual lifecycle-thread exit emits final
shutdown. The independent communication owner also stops admission and drains any
active request on its background thread. Window close is accepted only after both
application termination and communication-thread exit; communication delivery is
disposed at that point. Application cleanup failure keeps the shell open and resumes
the live communication workspace. Normal Qt Quit requests use this deferred path.
Repeated requests do not reset a pending close deadline.

An idle communication drain can notify synchronously during the initial native
close event. The shell requests its final close only when **both** owners have
terminated, avoiding a reentrant close that hides the window before cleanup.

The default **30-second** bridge deadline reports continued waiting once and keeps
observing. It does not cancel native work, drop ownership or declare termination.
Cleanup failure clears the bridge's pending-close flag, keeps the window open with
the actual application state/issue, and allows an explicit later Close retry.
Reset remains unavailable while runtime ownership is unsafe. Lower-layer limits
are in [runtime shutdown](inference-runtime.md#shutdown) and
[application shutdown](application.md#shutdown).

Final accepted close detaches delivery and removes the Quit filter. Launchers'
blocking fallback close/join occurs only **after** the Qt event loop exits, never
inside GUI dispatch. Successful shell shutdown also requires the Qt event loop
to exit and the launching command to return; a hidden window alone is insufficient.
The Windows virtual-environment Python redirector and uv may legitimately be
parent processes during execution; all launched processes must terminate.
Direct process termination/forced exit or OS shutdown is not
a supported safe-recovery mechanism.

### One-image diagnostic

`DemoWindow` retains its bridge/worker until actual worker exit. Close requests
worker Stop, disables submission, ignores the first close event and leaves Qt alive.
An active constructor/inference call finishes naturally; pending tasks cancel under
the worker contract and cleanup runs on its worker thread. Normal Qt Quit is routed
through the same close policy. Actual exit triggers final detach/window close.

Its 30-second timeout reports once, retains ownership and continues observing;
it does not retry cleanup, kill the process or fabricate success. A permanently
blocked native call therefore keeps the window/process alive. Unlike the Production
bridge, this diagnostic can close after observed worker exit **with** a reported
cleanup error: its exit notification certifies termination, not safe Production
session recovery. Do not infer a new retry/restart policy from that diagnostic.

## Review

### Supported manual workflows

Run from the repository root with the existing verified environment. Equipment-free
review uses the real application/runtime with an explicitly fake engine:

```powershell
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920
uv run --no-sync python scripts/run_production_ui.py --language zh --size 1080x1920 --scenario ng
uv run --no-sync python scripts/run_production_ui.py --language en --size 1920x1080 --scenario warning
uv run --no-sync python scripts/run_production_ui.py --language zh --size 900x1200 --scenario fault
```

Scenarios cover normal, product NG, ordinary image-processing failure and terminal
runtime fault. Synthetic default pixels require no GPU/private image/ViMo setup.
Fake delays make STARTING/STOPPING observable. `--frames-dir images` still uses
fake inference; the generated fake artifact under ignored `outputs/production_ui_demo/`
is not a ViMo model. Launching this review may write demo configuration; it does not
modify source images or create a cycle archive.

Review both languages at 1080 x 1920 and 1920 x 1080, plus a narrower window:
check navigation/accessibility, camera/slot order, long Recipe/Batch identities,
selected/disabled states, aspect-ratio letterboxing and matched/stale boxes. Exercise
Start/Stop, retained Stop frames, new-generation suppression, Batch Cancel/confirm,
quiet product NG, cross-page technical FAULT/ERROR and responsive deferred close.
Configuration repair checks should use isolated test/manual inputs, not overwrite
persistent machine calibration to obtain a screenshot.

Normal real `app.py` startup and explicit-path real Production launch are separate
supported workflows; see [application entry points](application.md#developer-entry-points)
for prerequisites, saved Folder versus fixed-image overrides and exact commands.
Do not substitute fake mode for normal recovery or treat fixed overrides as persistent
source configuration. The independently supported real one-image diagnostic is:

```powershell
uv run --no-sync python -m vimo_demo.ui.app models/model_trt_fp16.vimosln
```

Click **Start Worker**, wait for READY while moving/resizing, select a local image
(or supply `--image <path>`), then **Run Inference**. Inspect typed labels/counts,
`has_defects` and mask shape, run again and close; confirm return to the terminal.
This does not produce Production cycles/counters or acceptance decisions. Fast real
inference may be too brief to observe responsiveness; blocked fake engines provide
deterministic lifecycle coverage.

### Verification and limits

Existing GPU-independent checks cover bilingual layouts, state/actions, Batch
confirmation, configured slots, BGR/copy lifetime, generation/full identity,
stale overlays, queued delivery/exception identity, destroyed bridges, global/local
feedback, zero warm-up production effects, close timeout/unsafe cleanup and actual
termination. Focused commands, not executed by this documentation batch:

```powershell
uv run --no-sync pytest tests/unit/test_production_presentation.py tests/integration/test_production_ui.py tests/integration/test_global_shell.py tests/unit/test_qt_integration.py --tb=short
uv run --no-sync pytest tests/integration/test_feedback_corrections.py tests/integration/test_application_bootstrap_ui.py tests/integration/test_traditional_preview.py --tb=short
```

Representative screenshot cases are maintained in the Production/global-shell/
feedback tests. Their existing offscreen workflow uses fake inference and local
installed fonts where available; screenshots are generated evidence, not a
prerequisite for result delivery. Dated screenshots and original validation results
remain historical evidence, not claims that this reconstruction reran them.
Normal gates remain in [AGENTS](../AGENTS.md).

The optional one-image Qt/worker real-GPU checks are separate:

```powershell
uv run --no-sync pytest tests/integration/test_qt_vimo.py tests/integration/test_worker_vimo.py::test_worker_one_image --run-gpu --require-gpu -s --tb=short
```

Their wiring/thread checks do not establish model accuracy, performance or long-run
stability. Real Production local replay verification belongs to
[application verification](application.md#verification).

Offscreen DPR=1 review does **not** establish native Windows 100/125/150% DPI,
physical portrait monitor/touch behavior, viewing distance or brightness/contrast.
Native Browse/dialogs, larger fonts and actual-screen ergonomics need separate
manual acceptance; no such testing is performed in this documentation batch.

**Target / deferred:** commissioned camera/PLC integration, physical production
counting/safety, History/archive execution, commissioned Communication hardware wiring, a diagnostics
panel/message-history service and hardware throughput/deadline guarantees remain
unimplemented. Current Qt topology and layout do not settle those future designs.
