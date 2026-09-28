"""Long-run serial ViMo stability experiment on GPU 0 (no acceptance thresholds).

One measured round is six calls. Warmup counts individual, unmeasured calls.
Images and requests are prepared once; one Solution/module lives for the run.
Run manually: this script performs real GPU inference unless using --help.
"""

import argparse
import csv
import ctypes
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from collections import Counter
from ctypes import wintypes
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
from benchmark_vimo import positive_int
from compare_vimo_results import parameter_snapshot, recoverable_inference_error
from scan_vimo_folder import (
    EXTENSIONS,
    discover_images,
    exception_details,
    validate_paths,
)
from smoke_test_vimo import configure_regions, read_export, select_module, stage_model


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def load_pool(directory, size):
    import cv2

    candidates = discover_images(directory, False)
    pool, images, failures = [], [], []
    for path in candidates:
        try:
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("OpenCV could not decode the image")
        except (OSError, ValueError, cv2.error) as exc:
            failures.append({"filename": path.name, **exception_details(exc)})
            print(f"[WARN] {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        pool.append(
            {
                "filename": path.name,
                "relative_path": path.relative_to(directory).as_posix(),
                "shape": list(image.shape),
            }
        )
        images.append(image)
        if len(images) == size:
            break
    if len(images) < 6:
        raise ValueError(
            f"Need at least 6 usable images; found {len(images)} in {directory}"
        )
    if len(images) < size:
        print(
            f"[WARN] Requested pool of {size}; using all {len(images)} usable images."
        )
    return pool, images, failures, len(candidates)


def semantic_signature(response, vimo):
    if not isinstance(response, vimo.SegmentationResponse):
        raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
    counts = Counter(region.label for region in response.region_infos)
    return {
        "label_map": {label: int(code) for label, code in response.label_map.items()},
        "total_region_count": sum(counts.values()),
        "regions_per_label": dict(sorted(counts.items())),
    }


def process_memory():
    """Win32 bytes: working set and private committed memory, not peak RSS.

    Layout: https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex
    """
    if os.name != "nt":
        raise OSError("Process memory sampling requires Windows GetProcessMemoryInfo")

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ],
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.argtypes = []
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "process_working_set_bytes": counters.WorkingSetSize,
        "process_private_bytes": counters.PrivateUsage,
    }


def smi_query(executable, query):
    if executable is None:
        raise FileNotFoundError(
            "nvidia-smi not found on PATH or in its standard Windows locations"
        )
    result = subprocess.run(
        [executable, query, "--format=csv,noheader,nounits", "-i", "0"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return list(csv.reader(result.stdout.strip().splitlines(), skipinitialspace=True))


def memory_bytes(value):
    # nvidia-smi reports memory in MiB. N/A / Not Supported must never become 0.
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Invalid GPU memory value: {value!r}")
    return int(number * 1024 * 1024)


def sample_resources(report, executable, round_number, call_count, reason):
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round": round_number,
        "inference_count": call_count,
        "reason": reason,
        "process_working_set_bytes": None,
        "process_private_bytes": None,
        "gpu_device_used_bytes": None,
        "gpu_device_capacity_bytes": None,
        "gpu_process_used_bytes": None,
        "unavailable_reasons": {},
    }

    def unavailable(scope, exc):
        details = exception_details(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            details.update(stdout=exc.stdout, stderr=exc.stderr)
        snapshot["unavailable_reasons"][scope] = details["message"]
        report["failures"]["resource_sampling"].append(
            {
                "round": round_number,
                "scope": scope,
                **details,
            }
        )

    try:
        snapshot.update(process_memory())
    except OSError as exc:
        unavailable("process_memory", exc)
    try:
        rows = smi_query(executable, "--query-gpu=memory.used,memory.total")
        if len(rows) != 1 or len(rows[0]) != 2:
            raise ValueError(f"Unexpected GPU memory query output: {rows!r}")
        snapshot["gpu_device_used_bytes"] = memory_bytes(rows[0][0])
        snapshot["gpu_device_capacity_bytes"] = memory_bytes(rows[0][1])
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        csv.Error,
        UnicodeError,
    ) as exc:
        unavailable("gpu_device_memory", exc)
    try:
        rows = smi_query(executable, "--query-compute-apps=pid,used_gpu_memory")
        own_rows = [
            row for row in rows if len(row) == 2 and row[0].strip() == str(os.getpid())
        ]
        if len(own_rows) != 1:
            raise ValueError(
                "No unique memory entry for this PID; unavailable (not zero). "
                "Windows/WDDM may not expose process GPU memory."
            )
        try:
            snapshot["gpu_process_used_bytes"] = memory_bytes(own_rows[0][1])
        except ValueError as exc:
            raise ValueError(
                f"Process GPU memory unavailable: {own_rows[0][1]!r}. "
                "Under Windows/WDDM memory is managed by Windows; nvidia-smi may report N/A."
            ) from exc
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        csv.Error,
        UnicodeError,
    ) as exc:
        unavailable("gpu_process_memory", exc)
    report["resource_snapshots"].append(snapshot)


