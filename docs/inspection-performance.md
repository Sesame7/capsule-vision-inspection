# Inspection performance baseline — Phase 3F-1

`scripts/benchmark_production.py` is an opt-in, Qt-free fixed-image experiment.
It uses `ProductionApplication -> ProductionRuntimeSession -> RuntimeCoordinator`
and its existing configured pipeline/worker. No production code, algorithm,
queue capacity, engine ownership or judgment rule is changed.

## Workload and invocation

The first six supported files in the existing case-insensitive filename order are
decoded once; unreadable selected files fail explicitly. They map to configured
camera order. Saved MachineLayout bounds are validated for each image. Immutable
BGR arrays are reused; `LocalCycleIngress.submit()` performs its normal owned-frame
copies inside the primary timer. No resizing, image replacement or ROI repair.

```powershell
cd C:\Users\Sesame\Projects\vimo-demo
$config = "outputs/manual_phase3c"
uv run --no-sync python scripts/benchmark_production.py --model-config "$config/model.json" --machine-config "$config/machine.json" --recipe "$config/local.recipe.json" --frames-dir images --warmup 30 --cycles 300 --repetitions 3
```

For a short wiring check use `--warmup 2 --cycles 3 --repetitions 1`.
The model path is resolved against the repository root as in existing tools.
Recipes must use the current schema; old fields are rejected, never migrated.
The script reads the supplied files and never persists its mode configurations.

| Mode | ViMo | Size | Color |
| --- | --- | --- | --- |
| A | on | off | off |
| B | on | on | off |
| C | on | on | on |
| D | off | on | on |

Only enabled flags change in memory (ViMo on/off for all configured channels).
Model/module, ROIs, thresholds, required policies and label filters stay identical.
B and C share the same full capsule measurement, including Hue extraction even in
B; C-B describes adding Color judgment, **not** the full cost of computing color.
NG is valid product evidence, not an execution failure. No thresholds are tuned to
force OK and no bad content is replaced by synthetic measurements.

Defaults are 30 untimed complete warm-up cycles and 300 measured cycles per mode,
with three repetitions. Each mode/repetition owns one new application/session/engine
and ends with Stop, Close and actual application-thread join. Existing engine
startup still warms twice per unique image shape, including D; D has no per-cycle
ViMo calls. Engine startup/warm-up is outside measurement. Cycle warm-up uses real
cycles in the disposable application and is recorded separately, without latency
samples. It is never included in measured statistics.

Order rotates deterministically: ABCD, BCDA, CDAB. This records/reduces a fixed
ordering bias but is not a fully counterbalanced thermal experiment. There is one
cycle in flight, no playback sleep and no added concurrency. Avoid other heavy
CPU/GPU tasks while collecting comparable runs.

## Metric boundaries

| Metric | Meaning |
| --- | --- |
| `cycle_e2e_ms` | `perf_counter()` immediately before ingress submission to obtaining its formal result Future value. Includes ownership copies, admission, scheduling/waiting, inspection and formal result delivery. |
| `submission_ms` | Same start to ingress return after all admission acknowledgements. May overlap inspection. |
| `result_wait_ms` | Ingress return to result obtained. Not a pure queue/dispatch measurement. |
| `pipeline_ms` | Existing production `inspection_started_at -> result_ready_at` interval. Nested inside e2e, excludes ingress and result delivery. |
| `traditional_ms` | Sum of existing per-camera traditional-stage timings, only when Size/Color enabled. Includes shared measurement and judgment. |
| `vimo_ms` | Sum of existing per-camera inference intervals, including worker wait, SDK and normalization; not pure GPU kernel time. |
| `mapping_ms` | Sum of existing camera mapping/filtering/result-assembly durations. |
| `aggregation_ms` | Existing production aggregate timing; overlaps mapping. Never add both. |
| `attempt_ms` | Primary start to return or exception. Exceptions have no fabricated formal-result latency. |
| `dispatch_ms` | Null: coordinator queue/start timestamps are not exposed through the application result. |

