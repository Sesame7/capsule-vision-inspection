"""S1-E1-only scheduling harness; deliberately not a Production runtime API."""

import hashlib
import json
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from queue import Queue
from threading import Condition, Thread
from time import perf_counter

import numpy as np
from numpy.typing import NDArray

from vimo_demo.application import ConfiguredProductionPipeline
from vimo_demo.configuration import ResolvedInspectionConfiguration
from vimo_demo.inference import ImageResult
from vimo_demo.inspection import (
    CameraId,
    CollectedCycle,
    CollectionStatus,
    CycleId,
    Frame,
    FrameId,
    ReceivedFrame,
)
from vimo_demo.inspection.production import (
    CameraInspectionResult,
    ChannelId,
    FindingSource,
    InspectionLayout,
    ProductJudgment,
)
from vimo_demo.runtime import InferenceWorker


def json_default(value: object) -> str:
    if isinstance(value, Enum):
        return value.name
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, default=json_default, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class CameraObservation:
    camera: CameraId
    sequence: int
    semantic: str
    geometry: str
    waiting_ms: float
    inference_ms: float | None
    checks_ms: float
    mapping_ms: float
    model_ng_channels: tuple[int, ...]
    technical_error_channels: tuple[int, ...]
    # Compact evidence only. No ImageResult, mask, contour or source pixels.
    detail: dict[str, object]


def compact(
    camera: CameraInspectionResult, raw: ImageResult | None
) -> dict[str, object]:
    return {
        "camera": camera.camera_id,
        "label_map": dict(raw.label_map) if raw else None,
        "regions_per_label": raw.regions_per_label if raw else None,
        "channels": [
            {
                "id": row.channel_id,
                "assessment": asdict(row.assessment),
                "business": row.business_status.name,
                "occupancy": asdict(row.occupancy) if row.occupancy else None,
                "skip": row.traditional_skip,
                "checks": [
                    {
                        "source": check.source,
                        "assessment": asdict(check.assessment),
                        "required": check.required,
                        "reasons": [
                            (r.code, r.label, r.exception_type) for r in check.reasons
                        ],
                        "measurements": [asdict(m) for m in check.measurements],
                    }
                    for check in row.checks
                ],
                "assigned_labels": sorted(f.finding.label for f in row.vimo_findings),
            }
            for row in camera.channels
        ],
        "unassigned_labels": sorted(f.label for f in camera.unassigned_findings),
    }


@dataclass(frozen=True)
class Job:
    cycle: int
    started: float
    deadline: float
    frames: tuple[ReceivedFrame, ...]
    mode: str