def statistics(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    result: dict[str, float | int | None] = dict.fromkeys(
        ("mean_ms", "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "std_ms")
    )
    result["count"] = int(values.size)
    if values.size:
        p50, p95, p99 = np.percentile(values, [50, 95, 99], method="linear")
        result.update(
            mean_ms=float(values.mean()),
            p50_ms=float(p50),
            p95_ms=float(p95),
            p99_ms=float(p99),
            min_ms=float(values.min()),
            max_ms=float(values.max()),
            std_ms=float(values.std(ddof=0)),
        )
    return result


def timing_summary(calls, states, rounds, round_calls):
    successful_calls = np.where(states == 1, calls, np.nan)
    good_rounds = np.array(
        [
            count == 6 and np.all(states[index * 6 : index * 6 + 6] == 1)
            for index, count in enumerate(round_calls)
        ],
        dtype=bool,
    )
    successful_rounds = np.where(good_rounds, rounds, np.nan)
    count = len(rounds)
    width = max(1, math.ceil(count * 0.1))
    starts = (
        {
            "first_10_percent": 0,
            "middle_10_percent": (count - width) // 2,
            "last_10_percent": count - width,
        }
        if count
        else {}
    )
    segments = {}
    for name, start in starts.items():
        end = start + width
        segments[name] = {
            "first_round": start + 1,
            "last_round": end,
            "calls": statistics(successful_calls[start * 6 : end * 6]),
            "rounds": statistics(successful_rounds[start:end]),
        }
    drift = {}
    for kind in ("calls", "rounds"):
        drift[kind] = {}
        for metric in ("mean_ms", "p95_ms"):
            first = segments.get("first_10_percent", {}).get(kind, {}).get(metric)
            last = segments.get("last_10_percent", {}).get(kind, {}).get(metric)
            drift[kind][metric.replace("_ms", "_change_percent")] = (
                100 * (last / first - 1) if first and last is not None else None
            )
    return {
        "individual_calls": statistics(successful_calls),
        "six_call_rounds": statistics(successful_rounds),
        "segments": segments,
        "drift": drift,
        "policy": "Successful SDK calls only; round statistics require six successful SDK calls. "
        "Semantic mismatches/result-processing failures do not remove returned-call timings. "
        "Segments use chronological attempted rounds, ceil(10%) each; small runs may overlap. "
        "Percentiles use linear interpolation; standard deviation is population (ddof=0).",
    }


def resource_trends(snapshots):
    trends = {}
    for metric in (
        "process_working_set_bytes",
        "process_private_bytes",
        "gpu_device_used_bytes",
        "gpu_device_capacity_bytes",
        "gpu_process_used_bytes",
    ):
        valid = [s for s in snapshots if s[metric] is not None]
        trends[metric] = {
            "valid_samples": len(valid),
            "first_valid_sample": None,
            "last_valid_sample": None,
            "min": None,
            "max": None,
            "change_bytes": None,
            "change_percent": None,
        }
        if valid:
            first, last = valid[0][metric], valid[-1][metric]
            trends[metric].update(
                first_valid_sample={"round": valid[0]["round"], "value": first},
                last_valid_sample={"round": valid[-1]["round"], "value": last},
                min=min(s[metric] for s in valid),
                max=max(s[metric] for s in valid),
                change_bytes=last - first,
                change_percent=100 * (last - first) / first if first else None,
            )
    return trends


def measured_run(module, requests, report, args, vimo, executable):
    # Touch timing storage before initial sampling, rather than growing Python
    # lists on every inference and mistaking recorder growth for runtime growth.
    calls = np.full(args.rounds * 6, np.nan)
    states = np.full(args.rounds * 6, 0, dtype=np.uint8)
    rounds = np.full(args.rounds, np.nan)
    round_calls = np.full(args.rounds, 0, dtype=np.uint8)
    attempted = attempted_rounds = consecutive_failures = 0
    round_total = 0.0
    round_successes = 0
    report["completion_status"] = "running"
    try:
        sample_resources(report, executable, 0, 0, "before_measured_run")
        for round_index in range(args.rounds):
            attempted_rounds = round_index + 1
            round_start = time.perf_counter()
            try:
                for _ in range(6):
                    index = attempted
                    pool_index = index % len(requests)
                    context = {
                        "round": round_index + 1,
                        "inference_index": index + 1,
                        "filename": report["input_pool"][pool_index]["filename"],
                    }
                    response = None
                    attempted += 1
                    round_calls[round_index] += 1
                    started = time.perf_counter()
                    try:
                        response = module.run(requests[pool_index])
                    except Exception as exc:  # noqa: BLE001 - SDK exception types are classified below.
                        calls[index] = (time.perf_counter() - started) * 1000
                        states[index] = 2
                        consecutive_failures += 1
                        report["failures"]["inference"].append(
                            {**context, **exception_details(exc, vimo)}
                        )
                        if (
                            not recoverable_inference_error(exc, vimo)
                            or consecutive_failures >= 3
                        ):
                            report["stop_reason"] = (
                                "Module usability uncertain: non-recoverable inference exception "
                                "or three consecutive inference exceptions; no reset/retry."
                            )
                            report["completion_status"] = "aborted"
                            break
                        continue
                    except KeyboardInterrupt:
                        calls[index] = (time.perf_counter() - started) * 1000
                        states[index] = 3
                        raise
                    else:
                        calls[index] = (time.perf_counter() - started) * 1000
                        states[index] = 1
                        consecutive_failures = 0
                    try:
                        observed = semantic_signature(response, vimo)
                        baseline = report["baseline_signatures"][pool_index]
                        if observed != baseline:
                            report["semantic_mismatches"].append(
                                {
                                    **context,
                                    "baseline_signature": baseline,
                                    "observed_signature": observed,
                                }
                            )
                    except Exception as exc:  # noqa: BLE001 - record any response-processing failure.
                        report["failures"]["response_processing"].append(
                            {**context, **exception_details(exc, vimo)}
                        )
                    finally:
                        # Snapshot semantics before the next call; retain no SDK response buffers.
                        response = None
            finally:
                rounds[round_index] = (time.perf_counter() - round_start) * 1000
            if round_calls[round_index] == 6 and np.all(
                states[round_index * 6 : attempted] == 1
            ):
                round_total += rounds[round_index]
                round_successes += 1
            if attempted_rounds % args.sample_interval == 0:
                sample_resources(
                    report, executable, attempted_rounds, attempted, "periodic"
                )
                failures = sum(
                    len(report["failures"][key])
                    for key in ("inference", "response_processing")
                )
                average = (
                    f"{round_total / round_successes:.3f}" if round_successes else "n/a"
                )
                print(
                    f"[{attempted_rounds}/{args.rounds}] failures={failures} "
                    f"semantic_mismatches={len(report['semantic_mismatches'])} mean_round_ms={average} "
                    f"resource_failures={len(report['failures']['resource_sampling'])}",
                    flush=True,
                )
            if report["completion_status"] == "aborted":
                break
        if report["completion_status"] == "running":
            report["completion_status"] = "completed"
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve fatal run state in the report.
        report["completion_status"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "aborted"
        )
        report["fatal_error"] = {
            "phase": "measured run",
            **exception_details(exc, vimo),
        }
        traceback.print_exc()
    finally:
        # Module is still alive. Sample before teardown and JSON/statistics allocation.
        try:
            sample_resources(
                report, executable, attempted_rounds, attempted, "end_measured_run"
            )
        except KeyboardInterrupt:
            report["failures"]["resource_sampling"].append(
                {"scope": "final_sample", "message": "Interrupted"}
            )
        calls, states = calls[:attempted], states[:attempted]
        rounds, round_calls = rounds[:attempted_rounds], round_calls[:attempted_rounds]
        report["counts"] = {
            "attempted_calls": attempted,
            "successful_sdk_calls": int(np.count_nonzero(states == 1)),
            "attempted_rounds": attempted_rounds,
            "rounds_with_six_attempts": int(np.count_nonzero(round_calls == 6)),
        }
        report["timing_statistics"] = timing_summary(calls, states, rounds, round_calls)
        report["raw_timings"] = {
            "individual_ms": [float(x) if np.isfinite(x) else None for x in calls],
            "call_states": states.tolist(),
            "call_state_codes": {
                "0": "not_finished",
                "1": "returned",
                "2": "inference_exception",
                "3": "interrupted",
            },
            "round_ms": [float(x) if np.isfinite(x) else None for x in rounds],
            "calls_per_round": round_calls.tolist(),
            "indexing": "Arrays are chronological. Call i (1-based) uses pool[(i-1) % pool_size]. "
            "Incomplete/failed calls and rounds remain in raw data; statistics filter them.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path, help="Source .vimosln (read-only)")
    parser.add_argument("images", type=Path, help="Non-recursive image directory")
    parser.add_argument(
        "--warmup",
        type=nonnegative_int,
        default=20,
        help="Unmeasured serial calls before baselines (default: 20)",
    )
    parser.add_argument(
        "--rounds",
        type=positive_int,
        default=5000,
        help="Six serial calls per round (default: 5000)",
    )
    parser.add_argument(
        "--pool-size",
        type=positive_int,
        default=24,
        help="First N usable images, at least 6 (default: 24)",
    )
    parser.add_argument(
        "--sample-interval",
        type=positive_int,
        default=100,
        help="Resource/progress interval in rounds (default: 100)",
    )
    args = parser.parse_args()
    if args.pool_size < 6:
        parser.error("--pool-size must be at least 6")
    try:
        model, directory = validate_paths(args.model, args.images)
    except (OSError, ValueError) as exc:
        parser.error(f"{type(exc).__name__}: {exc}")

    timestamp = datetime.now(timezone.utc)
    report = {
        "timestamp": timestamp.isoformat(),
        "completion_status": "initializing",
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "pid": os.getpid(),
            "versions": {},
        },
        "model_path": str(model),
        "model_sha256": None,
        "settings": {
            "image_directory": str(directory),
            "warmup": args.warmup,
            "warmup_unit": "individual serial calls cycling the pool, before one baseline call per image",
            "rounds": args.rounds,
            "calls_per_round": 6,
            "planned_calls": args.rounds * 6,
            "requested_pool_size": args.pool_size,
            "sample_interval_rounds": args.sample_interval,
            "recursive": False,
            "extensions": sorted(EXTENSIONS),
            "ordering": "case-insensitive relative filename, original-case tie break; first N decodable images",
            "use_gpu": True,
            "device_id": 0,
            "max_batch_size": 1,
            "failure_policy": "Continue input/binding rejections only; stop on unknown/runtime/license errors "
            "or 3 consecutive inference errors. Result-processing errors and semantic mismatches are recorded.",
        },
        "input_pool": [],
        "baseline_signatures": [],
        "warmup_calls_completed": 0,
        "resource_snapshots": [],
        "semantic_mismatches": [],
        "failures": {
            "decode": [],
            "inference": [],
            "response_processing": [],
            "resource_sampling": [],
        },
        "fatal_error": None,
        "stop_reason": None,
        "notes": [
            "One module throughout. Baselines and warmup are excluded from measured data.",
            "Individual latency times only module.run; round wall time also includes loop bookkeeping, ",
            "semantic checks and response release. Neither includes decoding, sampling, printing or JSON serialization.",
            "Timing arrays are preallocated before sampling; memory still includes Python/SDK allocators, ",
            "resource sampling and any growing error/mismatch records. Samples are taken with module alive.",
            "GPU device memory is device-wide; other applications affect it. nvidia-smi index 0 is queried; ",
            "check UUID/PCI identity if CUDA device ordering/visibility has been changed.",
            "Process private bytes are private committed memory; working set is resident process memory.",
            "Process GPU memory unavailable/N/A (including WDDM) is null with a reason, never zero.",
            "Descriptive measurements only: no automatic drift/leak/accuracy pass-fail conclusions.",
        ],
    }
    for package in (
        "smartmore.vimo.inference",
        "smartmore.vimo.inference.custom",
        "numpy",
        "opencv-python",
    ):
        try:
            report["environment"]["versions"][package] = version(package)
        except PackageNotFoundError:
            report["environment"]["versions"][package] = None
    executable = shutil.which("nvidia-smi")
    if executable is None and os.name == "nt":
        for candidate in (
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32/nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "NVIDIA Corporation/NVSMI/nvidia-smi.exe",
        ):
            if candidate.is_file():
                executable = str(candidate)
                break
    report["environment"]["nvidia_smi"] = executable
    phase = "decode input pool"
    vimo = None
    try:
        pool, images, decode_failures, discovered = load_pool(directory, args.pool_size)
        report["input_pool"] = pool
        report["failures"]["decode"] = decode_failures
        report["settings"]["actual_pool_size"] = len(pool)
        report["discovered_images"] = discovered
        phase = "SDK import"
        import smartmore.vimo.inference as vimo

        phase = "construct requests"
        requests = [vimo.Request(image) for image in images]
        phase = "read export and stage model"
        exported = read_export(model.with_name("index.html"))
        runtime_model = stage_model(model)
        report["model_sha256"] = runtime_model.parent.name
        phase = "load Solution"
        with vimo.Solution(str(runtime_model)) as solution:
            module_id = select_module(solution.module_info_list, exported, None, vimo)
            report["module_id"] = module_id
            phase = "initialize/configure serial GPU module"
            with solution.create_module(module_id, True, 0, 1) as module:
                if module.max_batch_size != 1:
                    raise RuntimeError(
                        f"Expected max_batch_size=1, got {module.max_batch_size}"
                    )
                configure_regions(module, vimo)
                report["module_parameters"] = parameter_snapshot(module.get_params())
                report["settings"]["parameter_source"] = (
                    "Verified smoke-test defaults and region flags; no vimo_params.json"
                )
                phase = "warmup"
                print(
                    f"Pool: {len(pool)} images; warmup: {args.warmup} individual calls (excluded)",
                    flush=True,
                )
                for index in range(args.warmup):
                    module.run(requests[index % len(requests)])
                    report["warmup_calls_completed"] += 1
                phase = "baseline signatures"
                for index, request in enumerate(requests):
                    phase = f"baseline signature: {pool[index]['filename']}"
                    report["baseline_signatures"].append(
                        semantic_signature(module.run(request), vimo)
                    )
                try:
                    rows = smi_query(
                        executable,
                        "--query-gpu=index,uuid,name,pci.bus_id,driver_version,driver_model.current",
                    )
                    if len(rows) != 1 or len(rows[0]) != 6:
                        raise ValueError(f"Unexpected device metadata: {rows!r}")
                    report["environment"]["gpu"] = dict(
                        zip(
                            (
                                "index",
                                "uuid",
                                "name",
                                "pci_bus_id",
                                "driver_version",
                                "driver_model",
                            ),
                            rows[0],
                        )
                    )
                except (
                    OSError,
                    subprocess.SubprocessError,
                    ValueError,
                    csv.Error,
                    UnicodeError,
                ) as exc:
                    report["environment"]["gpu"] = None
                    report["failures"]["resource_sampling"].append(
                        {"scope": "device_metadata", **exception_details(exc)}
                    )
                phase = "measured serial run"
                print(
                    f"Starting {args.rounds} rounds / {args.rounds * 6} calls; resource samples outside timing.",
                    flush=True,
                )
                measured_run(module, requests, report, args, vimo, executable)
                phase = "release module"
            phase = "release Solution"
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - preserve fatal run state in the report.
        report["completion_status"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "aborted"
        )
        report["fatal_error"] = {"phase": phase, **exception_details(exc, vimo)}
        traceback.print_exc()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["resource_trends"] = resource_trends(report["resource_snapshots"])
    report["failure_counts"] = {
        key: len(value) for key, value in report["failures"].items()
    }
    report["semantic_mismatch_count"] = len(report["semantic_mismatches"])
    if report["completion_status"] == "completed" and (
        any(report["failure_counts"].values()) or report["semantic_mismatch_count"]
    ):
        report["completion_status"] = "completed_with_observations"
    output = Path(__file__).resolve().parents[1] / "outputs/vimo_stability"
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"stability_{timestamp.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"\nStatus: {report['completion_status']}\nJSON: {destination}")
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "counts",
                    "failure_counts",
                    "semantic_mismatch_count",
                    "stop_reason",
                    "timing_statistics",
                    "resource_trends",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        "Descriptive stability measurements only; no automatic memory-leak or latency-drift verdict."
    )
    if report["completion_status"] == "interrupted":
        return 130
    return int(
        report["completion_status"] == "aborted"
        or bool(
            report["failure_counts"]["inference"]
            or report["failure_counts"]["response_processing"]
            or report["semantic_mismatch_count"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
