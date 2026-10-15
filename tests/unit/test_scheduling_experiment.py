"""S1-E1 experiment contracts, without ViMo, cameras, PLC or sleep-driven races."""

from dataclasses import replace
from pathlib import Path
from threading import get_ident
from time import perf_counter

import numpy as np
import pytest
from _scheduling_experiment import (
    CameraObservation,
    Job,
    Runner,
    Schedule,
    Workload,
    owned_frame,
    paced_run,
    simulated_case,
)
from tests.application_support import prepared
from tests.cycle_support import image_result

from vimo_demo.inference import ImageResult
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId, ReceivedFrame
from vimo_demo.inspection.production.capsule import CapsuleSettings, ColorSettings
from vimo_demo.runtime import InferenceWorker

CAMERAS = tuple(CameraId(f"CAM{i}") for i in range(1, 7))


def small_frame(cycle: int, index: int, sequence: int = 8) -> Frame:
    pixels = np.full((20, 60, 3), 30, dtype=np.uint8)
    pixels[0, 0] = index
    return Frame(FrameId(CycleId(cycle), CAMERAS[index], sequence), 0, pixels)


def observations(job: Job) -> tuple[CameraObservation, ...]:
    return tuple(
        CameraObservation(
            entry.frame.id.camera_id,
            entry.frame.id.sequence,
            f"{job.cycle}-{entry.frame.id.camera_id}",
            "geometry",
            0,
            1,
            2,
            0,
            (1,) if entry.frame.id.camera_id == CAMERAS[0] else (),
            (),
            {},
        )
        for entry in job.frames
    )


@pytest.mark.parametrize("mode", ["A", "B"])
def test_identity_order_completion_and_no_double_finish(mode: str) -> None:
    schedule = Schedule(CAMERAS, mode)
    schedule.begin(1, 0)
    schedule.begin(2, 0)
    # N+1 can collect and complete first. Invocation/arrival and output orders differ.
    for cycle in (2, 1):
        for index in (5, 1, 3, 2, 4, 0):
            schedule.arrive(small_frame(cycle, index, 5 - index), 0.01)
        while (job := schedule.claim(0.02)) is not None:
            if mode == "A":
                assert [r.frame.id.camera_id for r in job.frames] == list(CAMERAS)
            schedule.finish(job, observations(job), 0.03)
        row = schedule.records[-1]
        assert row["cycle"] == cycle and row["completed_cameras"] == list(CAMERAS)
        assert row["semantic"] == [f"{cycle}-{c}" for c in CAMERAS]
    assert len(schedule.records) == 2
    with pytest.raises(ValueError, match="double"):
        schedule.finish(Job(1, 0, 0.3, (), mode), (), 0.04)


def test_duplicates_unknown_and_wrong_sequence_are_explicit() -> None:
    schedule = Schedule(CAMERAS, "B")
    schedule.begin(1, 0)
    schedule.arrive(small_frame(1, 0), 0.01)
    with pytest.raises(ValueError, match="duplicate"):
        schedule.arrive(small_frame(1, 0), 0.01)
    unknown = Frame(
        FrameId(CycleId(1), CameraId("unknown"), 0),
        0,
        np.zeros((2, 3, 3), dtype=np.uint8),
    )
    with pytest.raises(ValueError, match="Unknown"):
        schedule.arrive(unknown, 0.01)
    job = schedule.claim(0.02)
    assert job
    with pytest.raises(ValueError, match="Mismatched"):
        schedule.finish(job, (replace(observations(job)[0], sequence=123),), 0.03)


def test_deadline_retains_other_cameras_and_separates_model_ng() -> None:
    schedule = Schedule(
        CAMERAS,
        "B",
        deadline=0.1,
        channel_ids=tuple(tuple(range(36 - i * 6, 30 - i * 6, -1)) for i in range(6)),
    )
    schedule.begin(1, 0)
    for index in range(5):
        schedule.arrive(small_frame(1, index), 0.001)
        job = schedule.claim(0.002)
        assert job
        observed = observations(job)
        if index == 0:
            observed = (replace(observed[0], model_ng_channels=(36,)),)
        schedule.finish(job, observed, 0.01)
    schedule.expire(0.1)
    row = schedule.records[0]
    assert row["status"] == "DEADLINE"
    assert row["completed_cameras"] == list(CAMERAS[:5])
    assert row["missing"] == [CAMERAS[5]] and row["unfinished"] == []
    assert row["target_timeout_reject_channels"] == [6, 5, 4, 3, 2, 1]
    assert row["model_ng_channels"] == [36]
    # A later trigger's camera cannot fill the earlier hole, or vice versa.
    schedule.begin(2, 0.11)
    assert not schedule.arrive(small_frame(1, 5), 0.12)
    assert schedule.arrive(small_frame(2, 5), 0.12)
    assert len(schedule.pending[2].frames) == 1
    assert len(schedule.records) == 1


def test_active_native_work_is_not_reused_after_deadline() -> None:
    schedule = Schedule(CAMERAS, "B", deadline=0.1)
    schedule.begin(1, 0)
    schedule.arrive(small_frame(1, 0), 0.01)
    job = schedule.claim(0.02)
    assert job
    schedule.expire(0.1)
    assert schedule.records[0]["unfinished"] == [CAMERAS[0]]
    schedule.begin(2, 0.11)
    schedule.arrive(small_frame(2, 0), 0.12)
    assert schedule.claim(0.13) is None  # Cannot force-cancel an active native call.
    schedule.finish(job, observations(job), 0.14)
    assert schedule.late_completions == 1
    assert not schedule.pending[2].results
    next_job = schedule.claim(0.14)
    assert next_job and next_job.cycle == 2
    assert schedule.records[0]["semantic"] == [None] * 6


