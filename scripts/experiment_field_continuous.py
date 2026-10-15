"""S1-E2A finite field-only photo/acquisition/GPU experiment, never Production.

Requires explicit --execute-field. Fixed PLC W10.7 only. No reject writes,
configuration persistence, camera parameter changes, or automatic activations
after an uncertain exchange. Every result remains experimental, not PLC output.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import traceback
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from time import perf_counter

import numpy as np
from _field_basler_experiment import FiniteBackend
from _field_trigger_experiment import (
    HOST,
    PhotoClient,
    pulse,
    release,
    wait_until,
)
from _scheduling_experiment import Job, Runner, Schedule, Workload, json_default
from _tool_helpers import decode, nonnegative_int, positive_int, run_cli, statistics
from benchmark_production import command_info, digest
from stress_inference import sample_resources

from vimo_demo.application._basler_sdk import enumerate_devices
from vimo_demo.configuration import (
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
    validate_model_artifact,
)
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId, ReceivedFrame
from vimo_demo.runtime import InferenceWorker


def save(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value, default=json_default, ensure_ascii=False, indent=2, allow_nan=False
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class Begin:
    cycle: int
    at: float


@dataclass(frozen=True)
class Arrival:
    frame: Frame
    at: float


class Windows:
    """One unresolved capture window, never zip independent frame counters.

    A new pulse is forbidden until all six prior observations and a subsequent
    empty SDK sweep. This is controlled-window evidence, not hardware-tag proof.
    Missing/duplicate/extra observations stop further pulses. No later trigger
    can be used to repair a missing earlier image.
    """

    def __init__(self, serials: tuple[str, ...]) -> None:
        self.serials = serials
        self.lock = Lock()
        self.current: int | None = None
        self.seen: set[str] = set()
        self.quiet = True
        self.last_sequences: dict[str, int] = {}

    def open(self, cycle: int) -> None:
        with self.lock:
            if not self.quiet or (self.current is not None and len(self.seen) != 6):
                raise RuntimeError("Unresolved capture/SDK backlog; no new activation")
            self.current, self.seen, self.quiet = cycle, set(), False

    def empty_sweep(self) -> None:
        with self.lock:
            if self.current is None or len(self.seen) == 6:
                self.quiet = True

    def accept(self, serial: str, sequence: int | None) -> int:
        with self.lock:
            if (
                serial not in self.serials
                or self.current is None
                or serial in self.seen
            ):
                raise RuntimeError(
                    f"Ambiguous extra/duplicate/pretrigger frame: {serial}"
                )
            if sequence is None or sequence != self.last_sequences.get(serial, 0) + 1:
                raise RuntimeError(
                    f"Nonconsecutive SDK retrieval sequence: {serial}/{sequence}"
                )
            self.last_sequences[serial] = sequence
            self.seen.add(serial)
            return self.current


class Trial:
    def __init__(
        self, serials: tuple[str, ...], runner: Runner, pool_size: int
    ) -> None:
        self.serials, self.runner = serials, runner
        self.windows = Windows(serials)
        self.stop, self.armed, self.source_done, self.trigger_done = (
            Event() for _ in range(4)
        )
        self.events: Queue[Begin | Arrival] = Queue(32)
        self.pool_size = pool_size
        self.pool: dict[int, dict[CameraId, Arrival]] = {}
        self.observations: list[dict[str, object]] = []
        self.pulses: list[dict[str, object]] = []
        self.errors: list[dict[str, str]] = []
        self.settings: list[dict[str, object]] = []
        self.cleanup: list[dict[str, object]] = []
        self.readiness: list[dict[str, object]] = []
        self.guard_states: list[dict[str, object]] = []
        self.max_events = 0
        self.trigger_finished_at = 0.0
        self.release_verified = False
        self.failure_lock = Lock()

    def fail(self, stage: str, exc: BaseException) -> None:
        with self.failure_lock:
            if len(self.errors) < 6:
                self.errors.append(
                    {
                        "stage": stage,
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
        self.stop.set()
        with self.runner.condition:
            self.runner.condition.notify_all()

    def publish(self, event: Begin | Arrival) -> None:
        with self.runner.condition:
            self.events.put_nowait(event)
            self.max_events = max(self.max_events, self.events.qsize())
            self.runner.condition.notify_all()

    def acquire(self, count: int, cameras: tuple[CameraId, ...]) -> None:
        backend: FiniteBackend | None = None
        try:
            backend = FiniteBackend(count + 1)
            backend.inventory(self.serials)
            for serial in self.serials:
                settings = backend.prepare(serial)
                self.settings.append(
                    {
                        "serial": serial,
                        "features": dict(settings.features),
                        "triggers": {k: dict(v) for k, v in settings.triggers.items()},
                    }
                )
            self.readiness = [asdict(r) for r in backend.arm()]
            if any(backend.available(serial, 0) for serial in self.serials):
                raise RuntimeError("SDK frame queued before ARMED; no trigger allowed")
            self.armed.set()
            print("ARMED", self.serials, flush=True)
            cursor = 0
            while not self.stop.is_set():
                if (
                    self.trigger_done.is_set()
                    and perf_counter() >= self.trigger_finished_at + 0.2
                ):
                    break
                found = False
                for index, serial in enumerate(self.serials):
                    if not backend.available(serial, 0):
                        continue
                    found = True
                    started = perf_counter()
                    metadata, pixels = backend.retrieve(serial)
                    copied = perf_counter()
                    row: dict[str, object] = {
                        **asdict(metadata),
                        "retrieval_started": started,
                        "owned_pixels_ready": copied,
                        "retrieve_convert_copy_release_ms": (copied - started) * 1000,
                        "conversion_ms": backend.conversion_timer.elapsed_ms
                        if backend.conversion_timer
                        else None,
                    }
                    self.observations.append(row)
                    if (
                        not metadata.succeeded
                        or pixels is None
                        or metadata.skipped_images
                    ):
                        raise RuntimeError(f"SDK failed/skipped acquisition: {row}")
                    cycle = self.windows.accept(serial, metadata.image_number)
                    row["candidate_window"] = cycle
                    # Backend already transferred independent C-owned BGR pixels
                    # before SDK Release. Do not copy the 3 MB image a second time.
                    frame_started = perf_counter()
                    frame = Frame(
                        FrameId(
                            CycleId(cycle), cameras[index], metadata.image_number or 0
                        ),
                        metadata.received_at,
                        pixels,
                    )
                    arrival = Arrival(frame, perf_counter())
                    row["frame_transfer_ms"] = (arrival.at - frame_started) * 1000
                    row["frame_owns_pixels"] = pixels.flags.owndata
                    row["frame_readonly"] = not pixels.flags.writeable
                    if cycle < self.pool_size:
                        self.pool.setdefault(cycle, {})[cameras[index]] = arrival
                    self.publish(arrival)
                if not found:
                    self.windows.empty_sweep()
                    backend.available(self.serials[cursor], 1)
                    cursor = (cursor + 1) % 6
        except BaseException as exc:
            self.fail("acquisition", exc)
        finally:
            if backend is not None:
                self.cleanup = [asdict(r) for r in backend.cleanup()]
                if not all(
                    r["stopped"]
                    and r["settings_restored"]
                    and r["closed"]
                    and r["detached"]
                    and not r["issues"]
                    for r in self.cleanup
                ):
                    self.fail(
                        "camera_cleanup",
                        RuntimeError("Unsafe camera cleanup; do not restart"),
                    )
            self.source_done.set()
            with self.runner.condition:
                self.runner.condition.notify_all()

    def trigger(self, count: int, interval: float, hold_ms: float, full: bool) -> None:
        client: PhotoClient | None = None
        recoveries: list[PhotoClient] = []

        def recovery() -> PhotoClient:
            fresh = PhotoClient(HOST, timeout=0.5)
            recoveries.append(fresh)
            return fresh

        attempted = False
        try:
            if not self.armed.wait(30) or self.stop.is_set():
                raise RuntimeError("No safe ARMED observation")
            client = PhotoClient(HOST, timeout=0.5)
            original = client.snapshot()
            original.require_idle()
            self.guard_states.append({"at": perf_counter(), "words": original.words})
            origin = perf_counter() + 0.05
            for cycle in range(count):
                wait_until(origin + cycle * interval, self.stop)
                if self.stop.is_set():
                    break
                if perf_counter() > origin + cycle * interval + interval / 2:
                    raise RuntimeError("Trigger scheduling overrun; no catch-up pulse")
                guard = client.snapshot()
                guard.require_idle(
                    original
                )  # Also independently verifies previous release.
                self.guard_states.append({"at": perf_counter(), "words": guard.words})
                self.windows.open(cycle)
                at = perf_counter()
                self.publish(Begin(cycle, at))
                row: dict[str, object] = {
                    "cycle": cycle,
                    "planned": origin + cycle * interval,
                    "guard_verified_low": True,
                    "window_opened": at,
                }
                self.pulses.append(
                    row
                )  # Record attempted activation before any network write.
                attempted = True
                pulse(
                    client,
                    hold_ms=hold_ms,
                    full_readback=full,
                    stop=self.stop,
                    recovery=recovery,
                    trace=row,
                )
            client.snapshot().require_idle(original)
            self.release_verified = client.read_photo() == 0
            if not self.release_verified:
                raise RuntimeError("UNRESOLVED RELEASE at final readback")
        except BaseException as exc:
            self.fail("trigger", exc)
            if attempted and client is not None:
                try:
                    fresh, low = release(client, recovery)
                    self.release_verified = low == 0
                    self.guard_states.append(
                        {
                            "release_recovery": True,
                            "fresh_connection": fresh,
                            "low_readback": low,
                            "at": perf_counter(),
                        }
                    )
                except BaseException as release_error:
                    self.fail("UNRESOLVED_RELEASE", release_error)
        finally:
            for owned in ([client] if client else []) + recoveries:
                owned.close()
            self.trigger_finished_at = perf_counter()
            self.trigger_done.set()
            with self.runner.condition:
                self.runner.condition.notify_all()


def consume(trial: Trial, schedule: Schedule) -> None:
    while not (
        trial.source_done.is_set()
        and trial.trigger_done.is_set()
        and trial.events.empty()
        and not schedule.pending
        and schedule.active is None
    ):
        with trial.runner.condition:
            now = perf_counter()
            schedule.expire(now)
            if not trial.runner.done.empty():
                job, result = trial.runner.done.get_nowait()
                if isinstance(result, BaseException):
                    trial.fail("inspection", result)
                    raise RuntimeError("Inspection infrastructure failed") from result
                schedule.finish(job, result, now)
            while not trial.events.empty():
                event = trial.events.get_nowait()
                if isinstance(event, Begin):
                    if not schedule.begin(event.cycle, event.at):
                        trial.fail("overload", RuntimeError("Cycle capacity exhausted"))
                elif not schedule.arrive(event.frame, event.at):
                    trial.fail(
                        "late_frame", RuntimeError("Late/unadmitted captured frame")
                    )
            if (
                not trial.stop.is_set()
                and (job := schedule.claim(perf_counter())) is not None
            ):
                trial.runner.commands.put_nowait(job)
            if (
                trial.source_done.is_set()
                and trial.trigger_done.is_set()
                and trial.events.empty()
            ):
                # Keep observing actual result deadlines, even after trigger failure.
                if not schedule.pending and schedule.active is None:
                    break
            deadlines = [p.deadline for p in schedule.pending.values()]
            wait = max(0, min(deadlines) - perf_counter()) if deadlines else None
            trial.runner.condition.wait(wait)


def replay(
    trial: Trial, mode: str, cameras: tuple[CameraId, ...], deadline: float
) -> Schedule:
    groups = {cycle: rows for cycle, rows in trial.pool.items() if len(rows) == 6}
    by_id = {
        int(str(row["cycle"])): float(str(row["activation_send"]))
        for row in trial.pulses
        if "activation_send" in row
    }
    if not groups:
        raise RuntimeError("No complete captured group for real-data replay")
    trace: list[tuple[float, Begin | Arrival]] = []
    reference = min(by_id[c] for c in groups)
    origin = perf_counter() + 0.02
    for cycle, rows in groups.items():
        at = origin + by_id[cycle] - reference
        trace.append((at, Begin(cycle, at)))
        for row in rows.values():
            arrival = origin + row.at - reference
            trace.append((arrival, Arrival(row.frame, arrival)))
    trace.sort(key=lambda pair: pair[0])
    schedule = Schedule(cameras, mode, capacity=4, deadline=deadline)
    position = 0
    while position < len(trace) or schedule.pending or schedule.active:
        with trial.runner.condition:
            now = perf_counter()
            schedule.expire(now)
            if not trial.runner.done.empty():
                job, result = trial.runner.done.get_nowait()
                if isinstance(result, BaseException):
                    raise RuntimeError("Replay inspection failed") from result
                schedule.finish(job, result, now)
            while position < len(trace) and trace[position][0] <= perf_counter():
                _, event = trace[position]
                position += 1
                if isinstance(event, Begin):
                    schedule.begin(event.cycle, event.at)
                else:
                    schedule.arrive(event.frame, event.at)
            if (job := schedule.claim(perf_counter())) is not None:
                trial.runner.commands.put_nowait(job)
            deadlines = [p.deadline for p in schedule.pending.values()]
            if position < len(trace):
                deadlines.append(trace[position][0])
            if position < len(trace) or schedule.pending or schedule.active:
                trial.runner.condition.wait(
                    max(0, min(deadlines) - perf_counter()) if deadlines else None
                )
    return schedule


def records(schedule: Schedule) -> dict[str, object]:
    completed = [r for r in schedule.records if r["status"] == "COMPLETE"]
    ready = [float(str(r["ready"])) for r in completed]
    return {
        "cycles": schedule.records,
        "camera_results": schedule.camera_rows,
        "terminal_counts": dict(Counter(str(r["status"]) for r in schedule.records)),
        "first_to_result_ms": statistics(
            [float(str(r["first_to_result_ms"])) for r in completed]
        ),
        "last_to_result_ms": statistics(
            [float(str(r["last_to_result_ms"])) for r in completed]
        ),
        "completed_cycles_per_second": (len(ready) - 1) / (ready[-1] - ready[0])
        if len(ready) > 1
        else None,
        "max_outstanding": schedule.max_pending,
        "max_queued_jobs": schedule.max_queue,
        "max_frame_arrays_in_scheduler": schedule.max_pixels,
        "late_completions": schedule.late_completions,
        "depths": schedule.depths,
    }


def ownership_probe(trial: Trial) -> dict[str, object]:
    """Same captured arrays, outside acquisition/GPU timing; no retained copies."""
    borrowed_ms: list[float] = []
    copied_ms: list[float] = []
    fingerprints: dict[str, str] = {}
    for group in trial.pool.values():
        for arrival in group.values():
            original = arrival.frame
            fingerprints[str(original.id)] = hashlib.sha256(original.image).hexdigest()
            for repetition in range(3):
                for copy in (False, True) if repetition % 2 == 0 else (True, False):
                    started = perf_counter()
                    image = original.image.copy(order="C") if copy else original.image
                    transferred = Frame(original.id, original.captured_at, image)
                    elapsed = (perf_counter() - started) * 1000
                    assert np.array_equal(transferred.image, original.image)
                    assert not transferred.image.flags.writeable
                    (copied_ms if copy else borrowed_ms).append(elapsed)
                    del image, transferred
    return {
        "direct_owned_transfer_ms": statistics(borrowed_ms),
        "extra_copy_plus_transfer_ms": statistics(copied_ms),
        "retained_pixel_sha256": fingerprints,
        "boundary": "offline same-array construction probe; validation/hash outside timing; not an end-to-end gain",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-field", action="store_true")
    parser.add_argument("--pulses", type=positive_int, required=True)
    parser.add_argument("--interval-ms", type=positive_int, required=True)
    parser.add_argument("--hold-ms", type=positive_int, required=True)
    parser.add_argument("--verification", choices=("full", "ack"), default="full")
    parser.add_argument("--mode", choices=("A", "B"), default="B")
    parser.add_argument("--deadline-ms", type=positive_int, default=100)
    parser.add_argument("--replay-repetitions", type=nonnegative_int, default=0)
    parser.add_argument("--pool-cycles", type=positive_int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute_field or not 1 <= args.pulses <= 200:
        parser.error("Explicit field execution and a finite count <=200 are required")
    if (
        not 5 <= args.hold_ms < args.interval_ms
        or args.interval_ms < 80
        or args.pool_cycles > 12
    ):
        parser.error("Require 5 <= hold < interval, interval >=80ms, pool <=12 cycles")
    # Explicit process inventory; no process termination or camera discovery by order.
    process = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$owner = Get-CimInstance Win32_Process -Filter 'ProcessId = {os.getpid()}'; "
            "$launcher = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $owner.ParentProcessId); "
            f"$ownIds = @({os.getpid()}); "
            "if ($launcher.Name -eq 'python.exe' -and $launcher.CommandLine -like '*experiment_field_continuous.py*') {$ownIds += $launcher.ProcessId}; "
            "Get-CimInstance Win32_Process | Where-Object {$_.Name -match 'python|pylon' -and $_.ProcessId -notin $ownIds} | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if process.stdout.strip():
        raise RuntimeError(f"Competing camera/Python process: {process.stdout.strip()}")
    args.output.mkdir(parents=True, exist_ok=False)
    code = args.output / "code"
    code.mkdir()
    for name in (
        "experiment_field_continuous.py",
        "_field_trigger_experiment.py",
        "_field_basler_experiment.py",
        "_scheduling_experiment.py",
        "_tool_helpers.py",
        "benchmark_production.py",
        "stress_inference.py",
    ):
        shutil.copyfile(Path(__file__).with_name(name), code / name)
    model_file = Path("data/models/model.json")
    machine_file, recipe_file = (
        Path("data/machine/machine.json"),
        Path("data/recipes/local.recipe.json"),
    )
    model = load_model_definition(model_file)
    model_path = validate_model_artifact(
        model, base_directory=model_file.parent.resolve()
    )
    config = resolve_configuration(
        load_machine_layout(machine_file),
        load_recipe(recipe_file),
        ModelRegistry((model,)),
        model.model_id,
    )
    sources = load_camera_sources(Path("data/sources/camera_sources.json"))
    serials = tuple(row.basler_serial or "" for row in sources.cameras)
    cameras = config.layout.camera_ids
    if (
        sources.mode != "basler"
        or tuple(row.camera_id for row in sources.cameras) != cameras
        or len(set(serials)) != 6
        or "" in serials
    ):
        raise ValueError("Saved six-camera Basler configuration/order required")
    assets = {str(p): digest(p) for p in Path("data").rglob("*") if p.is_file()}
    report: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "settings": vars(args) | {"output": str(args.output)},
        "python": sys.version,
        "platform": platform.platform(),
        "versions": {
            n: version(n)
            for n in (
                "pypylon",
                "numpy",
                "smartmore.vimo.inference",
                "smartmore.vimo.inference.custom",
            )
        },
        "git_revision": command_info(["git", "rev-parse", "HEAD"]),
        "git_status": command_info(["git", "status", "--short"]),
        "gpu": command_info(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,pstate,clocks.gr,temperature.gpu",
                "--format=csv",
            ]
        ),
        "assets_before": assets,
        "devices": [asdict(d) for d in enumerate_devices()],
        "mapping": dict(zip(cameras, serials, strict=True)),
        "resolved_configuration": asdict(config),
        "script_hashes": {
            p.name: digest(p) for p in Path(__file__).parent.glob("*experiment*.py")
        },
        "business_results_accepted": False,
        "identity_basis": "controlled single unresolved trigger window; not hardware-tagged cycle identity",
        "host_clocks": "perf_counter observations; raw camera timestamps not synchronized to PLC/host",
        "completed": False,
    }
    workload = Workload(config)
    worker = InferenceWorker(
        lambda: VimoSegmentationEngine(model_path, module_id=model.module_id)
    )
    runner = Runner(workload, worker)
    trial = Trial(serials, runner, min(args.pool_cycles, args.pulses))
    schedule = Schedule(
        cameras,
        args.mode,
        capacity=4,
        deadline=args.deadline_ms / 1000,
        channel_ids=tuple(
            tuple(s.channel_id for s in c.slots) for c in config.layout.cameras
        ),
    )
    source = Thread(
        target=trial.acquire,
        args=(args.pulses, cameras),
        name="S1E2A-Acquisition",
        daemon=False,
    )
    trigger = Thread(
        target=trial.trigger,
        args=(
            args.pulses,
            args.interval_ms / 1000,
            args.hold_ms,
            args.verification == "full",
        ),
        name="S1E2A-Trigger",
        daemon=False,
    )
    replay_rows: list[dict[str, object]] = []
    try:
        worker.start().result()
        warm_started = perf_counter()
        entries: list[ReceivedFrame] = []
        for index, row in enumerate(sources.cameras):
            paths = sorted(Path(row.folder).glob("*.png"))
            if not paths:
                raise ValueError(f"No saved Folder warmup image for {row.camera_id}")
            pixels = decode(paths[0])
            config.layout.cameras[index].validate_shape(
                (pixels.shape[0], pixels.shape[1], pixels.shape[2])
            )
            entries.append(
                ReceivedFrame(
                    Frame(FrameId(CycleId(0), row.camera_id, 0), warm_started, pixels),
                    warm_started,
                )
            )
        for _ in range(3):
            workload.execute(
                Job(0, warm_started, warm_started + 60, tuple(entries), "A"),
                lambda a: worker.submit(a).result(),
            )
        report["warmup_ms"] = (perf_counter() - warm_started) * 1000
        report["resources_after_warmup"] = sample_resources(0, False)
        runner.thread.start()
        source.start()
        trigger.start()
        consume(trial, schedule)
        source.join()
        trigger.join()
        report["live"] = records(schedule)
        report["resources_after_live"] = sample_resources(args.pulses, False)
        if not trial.errors:
            for repetition in range(args.replay_repetitions):
                for mode in ("A", "B") if repetition % 2 == 0 else ("B", "A"):
                    sampled = replay(trial, mode, cameras, args.deadline_ms / 1000)
                    replay_rows.append(
                        {"repetition": repetition + 1, "mode": mode, **records(sampled)}
                    )
            report["ownership_probe"] = ownership_probe(trial)
        report["completed"] = not trial.errors and len(trial.pulses) == args.pulses
    except BaseException as exc:
        trial.fail("owner", exc)
    finally:
        trial.stop.set()
        for thread in (source, trigger):
            if thread.ident is not None:
                thread.join()  # No force kill / timeout masquerading as termination.
        if runner.thread.ident is not None:
            runner.close()
        worker.request_stop()
        worker.join()
        report.update(
            observations=trial.observations,
            pulses=trial.pulses,
            plc_guards=trial.guard_states,
            errors=trial.errors,
            camera_settings=trial.settings,
            camera_readiness=trial.readiness,
            camera_cleanup=trial.cleanup,
            release_verified=trial.release_verified,
            live=records(schedule),
            replays=replay_rows,
            max_source_event_queue=trial.max_events,
            retained_replay_images=sum(len(g) for g in trial.pool.values()),
            retained_replay_bytes=sum(
                e.frame.image.nbytes for g in trial.pool.values() for e in g.values()
            ),
            cleanup={
                "source_alive": source.is_alive(),
                "trigger_alive": trigger.is_alive(),
                "inspection_alive": runner.thread.is_alive(),
                "worker_alive": worker.is_alive,
                "worker_failure": repr(worker.failure) if worker.failure else None,
            },
            assets_after={
                str(p): digest(p) for p in Path("data").rglob("*") if p.is_file()
            },
            resources_after_shutdown=sample_resources(args.pulses, False),
        )
        if (
            report["assets_before"] != report["assets_after"]
            or worker.failure
            or not trial.release_verified
        ):
            report["completed"] = False
        attempted = sum(bool(row.get("activation_attempted")) for row in trial.pulses)
        report["counts"] = {
            "activation_attempts": attempted,
            "expected_frames": attempted * 6,
            "received_per_serial": dict(
                Counter(str(row["serial"]) for row in trial.observations)
            ),
            "missing_observations": max(0, attempted * 6 - len(trial.observations)),
            "ambiguous_observations": sum(
                "candidate_window" not in row for row in trial.observations
            ),
            "deadline_cycles": sum(
                row["status"] == "DEADLINE" for row in schedule.records
            ),
            "inspection_error_camera_results": sum(
                bool(row["technical_error_channels"]) for row in schedule.camera_rows
            ),
        }
        report["healthy"] = bool(
            report["completed"]
            and len(trial.observations) == args.pulses * 6
            and all(row["status"] == "COMPLETE" for row in schedule.records)
            and len(schedule.records) == args.pulses
            and not any(row["technical_error_channels"] for row in schedule.camera_rows)
        )
        save(args.output / "report.json", report)
    print(
        json.dumps(
            {
                "completed": report["completed"],
                "healthy": report["healthy"],
                "pulses": len(trial.pulses),
                "frames": len(trial.observations),
                "release_verified": trial.release_verified,
                "errors": trial.errors,
                "output": str(args.output),
            },
            default=json_default,
        ),
        flush=True,
    )
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