class Workload:
    """A uses the real six-camera entry; B projects one camera through that entry.

    The formal layout requires contiguous CH1..N. Projection renumbers only the
    six channel identities locally, preserves resolved checks/ROI/camera/model,
    and restores global identities (including finding assignments) immediately.
    No private pipeline methods or alternate inspection algorithms are used.
    """

    def __init__(self, configuration: ResolvedInspectionConfiguration) -> None:
        self.configuration = configuration
        self.full = ConfiguredProductionPipeline(configuration)
        self.single: dict[CameraId, ConfiguredProductionPipeline] = {}
        for camera in configuration.layout.cameras:
            slots = tuple(
                replace(slot, channel_id=ChannelId(i + 1))
                for i, slot in enumerate(camera.slots)
            )
            layout = InspectionLayout((replace(camera, slots=slots),))
            channels = tuple(
                replace(
                    configuration.channels[slot.channel_id - 1],
                    channel_id=ChannelId(i + 1),
                )
                for i, slot in enumerate(camera.slots)
            )
            self.single[camera.camera_id] = ConfiguredProductionPipeline(
                replace(configuration, layout=layout, channels=channels)
            )

    def execute(
        self, job: Job, infer: Callable[[NDArray[np.uint8]], ImageResult]
    ) -> tuple[CameraObservation, ...]:
        camera_id = job.frames[0].frame.id.camera_id
        pipeline = self.full if job.mode == "A" else self.single[camera_id]
        cycle = CollectedCycle(
            CycleId(job.cycle),
            pipeline.configuration.layout.camera_ids,
            job.started,
            job.deadline,
            max(r.arrived_at for r in job.frames),
            CollectionStatus.READY,
            job.frames,
        )
        execution_started = perf_counter()
        raw, production = pipeline.inspect(cycle, infer)
        observations: list[CameraObservation] = []
        for camera, outcome in zip(production.cameras, raw.frames, strict=True):
            if job.mode == "B":
                original = next(
                    c
                    for c in self.configuration.layout.cameras
                    if c.camera_id == camera.camera_id
                )
                camera = replace(
                    camera,
                    layout=original,
                    channels=tuple(
                        replace(
                            row,
                            channel_id=slot.channel_id,
                            vimo_findings=tuple(
                                replace(f, channel_id=slot.channel_id)
                                for f in row.vimo_findings
                            ),
                        )
                        for row, slot in zip(
                            camera.channels, original.slots, strict=True
                        )
                    ),
                )
            expected = next(
                r.frame.id
                for r in job.frames
                if r.frame.id.camera_id == camera.camera_id
            )
            if camera.frame_id != expected or outcome.frame_id != expected:
                raise AssertionError("Camera/FrameId corruption")
            detail = compact(camera, outcome.result)
            detail["failure"] = asdict(outcome.failure) if outcome.failure else None
            timing = camera.timing
            checks = (timing.occupancy_ms or 0) + (timing.traditional_ms or 0)
            # A's public pipeline exposes GPU start and CPU-stage durations, not
            # an exact camera dispatch milestone. This derived wait excludes
            # those CPU checks; disabled-inference cameras use accumulated stages.
            camera_start = (
                outcome.started_at - checks / 1000
                if outcome.started_at is not None
                else execution_started
            )
            observations.append(
                CameraObservation(
                    camera.camera_id,
                    expected.sequence,
                    fingerprint(detail),
                    fingerprint(
                        [
                            (f.label, f.area, asdict(f.bounding_rect))
                            for f in (outcome.result.regions if outcome.result else ())
                        ]
                    ),
                    max(0.0, camera_start - outcome.arrived_at) * 1000,
                    timing.vimo_ms,
                    checks,
                    timing.mapping_ms,
                    tuple(
                        row.channel_id
                        for row in camera.channels
                        if any(
                            check.source is FindingSource.VIMO
                            and check.assessment.judgment is ProductJudgment.NG
                            for check in row.checks
                        )
                    ),
                    tuple(
                        row.channel_id
                        for row in camera.channels
                        if row.assessment.technical_status.name == "ERROR"
                    ),
                    detail,
                )
            )
            execution_started = camera_start + timing.total_ms / 1000
        return tuple(observations)


@dataclass
class Pending:
    started: float
    deadline: float
    frames: dict[CameraId, ReceivedFrame] = field(
        default_factory=dict[CameraId, ReceivedFrame]
    )
    arrivals: dict[CameraId, float] = field(default_factory=dict[CameraId, float])
    results: dict[CameraId, CameraObservation] = field(
        default_factory=dict[CameraId, CameraObservation]
    )
    copy_ms: float = 0


