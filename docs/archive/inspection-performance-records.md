# Inspection performance records

**Historical — frozen measurements, not Current performance promises.** This
archive was consolidated on 2026-10-12 from the accepted historical section of
`docs/inspection-performance.md`, read alongside its retained local artifacts.
The source checkpoint was `8fad326aaaf5e0e981224963607a7e000eb95604`;
the reconstructed source page read for this migration had SHA-256
`34ed26c1d3d796412a4cbabb4d1e9ee22edb255a465791fc3ebd12c86c224ed6`.

Current tooling/methodology belongs to [inspection performance](../inspection-performance.md);
algorithm meaning belongs to [inspection](../inspection.md). This archive preserves
all **10 historical tables**, with their original values/units and sample scope.
It is not a new benchmark, an execution plan or a parallel Current contract.

## Provenance and measurement scope

Three GPU record groups were independently collected on 2026-10-08 UTC (the later
groups fell on October 9 in Asia/Shanghai). The report Git IDs were checkpoints,
not claims of clean committed experiment trees:

- **Original A–D baseline:** `2dc79ac9a1c538f633a0eceb9dd57b95b0dbe001`;
  benchmark code was untracked in that report, identified by tool SHA-256
  `be0ff29b44d6a2ff326799dde65af68f316b0e4479db5c7f357d3a79a30253ce`.
- **Hue-optimized A–D comparison:** `4446e03d02db17f40180cfb099c5124c50bc5dcd`;
  capsule code was modified. Its actual accepted source hash was
  `37a05a92fa2c8f2ea68c92fe744a13ba55af6c94cf68fe81221db0c20aa98a51`,
  recorded in `final_lifecycle.json`/`comparison.json`, not a missing
  `report.json` field. The benchmark hash was unchanged.
- **Selective A–F short run:** `7e0496c4ee7ac999e69c40f428955a223569f9f2`
  with working-tree edits; the report recorded capsule SHA-256
  `3d531726fceb803c4365fddb728e07a862971734abd8c870196b09c887075b0b`
  and tool SHA-256
  `641982085943ea2bfc54fa5c7f5e10d444797160a8c209a6d11660fa595d4456`.

The reported environment was Windows 10 build 19044, Python 3.14.7 x64,
RTX 4060 Ti nominal 16 GB (NVIDIA tool reported 16380 MiB), driver 616.92,
ViMo inference/custom 3.20.0.2, NumPy 2.5.3 and OpenCV 5.0.0.93.
The model, six ordered inputs and saved configuration fingerprints matched across
these three record groups. Exact identities appear below and in the reports;
no equality with arbitrary later manual edits is implied.

A cycle meant six configured camera frames and 36 channel ROIs. Workloads were
A: ViMo; B: ViMo + Size; C: ViMo + Size + Color; D: Size + Color; later
E: ViMo + Color and F: Color. The first two GPU groups used the historical
**shared body-measurement path**, including Hue in B and eroded body-interior
Color. The last used **selective execution and intentional whole-view Color**.
Hue-only equivalence evidence did not establish equivalence of those Color rules.

Historical GPU end-to-end timing ran from immediately before `ingress.submit`
through terminal formal result retrieval. It included owned-frame copies,
admission, configured checks, worker wait/inference/normalization and result delivery.
Decoding/configuration/model setup, readiness, untimed benchmark cycles, printing
and serialization were outside measured cycle latency. Reports specified two direct
engine-readiness calls per distinct shape separately from real untimed warm-up cycles.

ViMo-stage values included worker wait/SDK/normalization, not just kernels.
Submission overlapped inspection; aggregation overlapped mapping; independently
reported percentiles were not additive. Disabled/unavailable stages stayed null
or explicitly unavailable, not measured zero; dispatch was unavailable, not inferred
by subtraction. Formal ERROR results were eligible timing samples, while exceptional
attempts were separate; the accepted measured datasets below had neither.
The later report retained a generic “shared measurement and judgment” stage
description: it did not negate that run's recorded selective source/Color semantics.

