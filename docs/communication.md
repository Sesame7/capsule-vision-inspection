# FINS/UDP communication

## Current scope and authority

`vimo_demo.communication` provides a standalone, synchronous, standard-library
FINS transport and loopback simulator. Its first-station projection consumes the
existing Inspection business-status enum, without changing inspection evidence,
judgments or counters. This dependency is loaded only when projecting statuses;
transport/simulator imports and the existing CLI demo still require no image stack.
There is no Qt, ViMo, Production runtime, configuration-persistence or real-equipment
integration. The Qt Communication workspace uses the transport for explicit manual
D operations in a separate background context. Automatic Production reporting,
PLC triggering/consumption handshakes and hardware commissioning are **not implemented**.
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
areas, signed/32-bit values, routed-network commissioning, FINS/TCP and polling
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
synchronization and scan-cycle consumption guarantees remain deferred. There is
no persistent synchronization state, automatic read, retry or reconnect.

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
rejection; H2A implements neither. H2B must define Production scheduling, admission,
result association, failure policy and shutdown ownership before wiring reporting;
H2C UI, second station and physical synchronization remain outside this contract.

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
timeout, D start address, count and write values are session-local, never persisted
into `data/`, Recipes, Model, MachineLayout or source definitions.

Three vertically stacked, scrollable sections use the shared Chinese/English
style catalog: connection settings, register operations and communication feedback.
Common fields are immediately accessible; advanced destination/source network,
node and unit controls are collapsible. Only FINS/UDP is available. Numeric IPv4
defaults to `127.0.0.1`, port to 9600 and timeout to one second (UI range **0.05–30
seconds**). FINS nodes remain explicit when the host changes.

**Test read** reads one word at the current D start address; it is not a connection
handshake. **Read** uses the configured count. **Write** takes decimal unsigned
words separated by commas/whitespace and writes that many consecutive words from
the start address; the read-count field does not determine write length. Validation
rejects invalid host, range, count and values before dispatch. Multiword read values
are shown with their actual D addresses in a read-only, scrollable table (at most
999 rows). A write ACK is expressly **not readback verification**: no automatic read
is issued, and the separately timestamped last-read table remains historical.

Reads can target configured numeric unicast IPv4 endpoints. **Writes require the
exact target `127.0.0.1`**: other targets disable Write with a localized explanation,
and `CommunicationRequest.validate()` repeats the restriction inside
`execute_request()` before constructing a socket. Even another loopback address
such as `127.0.0.2` is rejected. There is no override, target substitution or retry.
The unrestricted H1A low-level client API is unchanged; this manual-workspace
execution policy is a separate boundary in `communication/operations.py`.

Dispatch captures an immutable `CommunicationRequest` (including routing and word
values). One non-daemon **FinsCommunication** thread constructs, invokes and closes
one H1A client for that request. No communication queue or permanent polling thread
exists. Conflicting inputs/actions are disabled until actual thread exit. Navigation
does not abandon the thread; changing fields programmatically cannot redirect its
snapshot. This is separate from the ViMo worker and ProductionRuntimeSession.

The page reuses `FutureDelivery` with explicit queued Qt delivery and a 20-ms timer
that observes **thread termination**, not PLC connectivity. Future completion or
notification is insufficient: only after `is_alive()` is false does `join(0)`
release the thread and publish its result. Completion names distinguish consecutive
requests. Final `dispose()` requires no owned work, revokes delivery, and ignores
stale notifications; deleting an active workspace prematurely is unsupported.

Window close first uses the existing draft/job guards. Once approved it stops
communication admission and requests application shutdown independently. Active UDP
I/O finishes/reaches its bounded timeout; its client closes on the same background
thread. Qt never closes an active client concurrently or forcibly terminates the
thread. The window stays alive until **both** actual communication exit and the
existing application close/join condition. Final accepted close disposes delivery.
If application cleanup fails and keeps the shell open, the live communication page
resumes admission (still waiting for any active request); no unsafe thread is dropped.

Feedback identifies the current manual request, latest successful response with
endpoint/address/time/elapsed duration, and at most **100** compact log entries.
Only the latest read values are retained; no result/exception history is unbounded.
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

Do not start Production for this check. Open **Communication / 通信**. Keep target
`127.0.0.1`, port 9600 and D100. On a fresh simulator, Test read returns 1234. Write
4321, observe the ACK-only feedback, then explicitly Read and verify D100=4321.
For multiword review, write `0, 65535, 4321`, set Read words to 3 and read D100–D102.
Switch Chinese/English and page destinations; observations remain page-local.
Change target to `192.0.2.10` **without sending**: Write is disabled. This is only a
permission-display check, not a remote PLC test. Restore localhost for operations.

Stop the simulator with Ctrl+C, then Test read: observe a local timeout or OS
transport error, with Production unaffected. To review active close, choose an
unused localhost port and a few-second timeout, submit Read, then close the shell;
it stays responsive until actual communication exit. At smaller windows/expanded
advanced settings, scroll to feedback rather than squeezing controls. Verify on
native Windows DPI/touch settings; offscreen screenshots do not certify these.

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
worker-only client construction/use/close, malformed/timeout/PLC feedback, guarded
non-loopback writes, controlled remote-read parameter forwarding, duplicate admission,
navigation, actual thread/socket exit, application-close failure recovery, bounded
logs, stale delivery, UNCONFIGURED availability and bilingual layout/screenshots:

```powershell
uv run --no-sync pytest tests/unit/test_communication_operations.py tests/integration/test_communication_ui.py tests/integration/test_global_shell.py tests/integration/test_production_ui.py --tb=short
```

Remote-read forwarding is verified with a controlled client, **not actual equipment**.
H1C/hardware supervision, persistent profiles, automatic Production reporting and consumption handshakes
remain deferred; this workspace does not select their implementation.

**Not yet verified on NX102:** the specific CPU/version, FINS enablement and port
settings, IP/node/routing conversion tables, host source-address handling, network
filtering/write protection and actual allocated CJ-compatible DM/WR memory. W596
identifies built-in EtherNet/IP **port 2** for FINS and requires configured
CJ-compatible memory; it also cautions that future products may not support FINS.
This phase does not configure Sysmac Studio or send to equipment. It establishes
the local protocol subset, not hardware compatibility, machine safety, delivery
guarantees or Production integration.