Per-camera stage values accompany sums. A disabled or unavailable stage is null,
not zero; a missing camera timing makes its aggregate unavailable rather than a
misleading partial total. Disabled traditional fast-path overhead still exists
inside e2e but is not labeled executed algorithm work. Camera `total_ms` is not
used as full cycle latency. No subtractions manufacture missing dispatch metrics.

Disk decoding, artifact hashing, startup, warm-up, reporting/serialization and GUI
rendering are excluded from the primary timer. Conversion to scalar report data
happens after the end timestamp. No profiler is attached during the baseline.

## Outputs and interpretation

Each run creates `outputs/inspection_performance/<UTC timestamp>/` containing:

- `cycles.csv`: per-cycle scalar timing, judgment, channel/check counts, reason/error
  counts, measurement count and exception text. Warm-up rows are explicitly marked
  and have no latency samples. UTF-8 with BOM supports Chinese labels.
- `report.json`: environment/Git/tool identity, verified model hash, exact resolved
  settings, original configuration fingerprints, six file/pixel hashes and camera
  assignments, execution order, per-mode/per-round statistics and lifecycle status.
- `report.md`: concise cycle/stage tables and B-A, C-A, C-B comparisons.

Reports contain no source images, masks, contours, models or full result objects.
CSV streams one row at a time. Only the current session's bounded
`warmup + cycles` scalar rows are retained for summary statistics; completed sessions
retain summaries. The application's normal latest result is not a history buffer.

Means/P50/P95/P99/min/max/std use NumPy linear percentiles, in milliseconds.
The shared statistics field `calls` means sample count here; its reciprocal-rate
field is descriptive timing math, not equipment throughput. The >100 ms percentage
uses strictly greater than 100 and formal-result samples as denominator.
Formal ERROR samples remain in latency statistics and are counted separately;
exception attempts have separate durations/counts. Optional check errors also remain
visible even if another successful required check keeps overall status SUCCESS.
Exceptional submission/result delivery aborts the session/experiment after cleanup,
with the partial CSV/report retained. Ordinary technical result errors remain sampled.
Native work is never force-cancelled; blocked native work can delay CLI completion.

The provisional comparison is complete-cycle P95 <=100 ms, with 70–80 ms preferred
margin. Differences of independently sampled percentiles are descriptive, not exact
additive decompositions. Stage timing identifies where to investigate; it does not
prove which inner OpenCV/NumPy operation dominates. Any deeper profile/optimization
requires a separate experiment rather than contaminating this baseline.

These are fixed local images of unknown physical camera origin, potentially repeated
capsule sets. Results do not establish acquisition throughput, PLC capacity, model
accuracy or guaranteed physical machine cycle time.

## Verification

```powershell
uv run --no-sync pytest tests/unit/test_benchmark_production.py --tb=short
```

Tests use deterministic clocks for timing boundaries and real application/worker
composition with fake engines for ownership, warm-up exclusion and cleanup. They
cover all modes, strict workload identity, null stages, percentile boundaries,
NG/ERROR/exception accounting, bounded mask retention and read-only input artifacts.
The real benchmark is deliberately outside ordinary pytest.

## Measured baseline — 2026-10-08

Accepted production baseline: `2dc79ac9a1c538f633a0eceb9dd57b95b0dbe001`
(Phase 3E-2); the benchmark itself is identified by SHA-256 in its report.
Windows 10 / Python 3.14.7 / RTX 4060 Ti 16 GB, driver 616.92,
ViMo and custom SDK 3.20.0.2, NumPy 2.5.3, OpenCV 5.0.0.93.
Model SHA-256: `b2380a2c8714b36f0c045e2d79600ff3bed84f9a745822abe473830dd40d982b`.

The real run used `outputs/manual_phase3c/{model.json,machine.json,local.recipe.json}`
and the first six `images/` files, assigned CAM1 through CAM6, all 1056x960 BGR.
The report records the exact saved manual ROIs, Recipe values and image hashes;
these local files are not required by ordinary tests and may change after later
manual calibration. Their fingerprints were unchanged after this run.