CPU probes decoded before timing and excluded GPU, judgment where stated and report
serialization. They were separate scope/total measurements, not GPU e2e equivalents.
Percentiles used linear interpolation; “>100 ms” excluded exactly 100 ms.
Independent runs, differing sample sizes/Color rules and uncontrolled desktop,
frequency/thermal factors prevented paired or physical-throughput conclusions.
The **100 ms P95 / preferred 70–80 ms margin** was a provisional software comparison
criterion, not machine acceptance, safety or a guaranteed deadline.

## Measurements

### Measured baseline — 2026-10-08

**Historical:** original fixed-workload A–D production measurement, before the
Hue optimization and the later selective-execution/Color change.

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

**Historical software-budget comparison:** A is below both the 100 ms target and preferred 70-80 ms margin.
C exceeds 100 ms by 84.73 ms at pooled P95; B and D also exceed it. Traditional
processing is a meaningful added cost and the strongest measured bottleneck:
it occupies about 77% of C's mean e2e latency. ViMo, mapping and admission do not
explain the bulk of that cost.

**Dated recommendation (superseded by the investigation below):** a narrow Phase 3F-2 investigation of the shared
`measure_capsule()` path across the 36 saved ROIs. First profile HSV conversion,
connected components, interior-mask preparation and circular Hue statistics
separately, then choose a change only with measured evidence and equivalence
checks. That baseline did not identify which inner operation dominates;
no algorithm or runtime optimization was made here. CPU scheduling, frequency,
thermal state and other desktop activity were not controlled, and the observed
round drift cannot be assigned a cause from these measurements alone.


### Capsule hotspot profiling — Phase 3F-2A

**Historical:** the 2026-10-08/09 investigation of the then-shared body-measurement
path. Its scoped probes and oracle are local evidence, not Current benchmark
instrumentation or the whole-view Color algorithm.

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

The accepted optimization was confined to circular Hue: count 180 integer bins,
reduce reusable complex unit vectors once per bin, then select the two weighted
median ranks in circular order. It avoided per-pixel float/complex arrays,
exponentials and median partitioning. Numerical guards chose the original
pixel-order calculation at branch/tie boundaries; they were not new acceptance
tolerances. Generic floating `median_hue()` retained the original implementation.
The OpenCV uint8 output used `astype(uint8, copy=False)` for precise typing, without
an image copy for this input contract. Body/mask/selection code and judgment rules
were unchanged.

Two 40-cycle optimized CPU runs measured 45.33-52.44 ms mean for the same 36-ROI
measurement. `histogram_default_*.json` retain raw component and total samples.
The intermediate real A/C check (10 warm-up + 50 measured cycles each) produced
A P95 41.28 ms and C P95 79.99 ms, with C traditional-stage mean 40.29 ms;
all results/cleanup were successful. This small check motivated stopping further
local changes and proceeding to the final repeated four-mode measurement.

Correctness evidence covered every two-bin pair, weighted/random distributions, 0/179
wraparound, ambiguous distributions, magnitude/center numerical boundaries and
pixel-order permutations. Reported checks covered size/color tolerance boundaries,
missing/competing/truncated bodies, particles/dark edges, technical failures and
preview/production behavior. All 36 real ROI measurements and 144 complete
four-mode check results were exactly equal to the committed reference, including
measurement and reason evidence. No simplified color-only rejection behavior was
introduced.


#### Final real production comparison

**Historical:** the final Hue-only before/after comparison retained the old A–D
workload. The report's Git identity names the checkpoint; the actual optimized
capsule source hash below is recorded in `final_lifecycle.json` and
`comparison.json` (the older `report.json` had no `capsule_sha256` field).

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

### Selective execution and fast Color — Phase 3F-2B

**Historical:** the later A–F short run deliberately changed Color semantics.
It is retained as its own workload, not as an equivalence-preserving extension
of the Hue-only comparison or a measurement of the current working tree.

