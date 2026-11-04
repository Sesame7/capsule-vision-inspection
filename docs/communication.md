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
views. A forced rejection, a channel NG, an error in any enabled channel check
(including an optional check), or an unassigned defect on that camera prevents
the bit from clearing for EMPTY. A disabled check or a Size/Color check skipped
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

## Four-cycle reporting

Communication → Production reporting offers **Per-cycle reporting** and
**Four-cycle reporting** as mutually exclusive header segments (Chinese
“逐次 / 4次合并”, English “Per cycle / 4-cycle”). Full localized meanings remain
in tooltips and accessible names. The selector sits between the title/summary
and the lifecycle status with its independent Enable/Disable action. Mode edits
remain drafts until Save; Discard restores the saved selection. A new normal
installation selects four_cycle, FINS/TCP and W49, with reporting disabled.
Engineer/Administrator explicitly saves the next-Start selection. Loading settings,
opening Communication and Start warm-up never contact a PLC. Disabled reporting
creates no automatic reporting owner: no counter reads, group attribution or result
writes occur. Inspection and per-Cycle occurrence counters continue normally;
four consecutive CycleIds alone never establish physical groups. Independent
manual Diagnostics retain their existing authorization and ownership.

Four-cycle reporting requires Basler and FINS/TCP. The commit base is exactly
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
four-cycle transport/base combinations are invalid even when disabled;
Folder/explicit-image Start with enabled four-cycle reporting is rejected before
session/engine creation. Obsolete w48_observe and w48_active values are rejected,
not migrated or interpreted. Invalid files remain untouched with recovery feedback.

Formal reporting still depends on the established external attribution conditions;
its product name does **not** establish independent hardware trigger identity:

- Production reaches RUNNING before the first trigger of a new physical group.
- PLC advances the unsigned 16-bit counter before that trigger and holds it across
  exactly four triggers and until reporting completes, including its ACK.
- Every trigger reaches every configured camera; no complete electrical trigger
  is silently missed by all cameras.
- No unobserved counter reset/reuse occurs while the session runs.

Four consecutive observed Basler CycleIds are associated only under that initial
phase and the counter/source checks below. ImageNumber continuity does not prove
physical trigger continuity; a counter read is not exposure-correlated evidence.
Counter skips and 65535 → 0 wrap between groups are accepted without inventing
missing groups. A repeated counter in the next group is rejected; hidden full
reuse/reset remains undetectable. The [retained offline evaluation](plc-batch-proposal.md)
explains these limits and is not the current register contract.

`FourCycleReportingWorker` is the generation's sole reporting socket owner. The
first observed cycle queues a read at base−1, without socket work on camera,
inference, Qt or result-callback threads. This read must finish before the second
observed cycle is admitted; otherwise phase is lost. Four distinct attributable
terminal results are required. Their existing `FirstStationReport.reject_words`
are ORed, preserving verified EMPTY projection. Partial/error cycles contribute
their conservative words. Missing triggers or unavailable results are never invented.

After all four results and the pre-write counter/source attribution check succeed,
the worker publishes one immutable inspection completion to Application. Existing
separate channel-position totals remain unchanged: confirmed NG wins, otherwise
any reject bit means UNKNOWN, then all verified EMPTY means EMPTY, otherwise
OK/verified EMPTY mixtures mean OK. Each ChannelId counts once using the original
statuses and authoritative OR bitmap. Incomplete or initially unattributable
groups do not count. This checkpoint precedes result writes: later transport
failure, commit uncertainty or attribution loss does not retract an already
eligible inspection completion. These totals are neither delivered-group counts
nor proof of physical traceability. Application retains cumulative totals and a
generation-local CycleId high-water mark; Stop/Start and Recipe changes retain
totals, while explicit operator Batch resets them. History remains per-Cycle.

After the pre-write check, write exactly two bitmap words for 30 channels or three
for 36 at base+1. After bitmap ACK, confirm the retained counter and source
attribution again, immediately before writing the retained counter value at base.
No local sequence is allocated. Each eligible group has at most one reporting
attempt; failed or uncertain writes are never replayed. ACK does not prove PLC
scan consumption, physical association or mechanical ejection. The read/check/write
sequence is not atomic; an in-flight commit cannot be recalled after association loss.

