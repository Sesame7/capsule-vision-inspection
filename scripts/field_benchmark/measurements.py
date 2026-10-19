"""Live-only acceptance checks and scalar timing/resource summaries after drain."""

from collections import Counter, defaultdict
from typing import cast

from .analyze import Row, frame_key


def distribution(values: list[float]) -> Row:
    values.sort()
    return {
        "count": len(values),
        **{
            key: values[(len(values) - 1) * n // 100] if values else None
            for key, n in (
                ("min", 0),
                ("p50", 50),
                ("p95", 95),
                ("p99", 99),
                ("max", 100),
            )
        },
    }


def live_measurements(rows: list[Row], manifest: Row) -> tuple[list[str], Row]:
    by: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by[str(row["kind"])].append(row)
    issues: list[str] = []
    for kind in (
        "run_guard",
        "run_failure",
        "cleanup_failure",
        "resource_error",
        "controller_failure",
        "supervisor_failure",
        "inference_error",
        "engine_initialization_failed",
        "report_rejected",
        "report_skipped",
    ):
        if by[kind]:
            issues.append(f"Live failure observation: {kind}")
    for kind in (
        "production_ready",
        "accounting_final",
        "source_armed",
        "engine_created",
        "engine_closed",
    ):
        if len(by[kind]) != 1:
            issues.append(f"Expected one live {kind} observation")
    ready = by["production_ready"]
    calls = by["inference_finished"]
    warmup = [
        r for r in calls if ready and cast(float, r["at"]) < cast(float, ready[0]["at"])
    ]
    production_calls = [
        r
        for r in calls
        if ready and cast(float, r["at"]) >= cast(float, ready[0]["at"])
    ]
    engine_events = by["engine_created"] + calls + by["engine_closed"]
    if len({r.get("thread") for r in engine_events}) != 1 or any(
        r.get("thread") is None for r in engine_events
    ):
        issues.append("Serial engine thread ownership unverified")
    if (
        not warmup
        or len(production_calls) != 6 * cast(int, manifest["pulse_budget"])
        or any(r.get("succeeded") is not True for r in calls)
    ):
        issues.append("Actual warm-up/production inference evidence incomplete")
    if ready and (
        ready[0].get("cycles_started") != 0 or ready[0].get("cycles_completed") != 0
    ):
        issues.append("Warm-up affected Production counters")
    if (
        not by["final_photo_readback"]
        or by["final_photo_readback"][-1].get("low") is not True
    ):
        issues.append("Final independent W10.7 low readback missing")
    if (
        not by["saved_definitions_final"]
        or by["saved_definitions_final"][-1].get("unchanged") is not True
    ):
        issues.append("Saved definition integrity unverified")
    serials = cast(list[str], manifest["serials"])
    cameras = cast(list[str], manifest["camera_ids"])
    connected = by["connected_camera"]
    if [(r.get("camera"), r.get("serial")) for r in connected] != list(
        zip(cameras, serials, strict=True)
    ):
        issues.append("Connected camera mapping differs from frozen configuration")
    cleaned = by["camera_cleanup"]
    if Counter(r.get("serial") for r in cleaned) != Counter(serials) or any(
        r.get("safe") is not True for r in cleaned
    ):
        issues.append("Six actual safe camera cleanups missing")
    accounting = by["accounting_final"]
    terminals = by["cycle_terminal"]
    business = Counter(
        s for r in terminals for s in cast(list[str], r.get("business", []))
    )
    if accounting:
        final = accounting[-1]
        if (
            final.get("state") != "READY"
            or final.get("cycles_started") != len(terminals)
            or final.get("cycles_completed") != len(terminals)
        ):
            issues.append("Final READY/accounting differs from terminal delivery")
        for status in ("OK", "NG", "EMPTY", "UNKNOWN"):
            if final.get(f"business_{status.lower()}") != business[status]:
                issues.append(f"Final {status} accounting mismatch")
    # Connection-lifetime ordinals, never timestamp proximity, correlate SDK inputs.
    sdk = by["sdk_metadata"]
    identities = Counter((r.get("camera"), r.get("image_number")) for r in sdk)
    admitted = Counter(
        (r.get("camera"), r.get("sequence")) for r in by["frame_admitted"]
    )
    if identities != admitted:
        issues.append("SDK observation/admitted frame identities differ")
    if any(r.get("succeeded") is not True or r.get("skipped_images") != 0 for r in sdk):
        issues.append("SDK failed/skipped inputs observed")
    source = by["source_final"]
    if (
        not source
        or source[-1].get("received") != len(sdk)
        or source[-1].get("discarded") != 0
    ):
        issues.append("Source receive/discard accounting mismatch")
    if len(terminals) != manifest["pulse_budget"]:
        issues.append("Terminal count differs from finite stage budget")
    clocks = by["clock"]
    same_clock = (
        bool(clocks)
        and len({(r.get("host"), r.get("implementation")) for r in clocks}) == 1
    )
    if not same_clock:
        issues.append("Cross-process monotonic clock provenance differs")
    sends = sorted(by["activation_send"], key=lambda r: cast(float, r["at"]))
    times = [cast(float, r["at"]) for r in sends]
    intervals = [(b - a) * 1000 for a, b in zip(times, times[1:], strict=False)]
    group_times = [
        cast(float, r["at"]) for r in sends if cast(int, r["pulse"]) % 4 == 0
    ]
    requests = {(r.get("generation"), r.get("cycle")): r for r in by["cycle_requested"]}
    latencies = [
        (
            cast(float, r["at"])
            - cast(float, requests[(r.get("generation"), r.get("cycle"))]["at"])
        )
        * 1000
        for r in terminals
        if (r.get("generation"), r.get("cycle")) in requests
    ]
    per_camera: dict[str, object] = {}
    for camera in cameras:
        observed = [r for r in sdk if r.get("camera") == camera]
        per_camera[camera] = {
            "sdk_successful": sum(r.get("succeeded") is True for r in observed),
            "sdk_skipped": sum(cast(int, r.get("skipped_images", 0)) for r in observed),
            "missing_ordinals": sorted(
                set(range(1, cast(int, manifest["pulse_budget"]) + 1))
                - {cast(int, r["image_number"]) for r in observed}
            ),
            "vimo_ms": distribution(
                [
                    float(v)
                    for r in by["camera_result"]
                    if r.get("camera") == camera
                    and isinstance(v := r.get("vimo_ms"), (int, float))
                ]
            ),
        }
    last_send = times[-1] if times else None

    def drain(kind: str) -> float | None:
        return (
            max(0, (max(cast(float, r["at"]) for r in by[kind]) - last_send) * 1000)
            if same_clock and last_send is not None and by[kind]
            else None
        )

    resource_fields = (
        "available_memory_bytes",
        "rss_bytes",
        "disk_free_bytes",
        "gpu_temperature_c",
        "gpu_utilization_percent",
        "gpu_memory_mib",
        "gpu_power_w",
        "gpu_graphics_mhz",
        "process_cpu_percent_of_one_core",
        "query_ms",
    )
    files = by["archive_file"]
    start_writes = by["archive_write_started"]
    archive_seconds = (
        (
            max(cast(float, r["at"]) for r in files)
            - min(cast(float, r["at"]) for r in start_writes)
        )
        if files and start_writes
        else None
    )
    return issues, {
        "warmup_calls": len(warmup),
        "warmup_inference_ms": distribution(
            [cast(float, r["duration_ms"]) for r in warmup]
        ),
        "production_inference_ms": distribution(
            [cast(float, r["duration_ms"]) for r in production_calls]
        ),
        "actual_intervals_ms": intervals,
        "intervals_ms": distribution(intervals.copy()),
        "group_intervals_ms": distribution(
            [(b - a) * 1000 for a, b in zip(group_times, group_times[1:], strict=False)]
        ),
        "command_lateness_ms": distribution(
            [
                (cast(float, r["at"]) - cast(float, r["planned_at"])) * 1000
                for r in sends
            ]
        ),
        "interval_rate_hz": (len(times) - 1) / (times[-1] - times[0])
        if len(times) > 1
        else None,
        "nominal_window_rate_hz": len(times) / (cast(int, manifest["groups"]) * 0.5),
        "cycle_request_to_terminal_ms": distribution(latencies),
        "per_camera": per_camera,
        "accounting": accounting[-1] if accounting else None,
        "resources": {
            field: distribution(
                [
                    float(v)
                    for r in by["resources"]
                    if isinstance(v := r.get(field), (int, float))
                ]
            )
            for field in resource_fields
        },
        "runtime_samples": by["runtime_snapshot"],
        "report_samples": by["report_snapshot"],
        "archive_samples": by["archive_snapshot"],
        "drain_after_last_activation_ms": {
            kind: drain(kind)
            for kind in ("cycle_terminal", "report_outcome", "archive_file")
        },
        "archive_encode_window_seconds": archive_seconds,
        "archive_encoded_bytes_per_second": sum(cast(int, r["bytes"]) for r in files)
        / archive_seconds
        if archive_seconds
        else None,
        "frame_keys": [list(frame_key(r)) for r in by["frame_admitted"]],
        "limits": "1 Hz resource samples; sampled queue bounds, not true peaks; host command timing is not an electrical edge; observer overhead included",
    }
