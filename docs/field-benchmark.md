# Backend benchmark diagnostics

The tools in [scripts/field_benchmark](../scripts/field_benchmark/) prepare and
observe a finite backend experiment. They do not add PLC triggering to
Production or change its early inspection, deadlines, reporting or archive
contracts. Live photo access requires an explicit diagnostic flag and is fixed
to `192.168.251.1:9600`; other non-loopback endpoints are rejected. Field execution
requires the readiness checks below. Local tests alone are not field acceptance.

## Finite live campaign

Run on the provisioned field repository on D:, with no other camera owner:

```powershell
uv run --no-sync python scripts/run_field_benchmark.py --campaign outputs/s1-p0b-2/campaign --stage single --model data/models/model.json --machine data/machine/machine.json --recipe data/recipes/local.recipe.json --sources data/sources/camera_sources.json --live-field-photo
```

After independently reviewing the completed stage, use `burst`, then `B`, `R`,
`A`, `RA` separately. Budgets are 1, 8, 80, 80, 80, 80 (329 total). Each stage
claims an exclusive directory; a failed/blocked stage cannot be replaced and
later stages require its successful reconciliation. There is no automatic
campaign escalation or retry. The runner freezes input hashes across stages.
It never loads saved Communication preferences. R/RA allocate a maintained
localhost UDP target; A/RA write all genuine categories under unique run roots.

The runner uses existing backend composition, explicit Connect, actual camera
dimensions/ROI validation, real engine initialization/warm-up and attached RUNNING
admission with zero counters before enabling stimulus. Fresh W15.0/manual,
W16.1/.2/stopped, W18 observation and W10.7/low checks precede activation. The
finite supervisor uses the established 20 ms hold and 10 ms lateness guard.
Unexpected pre-stimulus frames, runtime/identity/report/archive errors, telemetry
failure, RAM below 2 GiB, disk below 5 GiB or GPU temperature at least 60°C stop
further stimulus. These resource limits are experiment guards, not product limits.

Windows hosting uses a detached process/new process group with explicit job
breakaway and a bounded authenticated loopback channel. Hosting failure blocks
activation; there is no silent fallback. The common endpoint lock directory is
the campaign parent's `photo-locks/`. EOF/lease loss revokes further activation.
Actual process exit, final supervisor low verification and a separate final
readback are distinct observations. An external/uncooperative writer, supervisor
death, host power loss or total PLC network loss cannot be solved by this tool.

After the stimulus, the existing owners Stop/drain, Disconnect and Close. Native
initialization/inference is allowed to finish; no native process/thread is killed.
The sampler records approximately 1 Hz resource observations outside callbacks;
ledger footers record enqueue and writer wall time. Live analysis additionally
checks six cleanups, saved-definition integrity, SDK/admitted identities and
actual accounting. Command intervals/jitter, per-camera inference, cycle latency,
drain and encoded-byte throughput include observer overhead and are preliminary.

## Offline commands

From the repository root, inspect help or freeze definitions without starting
Production, opening cameras, loading ViMo or connecting to any endpoint:

```powershell
uv run --no-sync python scripts/field_benchmark_tool.py --help
uv run --no-sync python scripts/field_benchmark_tool.py prepare --mode RA --model data/models/model.json --machine data/machine/machine.json --recipe data/recipes/local.recipe.json --sources data/sources/camera_sources.json --simulator-port 19600
uv run --no-sync python scripts/field_benchmark_tool.py analyze "outputs/s1-p0b-1/prepared/RA-<run-id>"
```

`prepare` SHA-validates the selected artifact and uses the existing configuration
loaders/resolver/savers. It creates a UUID directory under
`outputs/s1-p0b-1/prepared/` by default. Model/module, algorithms, overrides, ROIs,
camera order and serial mapping are frozen; model and optional Folder references
become absolute. Original files are hashed before/after. Only archive settings are
overridden: B/R disable them; A/RA enable all four genuine statuses under that
run's new `archive/`. R/RA explicitly require localhost UDP reporting; saved
Communication settings and SavedImages are never loaded or edited. No model,
image, cache or license is copied/changed. Each run must retain its own manifest.
The example port must be replaced with the isolated target's actual allocated
port for execution. Analyze the actual completed run directory, not a fresh
configuration-only preparation; missing run evidence intentionally returns failure.

`analyze` writes one exclusive `reconciliation.json` and prints a concise summary.
It checks ledger footers, independent final release evidence and required actual
owner termination, as well as identity/count, bitmap/sequence and archive evidence.
It decodes one PNG at a time after drain to compare original BGR digests/bytes.
Missing/overflowed/partial evidence cannot become a clean result. Repeated analysis
does not overwrite prior evidence. Quantiles use `floor((n-1)*p)`, with observed
sample/missing counts; optional unavailable stage durations stay null.

## Trigger and release ownership

`BurstPlan` has explicit start/end, group/pulse budgets and a lateness guard.
Slots are `start + group*0.500 + offset*0.060`, offsets 0–3. `Controller.run`
records the complete plan, waits against local monotonic deadlines, heartbeats
and sends one request at a time. Cancellation, logging pressure, excessive
lateness or an unsuccessful pulse ends admission; no catch-up or replacement
pulses exist. Hold time is an explicit input less than 60 ms, not an electrical
recommendation. Host send/ACK/readback times do not establish exposure edges.

