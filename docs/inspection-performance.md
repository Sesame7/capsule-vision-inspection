# Inspection performance — Phase 3F-1 baseline and Phase 3F-2A

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


## Capsule hotspot profiling — Phase 3F-2A

Phase 3F-1 was committed as `4446e03` before this work; its fixed six images,
model, saved MachineLayout and Recipe file hashes were verified unchanged.
Profiling/reference helpers and detailed intermediate artifacts are ignored under
`outputs/capsule_profiling/phase3f2a/`. No persistent instrumentation was added to
the production path. `profile.py` loads the committed `reference_capsule.py` and
uses explicit CPU-only scopes on the same 36 ROIs. It also times the real,
uninstrumented measurement function and compares complete scalar measurements.

The component scopes are HSV conversion (including the borrowed ROI slice),
foreground threshold, connected components, dominant-body selection/validation,
mask creation/erosion, usable-pixel selection/extraction, and circular Hue.
The original Hue scope includes its float conversion. Validation/result construction
is present in the uninstrumented total; the component probe omits these small
parts. Totals include the fixed 36-call loop. Inputs are decoded before timing.

Three 40-cycle original runs measured 133.83-138.61 ms mean for uninstrumented
36-ROI measurement. Component means per six-image workload were:

| Component | Original mean range, ms |
| --- | --- |
| Circular Hue | 89.77-97.39 |
| Connected components | 11.03-12.60 |
| Usable pixels | 9.10-10.34 |
| Mask/erosion | 4.73-5.29 |
| Foreground threshold | 4.64-4.93 |
| HSV | 4.24-4.94 |
| Body selection | 0.71-0.77 |

Each workload has 36 HSV scopes and 72 of each view scope (468 scopes total).
An empty scope cost about 0.98-1.13 microseconds including context-manager/recording
work, or about 0.46-0.53 ms for that scope count. Instrumentation perturbs execution;
independently collected total differences are not used to subtract or estimate its
cost. The uninstrumented before/after measurements are the local speed comparison.
OpenCV retained its existing 20-thread setting in these measurements and production;
no process-wide threading change was investigated or accepted.

The accepted optimization is confined to circular Hue: count 180 integer bins,
reduce reusable complex unit vectors once per bin, then select the two weighted
median ranks in circular order. It avoids per-pixel float/complex arrays,
exponentials and median partitioning. Numerical guards choose the original
pixel-order calculation at branch/tie boundaries; they are not new acceptance
tolerances. Generic floating `median_hue()` remains the original implementation.
The OpenCV uint8 output uses `astype(uint8, copy=False)` for precise typing, without
an image copy for this input contract. Body/mask/selection code and judgment rules
are unchanged.

Two 40-cycle optimized CPU runs measured 45.33-52.44 ms mean for the same 36-ROI
measurement. `histogram_default_*.json` retain raw component and total samples.
The intermediate real A/C check (10 warm-up + 50 measured cycles each) produced
A P95 41.28 ms and C P95 79.99 ms, with C traditional-stage mean 40.29 ms;
all results/cleanup were successful. This small check motivated stopping further
local changes and proceeding to the final repeated four-mode measurement.

Correctness includes every two-bin pair, weighted/random distributions, 0/179
wraparound, ambiguous distributions, magnitude/center numerical boundaries and
pixel-order permutations. Existing tests retain size/color tolerance boundaries,
missing/competing/truncated bodies, particles/dark edges, technical failures and
preview/production behavior. All 36 real ROI measurements and 144 complete
four-mode check results were exactly equal to the committed reference, including
measurement and reason evidence. No simplified color-only rejection behavior was
introduced.


### Final real production comparison

The accepted code SHA-256 is
`37a05a92fa2c8f2ea68c92fe744a13ba55af6c94cf68fe81221db0c20aa98a51`.
One final 30-warm-up / 300-measured / 3-repetition ABCD/BCDA/CDAB experiment
created `outputs/inspection_performance/20261008T160444_673911Z/`.
The unchanged benchmark generated `cycles.csv`, `report.json` and `report.md`;
the local wrapper added `final_lifecycle.json`, and the post-run audit generated
`comparison.json` with pooled before/after statistics and per-round details.
The UTC date is October 8 (October 9 in the local Asia/Shanghai timezone).

All 3,600 measured cycles and 360 separately marked warm-up cycles succeeded,
with zero technical/check errors or exceptional Futures. All 12 sessions stopped,
closed and terminated. The final process had no additional Python threads; OpenCV
remained at 20 threads. Source/configuration/input fingerprints were unchanged
through the experiment. Original and optimized scalar outcome distributions
(channel/check outcomes, reasons, measurement counts, judgments) were exactly equal.

Each mode contains 900 measured cycles. Units are ms:

| Mode | Mean | P50 | P95 | P99 | Traditional mean | >100 ms |
| --- | --- | --- | --- | --- | --- | --- |
| A | 38.19 | 37.82 | 41.65 | 45.39 | disabled | 0% |
| B | 85.59 | 84.86 | 95.29 | 99.60 | 46.37 | 0.56% |
| C | 86.60 | 86.17 | 93.34 | 95.27 | 47.38 | 0.22% |
| D | 52.19 | 52.56 | 56.78 | 59.43 | 47.38 | 0% |

Primary C comparison with Phase 3F-1:

| Metric | Original | Optimized |
| --- | --- | --- |
| Complete-cycle mean | 176.75 | 86.60 |
| Complete-cycle P50 | 178.27 | 86.17 |
| Complete-cycle P95 | 184.73 | 93.34 |
| Complete-cycle P99 | 190.08 | 95.27 |
| Traditional-stage mean | 136.30 | 47.38 |
| ViMo-stage mean | 34.76 | 34.01 |

C P95 by independent round was 86.73, 90.68 and 94.49 ms; B's last-round
P95 was 97.91 ms. The primary P95 <=100 ms comparison target is met, while the
preferred 70-80 ms margin is not stable. C had two >100 ms observations (maximum
117.81 ms); this is not a hard-deadline guarantee. Independent sample comparisons
and uncontrolled desktop/frequency/thermal effects remain limitations.

After the large Hue reduction (89.77-97.39 to 5.57-5.85 ms in CPU component
measurements), connected components and full-view mask/usable-pixel operations
are the strongest remaining traditional costs. Profiling did not establish a
cause for round drift. Further architecture or rejection-rule changes are not
justified by this result. A simplified color-only algorithm is not needed to
meet the current P95 target; any future relaxation of the two-body rule would
require its own agreed semantics. No additional optimization was accumulated.

Final gates: Ruff/format/strict Pyright clean; 495 tests passed, 10 opt-in GPU/full
reference tests skipped. The actual four-mode GPU experiment passed separately.
Historical 400-image and 30,000-call experiments were not repeated.
