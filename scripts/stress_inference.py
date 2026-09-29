"""Soak one serial engine: six calls per round, fixed decoded pool, no retries.

Results are released after semantic checks; only timing arrays, bounded mismatch
examples and periodic resources are retained. No automatic leak/drift verdict.
"""

import argparse
import csv
import ctypes
import math
import os
import shutil
import subprocess
import time
import traceback
from ctypes import wintypes
from pathlib import Path
from typing import TypedDict

import numpy as np
from _tool_helpers import (
    engine_metadata,
    load_pool,
    nonnegative_int,
    output_directory,
    positive_int,
    run_cli,
    semantic_signature,
    statistics,
    write_json,
)
from numpy.typing import NDArray

from vimo_demo.inference import VimoSegmentationEngine


class MemoryCounters(ctypes.Structure):
    # PROCESS_MEMORY_COUNTERS_EX from Win32; SIZE_T fields are pointer-sized.
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
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
    ]
    cb: int
    WorkingSetSize: int
    PrivateUsage: int


class Resources(TypedDict):
    round: int
    rss_bytes: int | None
    private_bytes: int | None
    gpu_device_used_mib: float | None
    gpu_device_total_mib: float | None
    gpu_process_used_mib: float | None
    unavailable: dict[str, str]


def smi(query: str) -> list[list[str]]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        raise FileNotFoundError("nvidia-smi unavailable")
    result = subprocess.run(
        [executable, query, "--format=csv,noheader,nounits", "-i", "0"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))


def memory_value(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Invalid memory value: {value!r}")
    return number


def sample_resources(round_number: int, gpu: bool) -> Resources:
    sample: Resources = {
        "round": round_number,
        "rss_bytes": None,
        "private_bytes": None,
        "gpu_device_used_mib": None,
        "gpu_device_total_mib": None,
        "gpu_process_used_mib": None,
        "unavailable": {},
    }
    try:
        if os.name != "nt":
            raise OSError("Working set/private bytes sampler requires Windows")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.argtypes = []
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(MemoryCounters),
            wintypes.DWORD,
        ]
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sample.update(
            rss_bytes=counters.WorkingSetSize, private_bytes=counters.PrivateUsage
        )
    except OSError as exc:
        sample["unavailable"]["process"] = str(exc)
    if gpu:
        try:
            rows = smi("--query-gpu=memory.used,memory.total")
            if len(rows) != 1 or len(rows[0]) != 2:
                raise ValueError(f"Unexpected device memory output: {rows!r}")
            sample["gpu_device_used_mib"] = memory_value(rows[0][0])
            sample["gpu_device_total_mib"] = memory_value(rows[0][1])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sample["unavailable"]["device_gpu"] = str(exc)
        try:
            rows = [
                r
                for r in smi("--query-compute-apps=pid,used_gpu_memory")
                if len(r) == 2 and r[0].strip() == str(os.getpid())
            ]
            if len(rows) != 1:
                raise ValueError("No unique process memory record")
            sample["gpu_process_used_mib"] = memory_value(rows[0][1])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sample["unavailable"]["process_gpu"] = (
                f"{exc}; unavailable (often Windows/WDDM N/A), not zero"
            )
    return sample


