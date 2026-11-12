# PLC diagnostics and result reporting

The application supports synchronous FINS/UDP and FINS/TCP DM/WR word access,
manual operations/register watches and optional first-station Production reports.
Modbus TCP Holding Registers support both Diagnostics and first-station reporting.
Transport and reporting are independent of Qt/inference. Production is passive to
external camera triggers and never sends W10.7 photo commands.

## Clients and endpoints

Diagnostics and reporting share immutable `FinsUdpEndpoint`, `FinsTcpEndpoint`
and `ModbusSettings` values. UDP alone carries routing; Modbus alone carries a
Unit ID. A diagnostic request combines one endpoint with its operation/range/data.
Reporting settings keep the base address and bounded queue capacity separate from
that endpoint; each Start freezes the selected values for its generation.

`FinsUdpClient` and `FinsTcpClient` are serial context-managed clients. Use one
owner per client; overlapping operations fail rather than interleave. DM/WR words
are unsigned 16-bit values encoded big-endian. Reads/writes validate addresses,
counts, payloads, SID/routes, command/end codes and exact response lengths.
Timeout, malformed response and remote rejection remain explicit errors. A timeout
or missing ACK does not prove a write was not executed.

TCP validates FINS/TCP envelopes and command/error fields, negotiates nodes and
uses assigned nodes for the session. One deadline covers connection/negotiation
and exchange; poisoned/failed sockets are retired. No blind write retry or
implicit UDP fallback occurs. UDP uses explicit FINS routing. Clients do not
change PLC mode, implement safety interlocks or infer photo/reject semantics.

FINS endpoints independently select UDP/TCP and a
numeric target. Without a communication preferences file, both reporting and
Diagnostics initially select FINS/TCP at 127.0.0.1:9600 with a one-second timeout;
reporting remains disabled. Existing documents with omitted protocol or endpoint
fields retain FINS/UDP defaults. Initial values neither open a PLC connection nor
create a settings file. Normal Production
reporting uses an explicitly saved selection, never a camera-source-derived PLC
endpoint. Engineer authorization applies to manual writes on both local and
remote endpoints. Confirm the commissioned endpoint and intended operation.

## Modbus TCP

`ModbusTcpClient` uses standard-library sockets with one serial context owner.
FC03 reads 1–125 Holding Registers; single-value writes use FC06 and multiple-value
writes use FC16 (2–123 registers). Addresses are zero-based 0–65535, with the entire
range checked before I/O. There is no 40001-style conversion. Values are unsigned
16-bit big-endian words. Default port is 502 and Unit ID is 255; Unit ID 0–255 is
explicitly configurable for gateways and is checked in every response.

Each bounded exchange validates the MBAP transaction (including rollover), zero
protocol identifier, length, Unit ID, function and exact payload/echo. Fragmented
TCP data is assembled within one total deadline, not a new deadline per fragment.
Malformed frames, Modbus exception responses, disconnects and timeouts are explicit
failures; failed exchanges close the socket. There is no automatic retry,
reconnection, readback or transport fallback. A write response confirms the protocol
operation, never PLC program consumption or physical rejection. A timeout/lost or
invalid response can leave execution uncertain; do not repeat writes implicitly.

Diagnostics' saved `modbus_tcp` endpoint uses only host, port, timeout and Unit ID.
Its advanced control exposes Unit ID and hides FINS routing. Holding Register
selection is independent of DM/WR and the reporting range. Existing session-only
FINS watches retain their identities after switching protocols; incompatible
watches block the entire pass before I/O. Remove them and explicitly add Holding
Register groups (or restore the corresponding saved protocol). Unsaved endpoint
edits never redirect operations. All operations retain the existing single
background diagnostic worker and asynchronous Close/Stop ownership.