Accepted Phase 3F-2A baseline `7e0496c` was committed before edits. Original
reports above remain historical evidence. The later work intentionally changed Color
from eroded body-interior sampling to independent view screening while preserving
Size and ViMo execution. Current Color semantics are maintained in the
[inspection contract](../inspection.md#color), not this experiment record.

#### CPU reference and local choices

Ignored artifacts are under `outputs/capsule_profiling/phase3f2b/`.
`reference_capsule.py` is the committed 3F-2A oracle; `selective_initial.py` freezes
the correct independent implementation before local mask optimization.
`compare_cpu.py selective_final_cpu` decodes outside timing and measures the same
36 saved ROIs. Each entry had 5 untimed warm-ups and 25 complete CPU measurement
loops; timings exclude judgment, GPU, decoding and serialization. Actual saved
layout/Recipe and six input fingerprints agree with the previous GPU report.

| 36-ROI measurement | Initial selective mean / P95, ms | Final mean / P95, ms |
| --- | --- | --- |
| Size only | 18.13 / 19.05 | 18.25 / 18.45 |
| Color only | 15.22 / 17.28 | 13.36 / 17.21 |
| Combined | 38.04 / 39.78 | 33.40 / 34.62 |

Committed shared measurement reruns ranged 46.18–51.58 ms mean (53.54–58.43 ms
P95); the earlier preliminary local run was 54.49 ms mean. These are independent,
short CPU distributions, not paired operation costs or acceptance throughput.
Raw samples and source fingerprints are in `selective_initial_cpu.json`,
`selective_inRange_cpu.json` and `selective_final_cpu.json`.

Accepted local changes:

- Size-only uses exact BGR `inRange`/inversion for V >20, with no HSV or Hue work.
  Compared exact foreground variants; NumPy axis reduction was much slower and
  was rejected. Dominant component selection/Size rules are unchanged.
- Color creates one whole-ROI HSV usable mask with `inRange`, then counts/selects
  each view. The 72-view mask/count/Hue-selection probe (50 samples, HSV precomputed)
  measured NumPy 7.69–8.02 ms mean versus OpenCV 5.94–6.18 ms.
- Combined Size reuses Color's HSV Value. A 50-sample foreground-only probe measured
  BGR range 5.01 ms, HSV range 3.89 ms, HSV NumPy threshold 3.94 ms, and
  `extractChannel` + binary threshold 1.67 ms. The exact last variant was retained
  only when HSV is already needed. No global OpenCV thread setting changed.

The final measurements equaled the initial selective oracle for every real ROI.
All 72 real Size view boxes/reasons and 36 Size judgments matched the committed
reference; comparison omitted historical Hue evidence intentionally absent in
Size-only. `synthetic_semantics.json` additionally compares valid, missing,
competing, truncated and invalid-geometry Size cases. New Color passed the
bright truncated/competing synthetic cases that historical Color rejected. On these six
real images, all 72 representative Hues happened to agree; this does not establish
general equivalence of the intentionally changed pixel-selection semantics.

#### Short real production-path measurement

Historical invocation from the repository root (a new run with Current source/configuration would not reproduce the old fingerprints):

```powershell
$config = "outputs/manual_phase3c"
uv run --no-sync python scripts/benchmark_production.py --model-config "$config/model.json" --machine-config "$config/machine.json" --recipe "$config/local.recipe.json" --frames-dir images --warmup 10 --cycles 60 --repetitions 2
```

All six modes were retained to include the D-F comparison. For the focused five-mode
experiment append `--modes A B C E F`. This run used ABCDEF then BCDEFA, with
120 measured cycles per mode, the same six images/configuration/model as Phase
3F-2A, and existing default queue capacities. The SDK/driver/environment versions
remain as recorded above. Initialization and untimed warm-up remain excluded.

Artifacts: `outputs/inspection_performance/20261008T170442_094840Z/` contains
`cycles.csv`, `report.json`, `report.md` and the post-run pooled `comparison.json`.
The local guard recorded `outputs/capsule_profiling/phase3f2b/gpu_lifecycle.json`.
All 720 measured cycles and 120 untimed warm-up cycles succeeded, with zero
technical/check errors or exceptional Futures. All 12 sessions terminated, no
additional Python threads remained, OpenCV stayed at 20 threads, and inputs,
manual configuration and production source fingerprints remained unchanged.

| Mode | Mean | P50 | P95 | P99 | Traditional mean | >100 ms |
| --- | --- | --- | --- | --- | --- | --- |
| A: ViMo only | 38.13 | 37.54 | 43.04 | 43.87 | disabled | 0% |
| B: ViMo + Size | 56.37 | 55.90 | 60.79 | 62.22 | 18.53 | 0% |
| C: ViMo + Size + Color | 70.03 | 69.59 | 73.29 | 76.23 | 31.65 | 0% |
| D: Size + Color | 38.56 | 38.31 | 40.79 | 43.68 | 33.31 | 0% |
| E: ViMo + Color | 54.47 | 53.45 | 60.18 | 62.74 | 15.30 | 0% |
| F: Color only | 19.02 | 18.81 | 21.04 | 25.15 | 14.22 | 0% |

| Comparison | Mean difference, ms | P95 difference, ms |
| --- | --- | --- |
| B-A: Size enabled | +18.24 | +17.75 |
| E-A: Color enabled | +16.34 | +17.15 |
| C-A: both enabled | +31.90 | +30.25 |
| C-E: added Size | +15.56 | +13.10 |
| D-F: added Size without ViMo | +19.53 | +19.75 |

These compare different enabled-check workloads and independent distributions;
percentiles and stage metrics are not additive. B/C/D formerly all generated
216 scalar measurements including Hue. Now B generates 144 dimensions, E/F 144
usable-count/Hue values, and C/D 288 combined values when every view is valid.
No disabled feature fabricates measurements. All F results were OK; the other
modes were NG under the unchanged settings/input content. NG is not a test error.

"Current short run" in the retained table means the dated 3F-2B run, not a new measurement of today's working tree.

| Complete-cycle comparison | Phase 3F-2A mean / P95, ms | Current short run mean / P95, ms |
| --- | --- | --- |
| A | 38.19 / 41.65 | 38.13 / 43.04 |
| B | 85.59 / 95.29 | 56.37 / 60.79 |
| C | 86.60 / 93.34 | 70.03 / 73.29 |
| D | 52.19 / 56.78 | 38.56 / 40.79 |

Earlier rows have 900 samples per mode versus 120 in this 3F-2B run; desktop/thermal effects
were uncontrolled and Color semantics differ. The 3F-2B C P95 was 26.71 ms below
100 ms and lies in the preferred 70–80 ms interval. Its two round P95 values were
74.02 and 72.72 ms; maximum was 77.41 ms. This is useful short-run evidence, not a
hard deadline or production-throughput guarantee. No full repeated GPU benchmark,
400-image regression or 30,000-call soak was rerun.

The remaining dominant costs are six serial ViMo intervals (C mean 32.88 ms)
and traditional geometry/Color work (31.65 ms). That experiment's evidence supported stopping
further optimization here. Thread tuning, native extensions or scheduling changes
need a separate measured decision. Whole-view Color remains provisional and
uncalibrated; contamination/lighting and real acquisition/PLC timing need future
validation.

Verification: 522 tests passed, 10 opt-in tests skipped; Ruff, formatting and strict
Pyright were clean. The full normal suite ran once after stabilization. Two OpenCV
static dtype diagnostics were subsequently resolved with uint8 `copy=False`
normalization; the relevant 19 selective tests and strict typing were rerun clean.
Reported preview coverage included capability-cache invalidation, stale-job suppression,
threshold-only rejudgment and body-independent sampling without production effects.

## Recorded input and configuration identities

The three GPU reports retained these same six ordered uint8 BGR inputs, each
`(960,1056,3)`. File hashes and decoded-pixel hashes were different identities:

- CAM1: `20260911_102905_704_001213_25334173.png`
  file `53672ff2abe2b8d5a6ccd923193fa12a73fc47a660ec79a7a89ffe86cbe294c4`; pixels
  `7291f05cd74ab7b760b5da4a071088eff51c0946e89be3609fa06b245cbaaf9e`.
- CAM2: `20260911_102905_766_001219_25334173.png`
  file `7b505e3d15ae0dff75dc39b30142500ec2174248fb4051186499935498e2aba2`; pixels
  `e1d8e5e770f995070684cf0c2acacc82bb077d81215f37342a3b85866a29e303`.
- CAM3: `20260911_102905_833_001225_25334173.png`
  file `503cdb66ae98ae1bceae9222aa9198f8cca007ada2075cb786ba778f98059a72`; pixels
  `0b8e10a501905a7f3be1d34b66750f86508e1c58e4db78c3ba6f3cbd071e0a13`.
- CAM4: `20260911_102905_897_001231_25334173.png`
  file `2076cb4d96b7b56b6a2db1fb02499a7fa2a25eef7c1815947b71c5647aa239db`; pixels
  `08ed9361a39828fbd4f951cfa55933601931e1bd391e719d37d31c1e89bc8044`.
- CAM5: `20260911_102906_706_001237_25334173.png`
  file `b1256b070f8269cc1e78722b36ea9a07b2d7d57417a923be446ebc7af33198a5`; pixels
  `f31b6150eab76c7e83f7218e50c93d8148c30972de34dc2a838a1d76521cfae7`.
- CAM6: `20260911_102906_767_001243_25334173.png`
  file `4c7f0b7309daf298ed26bfd9a6d51da7453b5c1cf0b3b0f361e65961ab1d8f2e`; pixels
  `84ff1240fc3a227caa79186286f3120b244121a569d9d502bce97521c6bc8748`.

The saved manual configuration files under `outputs/manual_phase3c/` were:

- `model.json`: `1f76eb921709d733533154e763accb3c2be73238e8b1b2b84e68eda15a68f589`.
- `machine.json`: `16865e315505539ea1ba5f04af9e814975b1c80eb04b0207a4acaf342c234804`.
- `local.recipe.json`: `91b961e19f52d3531da662ac4f685a4dc20795ab050492cbd79205a55c1c990d`.

These hashes identify the measured snapshots. The archived reports also retain
the actual ROIs, resolved Recipe settings and expected model hash. The local files
were not silently repaired, treated as calibrated geometry or rewritten during
this migration. Optional raw artifact paths are plain evidence references, not
tracked documentation/test dependencies.

The retained optimized CPU oracle recorded
`reference_capsule.py` SHA-256
`b77f1b4f593c2d93c71b88b4d828380c5dcbd9910ea5aad2aea580cd64e6cd78`.
Selective CPU records separately captured capsule-source hashes:
`selective_initial_cpu.json` recorded
`57b07e45d17c77a6374b45cbdc7c71160c437d23095384ea93e4cde180ae8740`,
`selective_inRange_cpu.json` recorded
`224ba9fda943b62406f9441b32d8b69c9a20d102abd021af10987473968e8222`,
and `selective_final_cpu.json` recorded
`241e696ec0866f72d8d87e68c9ad81ea74067a2536ecd31be4b3554e34c872ab`.
These identified their CPU captures, not the later GPU source hash above;
equal outcomes did not imply byte-identical source versions.
Early reports without capsule/source fields were left incomplete; no source hash
was fabricated from today's working tree.

## Evidence verification and limits

During the documentation-only consolidation, all 10 tables were compared
row-for-row with the retained performance source and **188 numeric entries/endpoints**
were recalculated from the existing CSV/session summaries and CPU JSON records.
Read-only checks also covered measured versus untimed counts, successful results,
session termination, model/config/input identity, recorded source/tool hashes,
available lifecycle guards and selective semantic cases.

The available artifacts were under the three dated
`outputs/inspection_performance/` directories and
`outputs/capsule_profiling/phase3f2a/` / `phase3f2b/` listed above.
Equivalence tests/gate tallies were retained as dated reported outcomes, not rerun;
stored `golden_exact`/lifecycle/semantic records were independently readable.
This is preservation and static arithmetic over old evidence, not newly measured
performance, model accuracy, a full regression or a stress experiment.

Current-source commands, even with identical arguments, need not reproduce these
source/configuration/algorithm fingerprints. The source document's complete
Historical section remained unchanged in this batch, pending separate navigation
migration and retirement.
