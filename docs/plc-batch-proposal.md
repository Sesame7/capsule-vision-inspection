# Proposed W48/W49 batch synchronization — offline evaluation

**Retained historical offline experiment, not universal attribution evidence.**
The current [four-cycle reporting contract](communication.md#four-cycle-reporting)
owns formal W49/W149 register layouts and runtime behavior. The evidence and
unresolved equipment guarantees below remain applicable. This document and
`scripts/simulate_plc_batches.py` retain the original finite proposal, including
its experimental three-word CH30 mailbox. Current reporting writes only two CH30
bitmap words and leaves the third untouched. This proposal is not a deployment
procedure or authority for current defaults, modes or register ownership.
The reviewed physical process and target final group outcomes belong to
[Product Scope](product-scope.md#31-inspection-flow-and-accepted-baseline), not this
offline attribution experiment.

## Proposed mailbox contract

- PLC advances unsigned 16-bit W48 **before the first capture** of a physical batch
  and holds it across exactly four capture triggers. One trigger means one
  multi-camera acquisition cycle, not one camera frame.
- Four distinct, attributable terminal cycle results form one batch. Merge their
  existing `FirstStationReport.from_result(...).reject_words` by bitwise OR; do not
  reinterpret business statuses. Verified EMPTY can remain clear, while NG,
  UNKNOWN and unverified EMPTY retain the authoritative conservative projection.
- Stage the proposed W50–W52 bitmap, then commit W49 equal to that batch's W48.
  This is a **batch-number commit**, distinct from the default per-cycle
  attempted-result sequence.
  The experiment uses a fixed three-word proposed mailbox: 30 channels retain
  CH1–16 and CH17–30 in the first two words, with reserved high bits and W52 zero;
  36 channels use CH33–36 in W52 bits 0–3. Confirm W52 ownership for 30-channel
  machines before implementation: current CH30 reporting deliberately leaves W52
  untouched. No current writer or sequence allocator is changed.
- Counter skips and 65535 → 0 wrap are permitted; never fabricate skipped batches.
  Observed counter transitions identify separate local epochs, so later reuse of
  a numeric value is not merged with an older batch. Reset and unobserved full
  counter reuse are not detectable from two equal 16-bit reads.
- Missing entire triggers leave fewer than four results. An observed, attributable
  cycle with missing cameras or an ordinary inspection error still contributes
  its existing terminal reject bitmap. A missing frame is not a missing trigger.
  A known source-identity anomaly prevents attribution. Runtime exceptions with
  no terminal inspection evidence are not converted into a fabricated bitmap.
- Incomplete, duplicate, mixed-generation/layout or unattributable inputs produce
  diagnostic evidence, not submissions. There is no automatic all-NG batch,
  retry, PLC stop, W10.7 write or fabricated terminal cycle.

Readiness must precede the earliest allowed next W48 update. Both write stages
also need a bounded completion margin before that update if the mailbox is to
remain associated with the batch. A late/failed bitmap or commit is explicitly
unsuccessful in the experiment. Bitmap ACK at/after the cutoff prevents a proposed
W49 attempt; an already attempted late/failed commit remains uncertain. Nothing
retries it. Even two timely transport ACKs **do not prove PLC scan consumption**,
product tracking or ejector action.
After reset or counter reuse, an old W49 can already equal the new W48 before
any new bitmap is ready. The tests demonstrate this with a skipped/incomplete
intermediate report. PLC-side freshness/consumption semantics must distinguish
that stale equality; this proposal does not invent an acknowledgment or reset write.

## What the implementation actually observes

[`ContinuousBaslerSource`](../src/capsule_vision/application/continuous_basler.py)
uses connection-lifetime ImageNumber for cycle identity, checking consecutive
ImageNumber/BlockID per camera, expected common ordinal, complete prior camera
group and skipped-image/error metadata. It starts the 100 ms capture deadline at
the first valid frame; a newer ordinal cannot fill a missing peer. Observed
anomalies stop the source. None of those fields is an independent electrical
trigger ID or a PLC batch number. Host receipt time is not exposure time; camera
timestamps are not correlated to W48 by the current application.

The actual-source fake test demonstrates that one trigger missed by **every**
camera can leave three consecutive observed ordinals with no source anomaly.
Grouping every four observed ordinals consequently shifts across batch boundaries.
Connection/Start/Pause/Resume boundaries and intentionally discarded ordinals
also cannot supply an assumed PLC batch phase.

Normal missing-camera acquisition can fault before four cycles are collected.
The projection test with a partial fourth cycle does not claim the source can
continue accepting new groups after a missing peer. Existing partial-Step late
peer handling, source fencing and terminal delivery remain authoritative.

## Association experiments

| Candidate | Finding |
| --- | --- |
| Every four ImageNumbers, using a presumed initial W48 | Unproven phase; a wholly missed trigger shifts membership, and counter skips cannot be inferred arithmetically. |
| Last W48 reply before frame receipt | Can be a stale observation of the previous batch. |
| Asynchronous read requested on frame receipt | PLC sampling occurs somewhere between request and reply; a delayed image/read can observe the next batch. |
| Read after inspection | Can observe a newer W48 even for an old image. Processing completion is no attribution proof. |
| Equal reads bracketing a bounded exposure interval | Conditionally sufficient only with the external guarantees below; absent guarantees or a transition yields no association. |

An indistinguishable trace is included in the simulation output. Four captures
occur at 0, 60, 120 and 180 ms; software reads W48=7 at -220 ms and W48=8 at
220 ms. In history A, W48 changes to 8 at 190 ms, so all four captures belong to
7. In history B it changes at -10 ms, so all four belong to 8. Camera metadata,
arrivals and read replies can be identical. Both histories hold W48 during their
four captures and increment before the next batch's first capture. The initial
phase and update lead time have not been supplied. Even exact camera exposure
timestamps would not distinguish these two W48 histories.
For example, B's previous batch can have triggered at -380/-320/-260/-200 ms,
before these camera observations, leaving a 200 ms gap to the first observed
capture. A's next batch can start at 380 ms, also leaving a 200 ms gap. A minimum
inter-batch gap alone therefore does not resolve the unknown counter phase.

The conditional experiment assumes a certified maximum trigger-to-first-receipt
delay D, including transfer, SDK buffering and host dispatch. Receipt at A bounds
the trigger to [A−D, A]. A serial read whose **reply** is at/before A−D and a read
whose **request** is at/after A must observe the same counter epoch. Under a
no-counter-reuse guarantee, W48 then held throughout the uncertainty interval.
This uses request/reply intervals, not the simulator's hidden sample times.
The right reply can delay readiness beyond inspection completion.

Additional assumptions are explicit arguments, false/absent by default:

- All cameras in an ordinal share one physical trigger, with asymmetric trigger
  loss either excluded by a verified equipment guarantee or independently detected.
- No W48 reset/reuse can occur unnoticed inside an attribution window. Numeric
  equality alone is insufficient; the tests include an unobserved 7 → 8 → 7 case.
- The bound D is valid. Large/arbitrary frame delays cannot be repaired by choosing
  a newer W48. Reads are serial and non-overlapping in this experiment.

The merger consumes only proven associations, never the simulator's hidden PLC
timeline. It emits at most one proposal per observed epoch for exactly four
distinct results, irrespective of completion order. An unattributed capture in a
finite input trace blocks its proposals, because that trace cannot safely localize
which batch is affected. This conservative offline rule is not a proposed new
Production recovery policy.

## Timing and capacity findings

Illustrative captures are at 0/60/120/180 ms. For a 200 ms inter-batch capture gap,
the next first capture is 380 ms. Assuming W48 advances 10 ms before it gives an
illustrative report cutoff of **370 ms**. That phase/lead is an assumption to be
confirmed, not something the software learns from an ordinal.

The bounded FIFO timing model uses the existing 100 ms early-result deadline,
four active-cycle capacity and one serial camera inspector. All camera frames
arrive together in the table, with zero transfer/dispatch overhead. Service time
means the entire serial camera job, not merely a GPU kernel:

| Cameras | Per-camera service | Cycle terminal times, ms | Fully inspected cycles | Native owner finally free |
| --- | --- | --- | --- | --- |
| 5 | 10 ms | 50 / 110 / 170 / 230 | 4 | 230 ms |
| 6 | 10 ms | 60 / 120 / 180 / 240 | 4 | 240 ms |
| 5 | 15 ms | 75 / 150 / 220 / 280 | 2; last two expire | 285 ms |
| 6 | 15 ms | 90 / 160 / 220 / 280 | 1; last three expire | 285 ms |
| 5 or 6 | 20 ms | 100 / 160 / 220 / 280 | 0 | 280 ms |
| 5 or 6 | 450 ms | 100 / 160 / 220 / 280 | 0 | 450 ms |

Expiration preserves completed camera evidence and conservatively finalizes the
rest; it does not cancel a native call. The 450 ms call therefore blocks useful
work into the next batch despite earlier terminal results. The eight-capture
case at 0/60/120/180/380/440/500/560 ms expires all eight and retains native work
until 900 ms. This is not evidence of sustainable physical inspection throughput.

With constant cycle service C and no overhead, all four cycles finish before
their deadlines only if C < 70 ms (the fourth deadline is 280 ms). For continuously
repeated 60 ms triggers without a gap, no-backlog service instead requires C ≤
60 ms. Tested boundaries include 5×13 versus 5×14 ms and 6×11 versus 6×12 ms.
Real arrival skew, scheduling, copying and processing variation consume that margin.

At 60 ms spacing the model peaks at two live cycles; the four-cycle capacity is
not a throughput guarantee. Five admissions at 0/10/20/30/40 ms while native work
is held reject the fifth. Controlled tests exercise the actual coordinator's
default capacity, exact 99/100/101 ms completion boundary and retained native lease
through four 60 ms captures, not just the mathematical model.

If the fourth terminal result arrives at 280 ms, only 90 ms remains to the
illustrative 370 ms update, including attribution-read completion, reporting-owner
availability and both writes. A right-hand W48 reply at 365 ms makes even a 10 ms
bitmap stage late. Delayed frames can move batch readiness past the update entirely.
Transport failures and an occupied report owner are separate unsuccessful outcomes.
No queues or deadlines are enlarged by this work.

## Unresolved general-attribution requirements

Reliable general attribution is **not established** by the current signals.
The following requirements belong to the original offline proposal. The
[four-cycle reporting contract](communication.md#four-cycle-reporting) uses narrower,
explicit equipment assumptions; its implementation does not establish these
general guarantees:

1. Prefer a batch identifier plus capture index/trigger identifier latched with
   each exposure (or a trigger event independently correlated to every camera
   frame). Define loss, reset/reconnect and wrap behavior explicitly. This removes
   reliance on delayed host W48 reads for physical membership.
2. If polling alone is retained, certify common-trigger/no-asymmetric-loss behavior,
   a finite end-to-end frame-delay bound, adequate W48 update-to-first-trigger lead
   and minimum hold time, and read-latency/polling bounds that guarantee the equal
   bracketing observations for **every** cycle. Establish initial phase and exclude
   unnoticed counter resets/reuse. Arbitrary skipped counter values alone give no
   such guarantee. PLC cannot advance W48 before the agreed reporting window closes.
3. Specify the earliest next update and a worst-case budget for inspection,
   attribution reads, existing owner contention and both report stages. Measure it
   on target equipment. Clarify PLC consumption/acknowledgment and how an uncertain
   or absent batch report is handled; no stop/retry/ejection policy is supplied here.
4. Confirm the proposed fixed W50–W52 mailbox for CH30, W49 batch-commit semantics,
   initial W49 value, counter reset and same-value wrap/skip observability. Define
   operator Pause/Step/resume and interrupted batches without silently mapping them
   onto complete physical batches.

The OR projection and bounded scheduler tests support further design work, but
cannot establish general physical attribution, equipment throughput, reliable
half-capsule detection or pneumatic performance.

## Reproduce without equipment

```powershell
uv run --no-sync python scripts/simulate_plc_batches.py
$pytestBase = Join-Path "outputs/pytest" ([guid]::NewGuid().ToString("N"))
uv run --no-sync pytest tests/unit/test_plc_batch_simulation.py tests/unit/test_early_inspection.py tests/unit/test_continuous_basler.py --tb=short --basetemp $pytestBase
```

The script prints JSON and performs no file publication, socket operation, source
connection or native inference. Tests reuse synthetic business evidence, Basler
fakes and controlled clocks. Running this offline reproduction does not change
Production source, Folder Replay, reporting workers, sequence allocation,
persistence, History or UI.
