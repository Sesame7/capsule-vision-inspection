# Inspection performance

This document owns **Current** performance measurement methodology: workload
identity, timing boundaries, report interpretation and verification. A latency
number applies to its measured workload and boundary; it is not interchangeable
with another scope or a guaranteed machine deadline.

[Inference and runtime](inference-runtime.md) owns serial execution, typed results,
resource ownership and shutdown. [Inspection](inspection.md) owns cycle/evidence
and Size/Color semantics; [application](application.md) owns activation, readiness,
accounting and local ingress. This document does not redefine those contracts.

The dated results are preserved in the accepted
[performance archive](archive/inspection-performance-records.md), summarized under
[Historical results](#historical-results). Current tool behavior below was checked
against the implementation; this reconstruction produced no new measurements.

## Workloads

### Measurement scopes

| Scope | Measured unit and boundary | Excluded work / comparison limit |
| --- | --- | --- |
| Historical raw ViMo SDK | Six sequential `module.run()` calls or one batch=6 call returning vendor responses | Prebuilt request construction, decoding, setup, warm-up, response inspection and response release excluded. No formal typed normalization or application ingress. |
| Typed inference wrapper | One synchronous `VimoSegmentationEngine.infer()` returning an owned `ImageResult` | Includes input validation/necessary contiguity normalization, vendor request/run and owned response conversion/copies. Decoding, engine construction/staging, explicit warm-up, result release and reporting excluded. |
| Production end-to-end cycle | Six identified images submitted through `LocalCycleIngress` until its terminal formal production result is obtained | Includes owned-frame copies, admission, scheduling/waiting, configured checks, mapping and delivery. Decoding, setup, warm-up and reporting excluded; no GUI rendering or real acquisition/PLC. |
| CPU capsule profiling | Defined CPU-only measurement loop over saved ROIs, or explicit operation scopes | Excludes GPU, decoding and serialization; measurement-only totals exclude judgment unless explicitly included. Instrumented scopes and uninstrumented totals are separate experiments. |

The raw SDK experiment is historical, not a supported batch API in the formal
engine. Its original timing loop is preserved in Git at `1386d0b`
(`scripts/benchmark_vimo.py`); do not restore or depend on that deleted tool.
[Phase 0 findings](archive/validation-evidence.md#serial-and-batch-observations) retain its serial/batch
observation. Raw SDK six-image latency is not typed-wrapper per-call latency,
and neither is complete production-cycle latency.

### Workload and invocation

The maintained [production benchmark](../scripts/benchmark_production.py) is
opt-in and Qt independent. It composes
`ProductionApplication -> ProductionRuntimeSession -> RuntimeCoordinator` with
the existing configured pipeline and worker. Supply existing, validated benchmark
configuration files and run from the repository root. This is a template; replace
the three configuration placeholders before running:

```powershell
# Run from the repository root.
$modelConfig = "<existing benchmark Model JSON>"
$machineConfig = "<existing MachineLayout JSON>"
$recipe = "<existing Recipe JSON>"
uv run --no-sync python scripts/benchmark_production.py --model-config "$modelConfig" --machine-config "$machineConfig" --recipe "$recipe" --frames-dir images --warmup 30 --cycles 300 --repetitions 3
```

For a short wiring check replace the counts with
`--warmup 2 --cycles 3 --repetitions 1`. Optional `--modes A B C E F` selects
a subset in supplied order; duplicates are rejected. Warm-up accepts zero;
cycles/repetitions must be positive. These are opt-in commands, not documentation
validation steps.

The former `outputs/manual_phase3c/` example was deliberately retired. The supplied
files must exist and satisfy the current codecs;
obsolete Recipe fields fail rather than being migrated. The script reads inputs
and makes mode changes only in memory; it never saves configurations.

Model/Recipe/MachineLayout CLI paths and `--frames-dir` are ordinary paths from
the working directory. A **relative model artifact inside the Model JSON** is
verified/prepared against the repository root (`base_directory=ROOT`); absolute
artifact paths are supported. This benchmark-specific convention differs from
normal `app.py` and the real-local UI launcher, which anchor artifacts to the
Model JSON directory. Relative Folder-source paths use their source JSON
directory; this fixed-image benchmark does not load saved Folder sources.
The current `data/models/model.json` uses `model_trt_fp16.vimosln` relative to its
own directory for normal startup; it cannot be used unchanged with this benchmark's
repository-root artifact base. Benchmark metadata must instead already contain an
absolute artifact path or a repository-root-relative path such as
`data/models/model_trt_fp16.vimosln`, with the verified identity/module fields.
No replacement benchmark configuration was created and no active JSON was changed
by the cleanup. The saved MachineLayout and Recipe can be supplied explicitly;
their geometry/policies are not automatically calibrated by this tool.
See [artifact paths](configuration.md#artifact-paths) and
[developer entry points](application.md#developer-entry-points).

Input selection is nonrecursive PNG/JPG/JPEG/BMP/TIF/TIFF, ordered by
`(filename.lower(), filename)`, not replay's natural numeric order. The first
six supported files are decoded once and mapped to the **configured camera
order**. Exactly six images and six cameras are required; an unreadable selected
file fails explicitly without replacement. Each saved camera layout is validated
against its image. Cameras need not have identical image dimensions. Arrays are
uint8 BGR, read-only and reused without resize, orientation change or ROI repair.
The normal ingress makes independent owned-frame copies **inside the primary
timer**. Do not mutate any input alias during submission.

### Current production modes

| Mode | ViMo | Size | Color |
| --- | --- | --- | --- |
| A | on | off | off |
| B | on | on | off |
| C | on | on | on |
| D | off | on | on |
| E | on | off | on |
| F | off | off | on |

`select_mode()` changes only enabled flags (ViMo for every configured channel,
Size/Color at capsule scope). Model/module, saved ROIs, thresholds, required
policies and label filters remain identical. No algorithm, queue-capacity or
ownership change is made by selecting a mode.

Every Current mode also executes always-on [Occupancy](inspection.md#always-on-occupancy).
End-to-end cycle timing includes that CPU cost even in A. Confirmed EMPTY skips
enabled traditional work; PRESENT/UNKNOWN retain the full-ROI capabilities below.
The input's Occupancy distribution therefore matters to the measured workload.
Per-camera `CameraTiming.occupancy_ms` is available independently; the existing
benchmark report's named traditional/ViMo/mapping fields do not separately export
this new stage, and residual time must not be interpreted as Occupancy alone.
Historical pre-E1 timings excluded this integration and are not paired comparisons.

For non-EMPTY channels, Current B executes Size geometry without HSV/Hue work. E/F execute whole-view
Color independently of body geometry; C/D execute both and reuse HSV where
needed. Disabled capabilities produce no fabricated measurements. Current
[Size](inspection.md#size) and [Color](inspection.md#color) are authoritative,
not historical body-interior Color. NG is valid inspected content, not execution
failure; do not adjust thresholds or replace images merely to force OK.

The original and Hue-optimized historical experiments used **A–D only**. Their
B/C/D shared measurement path differs from Current selective execution even
where enabled flags have the same names. The later 3F-2B experiment used A–F.
Do not retroactively add E/F or reinterpret old B as the current geometry-only
cost.

### Sessions and warm-up

Production defaults are **30 untimed cycles + 300 measured cycles per mode,
three repetitions**. Each mode/repetition creates a fresh disposable application,
session and engine. The worker creates, uses and closes the engine on its own
serial thread. The benchmark leaves runtime queue capacities unchanged and
waits for each cycle's terminal result before submitting the next: **one cycle
in flight**, no replay pacing/sleep, extra engine or concurrent GPU work.

Two different warm-ups precede measured cycles:

- **Readiness warm-up:** session startup executes two direct serial engine calls
  per distinct input shape, including D/F. It creates no production CycleId,
  result, judgment or counter increment. Initialization/staging and readiness
  are outside measurement; D/F then make no per-cycle ViMo calls.
- **Untimed benchmark cycles:** `--warmup` submits complete real production
  cycles in this disposable application. They do produce cycle results/counters,
  but rows are marked `phase=warmup` with no timing samples and are summarized
  separately. They are not readiness work or part of measured latency statistics.

Readiness, admission and terminal results are distinct observation boundaries;
see [readiness warm-up](application.md#readiness-warm-up) and
[runtime admission](inference-runtime.md#admission-and-futures).

Default mode order rotates deterministically **ABCDEF, BCDEFA, CDEFAB**.
A subset rotates in its supplied order. Rotation reduces a fixed order bias;
it is not a fully counterbalanced thermal study. Each session ends with Stop,
Close and actual application-thread join. A Future becoming done is not itself
proof of thread termination. Native work is never force-cancelled; blocked native
work can delay cleanup/CLI completion.

### Typed-wrapper workload

The maintained [wrapper benchmark](../scripts/benchmark_inference.py) uses one
directly owned serial engine and a fixed decoded pool:

```powershell
uv run --no-sync python scripts/benchmark_inference.py models/model_trt_fp16.vimosln images --pool-size 6 --warmup 20 --rounds 100
```

Defaults are six usable images, **20 individual warm-up calls** and 100 visits
to the entire pool: 600 measured per-image calls for a six-image pool. Unlike
the production benchmark, pool loading warns/skips unreadable files while
finding the requested usable count, then fails if that count is unavailable.
No six-camera layout or production pipeline is involved.

Each `perf_counter()` interval surrounds only `engine.infer(image)`.
Necessary response normalization and owned mask/geometry conversion are included;
the result is released after the timestamp and before the next call. The script
reports per-call statistics, not six-call round distributions. Do not divide or
multiply percentiles to claim an equivalent production distribution.

Selected CPU records and two oracle snapshots are preserved under
`outputs/retained-evidence/capsule_profiling/`. The old profiling runners and
intermediate experiments were retired; the retained snapshots are historical
evidence, not maintained executable tools. Their scopes, sample sizes and reference
identities are documented in the archive. Running old imports/oracles against
Current code is not automatically a valid reproduction.

## Timing boundaries

### Production metrics

| Metric | Meaning |
| --- | --- |
| `cycle_e2e_ms` | `perf_counter()` immediately before ingress submission to obtaining its formal result Future value. Includes ownership copies, admission, scheduling/waiting, inspection and formal result delivery. |
| `submission_ms` | Same start to ingress return after all admission acknowledgements. May overlap inspection. |
| `result_wait_ms` | Ingress return to result obtained. Not a pure queue/dispatch measurement. |
| `pipeline_ms` | Existing production `inspection_started_at -> result_ready_at` interval. Nested inside e2e, excludes ingress, coordinator dispatch wait and formal-result delivery. |
| `traditional_ms` | Sum of existing per-camera traditional-stage timings, only when Size/Color enabled. Includes shared measurement and judgment. |
| `vimo_ms` | Sum of existing per-camera inference intervals, including worker wait, SDK and normalization; not pure GPU kernel time. |
| `mapping_ms` | Sum of existing camera mapping/filtering/result-assembly durations. |
| `aggregation_ms` | Existing production aggregate timing; overlaps mapping. Never add both. |
| `attempt_ms` | Primary start to return or exception. Exceptions have no fabricated formal-result latency. |
| `dispatch_ms` | Null: coordinator queue/start timestamps are not exposed through the application result. |

Disk decoding, artifact hashing, engine construction/staging, readiness, untimed
cycles, report processing/serialization and GUI rendering are outside the primary
timer. `measure_cycle()` stops timing before converting the formal result into
scalar report data. There is no profiler on this production measurement path.

### Overlapping and unavailable stages

Per-camera values accompany stage sums. Disabled or unavailable stages are
**null, not zero**; one missing camera value makes its stage sum unavailable,
rather than a misleading partial total. Disabled traditional fast-path overhead
still exists inside e2e but is not labeled executed algorithm work. Camera
`total_ms` is not whole-cycle latency.

Submission can overlap inspection. Mapping and aggregation measure overlapping
assembly work; do not add them, add stage percentiles, or subtract nested values
to fabricate dispatch delay. On a formal result, submission and post-submission
wait partition that attempt's e2e wall-clock interval; their independently
calculated percentiles still do not add to an e2e percentile.

Runtime queue/start milestones exist in the coordinator envelope, but are not
exposed by the application production result used here. `dispatch_ms` therefore
remains unavailable. Collection deadlines and observer/join timeouts are
lifecycle limits, not measured performance thresholds; see
[runtime timeouts](inference-runtime.md#bounds-and-timeouts).

## Reports

Every production invocation creates
`outputs/inspection_performance/<UTC timestamp>/`:

- `cycles.csv`: streamed per-cycle scalar timings, phase/sample/mode/repetition,
  technical/product judgment, channel/check outcomes, reason/error counts,
  measurement count and exception text. Warm-up rows have no latency values.
  UTF-8 with BOM supports Chinese labels.
- `report.json`: environment/Python/package/GPU metadata where available,
  Git commit/status, benchmark and capsule source SHA-256, resolved base
  configuration and mode flags, configuration-file hashes, six file/pixel hashes,
  shapes and camera assignments, settings/order, per-session measured/warm-up
  statistics and completion/termination/lifecycle errors.
- `report.md`: per-session timing/stage tables and supported
  B-A, E-A, C-A, C-E, D-F comparisons. A comparison is omitted if either selected
  mode is absent.

The expected model SHA-256 is in the resolved Model definition and is verified
during preparation/startup. Command metadata that cannot be collected has a null
value plus `unavailable_reason`; do not fabricate a driver/source identity.
Git HEAD plus status does not archive the complete dirty source tree. Tool and
capsule hashes improve traceability, but are not a complete deployment manifest
or a lease preventing concurrent file edits.

Reports retain no source images, masks, contours, model bytes or full result
objects. CSV streams rows; only the current session's bounded `warmup + cycles`
scalar rows are retained, and completed sessions retain summaries. The
application's normal latest result is not a completed-result history. Growth in
scalar samples is explicit in the chosen cycle/repetition counts, not an
unbounded image queue.

Measured technical ERROR results remain in latency samples and are counted
separately. Optional check errors remain visible even if overall status is
SUCCESS; NG alone is not a technical error. Exceptional submission/result
attempts instead record traceback/`attempt_ms`, no fabricated `cycle_e2e_ms`,
and any observed submission interval. They abort the session/experiment **after
cleanup**, retaining partial reports. Initialization/lifecycle errors and actual
termination are recorded separately. Pre-session input/configuration failure
can occur before the output directory exists; not every CLI failure creates a
partial report.

A session's `completed` flag means its requested measured cycles and cleanup
finished; it does not mean every check succeeded. The CLI returns failure for
an incomplete session or measured technical/check errors, while retaining their
ordinary formal-result samples. NG alone does not change that exit status.

The wrapper benchmark writes `outputs/benchmark/<UTC timestamp>/benchmark.json`:
model/module/device/area-threshold and Python metadata, pool filenames,
warm-up/round settings, raw per-call `timings_ms` and statistics. It does not
capture the production report's six-camera assignment or complete configuration,
input/source/environment fingerprints. Preserve those separately when needed
for a reproducible wrapper comparison; do not infer absent provenance.

Historical `pooled_summary.json`, `comparison.json`, `final_lifecycle.json`
and profiling/guard outputs were post-run audit/helper artifacts, not additional
outputs promised by the Current benchmark CLI. The selected originals now live
under `outputs/retained-evidence/`; unselected experiments/derived summaries were
removed through scoped approval. Ignored status alone does not authorize deleting
retained evidence or active input configurations.

## Interpretation

### Statistics and comparisons

Current shared statistics use NumPy **linear** P50/P95/P99, mean, min, max and
population standard deviation (`ddof=0`), in milliseconds. `calls` denotes
sample count: individual calls in the wrapper report, cycles for production e2e,
or available stage observations for a stage. Empty/unavailable distributions
have zero samples and null metrics. Production summary rejects nonfinite timing
values; the shared helper otherwise filters nonfinite values.

`calls_per_second=1000/mean_ms` is a reciprocal of that distribution's mean,
not achieved acquisition/product throughput. Production `over_100_ms_percent`
uses **strictly greater than 100 ms**, with formal-result samples (including
ERROR) as denominator; exactly 100 ms is excluded.

Current production reports summarize **each mode/repetition session separately**.
They do not automatically pool samples across sessions. A separately audited
pooled summary must use measured CSV rows only, group identical mode/workload
identities, preserve sample counts and also show individual-session variation.
A pooled percentile is not the average of session percentiles.

B-A, E-A, C-A, C-E and D-F are descriptive differences of independently sampled
enabled-check workloads. Percentile differences are neither paired per-cycle
deltas nor additive operation costs. Stage means may suggest a follow-up CPU
probe; they do not identify which inner NumPy/OpenCV operation dominates.
Instrumented scopes perturb execution and omit/include different work; compare
separately measured uninstrumented totals before attributing an improvement.

A comparison must name its scope, enabled checks/algorithm semantics, exact
images/pixels and camera assignment, model/module, saved ROIs/Recipe policies,
warm-up/count/order and environment/source identities. Input/config hashes
record point-in-time identity; they do not restore a changed manual layout.
Independent sessions, cold/cache state, CPU/OpenCV scheduling, frequency,
thermal conditions and other desktop activity limit reproducibility. Avoid
heavy competing tasks, but do not claim those factors were controlled merely
because mode order rotates.

### Provisional software budget and limits

The report's **complete-cycle P95 ≤100 ms, preferred 70–80 ms margin** is a
provisional software comparison budget. It is not industrial acceptance,
a hard deadline, a calibrated tolerance or a performance guarantee. A P95 below
100 ms does not mean every cycle was below 100 ms.

Fixed local images may have unknown physical camera origins or repeated capsule
sets. These experiments do not establish real acquisition/PLC capacity,
physical capsule counts, product throughput, model accuracy or annotation quality.
Production counters represent admitted cycles/channel judgments, not physical
product counts. Current whole-view Color remains provisional/uncalibrated;
acceptance policy and hardware deadlines require separate evidence and decisions.

Future concurrency, real camera/PLC integration and hardware throughput work are
**unimplemented here**, not adopted by documenting these numbers. Current serial
thread topology/workload settings describe these tools and may only change
through a separately approved and verified implementation decision.

## Isolated S1-E1 scheduling experiment

[`experiment_inspection_scheduling.py`](../scripts/experiment_inspection_scheduling.py)
compares complete-group dispatch (A) with early per-camera dispatch (B), outside
Production. Both use the same resolved configuration and one `InferenceWorker`.
B projects each camera through the existing configured pipeline with temporary
local channel IDs, then restores global IDs; it does not introduce new algorithms.

```powershell
uv run --no-sync python scripts/experiment_inspection_scheduling.py --model-config data/models/model.json --machine-config data/machine/machine.json --recipe data/recipes/local.recipe.json --frames-dir images --warmup 3 --cycles 30 --repetitions 3
uv run --no-sync pytest tests/unit/test_scheduling_experiment.py --tb=short
```

Model artifact paths are relative to the model-definition directory. Inputs are
the first filename-ordered image in each configured camera's lowercase subfolder
(`cam1` through `cam6` for the local layout), decoded once. At each simulated
arrival an independent immutable Frame copy is timed separately. First/last-frame
latency starts at admission **after** that copy. Full-run completion spacing
includes copying, dispatch and diagnostic result signatures, but excludes setup,
decoding, hashing, warm-up and report I/O. At least one untimed A/B equivalence
probe runs per repetition even with `--warmup 0`.

Near-simultaneous and reversed staggered arrival plans use an open-loop 100 ms
trigger interval. Actual host release lag is recorded; Windows waiting and CPU/GIL
scheduling can coalesce nominal arrivals. The back-to-back saturated case estimates
software capacity on the same fixed workload. Interpret sustained rate from
`steady_completed_cycles_per_second`, not the shared statistics helper's nested
reciprocal-duration `calls_per_second` fields. Per-camera wait is derived from
public inference-start and CPU-check timings, not an exact dispatch trace.

Results under `outputs/s1e1_scheduling/<UTC timestamp>/report.json` contain input/
configuration hashes, per-camera semantic/area-and-bbox fingerprints, raw cycle
latencies and bounded-state observations. Masks/contours are not compared bitwise
or retained in the report. Command/completion queues each have one slot; pending
state is limited by `--capacity`, with at most six additional active image buffers.
Real runs use `--deadline-ms` (default 300), an **experimental** deadline with no
established PLC meaning. Separate virtual-time missing/delayed and overload cases
use explicit synthetic service times; their timeout-reject channel lists are target
diagnostics, not changes to Current Inspection or reporting judgments. Claimed
native work finishes before the two owned threads are joined. No camera access,
PLC requests, Qt rendering, GPU concurrency or Production scheduling changes occur.

This experiment cannot establish physical 10 Hz capacity, reliable hardware cycle
association, a result deadline or PLC consumption behavior. Measurements and the
S1-E2 recommendation belong to the dated run evidence, not a performance promise.

### Isolated S1-E2A field experiment

[`experiment_field_continuous.py`](../scripts/experiment_field_continuous.py) is a
separate, explicitly authorized field diagnostic, not a Production entry point.
`--help` is safe; `--execute-field` enables actual W10.7 photo/flash writes to the
fixed field endpoint. Fresh stopped/manual/released checks and exclusive camera
access are required. The helper uses bit access only, one persistent TCP owner per
trial, bounded pulse counts, and release-only recovery after uncertain exchanges;
it never retries activation or writes reject registers. Normal localhost write
restrictions remain unchanged.

Six cameras are armed once per finite trial with the existing settings and three
SDK buffers each. Retrieval/conversion/owned-copy/release uses the existing SDK
boundary; converter timing is a transparent observer. The already owned readonly
BGR array transfers directly into Frame without another full-image copy. One
serial engine overlaps inspection with the acquisition owner. A/B replays reuse
bounded captured pixels and their actual host-observed arrival trace on the field
GPU; they are replay measurements, separately from live B capture-plus-inspection.

Only one capture window may remain unresolved: all six prior frames plus an empty
SDK sweep are required before another pulse. Missing/duplicate/extra frames,
status changes, overload and ambiguous communication stop further activation.
This controlled-window rule is not a hardware trigger tag or proof of association
under an independently running PLC source. Reports deliberately mark all results
experimental/unaccepted for business output. The default 100 ms diagnostic result
deadline does not establish PLC consumption or physical reject timing.

The runner records raw command/ACK and camera observations, per-camera stage
timings, bounded backlog, sampled process memory, input-pool hashes and cleanup.
Post-run ownership probes time an optional extra copy on identical captured
arrays; these isolated costs are not claimed as end-to-end gains. New runs save
their Python bundle beside the report. Dated measurements belong to ignored field
evidence, not a general throughput promise. CPU-only checks:

```powershell
uv run --no-sync pytest tests/unit/test_field_experiment.py tests/unit/test_basler_acquisition.py tests/unit/test_fins_tcp.py tests/unit/test_scheduling_experiment.py --tb=short
```

## Historical results

**Historical evidence, not Current performance promises.** The accepted
[performance archive](archive/inspection-performance-records.md) retains all
10 measured tables, sample counts, source/configuration/input identities, evidence
paths and qualifications from the 2026-10-08 baseline and later profiling runs.

The baseline and Hue-optimized GPU records used shared body measurement and
eroded body-interior Color. The later selective-execution record intentionally
changed Color to independent whole-view screening. Its shorter A–F workload was
not paired with the earlier A–D measurements; the Hue equivalence checks do not
establish universal equivalence between those Color algorithms.

These finite software measurements do not certify accuracy, physical throughput
or hardware deadlines. Historical recommendations describe their dated context;
Current workloads, timing boundaries and verification remain above and below.

## Verification

For later implementation changes, the smallest maintained methodology checks
are opt-in developer commands from the repository root:

```powershell
uv run --no-sync pytest tests/unit/test_benchmark_production.py tests/unit/test_tools.py --tb=short
uv run --no-sync pytest tests/unit/test_capsule_optimization.py tests/unit/test_capsule_selective.py --tb=short
```

The [production benchmark tests](../tests/unit/test_benchmark_production.py)
use deterministic clocks and real application/worker composition with fake
engines. They assert six-mode settings preservation, input order/decoding failure,
primary-timer boundaries, null/overlapping stages, strict >100 ms accounting,
formal ERROR versus exceptional attempts, warm-up exclusion, single-thread
engine ownership, bounded mask retention, actual cleanup and read-only inputs.
[Tool tests](../tests/unit/test_tools.py) cover pool ordering and shared
statistics. [Hue tests](../tests/unit/test_capsule_optimization.py) cover
two-bin/weighted distributions and numerical fallback boundaries;
[selective tests](../tests/unit/test_capsule_selective.py) cover requested-only
work and intentional body-independent Color behavior.

These GPU-independent tests verify method/contracts, not a measured hardware
latency or production acceptance. Real wrapper/production benchmarks are
deliberately outside ordinary pytest. Historical full-dataset/stress evidence
is not rerun to validate this page; current optional inference regressions are
documented in [inference/runtime verification](inference-runtime.md#verification).

For Markdown-only reconstruction, statically inspect tool CLI declarations,
timers/report fields, regression assertions, links/anchors and retained raw
evidence; run `git diff --check`. Do not execute the opt-in commands or generate
fresh measurements merely to verify documentation. No timing threshold,
concurrency choice or algorithm change is approved by this document.