A changed/stale counter, late initial read, duplicate/gapped CycleId, missing
terminal evidence, mixed layout or source ambiguity latches phase loss for the
generation. A next group arriving before the previous group settles also suppresses
the late report; a commit already in flight remains explicitly uncertain.
Inspection may continue, but reporting stays suppressed until Stop and a new
known-phase Start. There is no polling, guessed replacement counter, fabricated
all-NG group, PLC stop or W10.7 command. Existing source fault/overload handling
remains separate.

Pause/Step/Resume are rejected for an enabled four-cycle session; Stop/Close remain
available regardless of role. Stop settles source/runtime work, drains complete
admitted groups and records incomplete groups locally. Actual reporting-thread
exit and safe socket cleanup are required; timeout never releases ownership.
A new Start never resumes a partial group. While STARTING or the reporting owner
is alive, matching-endpoint manual writes are protected from base−1 through
base+2 (30 channels) or base+3 (36 channels), including unfinished Stop. Close
rejects new commands and retains the reporting owner until cleanup. FINS target
matching still covers UDP/TCP WR space; reads and independent targets retain their
existing rules outside Close. Engineer saves, Operator runs Basler; Guest cannot
run enabled four-cycle reporting.

The 100 ms early-inspection deadline, four-cycle capacity and serial inference
owner are unchanged. Reporting retains the existing bounded eight-plus-one cycle
handoff and four scalar results per group. Bounded diagnostics retain the latest
16 settled groups plus bounded pending groups: CycleIds, terminal outcomes, timed
counter reads with their actual address, OR words, eligibility, outcome/reason,
phase issue and commit timing/uncertainty. No frames, masks or new History records
are retained. Observations survive Stop, are replaced at the next Start and are
lost on process Close. Customer UI retains lifecycle/ACK/error/skip feedback;
there is no Pilot Evidence window or hidden Observe-only runtime mode.

Authorized equipment acceptance must independently verify counter phase/hold,
camera order, W49/W149 consumption, stale equality/reset behavior, register
ownership, trigger spacing and completion/write/ACK margins using external
engineering tools. Local loopback and offline timing evidence cannot establish
these guarantees on real equipment. A complete group may still be too late;
conservative timeout projection does not repair a lost timing window.

## Communication workspace

Engineer or Administrator can issue manual PLC reads and writes, read register
watches, save CommunicationPreferences and control the Web observer. Saving
requires this role even when automatic reporting is unchanged. A watch retains
its initiating session revision and checks it on each pass and before each
group reaches the transport. An admitted request may finish after logout.
Later requests stop on authorization failure. Any role can stop monitoring,
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

The page has three content-driven sections: **Production reporting**, **Web
observer**, and **Diagnostics**. Production protocol, endpoint, timeout, base and enabled
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
read/write observations are session-only; no operation history is retained.
TCP node negotiation remains with the serial network owner. Late responses cannot
replace a newer request's presentation.

**Save settings** atomically writes definitions for all three sections; failed
validation/write preserves the entire draft and effective settings. Unsaved fields
never change reporting admission. An active session retains its frozen selection.
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

The effective running URL is a browser action in the Web title row, followed by
actual service state and the power action. Unavailable URLs are hidden; long URLs
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
Production. Reporting puts its name and compact ACK/success/ERR statistics on the
left, with actual lifecycle and a power-style Enable/Disable toggle on the right.
This toggle edits the existing draft only: Save/Discard and next-Start semantics
apply, and active-session editing remains locked. It never starts/stops a reporting
worker or adopts Web's immediate runtime power semantics. Lifecycle and
ACK/success/ERR remain visible; neither label has a runtime diagnostic tooltip.
Generation, queue, failure and cleanup observations stay with the reporting owner.
ACK never replaces the lifecycle label.
There is no separate ACK row, endpoint summary or status footer.

The initial editing endpoint is 127.0.0.1:8765 at 500 ms, disabled without a saved
configuration. Saved enablement is restored on the next application launch.
The observer remains read-only and unauthenticated;
use only trusted networks. See
[configuration](configuration.md#communication-preferences) for validation/recovery.

Automatic reporting presents its lifecycle and ACK/success/ERR summary separately
from manual connection state; other observations remain with the reporting owner. Communication failures are not fabricated Production NG or
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
