# FINS/UDP communication

## Current scope and authority

`vimo_demo.communication` provides a standalone, synchronous, standard-library
FINS transport and loopback simulator. Its first-station projection consumes the
existing Inspection business-status enum, without changing inspection evidence,
judgments or counters. Only projection/reporting requires Inspection types;
transport/simulator imports and the existing CLI demo still require no image stack.
The transport and reporting owner are Qt/ViMo independent. Explicit local Production
composition can opt into [automatic first-station reporting](#local-production-reporting)
to exactly `127.0.0.1`; normal `app.py` leaves it disabled. The Qt Communication
workspace uses separate clients for manual D/W operations and explicitly enabled
register monitoring. There is no PLC configuration persistence, equipment wiring,
triggering/consumption handshake or hardware commissioning.
Transport/protocol errors are not inspection outcomes or product judgments.

The protocol authority is Omron's [NX FINS manual W596-E1-06](https://www.omron-ap.com/data_pdf/mnu/w596-e1-06_nx701_nx502_nx102.pdf?id=3900).
Its sections 1-1 and 4-1 refer frame formats/end codes to
[communications reference W342-E1-18](https://assets.omron.eu/downloads/latest/manual/en/w342_cs_cj_cp_nsj_communications_commands_reference_manual_en.pdf).
Implementation and literal test vectors use W342 sections 3-3, 5-1-3, 5-2-1/2 and
5-3-2/3; W342 section 5-2-2 lists WR word code `B1` and W0–W511.
W596 sections 1-4 and 2-2 establish NX transport/command support.
Local client/simulator agreement alone is not evidence of standards compliance.

## Supported subset

| Item | Current contract |
| --- | --- |
| Transport | Numeric unicast IPv4, UDP port 9600 by default; one response-required request at a time |
| Addressing | Explicit destination/source `FinsAddress(network, node, unit)`; network 0–127, unicast node 1–254, unit encoded as a byte 0–255 |
| Memory | DM/D **words**, code `82`, D0–D32767; W/WR **words**, code `B1`, W0–W511; bit address zero |
| Read | `01 01`; 1–999 consecutive unsigned words |
| Write | `01 02`; 1–997 consecutive unsigned words, values 0–65535 |
| Wire | 10-byte FINS header; big-endian address/count/word data; at most 2012 bytes per FINS datagram |
| Result | Read returns an immutable tuple in address order; write returns `None` after a successful response |

The unit byte range describes encoding, not availability of every unit/service.
The local example targets CPU unit 0. IP address and FINS node are independent;
there is no last-IP-octet inference. Hostnames, broadcast, bit access, other memory
areas, signed/32-bit values, routed-network commissioning, FINS/TCP and implicit client polling
are outside this subset. Invalid API values/ranges raise `ValueError` before send.
The simulator accepts protocol-defined zero-count no-ops; the client requires
positive counts. No silent splitting of an oversized operation is performed.

## Client API and ownership

```python
from vimo_demo.communication import FinsAddress, FinsUdpClient

with FinsUdpClient(
    "127.0.0.1",
    destination=FinsAddress(network=0, node=2, unit=0),
    source=FinsAddress(network=0, node=1, unit=0),
    port=9600,
    timeout=1.0,
) as client:
    initial = client.read_words(100)[0]
    client.write_words(100, (4321,))  # This example writes localhost only.
    updated = client.read_words(100)[0]
```

One serial caller owns the client/socket; it creates no threads or queues.
Overlapping calls or close during an active request fail explicitly with
`FinsTransportError`. `close()` is idempotent after a request returns; a context
manager also closes on exceptions. A new client is a new socket lifecycle.
Calls block for a bounded send/receive deadline and belong outside a future GUI
thread. Socket creation performs no handshake and proves nothing about PLC online
state. The OS selects the local UDP source port; source FINS address is explicit.

Responses must come from the configured IPv4 **and port**, have response direction,
reverse the request's destination/source addresses, and match SID and command.
Success payload length must exactly match the requested words (or empty write ACK).
W342 instructs applications not to check response RSV/GCT/ICF reserved bits; these
system fields are deliberately not request-identity checks. SID increments modulo
256. FINS/UDP identity checks are not authentication or protection against replay.

| Exception | Meaning / client state |
| --- | --- |
| `FinsProtocolError` | Wrong sender/identity, malformed, truncated or oversized response; socket closed |
| `FinsTransportError` | Socket/lifecycle failure; original OS exception chained when present; I/O failure closes socket |
| `FinsTimeoutError` | Transport subclass; request deadline elapsed; socket closed |
| `FinsPlcError` | Nonzero raw `end_code`, `command` and optional `response_data` preserved; socket remains usable |

End-code CPU/relay flags are preserved, never masked into success. No retry or
reconnection occurs. A write timeout or missing/malformed acknowledgement does
**not** establish whether the write executed. Closing a failed exchange prevents
late responses being consumed by another request on that client; recreating a
client is an explicit caller decision, not permission to retry a write.

### W/WR access

`read_words(start, count=1, *, area=FinsMemoryArea.DM)` and
`write_words(start, values, *, area=FinsMemoryArea.DM)` preserve existing D calls.
Pass `area=FinsMemoryArea.W` for W words; raw integer/string area selectors are
rejected. Both areas reuse the same framing, count limits, serial socket lifecycle
and response/error validation. The W range itself permits at most 512 words.

```python
from vimo_demo.communication import FinsMemoryArea

# Inside the explicitly owned localhost client context above:
client.write_words(50, (1, 32768, 15), area=FinsMemoryArea.W)
words = client.read_words(49, 4, area=FinsMemoryArea.W)
```

## First-station result projection and submission

This is a communication-specific projection of **one inspection pass**, not a
change to [channel business status](inspection.md#channel-business-status), an
accumulated observation of a physical capsule or a new application counter.

`first_station_reject_words(statuses)` accepts an iterable of explicit
`(ChannelId, ChannelBusinessStatus)` pairs. It requires exactly one entry for each
CH1–CH36; missing, duplicate, non-integer/bool and out-of-range identities, or
non-enum statuses, raise `ValueError` before I/O. Input order is irrelevant;
camera arrival order, slot position and presentation order never determine bits.
It returns an immutable three-word tuple in W50–W52 order:

| Channels | Address / bits | Projection |
| --- | --- | --- |
| CH1–CH16 | W50, bits 0–15 | OK=0; NG / EMPTY / UNKNOWN=1 |
| CH17–CH32 | W51, bits 0–15 | Same |
| CH33–CH36 | W52, bits 0–3 | Same; bits 4–15 are always zero |

The caller may explicitly project `((row.channel_id, row.business_status) for row
in result.channels)` from an existing result. No automatic result subscriber or
Production wiring is installed.

`FirstStationReporter(client, *, initial_sequence)` borrows an existing client;
one serial caller owns **both** objects and closes the client. Do not use the client
for unrelated operations during a submission. Overlapping `submit()` calls fail
with `FinsTransportError`, without a queue or interleaved submission writes.
`initial_sequence` is required, unsigned 0–65535, and is the **first attempted**
sequence, not the previous committed value. It is not derived from CycleId.

`submit(statuses)` validates the complete projection, reserves the sequence, then:

1. Sends one `01 02` request containing all three words to W50–W52.
2. Only after its validated successful ACK, sends one `01 02` request to W49 with
   the reserved sequence.
3. Only after the second successful ACK, returns `FirstStationCommit(sequence,
   reject_words)`. No readback occurs inside submission.

Every valid attempt reserves the next sequence modulo 65536, including attempts
that fail. Thus failures can leave gaps, and an uncertain sequence write is never
implicitly reused. Invalid inputs/overlapping calls reserve nothing.
`next_sequence` is local instrumentation, not remote confirmed/consumed state.
Consecutive successful submissions change W49 even when their bitmaps are equal.

W49 is the **proposed first-station commit sequence**, not part of the original
Excel reject bitmap. Explicit initialization alone does not establish that the
first write differs from the PLC's current W49. Restart/recovery, initial sequence
synchronization and scan-cycle consumption guarantees are not supplied by this
synchronous helper. It has no persistent synchronization state, automatic read,
retry or reconnect. The optional local owner below adds initial W49 reading and
later-cycle socket recovery, without industrial delivery guarantees.

For example, with the loopback simulator already running:

```python
from vimo_demo.communication import (
    FinsAddress, FinsMemoryArea, FinsUdpClient, FirstStationReporter,
)
from vimo_demo.inspection.production import ChannelBusinessStatus, ChannelId

statuses = tuple((ChannelId(i), ChannelBusinessStatus.UNKNOWN) for i in range(1, 37))
with FinsUdpClient(
    "127.0.0.1", destination=FinsAddress(0, 2, 0), source=FinsAddress(0, 1, 0),
) as client:
    reporter = FirstStationReporter(client, initial_sequence=1)  # Fresh simulator W49=0.
    first = reporter.submit(statuses)   # W49=1, W50=65535, W51=65535, W52=15.
    second = reporter.submit(statuses)  # Same bitmap; W49=2.
    observed = client.read_words(49, 4, area=FinsMemoryArea.W)  # Explicit verification.
    assert observed == (2, 65535, 65535, 15)
```

### Submission failures and limits

`FirstStationSubmissionError` preserves `stage` (`SubmissionStage.BITMAP` or
`SEQUENCE`), attempted `sequence` / `reject_words`, and the original `FinsError` as its chained
cause, including raw PLC end codes or timeout diagnostics. It is never a successful
commit:

- Bitmap failure prevents the sequence request entirely.
- Sequence failure may leave the newly written bitmap present without an
  acknowledged commit. No rollback is attempted.
- Timeout or malformed/missing ACK leaves that stage's remote write outcome
  uncertain, even if memory actually changed. Transport failure closes the client
  under the existing error contract; PLC end-code errors leave it usable.

The reporter does not close/recreate the borrowed socket, retry, repair remote
state, persist results or retain submission history. After an error, recovery is
an explicit caller decision; local sequence advancement is not a recovery protocol.
Even both successful ACKs establish only protocol-level write acknowledgement,
**not proof of PLC scan consumption**. The two requests are not one atomic remote
transaction. The PLC will eventually own multi-pass accumulation and mechanical
rejection; this protocol implements neither. The [local reporting owner](#local-production-reporting)
defines Production scheduling, admission, result association and shutdown. H2C
presentation, second station and physical synchronization remain unimplemented.

## Local Production reporting

### Handoff and ownership

`vimo_demo.communication.reporting` adds `LocalReportingSettings`,
`FirstStationReport`, `FirstStationReportingWorker` and immutable observations.
Pass `reporting=LocalReportingSettings(...)` to the existing local `compose()` or
`local_application()` entry. The developer launcher exposes `--report-first-station`;
the default is disabled. No Recipe, persisted PLC schema or normal recovery path
changes. Settings reject every host except the exact IPv4 string `127.0.0.1`
below the CLI/UI boundary, before socket creation.

Each application generation owns one non-daemon `FirstStationReporting-N` thread.
It alone constructs, uses, replaces and closes its FINS client. Engine ownership
remains with `InferenceWorker`; manual Communication requests/watches use their
own clients and lifecycle. Neither painting nor application snapshot polling
drives report submission.

A terminal Production task callback projects a COMPLETE or FAILED result only
when all six cameras have collected frames and the 36 channel statuses are valid.
TIMED_OUT, SHUTDOWN, exceptional runtime outcomes and incomplete/invalid projections
are explicitly skipped. Warm-up creates no production reports, counters or network
requests. The handoff retains only generation, CycleId, immutable channel/status
pairs and three reject words, never images, masks, contours or full results.
Only channel business status drives the bitmap; unassigned camera-level ViMo NG
does not spread reject bits.

The callback performs validation and nonblocking admission, not network I/O.
Local source execution waits for each inspection result, so reports enter FIFO in
local pass order. A per-generation accepted-CycleId high-water mark rejects duplicate
or out-of-order submissions without retaining an identity history. This is a local
monotonic-source contract, not ordering for arbitrary external hardware traffic.

### Admission, failures and observations

The FIFO holds at most `max_pending` reports (default **8**), plus one claimed
active report. There is no eviction, replacement, retry queue or completed history.
Admission rejection raises `ReportingCapacityError` or `ReportingStateError` and
records the rejected identity. In local composition it stops source admission via
the existing application Stop boundary and completes `source_done` exceptionally.
The already completed inspection still reaches accounting; overload is not product
NG or an ordinary model/check failure and does not itself enter application FAULT.

Before the first report, the owner reads W49 and starts at `(W49 + 1) & 0xffff`.
Production CycleId is never the PLC sequence. Failed initialization is observable
with no invented sequence; a later new cycle may read W49 again. Once initialized,
each submission preserves the helper's attempted-sequence advancement, including
gaps. If transport failure closed the socket, a later cycle gets a fresh client
and the known local next sequence, without rereading or reusing the failed attempt.
No failed cycle/uncertain write is resent and no memory rollback is attempted.

Ordinary communication failures do not change inspection judgments, counters or
Production RUNNING state. They preserve initialization/bitmap/sequence stage,
generation/CycleId, attempted sequence/words where available, the original FINS
exception and its cause, bounded diagnostic text and duration. A later successful
report does not retroactively deliver a prior failure. Unexpected reporting-owner
failure closes its admission; a later handoff exposes a state rejection and Stop.
Unsafe resource cleanup remains subject to the application's ownership safeguards.

`submit()` returns a noncancellable `Future[ReportObservation]` for an admitted
report. It resolves to ACKNOWLEDGED, FAILED or explicitly SKIPPED; it is separate
from the inspection task Future and from proof of thread exit. Observer callbacks
must remain short/nonblocking and must not mutate widgets. QUEUED and ATTEMPTED
are intermediate observations, never remote acknowledgement.

`snapshot()` reports pending/peak/active state, queued/attempted/acknowledged/failed/
skipped/rejected counts, local next sequence, latest queued/attempted/ACK/outcome
and latest failure/skip/rejection plus owner/cleanup issues. Only the latest item
per category is retained; no persistent reporting history is created. The local
application's `reporting_snapshot()` exposes the current/last generation or `None`
when disabled, separately from `ProductionApplicationSnapshot`. Later H2C UI may
consume these Qt-independent observations; no diagnostics UI is added here.

### Stop, restart and Close

Session Stop revokes reporting admission immediately. Unclaimed pending reports
are finalized SKIPPED on the reporting thread, with no socket requests. A result
finishing after Stop is also explicitly skipped. The claimed logical report is
allowed to finish naturally, including its ordered bitmap/sequence writes and,
if needed, initial read. Each exchange has the configured bounded timeout; Stop
does not interrupt between the helper's acknowledged write stages or force-cancel
a UDP request. Observer callbacks can also delay actual owner termination.

Session join must observe source, runtime and reporting thread exit and safe cleanup
before application READY or owner release. Repeated Stop is harmless. Failed socket
cleanup retains unsafe ownership rather than declaring success. Close keeps the
existing nonblocking Qt workflow; a new generation cannot start while the old
session is retained. Stop/Start creates a fresh report owner and rereads W49 before
its first new report; no old-generation queue survives into it.

This remains localhost protocol testing: ACK is not PLC scan consumption, exclusive
writer ownership, restart-safe delivery, capsule counting or mechanical rejection.
Do not concurrently write W49–W52 through manual operations while testing automatic
reporting. Read-only monitoring uses an independent client and is safe for observation.

### Local manual verification

Use two repository-root PowerShell terminals and existing validated local assets.
Terminal 1 runs the simulator:

```powershell
uv run --no-sync python scripts/run_fins_simulator.py --port 9600
```

Terminal 2 explicitly enables reporting in the real local developer launcher:

```powershell
uv run --no-sync python scripts/run_local_production_ui.py --model-config data/models/model.json --machine-config data/machine/machine.json --recipe data/recipes/local.recipe.json --sources-config data/sources/camera_sources.json --language zh --report-first-station --fins-port 9600 --fins-timeout 1 --fins-pending 8 --fins-destination 0 2 0 --fins-source 0 1 0
```

These local filenames must exist and validate. This command uses the real model;
fake/localhost tests below need no GPU. Click Start, then in the independent
Communication page add a **W49, count 4** watch and enable refresh. A fresh simulator
starts W49 at zero; acknowledged passes advance it, even for identical bitmaps.
W50–W52 reflect OK=0 and NG/EMPTY/UNKNOWN=1; W52 upper bits remain zero. Watch reads
are not atomic with the two writes, so a transitional read is not a delivery verdict.
Stopping monitoring or leaving Communication does not stop automatic reporting.

Stop and wait for READY before restarting; the new generation reads the current
W49. To observe ordinary communication failure, stop the simulator: inspection
continues until an explicit Stop or genuine backlog overload, with reporting failure
observations rather than production NG/FAULT. Restarting the simulator allows later
cycles to attempt new reports; it does not retry old ones, and a newly reset simulator
is not an industrial sequence-recovery model. Stop/Close the application normally,
then stop the simulator with Ctrl+C. H2C failure presentation is deferred; inspect
`reporting_snapshot()` through the local composition API or use the focused tests
for stage/count verification.

## Local simulator and manual demonstration

From the repository root, use two PowerShell terminals.

Terminal 1:

```powershell
uv run --no-sync python scripts/run_fins_simulator.py --port 9600
```

Terminal 2:

```powershell
uv run --no-sync python scripts/fins_local_demo.py --port 9600 --value 4321 --timeout 0.5
```

The demo has **no target-host option**: all writes go to `127.0.0.1`. A fresh
simulator yields `Read D100: 1234`, `Wrote D100: 4321 (localhost)`, then
`Read D100: 4321`. It additionally opens a silent loopback UDP endpoint and shows
an expected bounded timeout, then closes all demo sockets. Repeated demos see the
current in-memory value. Stop Terminal 1 with **Ctrl+C**; it exits through socket
cleanup. `--max-requests 3` optionally exits the simulator after the three demo
datagrams. Alternate ports work by passing the same port to both commands;
`--port 0` is useful for automatically allocated test ports.

The simulator binds **only** `127.0.0.1`, owns separate finite D0–D32767 and W0–W511
maps initialized to zero except D100=1234, and has no scan/ladder/physical-I/O behavior. Writes are
validated fully before mutation. Its public `serve(stop_event)` runs on the caller;
an Event requests stop, with an idle receive wait of at most 100 ms. If tests use
a serve thread, they join it before closing/releasing its socket. No thread is
created by the simulator itself. Initialization/bind failures are explicit.

Valid addressed requests receive reversed routes and echoed SID/command. Too-short
or invalid/unaddressed headers (including response/no-response frames) are ignored
without mutation. For supported invalid requests it returns these documented
end codes, without claiming the complete NX error-precedence behavior:

| Code | Simulator condition |
| --- | --- |
| `0401` | Unsupported command |
| `1001` | Command too long |
| `1002` | Command too short |
| `1003` | Write count/data-length mismatch |
| `1101` | Area not implemented by this DM/WR simulator |
| `1103` | Invalid starting word/bit address in the selected area |
| `1104` | End word outside the map |
| `110B` | Requested read response too long |

## Communication workspace

The existing Communication navigation destination (`diagnostics`) now hosts
`CommunicationPage`, independently of Production state. It remains usable in an
UNCONFIGURED shell. Opening/navigating to the page sends **no** request. Host/port,
timeout, D/W area, start address, count, write values and watch groups are session-local, never persisted
into `data/`, Recipes, Model, MachineLayout or source definitions.

Four vertically stacked, scrollable sections use the shared Chinese/English
style catalog: connection settings, watch groups/current values, collapsible manual
register operations and concise communication feedback. Manual operations start
collapsed; advanced FINS addressing also starts collapsed.
Common fields are immediately accessible; advanced destination/source network,
node and unit controls are collapsible. Only FINS/UDP is available. Numeric IPv4
defaults to `127.0.0.1`, port to 9600 and timeout to one second (UI range **0.05–30
seconds**). FINS nodes remain explicit when the host changes.

**Test read** reads one word at the manual area's current start address; it is not a connection
handshake. **Read** uses the configured count. **Write** takes decimal unsigned
words separated by commas/whitespace and writes that many consecutive words from
the start address; the read-count field does not determine write length. Validation
rejects invalid host, range, count and values before dispatch. Multiword read values
are shown with their actual D/W addresses, unsigned decimal and four-digit hex in a
read-only, scrollable table (at most 999 rows, further limited by the selected area).
`CommunicationRequest.area` defaults to `FinsMemoryArea.DM` for existing callers;
`FinsMemoryArea.W` is forwarded unchanged to the H2A client. A write ACK is expressly **not readback verification**: no automatic read
is issued, and the separately timestamped last-read table remains historical.

Reads can target configured numeric unicast IPv4 endpoints. **Writes require the
exact target `127.0.0.1`**: other targets disable Write with a localized explanation,
and `CommunicationRequest.validate()` repeats the restriction inside
`execute_request()` before constructing a socket. Even another loopback address
such as `127.0.0.2` is rejected. There is no override, target substitution or retry.
The unrestricted H1A low-level client API is unchanged; this manual-workspace
execution policy is a separate boundary in `communication/operations.py`.

### Register watches and refresh

`RegisterWatchGroup(area, start, count=1, name="")` in `communication.operations`
is a frozen, Qt-independent consecutive-read definition. It validates the same
area/address/count limits as the transport; names contain at most 64 characters.
The operator adds/removes groups using a compact form and group selector. The list
starts empty, accepts at most **16 groups / 2048 total words**, and is never saved.
Equal or disjoint ranges remain independent groups, including within one area;
there is no speculative merging or mandatory W49/D100 setup. Display numbering
distinguishes even groups with identical names/ranges.

Each word has its group, actual D/W address, unsigned decimal, four-digit hexadecimal
and observation time/status in one read-only table. No bit editing or writes are
available there. Unread entries show `--`. Only one latest successful observation
is retained per group; rows/response retention are bounded by the configured limits.
Failed/unstarted groups retain their old values and **original** observation time,
marked **Previous / 历史** rather than newly read. Groups successfully read earlier
in a partially failed pass remain marked **Latest pass / 本批成功**. The status also
shows the most recent successful group-observation time; it is not the timestamp
of a failed group or a claim of connectivity.

**Read once** takes one pass over the configured groups in their configured order.
**Auto refresh · 1 s** must be explicitly started and can be stopped while I/O is
active. Automatic mode remains off on opening/returning to the page. A single-shot
Qt timer schedules the next pass **one second after actual owner-thread exit**
and successful publication of the previous pass. This completion-based cadence is
nominal, not a guaranteed one-second sampling period: pass duration and GUI dispatch
add delay. No request/pass overlaps, pending tick queue or catch-up burst exists.
There is no permanent polling thread. A failure aborts unstarted groups, stops
automatic scheduling and retains localized error details; restart is explicit.
Successful automatic passes update values/status without logging each pass. Manual
passes add one compact record; monitoring errors enter the existing bounded log.

Endpoint/routing, area/ranges and groups are captured as immutable request snapshots
before dispatch. Manual fields do not determine watch ranges. One non-daemon
**FinsCommunication** thread executes each admitted manual request or monitoring
pass through the existing `execute_request()`. For a pass, each group gets a separate
serial read and client context, constructed/used/closed on that same thread. No
second transport owner or communication queue is added. GUI state never redirects
in-flight requests, and callbacks never mutate widgets.

Manual Test/Read/Write, connection fields and watch edits are excluded during a pass
**and between passes while automatic mode is enabled**. Stop monitoring before
editing or using manual operations. Stop/leave/approved close sets a pass-local Event
to skip unstarted group reads; the active bounded request completes naturally and
closes on its owner. Controls remain unavailable until actual thread exit. Leaving
the page stops scheduling, and returning does not resume it. Navigation never drops
the active thread. This ownership is separate from ViMo/ProductionRuntimeSession.

Multiple groups are **not one atomic PLC snapshot**. Monitoring is repeated FINS
reading, not PLC-pushed events or equipment-readiness verification. Reading W49–W52
does not interpret them as confirmed Production results or call `FirstStationReporter`.
No Production result, counter, application state or shared feedback is changed.

The page reuses `FutureDelivery` with explicit queued Qt delivery and a 20-ms timer
that observes **thread termination**, not PLC connectivity. Future completion or
notification is insufficient: only after `is_alive()` is false does `join(0)`
release the thread and publish its result. Completion names distinguish consecutive
requests. Final `dispose()` requires no owned work, revokes delivery, and ignores
stale notifications; deleting an active workspace prematurely is unsupported.

Window close first uses the existing draft/job guards. Once approved it stops
automatic scheduling/unstarted reads and communication admission, and requests application shutdown independently. Active UDP
I/O finishes/reaches its bounded timeout; its client closes on the same background
thread. Qt never closes an active client concurrently or forcibly terminates the
thread. The window stays alive until **both** actual communication exit and the
existing application close/join condition. Final accepted close disposes delivery.
If application cleanup fails and keeps the shell open, the live communication page
resumes admission (still waiting for any active request), without restarting automatic
refresh; no unsafe thread is dropped.

Feedback identifies the current request/pass, latest successful response with
endpoint/address/time/elapsed duration, and at most **100** compact log entries.
Only the latest manual read and per-group observations are retained; no result/exception history is unbounded.
Localized timeout/transport/protocol/PLC messages retain raw diagnostic details in
tooltips/logs and preserve PLC end codes. A timed-out write states that its outcome
is **unknown** and requires explicit verification. A successful response remains a
historical observation, never a persistent online indicator. Communication feedback
does not change Production state/FAULT, counters, judgments, Recipe activation or
the shared Production status strip.

### Manual Windows review

From the repository root, start the simulator and normal shell in separate terminals:

```powershell
uv run --no-sync python scripts/run_fins_simulator.py --port 9600
uv run --no-sync python app.py
```

Do not start Production for this check; all operations below target localhost.

1. Open **Communication / 通信**, leaving `127.0.0.1:9600`. The watch list is empty
   and no request is sent. Expand **Manual register operations**. Select D/D100:
   Test read returns 1234 on a fresh simulator. Write 4321, observe ACK-only feedback,
   then explicitly Read to verify. Repeat with W/W50 and `0, 65535, 4321`, read count
   3: addresses W50–W52, decimal values and `0000 / FFFF / 10E1` should agree.
2. Add named watch groups W49/count 4 and D100/count 1. Click **Read once**; verify
   actual addresses, values and per-group times. These addresses are examples only,
   not preloaded mandatory settings. Add/remove another valid group.
3. Start **Auto refresh · 1 s**. Inputs/manual operations lock; successful updates
   do not flood the operation log. Stop explicitly, wait for active work to exit,
   then edit or write. Change values with manual Write, and Read once to verify.
4. Start automatic refresh and navigate away. Future refreshes stop, the current
   read finishes, and returning keeps automatic mode off with observations retained.
5. Stop the simulator while monitoring. A timeout or OS transport error stops
   scheduling; retained values keep their old times and historical markers. Restart
   the simulator, then explicitly restart monitoring. It must not retry itself.
6. Stop monitoring, change the host to `192.0.2.10` **without sending** and verify
   Write is disabled in both D/W modes. Restore localhost. This is a display/policy
   check, not a remote-device test.
7. For active close, use an unused localhost port with a few-second timeout, start
   a watch pass, then close the shell. It stays responsive until actual thread exit;
   unstarted reads are skipped. No communication thread should remain afterward.

Switch Chinese/English and check portrait/landscape layouts. Expanded advanced/manual
forms or small windows may require vertical scrolling to feedback. Native Windows
DPI/touch review remains manual; offscreen screenshots do not certify ergonomics.

## Verification and hardware limitations

The focused tests can omit the project's Qt conftest entirely:

```powershell
uv run --no-sync pytest --confcutdir=tests/unit tests/unit/test_fins.py --tb=short
```

They use dynamic loopback ports, independent literal request/response vectors,
actual UDP read/write/error exchanges, sender/SID/command/routes, boundary values,
malformed packets, write atomicity, bounded timeout/unreachable behavior, SID wrap,
explicit overlap rejection, thread joining and socket rebinding after cleanup.
An independent-process CLI test runs with `python -S` and verifies that neither
Qt, NumPy nor SmartMore is imported. Normal repository gates still apply; no GPU,
model, dataset or hardware is used by these communication tests.

The H2A localhost verification includes all 36 reject bit positions and business
states, shuffled identity order, invalid identity/status rejection, reserved bits,
W multiword literal vectors, separate DM/W maps, boundary/atomicity validation,
two-request ordering, unchanged bitmaps, 65535→0 rollover and W49–W52 readback.
Test-only peers inject PLC errors, lost ACKs before/after remote mutation and
malformed ACKs at both stages; no retries or sequence write after bitmap failure
are permitted. Peers/clients terminate and release sockets. These are memory/protocol
checks, not PLC scan/ladder or mechanical-rejection simulation.

```powershell
uv run --no-sync python -m pytest --confcutdir=tests/unit tests/unit/test_fins.py tests/unit/test_first_station.py --tb=short
```

Using `python -m pytest` keeps the repository root importable for the existing
test-support package even with the Qt conftest excluded. Projection tests use
Inspection models/NumPy locally, without a ViMo model or GPU. The independent
`python -S` transport/demo check still verifies no Qt/NumPy/SmartMore imports.

Workspace tests separately cover real localhost UDP read/write/explicit readback,
mixed D/W watch passes, group bounds/add/remove, decimal/hex/timestamp display,
slow-pass deadline delivery without overlap/backlog, automatic failure/restart,
historical values, input exclusion, stop/leave skipping unstarted groups,
actual-exit scheduling and absence of automatic-success log flooding,
worker-only client construction/use/close, malformed/timeout/PLC feedback, guarded
non-loopback writes, controlled remote-read parameter forwarding, duplicate admission,
navigation, actual thread/socket exit, application-close failure recovery, bounded
logs, stale delivery, UNCONFIGURED availability and bilingual layout/screenshots:

```powershell
uv run --no-sync pytest tests/unit/test_communication_operations.py tests/integration/test_communication_ui.py tests/integration/test_global_shell.py tests/integration/test_production_ui.py --tb=short
```

Remote-read forwarding is verified with a controlled client, **not actual equipment**.
H1C/hardware supervision, persistent profiles, H2C reporting presentation and
consumption handshakes remain deferred; this workspace does not select their implementation.

Automatic local reporting tests use fake Production and actual loopback peers,
independently of Qt result polling. They cover eligible COMPLETE/FAILED projection,
unassigned-NG isolation, invalid/runtime skips, initial read/rollover, stage-specific
lost ACKs before/after mutation, later-cycle recovery without retry, bounded FIFO
rejection, Stop/Start isolation, warm-up exclusion, counters, slow-network GUI
responsiveness, independent manual communication and actual thread cleanup:

```powershell
uv run --no-sync pytest tests/unit/test_first_station_reporting.py tests/integration/test_local_reporting.py --tb=short
```

**Not yet verified on NX102:** the specific CPU/version, FINS enablement and port
settings, IP/node/routing conversion tables, host source-address handling, network
filtering/write protection and actual allocated CJ-compatible DM/WR memory. W596
identifies built-in EtherNet/IP **port 2** for FINS and requires configured
CJ-compatible memory; it also cautions that future products may not support FINS.
This phase does not configure Sysmac Studio or send to equipment. It establishes
the local protocol subset and opt-in local Production wiring, not hardware
compatibility, machine safety or industrial delivery guarantees.