class Schedule:
    """Single-owner state with explicit time; one claimed nonpreemptible job.

    Cycle IDs must increase at begin; no tombstone/history lookup is required.
    Terminal records contain scalars only and are bounded by the finite CLI run.
    Pixel retention is <= capacity*6 + 6 active frames, even after a deadline.
    Target deadline rejection below is diagnostic, NEVER a Production result.
    """

    def __init__(
        self,
        cameras: tuple[CameraId, ...],
        mode: str,
        *,
        capacity: int = 4,
        deadline: float = 0.3,
        channel_ids: tuple[tuple[int, ...], ...] | None = None,
    ) -> None:
        if mode not in ("A", "B") or capacity < 1 or deadline <= 0:
            raise ValueError("Invalid experiment settings")
        if len(cameras) != 6 or len(set(cameras)) != 6:
            raise ValueError("Expected six distinct camera identities")
        self.cameras, self.mode = cameras, mode
        self.capacity, self.deadline = capacity, deadline
        self.channel_ids = channel_ids or tuple(
            tuple(i * 6 + slot + 1 for slot in range(6)) for i in range(6)
        )
        self.pending: dict[int, Pending] = {}
        self.queue: deque[tuple[int, CameraId | None]] = deque()
        self.active: Job | None = None
        self.records: list[dict[str, object]] = []
        self.camera_rows: list[dict[str, object]] = []
        self.last_id = -1
        self.max_pending = self.max_queue = self.max_pixels = 0
        self.rejected_arrivals = self.late_completions = 0
        self.depths: list[tuple[float, int, int]] = []

    def depth(self, now: float) -> None:
        pixels = sum(len(p.frames) for p in self.pending.values())
        pixels += len(self.active.frames) if self.active else 0
        self.max_pixels = max(self.max_pixels, pixels)
        self.max_pending = max(self.max_pending, len(self.pending))
        self.max_queue = max(self.max_queue, len(self.queue))
        self.depths.append((now, len(self.pending), len(self.queue)))

    def begin(self, cycle: int, now: float) -> bool:
        if cycle <= self.last_id:
            raise ValueError("Experiment cycle IDs must strictly increase")
        self.last_id = cycle
        self.expire(now)
        if len(self.pending) == self.capacity:
            self.records.append({"cycle": cycle, "status": "OVERLOAD", "ready": now})
            return False
        self.pending[cycle] = Pending(now, now + self.deadline)
        self.depth(now)
        return True

    def arrive(self, frame: Frame, now: float, *, copy_ms: float = 0) -> bool:
        self.expire(now)
        state = self.pending.get(frame.id.cycle_id)
        if state is None:
            self.rejected_arrivals += 1
            return False  # Never reassociate an orphan/late frame.
        camera = frame.id.camera_id
        if camera not in self.cameras or camera in state.arrivals:
            raise ValueError("Unknown or duplicate camera frame")
        state.arrivals[camera] = now
        state.frames[camera] = ReceivedFrame(frame, now)
        state.copy_ms += copy_ms
        if self.mode == "B":
            self.queue.append((frame.id.cycle_id, camera))
        elif len(state.arrivals) == 6:
            self.queue.append((frame.id.cycle_id, None))
        self.depth(now)
        return True

    def claim(self, now: float) -> Job | None:
        self.expire(now)
        if self.active is not None or not self.queue:
            return None
        cycle, camera = self.queue.popleft()
        state = self.pending[cycle]
        frames = (
            (state.frames.pop(camera),)
            if camera is not None
            else tuple(state.frames.pop(c) for c in self.cameras)
        )
        self.active = Job(cycle, state.started, state.deadline, frames, self.mode)
        self.depth(now)
        return self.active

    def finish(
        self,
        job: Job,
        results: tuple[CameraObservation, ...],
        now: float,
    ) -> None:
        if job is not self.active:
            raise ValueError("Unknown/double job completion")
        self.expire(now)
        self.active = None
        state = self.pending.get(job.cycle)
        if state is None:
            self.late_completions += 1
        else:
            expected = tuple(r.frame.id.camera_id for r in job.frames)
            if tuple(r.camera for r in results) != expected or any(
                r.sequence != frame.frame.id.sequence
                for r, frame in zip(results, job.frames, strict=True)
            ):
                raise ValueError("Mismatched job/frame completion")
            state.results.update((r.camera, r) for r in results)
            for result in results:
                self.camera_rows.append(
                    {
                        "cycle": job.cycle,
                        **{
                            key: value
                            for key, value in asdict(result).items()
                            if key != "detail"
                        },
                    }
                )
            if len(state.results) == 6:
                self.terminal(job.cycle, now, "COMPLETE")
        self.depth(now)

    def expire(self, now: float) -> None:
        for cycle, state in tuple(self.pending.items()):
            if now >= state.deadline:
                self.terminal(cycle, state.deadline, "DEADLINE")

    def terminal(self, cycle: int, now: float, status: str) -> None:
        state = self.pending.pop(cycle)
        self.queue = deque(entry for entry in self.queue if entry[0] != cycle)
        missing = [c for c in self.cameras if c not in state.arrivals]
        unfinished = [
            c for c in self.cameras if c in state.arrivals and c not in state.results
        ]
        self.records.append(
            {
                "cycle": cycle,
                "status": status,
                "ready": now,
                "first_to_result_ms": (now - min(state.arrivals.values())) * 1000
                if state.arrivals
                else None,
                "last_to_result_ms": (now - max(state.arrivals.values())) * 1000
                if state.arrivals
                else None,
                "copy_ms": state.copy_ms,
                "completed_cameras": [c for c in self.cameras if c in state.results],
                "missing": missing,
                "unfinished": unfinished,
                "target_timeout_reject_channels": [
                    channel
                    for i, c in enumerate(self.cameras)
                    if c in missing or c in unfinished
                    for channel in self.channel_ids[i]
                ],
                "model_ng_channels": [
                    channel
                    for c in self.cameras
                    if c in state.results
                    for channel in state.results[c].model_ng_channels
                ],
                "technical_error_channels": [
                    channel
                    for c in self.cameras
                    if c in state.results
                    for channel in state.results[c].technical_error_channels
                ],
                "semantic": [
                    state.results[c].semantic if c in state.results else None
                    for c in self.cameras
                ],
                "geometry": [
                    state.results[c].geometry if c in state.results else None
                    for c in self.cameras
                ],
            }
        )


