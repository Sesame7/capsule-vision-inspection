"""Opt-in fixed six-image production-path baseline, without Qt or optimization."""

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import sys
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import cast

import numpy as np
from _tool_helpers import (
    ROOT,
    decode,
    image_paths,
    nonnegative_int,
    output_directory,
    positive_int,
    run_cli,
    statistics,
    write_json,
)
from numpy.typing import NDArray

from vimo_demo.application import (
    LocalCycleIngress,
    PreparedConfiguration,
    ProductionApplication,
    ProductionCycle,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.application.session import EngineFactory
from vimo_demo.configuration import (
    ModelRegistry,
    ResolvedInspectionConfiguration,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inspection.production import ProductionInspectionResult, TechnicalStatus


@dataclass(frozen=True)
class Mode:
    name: str
    vimo: bool
    size: bool
    color: bool


MODES = (
    Mode("A", True, False, False),
    Mode("B", True, True, False),
    Mode("C", True, True, True),
    Mode("D", False, True, True),
)
METRICS = {
    "cycle_e2e_ms": "perf_counter immediately before ingress.submit to result.result returning; formal results only (including ERROR).",
    "submission_ms": "Same start to ingress.submit returning: validation, owned copies and all admission acknowledgements; inspection can overlap.",
    "result_wait_ms": "ingress.submit return to formal result obtained; not an isolated dispatch delay.",
    "pipeline_ms": "ProductionInspectionResult inspection_started_at to result_ready_at; nested inside e2e, excludes ingress/dispatch/delivery.",
    "traditional_ms": "Sum of per-camera existing traditional-stage durations, only when capsule enabled; includes shared measurement and judgment.",
    "vimo_ms": "Sum of per-camera existing full-image inference intervals: worker wait, SDK and normalization, NOT pure GPU kernel time.",
    "mapping_ms": "Sum of per-camera existing extraction/filtering/result-assembly intervals.",
    "aggregation_ms": "Existing production aggregation_ms; overlaps mapping_ms, do NOT add them.",
    "attempt_ms": "perf_counter start to return or exception; exception attempts are kept separately from formal-result latency.",
    "dispatch_ms": "Unavailable: application result does not expose coordinator queue/start milestones. Never inferred by subtracting overlapping stages.",
}


def select_mode(
    base: ResolvedInspectionConfiguration, mode: Mode
) -> ResolvedInspectionConfiguration:
    """Only enablement changes; keep thresholds, label filters, required and ROIs."""
    return replace(
        base,
        channels=tuple(
            replace(
                c,
                checks=replace(
                    c.checks, vimo=replace(c.checks.vimo, enabled=mode.vimo)
                ),
            )
            for c in base.channels
        ),
        capsule=replace(
            base.capsule,
            size=replace(base.capsule.size, enabled=mode.size),
            color=replace(base.capsule.color, enabled=mode.color),
        ),
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def preload(
    paths: Sequence[Path], config: ResolvedInspectionConfiguration
) -> tuple[NDArray[np.uint8], ...]:
    if len(paths) != 6 or len(config.layout.cameras) != 6:
        raise ValueError(
            "Benchmark requires exactly six images and six configured cameras"
        )
    images = tuple(decode(path) for path in paths)
    for camera, image in zip(config.layout.cameras, images, strict=True):
        camera.validate_shape((image.shape[0], image.shape[1], image.shape[2]))
        image.flags.writeable = False
    return images


def result_record(result: ProductionInspectionResult, mode: Mode) -> dict[str, object]:
    metrics: dict[str, object] = {}
    stages = {
        "traditional_ms": [
            c.timing.traditional_ms if mode.size or mode.color else None
            for c in result.cameras
        ],
        "vimo_ms": [c.timing.vimo_ms if mode.vimo else None for c in result.cameras],
        "mapping_ms": [c.timing.mapping_ms for c in result.cameras],
    }
    for name, values in stages.items():
        available = [v for v in values if v is not None]
        metrics[name] = sum(available) if len(available) == len(values) else None
        for camera, value in zip(result.cameras, values, strict=True):
            metrics[f"{camera.camera_id}.{name}"] = value
    metrics["pipeline_ms"] = (
        None
        if result.inspection_started_at is None
        else (result.result_ready_at - result.inspection_started_at) * 1000
    )
    metrics["aggregation_ms"] = result.aggregation_ms
    metrics["dispatch_ms"] = None
    checks = [check for channel in result.channels for check in channel.checks]
    metrics.update(
        cycle_id=int(result.cycle_id),
        cycle_status=result.cycle_status.name,
        technical_status=result.assessment.technical_status.name,
        judgment=result.assessment.judgment.name,
        channel_outcomes=dict(
            Counter(
                f"{c.assessment.technical_status.name}/{c.assessment.judgment.name}"
                for c in result.channels
            )
        ),
        check_errors=dict(
            Counter(
                f"{c.source.name}:{r.code}:{r.exception_type}"
                for c in checks
                if c.assessment.technical_status is TechnicalStatus.ERROR
                for r in c.reasons
            )
        ),
        check_outcomes=dict(
            Counter(
                f"{c.source.name}/{c.assessment.technical_status.name}" for c in checks
            )
        ),
        reasons=dict(
            Counter(f"{c.source.name}:{r.code}" for c in checks for r in c.reasons)
        ),
        measurement_count=sum(len(c.measurements) for c in checks),
    )
    return metrics


def measure_cycle(
    submit: Callable[[], ProductionCycle],
    mode: Mode,
    *,
    clock: Callable[[], float] = perf_counter,
) -> dict[str, object]:
    """No inspection/reporting inside timing beyond the normal ingress/result path."""
    started = clock()
    admitted: float | None = None
    try:
        receipt = submit()
        admitted = clock()
        result = receipt.result.result()
        finished = clock()
    except BaseException:
        finished = clock()
        return {
            "attempt_ms": (finished - started) * 1000,
            "cycle_e2e_ms": None,
            "submission_ms": None if admitted is None else (admitted - started) * 1000,
            "technical_status": "EXCEPTION",
            "judgment": None,
            "exception": traceback.format_exc(),
        }
    # Convert only after the primary timer stopped. No result/Future is retained.
    return {
        **result_record(result, mode),
        "exception": None,
        "attempt_ms": (finished - started) * 1000,
        "cycle_e2e_ms": (finished - started) * 1000,
        "submission_ms": (admitted - started) * 1000,
        "result_wait_ms": (finished - admitted) * 1000,
    }


def summary(records: Sequence[dict[str, object]]) -> dict[str, object]:
    def values(key: str) -> list[float]:
        result: list[float] = []
        for row in records:
            value = row.get(key)
            if isinstance(value, (int, float)):
                if not np.isfinite(value):
                    raise ValueError(f"Nonfinite timing: {key}")
                result.append(float(value))
        return result

    times = values("cycle_e2e_ms")
    keys = sorted({k for row in records for k in row if k.endswith("_ms")})
    return {
        "attempted": len(records),
        "successful": sum(r["technical_status"] == "SUCCESS" for r in records),
        "technical_error_cycles": sum(
            r["technical_status"] == "ERROR" for r in records
        ),
        "failed_or_error_cycles": sum(
            r["technical_status"] in ("ERROR", "EXCEPTION") for r in records
        ),
        "exception_cycles": sum(r["technical_status"] == "EXCEPTION" for r in records),
        "cycles_with_check_errors": sum(bool(r.get("check_errors")) for r in records),
        "judgments": dict(Counter(str(r["judgment"]) for r in records)),
        "over_100_ms_percent": 100 * sum(t > 100 for t in times) / len(times)
        if times
        else None,
        "timings": {key: statistics(values(key)) for key in keys},
    }


def real_engine(prepared: PreparedConfiguration):
    return VimoSegmentationEngine(
        prepared.model_path, module_id=prepared.configuration.model.module_id
    )


def run_session(
    prepared: PreparedConfiguration,
    images: Sequence[NDArray[np.uint8]],
    mode: Mode,
    warmup: int,
    cycles: int,
    emit: Callable[[dict[str, object]], None],
    *,
    engine_factory: EngineFactory = real_engine,
) -> dict[str, object]:
    """One fresh application/session/engine; default queue capacities unchanged."""
    app = ProductionApplication(
        lambda p, g: ProductionRuntimeSession(
            p,
            g,
            warmup=lambda _: images,
            engine_factory=engine_factory,
        )
    )
    records: list[dict[str, object]] = []  # At most `cycles` scalar-only rows.
    warm: list[dict[str, object]] = []
    errors: list[str] = []
    terminated = False
    try:
        app.configure(prepared)
        app.start().result()  # Includes the existing 2 engine calls/unique shape, also in D.
        ingress = LocalCycleIngress(app, prepared.configuration.layout)
        for phase, count in (("warmup", warmup), ("measured", cycles)):
            for index in range(count):
                row: dict[str, object]
                if phase == "warmup":
                    try:
                        result = ingress.submit(images).result.result()
                        row = {
                            k: v
                            for k, v in result_record(result, mode).items()
                            if not k.endswith("_ms")
                        }
                        del result
                        row["exception"] = None
                    except BaseException:
                        row = {
                            "technical_status": "EXCEPTION",
                            "judgment": None,
                            "exception": traceback.format_exc(),
                        }
                else:
                    row = measure_cycle(lambda: ingress.submit(images), mode)
                row.update(phase=phase, sample=index + 1)
                emit(row)  # CSV serialization is outside timing.
                (warm if phase == "warmup" else records).append(row)
                if row["technical_status"] == "EXCEPTION":
                    raise RuntimeError(str(row["exception"]))
    except BaseException:
        errors.append(traceback.format_exc())
    finally:
        try:
            app.stop().result()
        except BaseException:
            errors.append(traceback.format_exc())
        try:
            app.close().result()
        except BaseException:
            errors.append(traceback.format_exc())
        terminated = app.join(0)
        if not terminated:
            # Notification precedes the lifecycle thread's actual return.
            terminated = app.join()
    return {
        "mode": asdict(mode),
        "summary": summary(records),
        "warmup": summary(warm),
        "lifecycle_errors": errors,
        "terminated": terminated,
        "completed": not errors and len(records) == cycles and terminated,
    }


def command_info(command: list[str]) -> dict[str, object]:
    try:
        run = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, timeout=10, check=True
        )
        return {"value": run.stdout.strip(), "unavailable_reason": None}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"value": None, "unavailable_reason": str(exc)}


def metadata(
    config: ResolvedInspectionConfiguration,
    paths: Sequence[Path],
    images: Sequence[NDArray[np.uint8]],
    files: Sequence[Path],
) -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for name in (
        "smartmore.vimo.inference",
        "smartmore.vimo.inference.custom",
        "numpy",
        "opencv-python",
    ):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "os": platform.platform(),
        "packages": packages,
        "git_commit": command_info(["git", "rev-parse", "HEAD"]),
        "git_status": command_info(["git", "status", "--short"]),
        "tool_sha256": digest(Path(__file__)),
        "gpu": command_info(
            [
                "nvidia-smi",
                "--query-gpu=index,name,driver_version,memory.total",
                "--format=csv,noheader",
            ]
        ),
        "configuration": asdict(config),
        "configuration_files": [
            {"path": str(p.resolve()), "sha256": digest(p)} for p in files
        ],
        "inputs": [
            {
                "camera_id": camera.camera_id,
                "path": str(path.resolve()),
                "shape": list(image.shape),
                "dtype": str(image.dtype),
                "format": "BGR",
                "file_sha256": digest(path),
                "pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            }
            for camera, path, image in zip(
                config.layout.cameras, paths, images, strict=True
            )
        ],
    }


def number_at(item: object, *keys: str) -> float | None:
    """Narrow nested report dictionaries; absent metrics remain null."""
    for key in keys:
        if not isinstance(item, dict):
            return None
        item = cast(dict[str, object], item).get(key)
    return float(item) if isinstance(item, (int, float)) else None


def comparisons(runs: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    # Read only known summary fields at this JSON/report boundary.
    result: list[dict[str, object]] = []
    for index in sorted({int(str(r["round"])) for r in runs}):
        group = {str(r["mode_name"]): r for r in runs if r["round"] == index}
        for left, right in (("B", "A"), ("C", "A"), ("C", "B")):
            if left not in group or right not in group:
                continue
            delta: dict[str, object] = {"round": index, "comparison": f"{left}-{right}"}
            for metric in ("mean_ms", "p50_ms", "p95_ms", "p99_ms"):
                a = number_at(group[left], "summary", "timings", "cycle_e2e_ms", metric)
                b = number_at(
                    group[right], "summary", "timings", "cycle_e2e_ms", metric
                )
                delta[metric] = None if a is None or b is None else a - b
            result.append(delta)
    return result


def markdown_report(runs: Sequence[dict[str, object]]) -> str:
    def metric(run: dict[str, object], stage: str, key: str = "mean_ms") -> str:
        value = number_at(run, "summary", "timings", stage, key)
        return "n/a" if value is None else f"{value:.3f}"

    lines = [
        "# Fixed-image production baseline",
        "",
        "Provisional comparison: complete-cycle P95 <= 100 ms; preferred margin 70–80 ms.",
        "These are software targets, not machine-throughput guarantees. NG is valid inspected evidence.",
        "",
        "| Round | Mode | Success | Error/exception | Mean ms | P50 | P95 | P99 | >100 ms % |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        lines.append(
            f"| {run['round']} | {run['mode_name']} | {number_at(run, 'summary', 'successful')} | {number_at(run, 'summary', 'failed_or_error_cycles')} | "
            + " | ".join(
                metric(run, "cycle_e2e_ms", k)
                for k in ("mean_ms", "p50_ms", "p95_ms", "p99_ms")
            )
            + f" | {number_at(run, 'summary', 'over_100_ms_percent')} |"
        )
    lines.extend(
        [
            "",
            "Stage means overlap with e2e; mapping and aggregation overlap each other. Do not add them.",
            "",
            "| Round | Mode | Traditional ms | ViMo ms | Mapping ms | Submission ms |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run['round']} | {run['mode_name']} | "
            + " | ".join(
                metric(run, k)
                for k in ("traditional_ms", "vimo_ms", "mapping_ms", "submission_ms")
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Descriptive differences of independently sampled means/percentiles, not additive cost decomposition.",
            "Size-only already computes Hue; C-B does not isolate Hue extraction.",
            "",
            "| Round | Difference | Mean ms | P95 ms |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in comparisons(runs):
        lines.append(
            f"| {row['round']} | {row['comparison']} | {row['mean_ms']} | {row['p95_ms']} |"
        )
    lines.extend(
        [
            "",
            "See report.json for lifecycle failures, null-stage boundaries, configuration and input fingerprints.",
            "cycles.csv includes separately marked untimed warm-up rows; exclude them from latency statistics.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--machine-config", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--frames-dir", type=Path, required=True)
    parser.add_argument(
        "--warmup",
        type=nonnegative_int,
        default=30,
        help="Untimed complete cycles per session",
    )
    parser.add_argument("--cycles", type=positive_int, default=300)
    parser.add_argument("--repetitions", type=positive_int, default=3)
    args = parser.parse_args()
    model = load_model_definition(args.model_config)
    base = resolve_configuration(
        load_machine_layout(args.machine_config),
        load_recipe(args.recipe),
        ModelRegistry((model,)),
        model.model_id,
    )
    paths = image_paths(args.frames_dir)[:6]
    images = preload(paths, base)  # Do not silently skip unreadable selected files.
    prepared = prepare_configuration(base, base_directory=ROOT)
    destination = output_directory("inspection_performance")
    report: dict[str, object] = {
        **metadata(
            base, paths, images, (args.model_config, args.machine_config, args.recipe)
        ),
        "settings": {
            "warmup_cycles": args.warmup,
            "measured_cycles": args.cycles,
            "repetitions": args.repetitions,
            "engine_warmup_calls_per_shape": 2,
        },
        "modes": [asdict(m) for m in MODES],
        "metric_boundaries": METRICS,
        "percentiles": "NumPy linear interpolation; >100 ms excludes exactly 100 ms. Formal ERROR samples included; exceptional attempts separate.",
        "limitations": [
            "Fixed local images, unknown physical camera origins; not verified machine throughput.",
            "Size-only also computes Hue in the current shared measurement; C-B is not full Color feature-extraction cost.",
            "Mode order rotates per repetition, not a fully counterbalanced thermal study.",
            "No isolated dispatch timing exposed by ProductionApplication; overlapping stage metrics must not be added.",
        ],
    }
    runs: list[dict[str, object]] = []
    report["runs"] = runs
    fields = [
        "round",
        "mode",
        "phase",
        "sample",
        "cycle_id",
        "technical_status",
        "judgment",
        "cycle_status",
        *METRICS,
        "channel_outcomes",
        "check_outcomes",
        "check_errors",
        "reasons",
        "measurement_count",
        "exception",
    ]
    fields += [
        f"{c.camera_id}.{metric}"
        for c in base.layout.cameras
        for metric in ("traditional_ms", "vimo_ms", "mapping_ms")
    ]
    print(f"Outputs: {destination}", flush=True)
    try:
        with (destination / "cycles.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for repetition in range(args.repetitions):
                order = MODES[repetition % 4 :] + MODES[: repetition % 4]
                for mode in order:
                    print(
                        f"Round {repetition + 1}/{args.repetitions}, mode {mode.name}: warmup={args.warmup}, measured={args.cycles}",
                        flush=True,
                    )

                    def emit(
                        row: dict[str, object],
                        round_number: int = repetition + 1,
                        mode_name: str = mode.name,
                    ) -> None:
                        writer.writerow(
                            {
                                "round": round_number,
                                "mode": mode_name,
                                **{
                                    k: json.dumps(v, ensure_ascii=False, sort_keys=True)
                                    if isinstance(v, dict)
                                    else v
                                    for k, v in row.items()
                                },
                            }
                        )

                    run = run_session(
                        replace(prepared, configuration=select_mode(base, mode)),
                        images,
                        mode,
                        args.warmup,
                        args.cycles,
                        emit,
                    )
                    run.update(
                        round=repetition + 1, mode_name=mode.name, order=len(runs) + 1
                    )
                    runs.append(run)
                    stream.flush()
                    print(
                        f"  mean={number_at(run, 'summary', 'timings', 'cycle_e2e_ms', 'mean_ms')} ms; P95={number_at(run, 'summary', 'timings', 'cycle_e2e_ms', 'p95_ms')} ms; completed={run['completed']}",
                        flush=True,
                    )
                    write_json(destination / "report.json", report)
                    if not run["completed"]:
                        return 1
    finally:
        report["comparisons_ms"] = comparisons(runs)
        report["completed"] = len(runs) == args.repetitions * 4 and all(
            r["completed"] for r in runs
        )
        write_json(destination / "report.json", report)
        (destination / "report.md").write_text(markdown_report(runs), encoding="utf-8")
    return (
        1
        if any(
            number_at(r, "summary", key)
            for r in runs
            for key in ("technical_error_cycles", "cycles_with_check_errors")
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
