"""S1-E1: isolated real-GPU A/B with simulated arrivals; no cameras or PLC writes."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from _scheduling_experiment import (
    Job,
    Runner,
    Schedule,
    Workload,
    json_default,
    owned_frame,
    paced_run,
    simulated_case,
)
from _tool_helpers import (
    decode,
    engine_metadata,
    image_paths,
    nonnegative_int,
    output_directory,
    positive_int,
    run_cli,
    statistics,
)
from benchmark_production import digest, metadata

from vimo_demo.configuration import (
    ModelRegistry,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
    validate_model_artifact,
)
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inspection import ReceivedFrame
from vimo_demo.runtime import InferenceWorker


def summarize(schedule: Schedule) -> dict[str, object]:
    complete = [r for r in schedule.records if r["status"] == "COMPLETE"]
    ready = [float(str(r["ready"])) for r in complete]
    # Remove the first/last 10% of completion observations for steady spacing.
    trim = len(ready) // 10
    steady = ready[trim : len(ready) - trim] if trim else ready
    return {
        "terminal_counts": dict(Counter(str(r["status"]) for r in schedule.records)),
        "cycles_with_technical_errors": sum(
            bool(r.get("technical_error_channels")) for r in complete
        ),
        "first_to_result_ms": statistics(
            [float(str(r["first_to_result_ms"])) for r in complete]
        ),
        "last_to_result_ms": statistics(
            [float(str(r["last_to_result_ms"])) for r in complete]
        ),
        "steady_completed_cycles_per_second": (len(steady) - 1)
        / (steady[-1] - steady[0])
        if len(steady) > 1 and steady[-1] > steady[0]
        else None,
        "completion_spacing_ms": statistics(
            [(b - a) * 1000 for a, b in zip(steady, steady[1:], strict=False)]
        ),
        "camera_wait_ms_derived": statistics(
            [float(str(r["waiting_ms"])) for r in schedule.camera_rows]
        ),
        "camera_inference_ms": statistics(
            [
                float(str(r["inference_ms"]))
                for r in schedule.camera_rows
                if r["inference_ms"] is not None
            ]
        ),
        "camera_configured_checks_ms": statistics(
            [float(str(r["checks_ms"])) for r in schedule.camera_rows]
        ),
        "ownership_copy_ms_per_cycle": statistics(
            [float(str(r["copy_ms"])) for r in schedule.records if "copy_ms" in r]
        ),
        "max_outstanding_cycles": schedule.max_pending,
        "max_waiting_jobs": schedule.max_queue,
        "max_retained_frames_including_active": schedule.max_pixels,
        "rejected_late_or_unadmitted_frames": schedule.rejected_arrivals,
        "late_nonpreemptible_job_completions": schedule.late_completions,
    }


def record(schedule: Schedule) -> dict[str, object]:
    return {
        "summary": summarize(schedule),
        "cycles": schedule.records,
        "cameras": schedule.camera_rows,
        "depth_observations": schedule.depths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--machine-config", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument(
        "--frames-dir",
        type=Path,
        required=True,
        help="Root containing cam1..cam6; first filename per configured camera",
    )
    parser.add_argument(
        "--warmup",
        type=nonnegative_int,
        default=3,
        help="Untimed full cycles and six single-camera jobs per repetition",
    )
    parser.add_argument("--cycles", type=positive_int, default=30)
    parser.add_argument("--repetitions", type=positive_int, default=3)
    parser.add_argument("--capacity", type=positive_int, default=4)
    parser.add_argument(
        "--deadline-ms",
        type=positive_int,
        default=300,
        help="Synthetic result deadline, NOT an established PLC deadline",
    )
    args = parser.parse_args()
    model = load_model_definition(args.model_config)
    model_path = validate_model_artifact(
        model, base_directory=args.model_config.resolve().parent
    )
    configuration = resolve_configuration(
        load_machine_layout(args.machine_config),
        load_recipe(args.recipe),
        ModelRegistry((model,)),
        model.model_id,
    )
    if len(configuration.layout.cameras) != 6:
        parser.error("Experiment requires exactly six configured cameras")
    paths: list[Path] = []
    for camera in configuration.layout.cameras:
        candidates = image_paths(args.frames_dir / camera.camera_id.lower())
        if not candidates:
            parser.error(f"No images for {camera.camera_id}")
        paths.append(candidates[0])
    images = tuple(decode(path) for path in paths)
    for camera, image in zip(configuration.layout.cameras, images, strict=True):
        camera.validate_shape((image.shape[0], image.shape[1], image.shape[2]))
        image.setflags(write=False)
    destination = output_directory("s1e1_scheduling")
    print(f"Evidence: {destination}", flush=True)
    report: dict[str, object] = {
        **metadata(
            configuration,
            paths,
            images,
            (args.model_config, args.machine_config, args.recipe),
        ),
        "experiment_timestamp": datetime.now(UTC).isoformat(),
        "experiment_sha256": digest(Path(__file__)),
        "support_sha256": digest(Path(__file__).with_name("_scheduling_experiment.py")),
        "actual_model_sha256": digest(model_path),
        "settings": vars(args)
        | {
            "model_config": str(args.model_config),
            "machine_config": str(args.machine_config),
            "recipe": str(args.recipe),
            "frames_dir": str(args.frames_dir),
        },
        "effective_untimed_equivalence_cycles_per_repetition": max(1, args.warmup),
        "methodology": [
            "Real GPU, same six decoded BGR arrays, immutable owned copy at each synthetic arrival.",
            "One InferenceWorker engine for the entire experiment, one inspection job at a time.",
            "A: unmodified configured full-cycle inspect; B: per-camera config projection with global IDs restored.",
            "Open-loop 100ms trigger plans do not wait for earlier results; host release lag recorded separately.",
            "Near offsets 0/1/2/3/4/5ms; staggered reversed camera order at 0/8/16/24/32/40ms.",
            "Saturated is closed-loop back-to-back complete groups, a capacity estimate, not hardware throughput.",
            "Initialization, warmup, decoding, hashing and report I/O outside measured runs.",
            "Results include dispatch/normalization/compact signature cost, exclude Qt/PLC/display/acquisition.",
            "Camera wait is derived from public inference-start minus measured CPU checks; not an exact dispatch probe.",
            "Configured checks = occupancy + Size/Color; mapping overlaps other pipeline aggregation metrics.",
            "Fixed inputs are local stored examples, not simultaneous hardware exposures or an accuracy corpus.",
            "Deadline/overload examples are virtual-time target diagnostics, not Production judgments/PLC outputs.",
            "Finite scalar reports; pixel bound capacity*6+6, command/completion queues one slot each.",
            "No GPU clock/thermal/background-load control; alternating order only reduces order bias.",
        ],
    }
    runs: list[dict[str, object]] = []
    report["runs"] = runs
    baseline: dict[str, str] = {}
    geometry: dict[str, str] = {}
    engine_info: dict[str, object] = {}

    def create_engine() -> VimoSegmentationEngine:
        engine = VimoSegmentationEngine(model_path, module_id=model.module_id)
        engine_info.update(engine_metadata(engine.info))
        return engine

    def save() -> None:
        (destination / "report.json").write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                default=json_default,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

    workload = Workload(configuration)
    worker = InferenceWorker(create_engine)
    runner = Runner(workload, worker)
    report["completed"] = False
    try:
        init = perf_counter()
        worker.start().result()
        report["initialization_ms"] = (perf_counter() - init) * 1000
        report["engine"] = engine_info
        runner.thread.start()
        for repetition in range(args.repetitions):
            # Readiness / warmup never enters the measured scheduler or counters.
            for _ in range(max(1, args.warmup)):
                now = perf_counter()
                frames = tuple(
                    ReceivedFrame(owned_frame(image, 0, camera, 0)[0], now)
                    for image, camera in zip(
                        images, configuration.layout.camera_ids, strict=True
                    )
                )
                full = workload.execute(
                    Job(0, now, now + 60, frames, "A"),
                    lambda a: worker.submit(a).result(),
                )
                for entry, expected in zip(frames, full, strict=True):
                    single = workload.execute(
                        Job(0, now, now + 60, (entry,), "B"),
                        lambda a: worker.submit(a).result(),
                    )[0]
                    if single.semantic != expected.semantic:
                        raise AssertionError(
                            f"Projection changes configured results: {single.camera}"
                        )
                if not baseline:
                    report["baseline"] = [r.detail for r in full]
                    baseline.update((r.camera, r.semantic) for r in full)
                    geometry.update((r.camera, r.geometry) for r in full)
                if any(r.technical_error_channels or r.detail["failure"] for r in full):
                    raise RuntimeError(
                        "Baseline has technical errors; not a successful GPU workload"
                    )
            cases = (
                ("near_100ms", [0, 0.001, 0.002, 0.003, 0.004, 0.005], 0.1),
                ("staggered_100ms", [0.040, 0.032, 0.024, 0.016, 0.008, 0], 0.1),
                ("saturated", [0.0] * 6, 0.0),
            )
            for name, offsets, interval in cases:
                for mode in ("A", "B") if repetition % 2 == 0 else ("B", "A"):
                    schedule, lag = paced_run(
                        runner,
                        images,
                        mode,
                        offsets,
                        cycles=args.cycles,
                        interval=interval,
                        capacity=args.capacity,
                        deadline=args.deadline_ms / 1000,
                    )
                    run = record(schedule)
                    mismatches = [
                        {"cycle": row["cycle"], "camera": row["camera"]}
                        for row in schedule.camera_rows
                        if row["semantic"] != baseline[str(row["camera"])]
                    ]
                    run.update(
                        repetition=repetition + 1,
                        case=name,
                        mode=mode,
                        release_lag_ms=statistics(lag),
                        semantic_mismatches=mismatches,
                        geometry_different_camera_results=sum(
                            row["geometry"] != geometry[str(row["camera"])]
                            for row in schedule.camera_rows
                        ),
                    )
                    runs.append(run)
                    latency = statistics(
                        [
                            float(str(r["first_to_result_ms"]))
                            for r in schedule.records
                            if r["status"] == "COMPLETE"
                        ]
                    )
                    print(
                        f"{repetition + 1} {name} {mode}: "
                        f"complete={latency['calls']}/{args.cycles} "
                        f"first-frame mean={latency['mean_ms'] or 0:.2f}ms "
                        f"P95={latency['p95_ms'] or 0:.2f}ms "
                        f"max_outstanding={schedule.max_pending} max_waiting={schedule.max_queue}",
                        flush=True,
                    )
                    save()  # Outside measured run.
                    if mismatches or any(
                        r["status"] != "COMPLETE" or r.get("technical_error_channels")
                        for r in schedule.records
                    ):
                        raise RuntimeError(
                            "Semantic mismatch or deadline/overload; inspect partial report"
                        )
        report["simulated_target_cases"] = {
            f"{name}_{mode}": record(
                simulated_case(
                    configuration.layout.camera_ids,
                    mode,
                    missing=(1, configuration.layout.camera_ids[5])
                    if name == "missing"
                    else None,
                    delayed=(1, configuration.layout.camera_ids[5])
                    if name == "delayed"
                    else None,
                )
            )
            for name in ("missing", "delayed")
            for mode in ("A", "B")
        }
        report["simulated_overload"] = {
            mode: record(
                simulated_case(
                    configuration.layout.camera_ids,
                    mode,
                    cycles=50,
                    service=0.025,
                    deadline=1.0,
                    capacity=3,
                )
            )
            for mode in ("A", "B")
        }
        report["completed"] = True
    finally:
        if runner.thread.ident is not None:
            runner.close()
        worker.request_stop()
        terminated = worker.join()
        report["shutdown"] = {
            "worker_terminated": terminated,
            "worker_alive": worker.is_alive,
            "inspection_thread_alive": runner.thread.is_alive(),
            "worker_failure": repr(worker.failure) if worker.failure else None,
        }
        save()
    print(f"Saved {destination / 'report.json'}", flush=True)
    return 0 if report["completed"] and worker.failure is None else 1


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