class Runner:
    """One inspection job in flight, two one-slot queues; existing worker owns GPU.

    The scheduler never blocks on infer. Shutdown drains the claimed job and
    joins both actual threads; it cannot forcibly cancel native inference.
    """

    def __init__(self, workload: Workload, worker: InferenceWorker) -> None:
        self.workload, self.worker = workload, worker
        self.commands: Queue[Job | None] = Queue(1)
        self.done: Queue[tuple[Job, tuple[CameraObservation, ...] | BaseException]] = (
            Queue(1)
        )
        self.condition = Condition()
        self.thread = Thread(target=self.run, name="S1E1-Inspection", daemon=False)

    def run(self) -> None:
        while (job := self.commands.get()) is not None:
            try:
                result = self.workload.execute(
                    job, lambda a: self.worker.submit(a).result()
                )
            except BaseException as exc:
                result = exc
            with self.condition:
                self.done.put_nowait((job, result))
                self.condition.notify()
            del job, result

    def close(self) -> None:
        # At most one job can be outstanding. A final unconsumed done item fits
        # in its one-slot queue; the sentinel never bypasses active native work.
        self.commands.put(None)
        self.thread.join()


def owned_frame(
    image: NDArray[np.uint8],
    cycle: int,
    camera: CameraId,
    sequence: int,
) -> tuple[Frame, float]:
    started = perf_counter()
    frame = Frame(
        FrameId(CycleId(cycle), camera, sequence), started, image.copy(order="C")
    )
    return frame, (perf_counter() - started) * 1000