def drift(values: NDArray[np.float64]) -> dict[str, object]:
    # Chronological positions retained, including NaN for failed/incomplete work.
    width = max(1, math.ceil(len(values) * 0.1))
    middle = max(0, (len(values) - width) // 2)
    first, mid, last = (
        statistics(segment)
        for segment in (
            values[:width],
            values[middle : middle + width],
            values[-width:],
        )
    )
    ratios: dict[str, float | None] = {}
    for key in ("mean_ms", "p95_ms"):
        a, b = first[key], last[key]
        ratios[key] = (b / a - 1) * 100 if a and b is not None else None
    return {
        "first_10_percent": first,
        "middle_10_percent": mid,
        "last_10_percent": last,
        "last_vs_first_change_percent": ratios,
        "note": "ceil(10%) samples; short runs may overlap",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("images", type=Path)
    parser.add_argument("--pool-size", type=positive_int, default=24)
    parser.add_argument("--rounds", type=positive_int, default=5000)
    parser.add_argument(
        "--warmup",
        type=nonnegative_int,
        default=20,
        help="Individual calls before baselines",
    )
    parser.add_argument("--sample-interval", type=positive_int, default=100)
    parser.add_argument(
        "--gpu-memory",
        action="store_true",
        help="Query device 0 with nvidia-smi outside timing",
    )
    args = parser.parse_args()
    if args.pool_size < 6:
        parser.error("--pool-size must be at least 6")
    pool = load_pool(args.images, args.pool_size)
    calls = np.full(args.rounds * 6, np.nan)
    rounds = np.full(args.rounds, np.nan)
    samples: list[Resources] = []
    mismatches: list[dict[str, object]] = []
    baselines: list[dict[str, object]] = []
    report: dict[str, object] = {
        "settings": vars(args) | {"model": str(args.model), "images": str(args.images)},
        "pool": [p.name for p, _ in pool],
        "calls_per_round": 6,
    }
    attempted = completed_rounds = mismatch_count = 0
    phase = "initialization"
    status = "completed"
    failure: dict[str, object] | None = None
    try:
        with VimoSegmentationEngine(args.model) as engine:
            report.update(engine_metadata(engine.info))
            phase = "warmup"
            for index in range(args.warmup):
                engine.infer(pool[index % len(pool)][1])
            phase = "baselines"
            baselines = [semantic_signature(engine.infer(image)) for _, image in pool]
            samples.append(sample_resources(0, args.gpu_memory))
            try:
                for round_index in range(args.rounds):
                    started_round = time.perf_counter()
                    for _ in range(6):
                        index = attempted
                        pool_index = index % len(pool)
                        attempted += 1
                        phase = "inference"
                        started = time.perf_counter()
                        result = engine.infer(pool[pool_index][1])
                        calls[index] = (time.perf_counter() - started) * 1000
                        phase = "semantic check"
                        observed = semantic_signature(result)
                        del result
                        if observed != baselines[pool_index]:
                            mismatch_count += 1
                            if len(mismatches) < 20:
                                mismatches.append(
                                    {
                                        "round": round_index + 1,
                                        "call": index + 1,
                                        "filename": pool[pool_index][0].name,
                                        "baseline": baselines[pool_index],
                                        "observed": observed,
                                    }
                                )
                    rounds[round_index] = (time.perf_counter() - started_round) * 1000
                    completed_rounds += 1
                    if completed_rounds % args.sample_interval == 0:
                        phase = "sampling"
                        samples.append(
                            sample_resources(completed_rounds, args.gpu_memory)
                        )
                        print(
                            f"[{completed_rounds}/{args.rounds}] mismatches={mismatch_count} mean_round_ms={float(np.nanmean(rounds[:completed_rounds])):.3f}",
                            flush=True,
                        )
            finally:
                samples.append(sample_resources(completed_rounds, args.gpu_memory))
            phase = "cleanup"
    except (Exception, KeyboardInterrupt) as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "aborted"
        failure = {
            "phase": phase,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
            "call": attempted,
            "filename": pool[(attempted - 1) % len(pool)][0].name
            if attempted
            else None,
        }
        traceback.print_exc()
    if status == "completed" and mismatch_count:
        status = "completed_with_mismatches"
    measured_calls = calls[:attempted]
    measured_rounds = rounds[:completed_rounds]
    report.update(
        status=status,
        failure=failure,
        inference_failures=int(failure is not None and failure["phase"] == "inference"),
        successful_calls=int(np.count_nonzero(np.isfinite(measured_calls))),
        completed_rounds=completed_rounds,
        baseline_signatures=baselines,
        semantic_mismatches=mismatch_count,
        mismatch_examples=mismatches,
        call_statistics=statistics(measured_calls),
        round_statistics=statistics(measured_rounds),
        call_drift=drift(measured_calls),
        round_drift=drift(measured_rounds),
        resources=samples,
        notes="Timings include wrapper normalization/copies; rounds also include semantic checks/release. "
        "Sampling/printing are outside timing. Device GPU memory includes other applications. "
        "Private bytes are committed memory. No automatic leak/drift verdict. "
        "Stop on first exception; no retry. Mismatch examples capped at 20. No per-call results retained.",
    )
    destination = output_directory("stress") / "stress.json"
    write_json(destination, report)
    print(
        f"{status}: calls={report['successful_calls']}, mismatches={mismatch_count}; JSON: {destination}"
    )
    return 0 if status == "completed" else 130 if status == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
