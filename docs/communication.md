# FINS communication and result reporting

The application supports synchronous FINS/UDP and FINS/TCP DM/WR word access,
manual operations/register watches and optional first-station Production reports.
Transport and reporting are independent of Qt/inference. Production is passive to
external camera triggers and never sends W10.7 photo commands.

## Clients and endpoints

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

Manual Communication and automatic reporting independently select UDP/TCP and a
numeric target. UDP remains the generic default. Normal Folder reporting defaults
to `127.0.0.1:9600`/UDP; saved Basler defaults to `192.168.251.1:9600`/TCP. There is
no localhost-only write authorization gate. Operators are responsible for choosing
the intended commissioned endpoint and operation.

## First-station registers

`first_station_reject_words()` requires exactly one explicit business status for
every ChannelId 1..36. Missing/duplicate/out-of-range/bool identities or invalid
statuses fail before I/O. Arrival order and presentation slots never determine bits.

| Register | Meaning |
| --- | --- |
| W49 | Unsigned attempted-result sequence, wrapping 65535 -> 0 |
| W50 bits 0..15 | CH1..CH16 reject bits |
| W51 bits 0..15 | CH17..CH32 reject bits |
| W52 bits 0..3 | CH33..CH36 reject bits; bits 4..15 always zero |

Business OK maps to 0; NG, EMPTY and UNKNOWN map to 1. This is one inspection
pass's communication projection, not physical product tracking or a new counter.
Technical status and product judgment remain separate inspection data.

`FirstStationReporter` borrows a client and one serial owner submits:

1. Validate the entire projection and reserve the attempt sequence.
2. Write all three bitmap words to W50–W52 in one request.
3. After its successful validated ACK, write the reserved W49 sequence.
4. Return a commit only after the W49 ACK. Submission performs no readback.

Every valid attempt advances local sequence, even after failure, so uncertainty
can leave gaps. Unchanged bitmaps still get a new sequence. Standalone callers
provide the first attempted sequence explicitly; the automatic worker first reads
current W49 and seeds current+1 modulo 65536. Reconnect continues local progression
for new cycles rather than replaying an old uncertain report.

The two writes are not atomic PLC memory/scan consumption. Bitmap ACK followed by
sequence failure leaves staged words; a lost sequence ACK leaves commit uncertain.
Never silently resend the old result or infer mechanical rejection from ACK.
PLC ladder consumption/handshake, restart association and physical reject timing
still require equipment commissioning.

## Automatic reporting lifecycle

Communication's Production enable/endpoint/routing fields are drafts. Only a
successful **Save settings** publishes preferences to the next Start and persists
them in `data/communication.json`. Persistence and publication share the existing
Application admission lock, so a competing Start cannot freeze a half-saved selection. Missing settings keep the previous defaults (disabled;
Folder localhost/UDP, Basler field endpoint/TCP). Restoring enabled never creates
a reporting client or sends before Start; warm-up still sends nothing. Production
settings remain locked during STARTING/RUNNING/STOPPING and FAULT. Old ACKs,
counts and connectivity are never restored as live observations.

`FirstStationReportingWorker` receives compact generation/CycleId and 36 scalar
statuses, never image/result histories. One non-daemon thread owns its connection,
with a bounded FIFO (default eight pending plus one active) and bounded observations.
Submission/settling is separate from terminal inspection and accounting. Socket
work never runs in Qt, inference or result-completion callbacks.

Ordinary network errors become failed observations without changing inspection
judgment/counters. Backlog/state rejection is explicit and requests Application
Stop. There is no retry loop. New-cycle reconnect may recover transport; failure
does not reinterpret old results or claim their delivery.

Basler reports attributable terminal cycles, including partial timeout/shutdown
results with six camera results and at least one observed frame. Missing/unfinished
camera channels are projected conservatively as UNKNOWN/reject; completed camera
judgments survive. No observed cycle means no report. The source's ambiguity fence
is checked at admission and again after bitmap ACK, before W49. A known ambiguous
cycle gets no commit; an already in-flight sequence write cannot be recalled.

Basler Stop first stops inspection/runtime admission, settles admitted cycles and lets
active native work finish, then drains already accepted reports in order. Folder
Stop keeps its existing active-attempt completion and explicit pending skips.
No new reports start from rejected work. Session release additionally requires
report-thread exit and safe socket cleanup; notification/timeout alone is not exit.
The separately connected Basler source keeps grabbing/discarding after Stop; only
Vision Disconnect or application Close releases cameras. Basler Pause/Step keeps the reporting owner alive and waits for accepted reports
to settle before PAUSED. Discarded paused captures send no report or sequence write.
These controls do not pause the PLC or mechanical equipment.

Manual clients/watches remain independent. While an automatic owner is starting
or alive, manual writes overlapping W49–W52 on the same host/port are rejected,
including cross-transport conflicts. Reads and independent endpoints remain
available. The guard prevents competing writers; it is not PLC-side locking or
access control.

## Communication workspace

The page has three content-driven sections: **Production reporting**, **Web
observer**, and **Diagnostics**. Production protocol, endpoint, timeout and enabled
selection are inline; FINS routing expands separately (TCP negotiates nodes).
Diagnostics has its own independently saved endpoint and routing, grouped DM/WR
watches and serialized manual/auto refresh, plus explicit one-shot writes.
Requests always snapshot the saved/effective diagnostic endpoint, identified above
the operations; draft edits never silently redirect them. Read errors are shown
at their groups, retaining stale values/timestamps; write ACK/error/uncertain
execution appears beside writing. Detailed errors remain in tooltips and bounded
observations. Test Read, standalone read controls and the Operation Details panel
are removed from the UI; underlying typed reads/tests remain supported. Register
groups, values and observation history are session-only.
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
The default settings can be explicitly saved before any edits. Portrait stacks
cards; landscape places Production/Web side by side only when their actual content
fits. Expanded routing/diagnostics grow in the outer scroll area.
Web enabled/bind/port/interval preferences apply only at the next process launch;
concise actual service status sits at the card header's right edge. Detailed
URLs/errors are in its tooltip. Reporting has a separate header status and compact
generation counters. The restart notice remains separate from edited values.
There is no runtime Web start/stop/rebind action. Binding still requires one
assigned local IPv4 (default 127.0.0.1:8765, 500 ms), and the observer remains
read-only and unauthenticated; use only trusted networks. See
[configuration](configuration.md#communication-preferences) for validation/recovery.

Automatic reporting presents current/last-generation ACK/failure/skip/rejection
counts, pending depth and recent compact observation, separately from manual
connection state. Communication failures are not fabricated Production NG or
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