def paced_run(
    runner: Runner,
    images: Sequence[NDArray[np.uint8]],
    mode: str,
    offsets: Sequence[float],
    *,
    cycles: int,
    interval: float,
    capacity: int,
    deadline: float,
) -> tuple[Schedule, list[float]]:
    """Open-loop synthetic arrivals on the host clock, independent of completion."""
    cameras = runner.workload.configuration.layout.camera_ids
    schedule = Schedule(
        cameras,
        mode,
        capacity=capacity,
        deadline=deadline,
        channel_ids=tuple(
            tuple(s.channel_id for s in c.slots)
            for c in runner.workload.configuration.layout.cameras
        ),
    )
    origin = perf_counter()
    # Finite scalar-only input plan; source pixels are decoded once, outside timing.
    initial_count = cycles if interval else 1
    events = sorted(
        [(i * interval, 0, i, -1) for i in range(initial_count)]
        + [
            (i * interval + offset, 1, i, camera)
            for i in range(initial_count)
            for camera, offset in enumerate(offsets)
        ]
    )
    next_cycle = 1
    position = 0
    release_lag: list[float] = []
    while position < len(events) or schedule.pending or schedule.active:
        with runner.condition:
            now = perf_counter()
            schedule.expire(now)
            if not runner.done.empty():
                job, result = runner.done.get_nowait()
                if isinstance(result, BaseException):
                    raise RuntimeError("Experimental inspection job failed") from result
                schedule.finish(job, result, perf_counter())
            if (
                not interval
                and position == len(events)
                and not schedule.pending
                and schedule.active is None
                and next_cycle < cycles
            ):
                due = perf_counter() - origin
                events.extend(
                    sorted(
                        [(due, 0, next_cycle, -1)]
                        + [
                            (due + offset, 1, next_cycle, camera)
                            for camera, offset in enumerate(offsets)
                        ]
                    )
                )
                next_cycle += 1
            while (
                position < len(events)
                and origin + events[position][0] <= perf_counter()
            ):
                due, kind, cycle, index = events[position]
                position += 1
                if kind == 0:
                    schedule.begin(cycle, origin + due)
                elif cycle in schedule.pending:
                    frame, copy_ms = owned_frame(
                        images[index], cycle, cameras[index], cycle
                    )
                    arrived = perf_counter()
                    release_lag.append((arrived - origin - due) * 1000)
                    schedule.arrive(frame, arrived, copy_ms=copy_ms)
                else:
                    schedule.rejected_arrivals += 1
            if (job := schedule.claim(perf_counter())) is not None:
                runner.commands.put_nowait(job)
            deadlines = [p.deadline for p in schedule.pending.values()]
            if position < len(events):
                deadlines.append(origin + events[position][0])
            if position < len(events) or schedule.pending or schedule.active:
                wait = max(0, min(deadlines) - perf_counter()) if deadlines else None
                runner.condition.wait(wait)
    return schedule, release_lag


def simulated_case(
    cameras: tuple[CameraId, ...],
    mode: str,
    *,
    cycles: int = 12,
    service: float = 0.005,
    deadline: float = 0.09,
    capacity: int = 3,
    missing: tuple[int, CameraId] | None = None,
    delayed: tuple[int, CameraId] | None = None,
) -> Schedule:
    """Exact virtual-time fault/overload model, never a GPU timing measurement."""
    schedule = Schedule(cameras, mode, capacity=capacity, deadline=deadline)
    events = sorted(
        [(i * 0.1, 0, i, -1) for i in range(cycles)]
        + [
            (i * 0.1 + (0.12 if delayed == (i, c) else index * 0.001), 1, i, index)
            for i in range(cycles)
            for index, c in enumerate(cameras)
            if missing != (i, c)
        ]
    )
    now = 0.0
    active_end = float("inf")
    while events or schedule.pending or schedule.active:
        now = min(
            events[0][0] if events else float("inf"),
            active_end,
            min((p.deadline for p in schedule.pending.values()), default=float("inf")),
        )
        schedule.expire(now)  # Equality belongs to the deadline, not success.
        if schedule.active and now == active_end:
            job = schedule.active
            observations = tuple(
                CameraObservation(
                    r.frame.id.camera_id,
                    r.frame.id.sequence,
                    "synthetic",
                    "synthetic",
                    0,
                    service * 1000,
                    0,
                    0,
                    (),
                    (),
                    {},
                )
                for r in job.frames
            )
            schedule.finish(job, observations, now)
            active_end = float("inf")
        while events and events[0][0] <= now:
            _, kind, cycle, index = events.pop(0)
            if kind == 0:
                schedule.begin(cycle, now)
            else:
                schedule.arrive(
                    Frame(
                        FrameId(CycleId(cycle), cameras[index], cycle),
                        now,
                        np.zeros((2, 3, 3), dtype=np.uint8),
                    ),
                    now,
                )
        if (job := schedule.claim(now)) is not None:
            active_end = now + service * len(job.frames)
    return schedule