Wire format follows the [Modbus application protocol](https://www.modbus.org/file/secure/modbusprotocolspecification.pdf)
and [TCP messaging guide](https://www.modbus.org/file/secure/messagingimplementationguide.pdf).

## First-station registers

The sequence and submission rules here describe the per-cycle mode.
The [four-cycle mode](#four-cycle-reporting) retains the bit projection
and bitmap widths but uses a PLC batch number instead of an allocated sequence.

`first_station_reject_words()` requires exactly one explicit business status for
every ChannelId 1..30 or 1..36. Missing/duplicate/out-of-range/bool identities or invalid
statuses fail before I/O. Arrival order and presentation slots never determine bits.

For FINS, reporting `base_address` defaults to 49 and must be an integer
0–508 so the entire four-register window fits WR memory. It is an absolute logical
register address, not an offset added to W49. Channel bit positions are unchanged.
Modbus uses the same offsets and bit projection in Holding Registers, with an
explicit zero-based `base_address` in 0–65532. Enabled Modbus reporting without a
base is invalid; it never inherits W49. Both protocols validate the entire
four-register address window before I/O, including for 30-channel machines.

| Register (default) | Meaning |
| --- | --- |
| base (W49) | Unsigned attempted-result sequence, wrapping 65535 -> 0 |
| base+1 (W50), bits 0..15 | CH1..CH16 reject bits |
| base+2 (W51) | 30 channels: CH17..CH30 in bits 0..13, bits 14..15 zero; 36 channels: CH17..CH32 in bits 0..15 |
| base+3 (W52) | 36 channels only: CH33..CH36 in bits 0..3, bits 4..15 zero |

A set bit requests rejection for its channel. OK clears the bit.
NG and UNKNOWN set it. EMPTY clears the bit only on a normally completed cycle
with an observed frame and valid shape for every configured camera and no collection
failure. The channel must have successful Occupancy evidence of EMPTY in both
views. A forced rejection, a channel NG or an error in any enabled channel check
(including an optional check) prevents the bit from clearing for EMPTY.
ViMo findings assigned to a confirmed EMPTY channel and findings outside all
channel ROIs contribute no reject bits. The same finding can still reject
another channel it overlaps. A disabled check or a Size/Color check skipped
for confirmed EMPTY allows the bit to clear when the other requirements hold.
Status-only submissions have no verifying evidence and set the bit for EMPTY.
This projection leaves the inspection judgments, counters and History intact.

`FirstStationReporter` borrows a client and one serial owner submits:

1. Validate the entire projection and reserve the attempt sequence.
2. Write exactly two bitmap words starting at base+1 for 30 channels, or three
   for 36, in one request. A 30-channel submission never writes or clears base+3.
3. After its successful validated ACK, write the reserved sequence at base.
4. Return a commit only after that ACK. Submission performs no readback.

For Modbus, the worker's one initialization read is FC03 at base. The bitmap
write is FC16 at base+1; after its validated response and the existing cycle
attribution check, FC06 writes the reserved sequence at base. Both validated
responses are required for ACKNOWLEDGED. FINS retains its existing WR operations.

Every valid attempt advances local sequence, even after failure, so uncertainty
can leave gaps. Unchanged bitmaps still get a new sequence. Standalone callers
provide the first attempted sequence explicitly; the automatic worker first reads
the configured base and seeds current+1 modulo 65536. Reconnect continues local progression
for new cycles rather than replaying an old uncertain report.

The two writes are not atomic PLC memory/scan consumption. Bitmap ACK followed by
sequence failure leaves staged words; a lost sequence ACK leaves commit uncertain.
Never silently resend the old result or infer mechanical rejection from ACK.
PLC ladder consumption/handshake, restart association and physical reject timing
still require equipment commissioning.

## Automatic reporting lifecycle

The following lifecycle describes per-cycle reporting. Explicitly selected
[four-cycle reporting](#four-cycle-reporting) replaces only its communication worker
and phase-breaking controls for a supported Basler session.

Communication's Production enable/endpoint/routing fields are drafts. Only a
successful **Save settings** publishes preferences to the next Start and persists
them in `data/communication.json`. Persistence and publication share the existing
Application admission lock, so a competing Start cannot freeze a half-saved
selection. Missing settings leave reporting and Web disabled and are visibly
unsaved; startup never creates the file. Restoring enabled never creates
a reporting client or sends before Start; warm-up still sends nothing. Production
settings remain locked during STARTING/RUNNING/STOPPING and FAULT. Old ACKs,
counts and connectivity are never restored as live observations.

`FirstStationReportingWorker` receives compact generation/CycleId, channel statuses
and the bitmap derived while inspection evidence is available. The worker uses
that bitmap for writes and reporting observations. One non-daemon thread owns its connection,
with a bounded FIFO (default eight pending plus one active) and bounded observations.
Submission/settling is separate from terminal inspection and accounting. Socket
work never runs in Qt, inference or result-completion callbacks.

Ordinary network errors become failed observations without changing inspection
judgment/counters. Backlog/state rejection is explicit and requests Application
Stop. There is no retry loop. New-cycle reconnect may recover transport; failure
does not reinterpret old results or claim their delivery.
Initialization, bitmap and sequence failures retain their stage and original
protocol cause. Modbus errors remain Modbus errors, with uncertain-write details
preserved in observations. A failed connection is closed; only a later new cycle
may reconnect. Once seeded, the worker keeps local sequence progression across
that reconnect, without reading back/replaying the failed attempt.

Basler reports attributable terminal cycles, including partial timeout/shutdown
results with all five or six configured camera results and at least one observed frame. Missing/unfinished
camera channels are projected conservatively as UNKNOWN/reject. Completed camera
judgments survive, but EMPTY keeps its reject bit on a partial or failed cycle.
No observed cycle means no report. The source's ambiguity fence
is checked at admission and again after bitmap ACK, before the sequence write. A known ambiguous
cycle gets no commit; an already in-flight sequence write cannot be recalled.

Basler Stop first stops inspection/runtime admission, settles admitted cycles and lets
active native work finish, then drains already accepted reports in order. Folder
Stop keeps its existing active-attempt completion and explicit pending skips.
No new reports start from rejected work. Session release additionally requires
report-thread exit and safe socket cleanup; notification/timeout alone is not exit.
The separately connected Basler source keeps grabbing/discarding after Stop; only
Vision Disconnect or application Close releases valid connections; cancellation of
an unfinished Start connection follows the [Application cleanup contract](architecture.md#application-and-shutdown).
Internal Basler Pause/Step keeps the reporting owner alive and waits for accepted reports
to settle before PAUSED. Discarded paused captures send no report or sequence write.
These controls are not exposed by the Basler Production player and do not pause
the PLC or mechanical equipment.

Manual clients/watches remain independent. While an automatic owner is starting
or alive, manual writes overlapping its frozen range (base through base+2 for 30
channels, base through base+3 for 36) are rejected for the matching target.
FINS matches host/port and WR space across UDP/TCP, as before. Modbus matches
host, TCP port, Unit ID and Holding Register space. Different Unit IDs are separate
targets; identical FINS/Modbus numeric addresses are different spaces. Protection
uses frozen settings during STARTING and for as long as the reporting owner is
alive, including unfinished Stop/Close. Outside Close, reads and independent
endpoints remain available, including writes to the former W49–W52 range when it
does not overlap. Close rejects all new commands while cleanup finishes.
The guard prevents competing writers; it is not PLC-side locking or
access control.

<a id="four-cycle-reporting"></a>

## PLC group reporting

Communication → Reporting has a compact **Merge** dropdown (Chinese
“合并”) with numeric values 0–8. Only **0** (per-cycle reporting, no merge), **1** (single-cycle PLC group) and
**4** (four-cycle merge) are selectable; the other numbers are visibly disabled, with
unsupported status in their tooltip/accessibility rather than repeated suffixes.
Full localized meanings remain in tooltips and accessibility.
Zero does not disable reporting: the independent Enable/Disable action retains
that meaning. At W49/W149, per-cycle mode still commits its attempted-report
sequence and does not track W48/W148; single- and four-cycle modes retain the PLC counter
association below. The header places its icon, title and actual lifecycle state
on the left, the selector in flexible space, and its independent Enable/Disable
action on the right. Mode edits
remain drafts until Save; Discard restores the saved selection. A new normal
installation selects four_cycle, FINS/TCP and W49, with reporting disabled.
Engineer/Administrator explicitly saves the next-Start selection. Loading reporting
settings, opening Communication and Start warm-up issue no reporting requests.
An independently enabled [status service](#plc-status-service-and-alarm-monitoring) may already be writing heartbeat/FAULT and reading alarms. Disabled reporting
creates no automatic reporting owner: no counter reads, group attribution or result
writes occur. Inspection and per-Cycle occurrence counters continue normally;
CycleIds alone never establish physical groups. Independent
manual Diagnostics retain their existing authorization and ownership.

Single-cycle (`single_cycle`, Merge = 1) and four-cycle (`four_cycle`, Merge = 4)
reporting require Basler and FINS/TCP. They share the existing reporting owner;
only the required member count differs. Merge = 1 is not the per-cycle mode. The commit base is exactly
W49 or W149; all addresses derive from that base:

| Commit base | PLC counter | Commit | 30-channel bitmap | 36-channel bitmap |
| --- | --- | --- | --- | --- |
| W49 | W48 | W49 | W50–W51 | W50–W52 |
| W149 | W148 | W149 | W150–W151 | W150–W152 |

A 30-channel report never writes the unused third bitmap word. The two groups
are independent: selecting W149 does not read, write or protect W48–W52, and
selecting W49 does not read, write or protect W148–W152. Per-cycle mode retains
its FINS UDP/TCP and Modbus protocols and address rules. Selecting a mode never
automatically enables reporting or replaces an endpoint/address. Unsupported
PLC-group transport/base combinations are invalid even when disabled;
Folder/explicit-image Start with enabled PLC-group reporting is rejected before
session/engine creation. Obsolete w48_observe and w48_active values are rejected,
not migrated or interpreted. Invalid files remain untouched with recovery feedback.

Formal reporting still depends on the established external attribution conditions;
its product name does **not** establish independent hardware trigger identity:

- Production reaches RUNNING before the first trigger of a new physical group.
- PLC advances the unsigned 16-bit counter before that trigger and holds it across
  exactly one or four triggers, according to the selected mode, and until reporting
  settles, including its ACK or explicit failure.
- Every trigger reaches every configured camera; no complete electrical trigger
  is silently missed by all cameras.
- No unobserved counter reset/reuse occurs while the session runs.

One observed Basler Cycle, or four consecutive observed Basler Cycles, is associated only under that initial
phase and the counter/source checks below. ImageNumber continuity does not prove
physical trigger continuity; a counter read is not exposure-correlated evidence.
Counter skips and 65535 → 0 wrap between groups are accepted without inventing
missing groups. A repeated counter in the next group is rejected; hidden full
reuse/reset remains undetectable. The [retained offline evaluation](plc-batch-proposal.md)
explains these limits and is not the current register contract.

`FourCycleReportingWorker` is the generation's sole reporting socket owner. The
first observed cycle queues a read at base−1, without socket work on camera,
inference, Qt or result-callback threads. This read must finish before the second
observed cycle is admitted; otherwise phase is lost. For Merge = 1 that next
Cycle begins another group: admission before the previous group settles loses
phase, including while its initial read is pending. A later counter is never
adopted for the earlier Cycle. Exactly one or four distinct attributable
terminal results are required, according to the frozen Run mode. Their existing `FirstStationReport.reject_words`
are ORed, preserving verified EMPTY projection. Partial/error cycles contribute
their conservative words. Missing triggers or unavailable results are never invented.

After all required results and the pre-write counter/source attribution check succeed,
the worker publishes one immutable inspection completion to Application. Each set
bit in the authoritative OR bitmap counts as final NG, including conservative
rejection. Clear bits mean EMPTY only when every required observation is verified EMPTY;
otherwise OK/verified EMPTY mixtures mean OK. Each ChannelId counts once. Original
per-Cycle UNKNOWN, technical ERROR and reasons remain unchanged; final NG does not
claim a confirmed defect in every rejected position. Incomplete or initially unattributable
groups do not count. This checkpoint precedes result writes: later transport
failure, commit uncertainty or attribution loss does not retract an already
eligible inspection completion. These totals are neither delivered-group counts
nor proof of physical traceability. Application retains a generation-local CycleId
high-water mark and offers the eligible group to its History owner without waiting.
The selected Production Batch accumulates these totals across Runs and restarts.
Cycle History retains per-exposure evidence and joins it to group membership by
Run and Cycle identity. See [Production history](architecture.md#production-history).

Batch Duration uses the worker's monotonic clock and runs from first Cycle
admission until the last required distinct terminal result arrives (the sole
result in Merge = 1),
regardless of callback order. It is published only at eligibility
and excludes subsequent attribution checks, bitmap/commit writes and ACK time.
The bounded latest group progress contains actual admitted CycleIds and their
scalar reports in admission order, separately from transport observations. The
next group's first terminal result replaces progressing indicators; admission alone
does not. Attribution loss or incomplete settlement invalidates unconfirmed
progress to neutral, without retracting earlier eligible completions. Qt and Web
consume these facts; neither reconstructs groups from CycleIds or displayed results.

After the pre-write check, write exactly two bitmap words for 30 channels or three
for 36 at base+1. After bitmap ACK, confirm the retained counter and source
attribution again, immediately before writing the retained counter value at base.
No local sequence is allocated. Each eligible group has at most one reporting
attempt; failed or uncertain writes are never replayed. ACK does not prove PLC
scan consumption, physical association or mechanical ejection. The read/check/write
sequence is not atomic; an in-flight commit cannot be recalled after association loss.

The optional [runtime diagnostics](architecture.md#runtime-diagnostics) record
these existing observations. Detailed mode correlates each retained counter
read and the bitmap/commit timings with the generation and first admitted CycleId.

A changed/stale counter, late initial read, duplicate/gapped CycleId, missing
terminal evidence, mixed layout or source ambiguity latches phase loss for the
generation. A next group arriving before the previous group settles also suppresses
the late report; a commit already in flight remains explicitly uncertain.
Inspection may continue, but reporting stays suppressed until Stop and a new
known-phase Start. There is no polling, guessed replacement counter, fabricated
all-NG group, PLC stop or W10.7 command. Existing source fault/overload handling
remains separate.

Pause/Step/Resume are rejected for an enabled PLC-group session; Stop/Close remain
available regardless of role. Stop settles source/runtime work, drains complete
admitted groups and records incomplete groups locally. Actual reporting-thread
exit and safe socket cleanup are required; timeout never releases ownership.
A new Start never resumes a partial group. While STARTING or the reporting owner
is alive, matching-endpoint manual writes are protected from base−1 through
base+2 (30 channels) or base+3 (36 channels), including unfinished Stop. Close
rejects new commands and retains the reporting owner until cleanup. FINS target
matching still covers UDP/TCP WR space; reads and independent targets retain their
existing rules outside Close. Engineer saves, Operator runs Basler; Guest cannot
run enabled PLC-group reporting.

The 100 ms early-inspection deadline, existing capacity and serial inference
owner are unchanged. Reporting retains the existing bounded eight-plus-one cycle
handoff and one or four scalar results per group. Merge = 1 uses ordinary
single-result camera/image presentation with physical-group counters; Merge = 4
retains its four lamps and identity-matched fourth frame. Bounded diagnostics retain the latest
16 settled groups plus bounded pending groups: CycleIds, terminal outcomes, timed
counter reads with their actual address, OR words, eligibility, outcome/reason,
phase issue and commit timing/uncertainty. No frames, masks or new History records
are retained. Observations survive Stop, are replaced at the next Start and are
lost on process Close. Customer UI retains lifecycle and actionable fault feedback;
routine ACK/success/error/skip counters are not displayed on the Reporting card.
The global strip displays the last protocol-acknowledged commit with its actual
frozen-run register label (for example W49, W149 or HR 100), or an unavailable
marker without an ACK. A later saved/draft target does not relabel an earlier
commit. This is a retained observation, never a live register read. Register/value
widths reserve the supported addresses and 16-bit values through 65535, so numeric
changes do not move neighboring groups. The strip omits routine Web status,
retains source and alarm indications, and uses existing feedback for failures.
An ACK proves neither PLC scan consumption nor physical rejection.
There is no Pilot Evidence window or hidden Observe-only runtime mode.

Authorized equipment acceptance must independently verify counter phase/hold,
camera order, W49/W149 consumption, stale equality/reset behavior, register
ownership, trigger spacing and completion/write/ACK margins using external
engineering tools. Local loopback and offline timing evidence cannot establish
these guarantees on real equipment. A complete group may still be too late;
conservative timeout projection does not repair a lost timing window.

<a id="plc-alarm-monitoring"></a>

## PLC status service and alarm monitoring

A saved enabled auxiliary service owns one serial FINS/TCP connection, independent
of Production reporting. Each pass writes the fault word, then, only after its
validated ACK, writes the heartbeat word, then reads one alarm word on that same
connection. These are ordered single-word operations, not an atomic pair.
It waits one second after the pass;
there is no periodic queue or catch-up backlog. Socket creation, negotiation,
reconnection and retirement belong exclusively to this worker, outside Qt,
inference and Production callbacks.

Heartbeat, fault and alarm addresses are independent, distinct WR words, defaulting
to W46, W47 and W18. The heartbeat increments modulo 65536 for every scheduled
attempt (first value 1). The fault word is 1 only for a fresh Application FAULT
observation, otherwise 0. Application state is sampled immediately before the
fault write; no Application lock spans network I/O. A failed fault write skips
that pass's heartbeat. A failed/uncertain heartbeat is not replayed. The next
scheduled pass advances the counter even after negotiation or fault-write
failure. Close has no final clearing write. Status communication failure does not set
Application FAULT. Heartbeat communication is not inspection readiness; zero in
the fault word is not guaranteed machine readiness. Process/network failure can
prevent publication: the PLC watchdog must detect missing activity. An ACK is
not evidence of PLC ladder consumption.

The separately editable alarm address defaults to W18 (W0–W511). A validated
status-write PLC rejection still allows the alarm read on the usable connection.
A transport/protocol failure retires that client; there is no read or immediate
reconnect on a broken connection. A later scheduled pass reconnects. Read and
write success, errors and last successful observations remain separate. Write
success requires both fault and heartbeat ACKs; a failure identifies its write
target. The last successful write observation represents that completed sequence,
not an atomic update. Write and read success mean Online; one means Partial
failure; neither means Unavailable.
Waiting has no completed pass, and Off means the saved service is disabled.
Successful writing alone does not establish fresh alarm values. Read failure
retains the previous word, active set and time as stale, never as cleared alarms.

Normal launch restores saved enablement under Guest. Logout, navigation and
Production Start/Stop do not control this service. Each activation freezes the
existing external JSON definitions (bundled fallback only when absent), labels
and active-high/active-low interpretation for display and History. Invalid
definitions leave activation unavailable without PLC I/O until a new selection
activation; there is no asset repair or continuous reload. New events retain
the actual alarm address and no station identity; historical provenance is
unchanged. The first valid read after a connection gap reconciles changes as
observed active/recovered, rather than inventing transition times or clearing
alarms during the gap.

The single auxiliary connection never shares the Production reporting socket,
queue or owner. A shared PLC must support separately negotiated concurrent
connections and sufficient capacity for [group reporting](#plc-group-reporting).
Field acceptance must verify watchdog timing and both connections under load;
loopback checks do not establish those guarantees. Keep the service disabled
where that PLC capability is unavailable.

Engineer/Administrator Save applies the auxiliary selection after atomic file
publication. Unsaved edits never redirect the active target. Enabled auxiliary
ranges must not overlap enabled automatic FINS reporting on the same host/port,
including W48/W148 for attributed modes. Start also checks any retiring status
owner before admitting a conflicting reporting session. Active status writes
reserve the two exact addresses against manual FINS UDP/TCP WR writes to the same
target. Unrelated words between noncontiguous addresses are not reserved by the
status service; Production reporting retains its own independent range protection.
Changing/disabling settings retains the old reservation until its socket retires;
new selections are reserved before activation. Diagnostics authorization and
Audit intent/completion remain unchanged. Automatic heartbeat transactions do
not emit periodic Audit Trail records. Close waits for actual worker exit.

The compact **Status / 状态** card retains the shared service header, actual
read/write health and a draft Enable/Disable action. Protocol, IPv4 and Port
appear once. Heartbeat, Fault, Alarm and Running are four independently editable WR
addresses in one equal-width row below them, with responsive wrapping when
necessary. These controls remain visible when the signal
section is collapsed, and reflow using actual available width. Guest/Operator
can observe but cannot edit. One signal header shows definition count, fresh
active count, last successful read and disclosure. Expansion survives refreshes
and language changes. Signal / Bit / Value / State preserve definition order,
polarity, translated names and full tooltips. Stale values have one table-level
notice; there is no current active count from stale evidence. Applied target
text appears only when the draft differs. Errors remain actionable. Disabled
service previews use the existing bounded background file job, without PLC I/O.
The global alarm strip remains an alarm-read observation, independent of
heartbeat-write success.

The independently configured Running target defaults to **W16.1**, fixed bit 1
(0 stopped, 1 running). Only its word is editable. It is not an automatic output
or part of the monitor's polling sequence; configuration alone never opens a
connection or reads the bit. A Running-only Save retains the current connection,
health and alarm baseline. It stays configurable with Status off, and is excluded
from applied-monitor target feedback and manual-write reservations. Its word cannot
alias heartbeat/fault outputs or the configured FINS reporting window on the same
host/port, even with services disabled. Save also checks frozen active reporting
ranges. Sharing the read-only alarm word is permitted. See the
[strict configuration contract](configuration.md#communication-preferences).

## Communication workspace

### Process parameter preview

Recipe's hold-to-view action uses the saved Status FINS/TCP endpoint and each
parameter's explicit DM address; it does not enable Status or change its live
target. Address and missing-value behavior are specified under
[Process parameters](configuration.md#process-parameters). Each hold reloads the
strict saved preferences and confirms the editor's catalog before transport.
Engineer/Administrator admission and the initiating session revision are checked
before each grouped read. A busy diagnostic/file job or active register watch
rejects the preview instead of queueing it. Recipe edits and saves remain independent.

Mapped DINTs use consecutive DM words, low word first, decoded as signed 32-bit
values without clamping to Recipe override ranges. Sorted mappings are combined
into windows of at most 999 words, including gaps, without splitting a DINT;
all ranges are validated before I/O. Each request uses the existing temporary
FINS/TCP diagnostic client and its deadlines/cleanup. The live Status and reporting
owners remain independent. Separate windows are acquired sequentially, not as an
atomic PLC-wide snapshot. Any grouped-read failure invalidates the whole
observation. All extension parameters have explicit addresses. There are no writes,
automatic polls, retries or stale-value fallback. Release/cancellation prevents
subsequent requests and display, while already admitted I/O finishes and closes
its client before the worker terminates. Close retains this actual join ownership.

A returned value is register evidence, not mechanical state or Recipe application.
Field acceptance requires a known-value DINT word-order check and sufficient PLC
connection capacity for the temporary diagnostic client alongside active services.
Synthetic loopback tests do not establish those hardware properties.

### Process parameter application

Preparing a selected saved Recipe with explicit overrides automatically admits one
application for Operator or higher, independently of continuous Status enablement.
Guest restoration does not write. No overrides means no connection or Running read.
The Application lifecycle worker uses strict saved Status settings and a private
FINS/TCP connection, with the existing per-exchange deadlines and no retries.
It validates the entire address/value plan before connecting, reads the configured
Running word once (default W16), and proceeds only when fixed bit 1 is zero.
Other bits have no Running meaning. A failed/unavailable read blocks all writes.

Each explicit value is written sequentially as two consecutive DM words, low word
first, signed 32-bit two's complement. Each pair is then read back and compared
before the next write. Omitted values, including absent overrides alongside explicit
zero, leave PLC registers unchanged. There are no WR writes or new handshake signals.
Each write requires recorded audit intent before admission. Per-parameter records
retain exact attempted addresses/values and readback outcomes, linked to the overall
prepared-Recipe attempt. Failed or uncertain readback blocks Start; evidence preserves
verified pairs and pairs that may have been written. There is no automatic rollback
or whole-set atomicity claim. A permission/session change, supersession or Close
prevents subsequent admissions and obsolete candidate success. Audit confirmation
failure also prevents reporting the application as ready.

The Running read is a one-time software check, not a hardware interlock. PLC logic
may change after the read; matching register readback is not proof of ladder
consumption or physical equipment state. Field acceptance must confirm DINT word
order using known values, Running timing, and independent connection capacity.
Simulated tests do not establish these hardware properties. Start warm-up and
ordinary draft editing/View PLC never perform process writes. See
[Application ownership](architecture.md#application-and-shutdown) for lifecycle rules.

### Workspace controls

Engineer or Administrator can issue manual PLC reads and writes, read register
watches, save CommunicationPreferences and control the Web observer. Saving
requires this role even when automatic reporting is unchanged. A watch retains
its initiating session revision and checks it on each pass and before each
group reaches the transport. An admitted request may finish after logout.
Later requests stop on authorization failure. Any role can stop a diagnostic register watch,
observe results and close the application.

The workspace keeps settings, diagnostic observations and service status visible
to Guest and Operator. Saving settings, issuing PLC requests and operating the
Web service require Engineer access. An active watch retains its Stop action.
The existing feedback areas distinguish a changed session or insufficient role
from file, transport, protocol and service failures.

Web power checks admission separately for the saved enable flag and the
service operation. Start, Stop and Apply/Restart also check authorization at
the Web owner. Normal startup can restore saved enablement while anonymous,
and Close drains the existing service regardless of role.

The page groups **Reporting**, **Web** and **Diagnostics**
with a compact, expandable **Status** card. Service headers use consistent
project-native icons and place actual state indicators/text after their title
slots, with actions aligned right. The three title slots share a width measured
from the current localized captions and actual Qt fonts, not all languages.
The budget is recalculated on caption/font changes, never on ordinary service
observations. Stacked cards align their indicators; side-by-side cards keep the
same local header layout without a shared screen coordinate.
Configuration controls wrap as whole groups when necessary; the bounded signal table scrolls
within the existing page. Production protocol, endpoint, timeout, base and enabled
selection are inline; UDP FINS routing expands separately. FINS/TCP negotiates
nodes and hides manual routing. Modbus shows an explicitly required HR base and
advanced Unit ID, with no FINS routing. Protocol changes use port 502 for Modbus
and 9600 for FINS when the port is still automatic; explicit edited ports survive.
Save removes protocol-inapplicable fields. The header power edits only the draft,
with Save and next-Start activation; it never interrupts an active worker.
Diagnostics has its own independently saved endpoint and routing, grouped DM/WR or Holding Register
watches and serialized manual/auto refresh, plus explicit one-shot writes.
Requests always snapshot the saved/effective diagnostic endpoint, identified above
the operations; draft edits never silently redirect them. The watch table has five
columns: Group, Address, DEC, HEX and Last successful read. They reserve readable
heading/value widths and share the remaining space, giving longer headings room.
The last column shows local `HH:mm:ss.SSS` from the validated response's completion timestamp,
not delayed Qt delivery. Failed/malformed reads retain the last successful values
and timestamp, with an ERR marker on the affected group, a stale/error icon and
color. Full group names remain in tooltips; values and batch status do not expand
runtime diagnostics. Failure and uncertainty remain visible near the operation.
Unvisited groups remain explicitly stale. Manual and auto passes use the same
presentation; an empty watch list has no empty table viewport. Write ACK/error/uncertain
execution appears beside writing. Detailed errors remain in the existing bounded
observations, without runtime error tooltips. Typed READ and WRITE requests support
diagnostics; a single-word read uses READ with count 1. Register groups and current
read/write panel observations are session-local. Manual writes also pass through
the [Audit Trail](architecture.md#audit-trail). Before sending, the diagnostic
worker obtains a durable intent for the initiating actor, endpoint, register
range and requested words, rechecks authorization and checks the current
reporting/status reservation. A matching protocol acknowledgment completes the audited
write. A PLC rejection records failure. A lost or malformed acknowledgment
leaves execution uncertain and never triggers a retry. Reads, watches and
automatic reporting transactions remain outside this trail.
TCP node negotiation remains with the serial network owner. Late responses cannot
replace a newer request's presentation.

**Save settings** atomically writes the communication preferences; failed
validation/write preserves the entire draft and effective settings. Unsaved fields
never change reporting admission. An active session retains its frozen selection.
The settings save and the Web power preference write each require their own
durable audit intent. Power records the saved enable bit independently of the
subsequent service start or stop.
Unchanged reporting allows independent Web/diagnostic settings to be saved during
Production; changed reporting is rejected while its editing lock applies.

The fixed bottom bar offers Save, confirmed Discard and concise dirty/error status.
Discard strictly reloads/validates the actual file, not an earlier page snapshot;
missing/invalid/unusable settings leave the draft intact with an error tooltip.
It does not persist, restart/stop any owner or undo earlier network operations.
The default settings can be explicitly saved before any edits. Production/Web
share a row when equally sized cards can accommodate their natural editor widths,
and otherwise stack at full width. Diagnostics remains full width below them.
Expanded routing/diagnostics grow in the outer scroll area.
Service cards reflow when their actual container is shown/resized, including the
first navigation after maximized startup; no manual resize is needed. Available
scroll-viewport width, not content stretched by a previous two-column layout,
determines whether both cards fit. Vertical overflow remains scrollable.
Reporting, Web and Status headers use a small filled dot with concise
state text: neutral when off, green when confirmed running/online, amber while
waiting/stale/unavailable and red for a confirmed service failure. Reserved
caption widths are measured from the three services' concise states across
supported languages, keeping state widths and right-side actions stable during
status, theme and language changes. Each dot follows its shared title slot
with a consistent gap. Shared action budgets align stacked headers; side-by-side
cards remain locally right-aligned. Service
power buttons are neutral and equally sized; their existing activation semantics
remain distinct. Active PLC alarms do not turn an online monitor into a failed
service. Retained reporting commits appear in the global strip, not as card counters.
Web has one header power action: **Turn on** or **Turn off**, alongside actual
service state, independent of Production and reporting. Power first reads and
validates the actual settings file, atomically changes only `web.enabled`, then
starts/stops using saved network settings. It never saves or discards network,
reporting or diagnostic drafts. A missing first-use file can be established from
effective startup defaults; invalid files require explicit review/Save. Persistence
failure leaves the runtime unchanged. Lifecycle failure retains the successfully
saved preference and shows the actual failure; it never pretends the service ran.

**Apply changes** appears only when saved settings differ from the running Web
configuration. Save or Discard Web network edits before applying them; unrelated
drafts are not applied. Saving alone never starts/rebinds Web or changes an active
session. Applying reuses the existing Stop/join/Start owner; there is no second
listener. Power-off persists disabled, so the next process launch stays off.

The effective running URL is a browser action between the Web title/state group
and the power action. Unavailable URLs are hidden; long URLs
retain their full value in the tooltip/accessibility description. Service state
and operation failure remain visible; the status has no runtime tooltip. Saved
file validation/persistence and address-enumeration recovery retain specific
error explanations at their existing controls. Open Browser uses
the actual running URL, not the edited endpoint. An explicit address refresh lists
assigned local IPv4s without network scanning; 127.0.0.1 and manual entry are always
available. An unavailable saved IP survives Save/Discard but fails actual startup;
there is no wildcard or alternative-address fallback. Restart stops/joins the old
HTTP/publisher/socket owner before starting a replacement. Failed binding attempts
one safe restoration of the previous running configuration; the failed saved
configuration remains saved for review. A failed restoration stays FAILED. Close
waits asynchronously for actual Web and diagnostic termination; it does not
abandon blocked work or claim socket release on timeout. Web errors never fault
Production. Reporting places actual lifecycle beside its title and keeps its
power-style Enable/Disable toggle on the right.
This toggle edits the existing draft only: Save/Discard and next-Start semantics
apply, and active-session editing remains locked. It never starts/stops a reporting
worker or adopts Web's immediate runtime power semantics. Routine counters and
confirmation text are omitted; actionable phase, submission and cleanup failures
remain visible. The lifecycle label has no runtime diagnostic tooltip.
Generation, queue, failure and cleanup observations stay with the reporting owner.
The retained commit in the global strip never replaces the card's lifecycle label.
There is no separate ACK row or routine endpoint summary.

The initial editing endpoint is 127.0.0.1:8765 at 500 ms, disabled without a saved
configuration. Saved enablement is restored on the next application launch.
The observer remains read-only and unauthenticated;
use only trusted networks. See
[configuration](configuration.md#communication-preferences) for validation/recovery.

Automatic reporting lifecycle remains separate from manual connection state;
other observations remain with the reporting owner. Communication failures are not fabricated Production NG or
application FAULT. Close stops watches/work and waits asynchronously for actual
network-owner termination before releasing the UI.

## Local simulator and verification

The maintained simulator is **localhost UDP-only**, not a TCP PLC emulator:

```powershell
uv run --no-sync python scripts/run_fins_simulator.py --trace-first-station
```

It binds `127.0.0.1:9600` by default, uses address 0/2/0, seeds D100=1234 and
W0..W511=0. Ctrl+C closes the socket; `--port 0` allocates a port and
`--max-requests` bounds diagnostic runs. Unsupported/malformed requests retain
their normal protocol errors.

Optional trace prints local observation time, sequence, four-digit hexadecimal
W50–W52 and set CH1..CH36 after every accepted write affecting W49, including
repeated sequences/bitmaps. It reads actual simulator memory without polling or
extra FINS requests. Rejected writes print no commit event. A trace is not proof
that the client received its ACK or a PLC scan consumed the result.

For a local manual check, run the simulator separately, select UDP localhost in
Communication, read D100, enable automatic reporting before Start on valid Folder
inputs, observe W49/bitmap traces, then End/Stop and close normally. The test suite
also uses controlled TCP peers to verify negotiation, fragmented/error responses,
sequence uncertainty, serialization and cleanup without real hardware.

Do not connect a diagnostic writer to an active reporting endpoint's result words.
Real photo stimulus and reject actuation are not supplied by these tools. Routine
remote deployment, PLC debugging and field validation are authorized; preserve
current equipment/configuration and verify applicable operating conditions.