Artifacts are local/ignored:
`outputs/inspection_performance/20261008T105206_936039Z/`.
`cycles.csv`, `report.json` and `report.md` are generated by the CLI;
`pooled_summary.json` was calculated afterward from the measured CSV rows for
this audit. It is a convenience summary, not an additional CLI output contract.

All 12 sessions completed and terminated. There were 3,600 successful measured
cycles, 360 separately recorded untimed warm-up cycles, zero technical/check
errors and zero exceptional Futures. All measured cycles had product judgment NG;
thresholds were not adjusted. Every B/C/D cycle executed 36 successful traditional
checks and returned 216 scalar measurements. A recorded 36 disabled traditional
checks; D recorded 36 disabled ViMo checks. This was real capsule execution.

Pooled statistics below contain **900 cycles per mode** (three independent
300-cycle sessions); milliseconds throughout:

| Mode | Mean | P50 | P95 | P99 | Min | Max | >100 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A: ViMo only | 38.80 | 38.56 | 41.86 | 44.06 | 36.33 | 48.69 | 0% |
| B: ViMo + Size | 171.94 | 176.27 | 185.27 | 187.33 | 152.11 | 210.49 | 100% |
| C: ViMo + Size + Color | 176.75 | 178.27 | 184.73 | 190.08 | 161.14 | 207.98 | 100% |
| D: Size + Color | 147.21 | 146.54 | 157.50 | 167.56 | 131.50 | 177.50 | 100% |

Independent-session variation must not be hidden by pooling. Each cell below is
**mean / P95**, in ms; actual execution order was ABCD, BCDA, CDAB:

| Round | A | B | C | D |
| --- | --- | --- | --- | --- |
| 1 | 38.98 / 41.02 | 156.58 / 162.35 | 171.00 / 184.29 | 142.21 / 150.52 |
| 2 | 39.41 / 43.24 | 176.80 / 183.39 | 178.04 / 182.47 | 146.38 / 156.77 |
| 3 | 38.02 / 40.14 | 182.45 / 187.00 | 181.21 / 186.22 | 153.04 / 160.28 |

| Mode | Traditional mean | ViMo mean | Mapping mean | Submission mean |
| --- | --- | --- | --- | --- |
| A | unavailable/disabled | 33.24 | 0.98 | 3.12 |
| B | 131.72 | 34.54 | 1.07 | 3.10 |
| C | 136.30 | 34.76 | 1.10 | 3.10 |
| D | 142.00 | unavailable/disabled | 0.74 | 3.10 |

Pooled mean differences are B-A **+133.14 ms**, C-A **+137.95 ms**,
and C-B **+4.80 ms**. Pooled P95 differences are respectively **+143.41 ms**,
**+142.87 ms**, and **-0.54 ms**. These are independent-distribution comparisons,
not paired or additive measurements. In particular C-B mean differences by round
were +14.42, +1.24 and -1.25 ms: they do not establish a stable Color-only cost.

**Budget result:** A is below both the 100 ms target and preferred 70-80 ms margin.
C exceeds 100 ms by 84.73 ms at pooled P95; B and D also exceed it. Traditional
processing is a meaningful added cost and the strongest measured bottleneck:
it occupies about 77% of C's mean e2e latency. ViMo, mapping and admission do not
explain the bulk of that cost.

**Next discussion candidate:** a narrow Phase 3F-2 investigation of the shared
`measure_capsule()` path across the 36 saved ROIs. First profile HSV conversion,
connected components, interior-mask preparation and circular Hue statistics
separately, then choose a change only with measured evidence and equivalence
checks. The current baseline does not identify which inner operation dominates;
no algorithm or runtime optimization was made here. CPU scheduling, frequency,
thermal state and other desktop activity were not controlled, and the observed
round drift cannot be assigned a cause from these measurements alone.