`start_supervisor` starts a separate process with a private pipe. An OS endpoint
lock and unique run identity exclude a second supervisor using the **same
host-wide lock directory**. That directory must be stable across runs, never a
per-run directory. The supervisor is the only FINS command/socket owner. The
controller has no PLC socket; this serial arbitration eliminates competing
activation during release takeover. Ready acknowledgment, a valid finite lease,
unexpired run/configuration/generation/source receipt, exact next slot and budgets
are all required for activation. Heartbeats cannot extend the run end/readiness
deadline. Receipt flags represent explicit evidence, not automatic commissioning.

Only W10.7 bit commands are exposed: `31 00 0a 07 00 01` plus one 00/01 data byte,
using the maintained FINS/TCP framing, routing and response validation (Omron
W342 memory bit commands; W421 TCP framing). No whole-word RMW is used.
The independent owner bounds every connect/handshake/exchange, pulse hold and
lease wait. It releases after each pulse; pipe interruption/EOF, invalid requests,
missed heartbeat or command uncertainty irrevocably revoke activation. Recovery
opens a separate **zero-only** connection, once, then verifies low. No activation
retry is permitted. An unresolved release remains explicit even if sockets close.
`supervisor-final.json` is release evidence; only the caller's successful
`join_supervisor` proves actual process exit. A join timeout retains uncertainty.

Local tests terminate only a synthetic controller subprocess while the controlled
peer holds its activation ACK. They verify surviving release and exclusion,
lease expiry, ACK loss, delayed/rejected release and subsequent socket cleanup.
The detached-hosting test also verifies actual subprocess exit after controller
channel loss. Run it in the actual Windows/SSH environment before live activation;
neither test proves survival of host power loss, supervisor death or total PLC
communication failure. An operator/PLC-side fallback remains necessary for those
conditions.

## Production observations

`Observations.composition_scope()` is explicit, process-local diagnostic
instrumentation around existing composition classes, installed **before** owner
creation and removed **after** all joins. It is not for a simultaneous Qt session.
Use existing `ui.local_production.compose` with the prepared paths, explicit
`reporting=RunFiles.reporting` and `engine_factory=obs.engine(real_engine)`.
It constructs no alternate application, acquisition source, runtime or engine
owner. Importing the composition uses existing Qt classes, but no widgets, Qt
event loop or Web owner are started by these diagnostic adapters.

Observers record SDK metadata and configured serial→CameraId under an explicit
connection epoch, full admitted FrameIds/shapes, cycle admission/terminal failures,
six-camera/36-channel technical and business results, actual per-camera timings,
early queue/peak snapshots, report eligibility/attempt/initial sequence/wire order/
terminal observations, camera cleanup and actual join outcomes. Engine-call
durations include warm-up and have no inferred FrameId; attributed production
timings come from camera results. Warm-up must be separately excluded at readiness.

`IsolatedTarget` only wraps the maintained localhost **UDP** simulator with a
finite request budget, accepted W49 memory-write callback and actual socket join.
Its event is not client ACK, PLC consumption or mechanical rejection. The real
reporter still sends W50–W52 before W49 once per eligible cycle, without retries.
The observer rejects non-localhost/non-UDP reporting configuration.

Archive instrumentation uses the existing writer and limits. It records eligible
result/switch candidates, retain attempts, aggregate drops, writer success/failure,
actual output path/encoded bytes and original pixel digest. Eligibility alone
does not prove retained pixels or a queued write. The existing drop hook has no
FrameId; it is logged as unavailable rather than assigned to a camera by proximity.
Missing candidates and aggregate drops invalidate clean reconciliation. Archive
queue/byte snapshots are samples, **not true peak measurements**. PNG hashing and
file observation run on the existing archive writer, so their measurement overhead
must be disclosed. Inference/completion callbacks never encode or write files.

The scalar ledger defaults to 1,024 pending events and 100,000 total events,
bounded text/tuple sizes, and one writer. No Frame, ndarray, mask, SDK object or
traceback is retained. Overflow/write failure sets `abort`; the controller must
share this ledger/abort boundary and stop further stimulus. Closing after owners
terminate drains accepted events and writes an independent footer. Post-run
analysis is also bounded. Device timestamps stay raw; there is no cross-clock
latency subtraction, timing-based frame association or physical-product count.

## Before any field experiment

1. Review the explicit diagnostic live endpoint path. Reconcile deployed revision and hashes without
   changing saved configuration, models, runtime or camera settings.
2. Establish independent field-local supervisor hosting, common lock directory,
   no competing owner/writer, tested release fallback and finite campaign budget.
   Check current resources/background load/storage and logging reserve.
3. Obtain fresh read-only W15.0 manual, stopped W16/relevant W18, W10.7 low and
   released HMI/no-competing-writer evidence. Qualify permitted hold/release bounds;
   previous observations and receipt booleans alone are insufficient.
4. Connect through the existing source owner, validate actual dimensions/ROIs,
   then complete real serial initialization/warm-up and attached RUNNING admission.
   Record generation/epoch/configuration identity and zero warm-up counters/results.
5. Start/verify the isolated UDP target and unique run/archive roots. Assert the
   saved field TCP reporting endpoint cannot enter any mode. Preserve all current
   capacities and 100 ms deadlines. Register complete shutdown observations.
6. Review one-pulse, then bounded small-burst evidence before separately proceeding
   to larger load modes. Any uncertainty, identity gap, capacity/drop/error,
   deadline miss or incomplete shutdown stops progression; no automatic escalation.

After admission stops, drain accepted results/reports/PNGs through current owners,
Stop→READY, explicitly Disconnect/Close persistent cameras, join all owners and
verify final low. A result or READY alone is not SDK/native/thread termination.
Backend-only evidence makes no Qt/Web or field-TCP performance claim. Current
owning contracts remain [architecture](architecture.md),
[configuration](configuration.md) and [communication](communication.md).