@pytest.mark.parametrize("mode", ["A", "B"])
def test_virtual_missing_delayed_and_bounded_overload(mode: str) -> None:
    for kind in ("missing", "delayed"):
        scenario = simulated_case(
            CAMERAS,
            mode,
            missing=(1, CAMERAS[5]) if kind == "missing" else None,
            delayed=(1, CAMERAS[5]) if kind == "delayed" else None,
        )
        assert len(scenario.records) == 12
        row = next(r for r in scenario.records if r["cycle"] == 1)
        assert row["status"] == "DEADLINE"
        assert row["completed_cameras"] == (list(CAMERAS[:5]) if mode == "B" else [])
        assert all(
            r["status"] == "COMPLETE" for r in scenario.records if r["cycle"] != 1
        )
    overload = simulated_case(
        CAMERAS, mode, cycles=50, service=0.025, deadline=1, capacity=3
    )
    assert len(overload.records) == len({r["cycle"] for r in overload.records}) == 50
    assert any(r["status"] == "OVERLOAD" for r in overload.records)
    assert overload.max_pending == 3
    assert overload.max_pixels <= 3 * 6 + 6
    assert overload.max_queue <= (3 if mode == "A" else 18)
    assert not overload.pending and not overload.queue and overload.active is None


@pytest.mark.parametrize("fail_camera", [None, 2])
def test_projection_preserves_resolved_checks_full_image_and_global_identity(
    tmp_path: Path,
    fail_camera: int | None,
) -> None:
    config = prepared(tmp_path).configuration
    config = replace(
        config,
        capsule=CapsuleSettings(color=ColorSettings(enabled=True)),
        channels=tuple(
            replace(
                row,
                checks=replace(
                    row.checks,
                    vimo=replace(
                        row.checks.vimo,
                        enabled=row.channel_id != 1,
                        disabled_labels=("fake-defect",)
                        if row.channel_id == 14
                        else (),
                    ),
                ),
            )
            for row in config.channels
        ),
    )
    workload = Workload(config)
    now = perf_counter()
    entries = tuple(ReceivedFrame(small_frame(4, i, 5 - i), now) for i in range(6))
    calls: list[int] = []

    def infer(image: np.typing.NDArray[np.uint8]) -> ImageResult:
        assert image.shape == (20, 60, 3) and image.dtype == np.uint8
        assert image.flags.owndata and not image.flags.writeable
        index = int(image[0, 0, 0])
        calls.append(index)
        if index == fail_camera:
            raise ValueError("specific camera failed")
        return image_result(image, defect=True)

    full = workload.execute(Job(4, now, now + 10, entries, "A"), infer)
    singles = [
        workload.execute(Job(4, now, now + 10, (entries[i],), "B"), infer)[0]
        for i in (5, 3, 1, 4, 0, 2)
    ]
    by_camera = {r.camera: r for r in singles}
    assert [r.camera for r in full] == list(CAMERAS)
    assert calls[:6] == list(range(6)) and calls[6:] == [5, 3, 1, 4, 0, 2]
    for i, result in enumerate(full):
        single = by_camera[result.camera]
        assert result.sequence == single.sequence == 5 - i
        assert result.semantic == single.semantic and result.geometry == single.geometry
        assert result.detail == single.detail
        assert (result.detail["failure"] is not None) == (i == fail_camera)


def test_real_thread_harness_ownership_copy_and_shutdown(tmp_path: Path) -> None:
    workload = Workload(prepared(tmp_path).configuration)
    images = tuple(np.full((20, 60, 3), 30, dtype=np.uint8) for _ in CAMERAS)
    frame, _ = owned_frame(images[0], 0, CAMERAS[0], 7)
    assert (
        not np.shares_memory(frame.image, images[0]) and not frame.image.flags.writeable
    )
    images[0][:] = 9
    assert frame.image[0, 0, 0] == 30
    owners: list[int] = []

    class Engine:
        def __init__(self) -> None:
            owners.append(get_ident())

        def infer(self, image_bgr: np.typing.NDArray[np.generic]) -> ImageResult:
            owners.append(get_ident())
            assert image_bgr.dtype == np.uint8 and not image_bgr.flags.writeable
            return image_result(np.asarray(image_bgr, dtype=np.uint8))

        def close(self) -> None:
            owners.append(get_ident())

    worker = InferenceWorker(Engine)
    runner = Runner(workload, worker)
    worker.start().result(timeout=5)
    runner.thread.start()
    try:
        for mode in ("A", "B"):
            schedule, lag = paced_run(
                runner,
                images,
                mode,
                [0.001 * i for i in range(6)],
                cycles=3,
                interval=0.01,
                capacity=4,
                deadline=5,
            )
            assert len(schedule.records) == 3 and len(schedule.camera_rows) == 18
            assert len(lag) == 18
            assert all(row["status"] == "COMPLETE" for row in schedule.records)
    finally:
        runner.close()
        worker.request_stop()
        assert worker.join(5)
    assert not runner.thread.is_alive() and not worker.is_alive
    assert len(owners) == 38 and len(set(owners)) == 1 and owners[0] != get_ident()
