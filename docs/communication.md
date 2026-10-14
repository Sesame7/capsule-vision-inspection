# FINS/UDP communication

## Current scope and authority

`vimo_demo.communication` is a standalone, synchronous, standard-library protocol
library. It has no Qt, ViMo, Production, configuration-persistence or real-equipment
integration. The Qt Communication workspace uses this library for explicit manual
operations in a separate background context. PLC triggering, result handshakes and
hardware commissioning are **not implemented**.
Transport/protocol errors are not inspection outcomes or product judgments.

The protocol authority is Omron's [NX FINS manual W596-E1-06](https://www.omron-ap.com/data_pdf/mnu/w596-e1-06_nx701_nx502_nx102.pdf?id=3900).
Its sections 1-1 and 4-1 refer frame formats/end codes to
[communications reference W342-E1-18](https://assets.omron.eu/downloads/latest/manual/en/w342_cs_cj_cp_nsj_communications_commands_reference_manual_en.pdf).
Implementation and literal test vectors use W342 sections 3-3, 5-1-3, 5-2-1/2 and
5-3-2/3; W596 sections 1-4 and 2-2 establish NX transport/command support.
Local client/simulator agreement alone is not evidence of standards compliance.

## Supported subset

| Item | Current contract |
| --- | --- |
| Transport | Numeric unicast IPv4, UDP port 9600 by default; one response-required request at a time |
| Addressing | Explicit destination/source `FinsAddress(network, node, unit)`; network 0–127, unicast node 1–254, unit encoded as a byte 0–255 |
| Memory | DM/D **words** only, area code `82`, D0–D32767, bit address zero |
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

The simulator binds **only** `127.0.0.1`, owns a finite D0–D32767 map initialized
to zero except D100=1234, and has no scan/ladder/physical-I/O behavior. Writes are
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
| `1101` | Area not implemented by this DM-only simulator |
| `1103` | Invalid DM starting word/bit address |
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

Workspace tests separately cover real localhost UDP read/write/explicit readback,
worker-only client construction/use/close, malformed/timeout/PLC feedback, guarded
non-loopback writes, controlled remote-read parameter forwarding, duplicate admission,
navigation, actual thread/socket exit, application-close failure recovery, bounded
logs, stale delivery, UNCONFIGURED availability and bilingual layout/screenshots:

```powershell
uv run --no-sync pytest tests/unit/test_communication_operations.py tests/integration/test_communication_ui.py tests/integration/test_global_shell.py tests/integration/test_production_ui.py --tb=short
```

Remote-read forwarding is verified with a controlled client, **not actual equipment**.
H1C/hardware supervision, persistent profiles, PLC mapping and production handshakes
remain deferred; this workspace does not select their implementation.

**Not yet verified on NX102:** the specific CPU/version, FINS enablement and port
settings, IP/node/routing conversion tables, host source-address handling, network
filtering/write protection and actual allocated CJ-compatible DM memory. W596
identifies built-in EtherNet/IP **port 2** for FINS and requires configured
CJ-compatible memory; it also cautions that future products may not support FINS.
This phase does not configure Sysmac Studio or send to equipment. It establishes
the local protocol subset, not hardware compatibility, machine safety, delivery
guarantees or Production integration.
