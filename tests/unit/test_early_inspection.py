"""Controlled clocks/native leases; no camera, PLC or GPU required."""

from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.support.application import WAIT, prepared
from tests.support.cycles import image_result

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.inference import ImageResult
from capsule_vision.inference._validation import uint8_array
from capsule_vision.inspection import CameraId, CycleId, CycleStatus, Frame, FrameId
from capsule_vision.inspection.production import ProductJudgment, TechnicalStatus
from capsule_vision.runtime import CoordinatorCapacityError, InferenceWorker
from capsule_vision.runtime.early_inspection import EarlyInspectionCoordinator


class Clock:
    def __init__(self) -> None:
        self.lock = Lock()
        self.now = 100.0

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def advance(self) -> None:
        with self.lock:
            self.now += 0.101


class Engine:
    def __init__(self, *, block: int = -1, fail: int = -1) -> None:
        self.block, self.fail = block, fail
        self.entered, self.release = Event(), Event()
        self.calls: list[int] = []
        self.owners: list[int] = []
        self.closed = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        assert image_bgr.shape == (20, 60, 3)
        assert image_bgr.dtype == np.uint8 and not image_bgr.flags.writeable
        marker = int(image_bgr[0, 0, 0])
        self.calls.append(marker)
        self.owners.append(get_ident())
        if marker == self.block:
            self.entered.set()
            assert self.release.wait(WAIT)
        if marker == self.fail:
            raise ValueError("Ordinary image failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        self.closed = True
        self.owners.append(get_ident())


def runtime(
    tmp_path: Path, engine: Engine, clock: Clock, capacity: int = 4
) -> EarlyInspectionCoordinator:
    pipeline = ConfiguredProductionPipeline(prepared(tmp_path).configuration)

    def factory() -> Engine:
        engine.owners.append(get_ident())
        return engine

    coordinator = EarlyInspectionCoordinator(
        InferenceWorker(factory),
        pipeline.configuration.layout,
        pipeline.inspect_frame,
        pipeline.forced_camera,
        capacity=capacity,
        clock=clock,
    )
    coordinator.start().result(WAIT)
    return coordinator


def frame(cycle: int, camera: int) -> Frame:
    return Frame(
        FrameId(CycleId(cycle), CameraId(f"CAM{camera}"), 90 - camera),
        10,
        np.full((20, 60, 3), cycle * 10 + camera, np.uint8),
    )


def close(coordinator: EarlyInspectionCoordinator, engine: Engine) -> None:
    engine.release.set()
    coordinator.request_stop()
    assert coordinator.join(WAIT)
    assert engine.closed
    assert not any(t.name in coordinator.thread_names for t in threads())


@pytest.mark.parametrize("fail", [-1, 13])
def test_early_ordered_results_identity_and_ordinary_failure(
    tmp_path: Path, fail: int
) -> None:
    engine, clock = Engine(fail=fail), Clock()
    coordinator = runtime(tmp_path, engine, clock)
    try:
        receipt = coordinator.begin(CycleId(1))
        receipt.accepted.result(WAIT)
        for camera in (6, 2, 3, 1, 5, 4):
            coordinator.submit_frame(frame(1, camera)).result(WAIT)
        result = receipt.result.result(WAIT)
        assert [c.camera_id for c in result.cameras] == list(
            coordinator.layout.camera_ids
        )
        assert [c.channel_id for c in result.channels] == list(range(1, 37))
        assert [c.frame_id for c in result.cameras] == [
            frame(1, i).id for i in range(1, 7)
        ]
        assert engine.calls == [16, 12, 13, 11, 15, 14]
        assert all(row.forced_reject is None for row in result.channels)
        assert result.cycle_status is (
            CycleStatus.FAILED if fail == 13 else CycleStatus.COMPLETE
        )
        if fail == 13:
            assert (
                result.cameras[2].assessment.technical_status is TechnicalStatus.ERROR
            )
            assert result.cameras[2].assessment.judgment is ProductJudgment.UNKNOWN
            assert (
                sum(
                    c.assessment.technical_status is TechnicalStatus.ERROR
                    for c in result.cameras
                )
                == 1
            )
        assert coordinator.failure is None
    finally:
        close(coordinator, engine)
    assert len(set(engine.owners)) == 1 and engine.owners[0] != get_ident()


def test_deadline_preserves_finished_camera_rejects_missing_and_unfinished(
    tmp_path: Path,
) -> None:
    engine, clock = Engine(block=12), Clock()
    coordinator = runtime(tmp_path, engine, clock)
    try:
        receipt = coordinator.begin(CycleId(1))
        coordinator.submit_frame(frame(1, 1))
        coordinator.submit_frame(frame(1, 2))
        assert engine.entered.wait(WAIT)
        clock.advance()
        coordinator.expire()
        result = receipt.result.result(WAIT)
        assert result.cycle_status is CycleStatus.TIMED_OUT
        assert all(c.forced_reject is None for c in result.cameras[0].channels)
        assert result.cameras[1].frame_id == frame(1, 2).id
        assert result.cameras[2].frame_id is None
        assert {
            c.forced_reject.code for c in result.cameras[1].channels if c.forced_reject
        } == {"basler.inspection_deadline"}
        assert {
            c.forced_reject.code for c in result.cameras[2].channels if c.forced_reject
        } == {"basler.missing_camera"}
        assert all(
            c.assessment.judgment is ProductJudgment.NG
            and c.assessment.technical_status is TechnicalStatus.ERROR
            for c in result.channels[6:]
        )
        assert all(not c.vimo_findings for c in result.channels[6:])
        with pytest.raises(ValueError, match="Late"):
            coordinator.submit_frame(frame(1, 3))
        settled = coordinator.when_settled(CycleId(1))
        assert not settled.done() and not settled.cancel()
        assert coordinator.when_settled(CycleId(1)) is settled
        coordinator.request_stop()
        assert not coordinator.join(0) and not engine.closed
        engine.release.set()
        assert coordinator.join(WAIT)
        settled.result(0)
        assert (
            receipt.result.result() is result
        )  # Late native completion cannot overwrite.
    finally:
        close(coordinator, engine)


def test_bounded_admission_duplicate_unknown_and_shutdown(tmp_path: Path) -> None:
    engine, clock = Engine(block=11), Clock()
    coordinator = runtime(tmp_path, engine, clock, capacity=2)
    try:
        first = coordinator.begin(CycleId(1))
        coordinator.submit_frame(frame(1, 1))
        assert engine.entered.wait(WAIT)
        with pytest.raises(ValueError, match="Duplicate"):
            coordinator.submit_frame(frame(1, 1))
        with pytest.raises(ValueError, match="Unknown camera"):
            coordinator.submit_frame(
                replace(frame(1, 2), id=FrameId(CycleId(1), CameraId("other"), 0))
            )
        second = coordinator.begin(CycleId(2))
        for camera in range(1, 7):
            coordinator.submit_frame(frame(2, camera))
        with pytest.raises(CoordinatorCapacityError):
            coordinator.begin(CycleId(3))
        snapshot = coordinator.snapshot()
        assert snapshot.cycles == 2 and snapshot.queued_cameras == 6
        assert snapshot.peak_cycles == 2 and snapshot.peak_queued_cameras <= 12
        coordinator.request_stop()
        coordinator.request_stop()
        assert first.result.result(WAIT).cycle_status is CycleStatus.SHUTDOWN
        assert second.result.result(WAIT).cycle_status is CycleStatus.SHUTDOWN
        assert engine.calls == [11]  # Unclaimed work never starts after Stop.
        assert not coordinator.join(0)
    finally:
        close(coordinator, engine)


def test_many_overlapping_cycles_no_identity_mixing_or_history(tmp_path: Path) -> None:
    engine, clock = Engine(), Clock()
    coordinator = runtime(tmp_path, engine, clock)
    try:
        for cycle in range(1, 21, 2):
            a, b = (
                coordinator.begin(CycleId(cycle)),
                coordinator.begin(CycleId(cycle + 1)),
            )
            for camera in (6, 1, 5, 2, 4, 3):
                coordinator.submit_frame(frame(cycle + 1, camera))
                coordinator.submit_frame(frame(cycle, camera))
            for identity, receipt in ((cycle, a), (cycle + 1, b)):
                result = receipt.result.result(WAIT)
                assert result.cycle_id == identity
                assert all(
                    c.frame_id and c.frame_id.cycle_id == identity
                    for c in result.cameras
                )
        assert len(engine.calls) == 120
        assert coordinator.snapshot().cycles == 0
        assert coordinator.snapshot().peak_cycles <= 2
    finally:
        close(coordinator, engine)


def test_late_native_result_discarded_while_next_cycle_completes(
    tmp_path: Path,
) -> None:
    engine, clock = Engine(block=11), Clock()
    coordinator = runtime(tmp_path, engine, clock)
    try:
        first = coordinator.begin(CycleId(1))
        coordinator.submit_frame(frame(1, 1))
        assert engine.entered.wait(WAIT)
        clock.advance()
        coordinator.expire()
        terminal = first.result.result(WAIT)
        assert terminal.cycle_status is CycleStatus.TIMED_OUT
        second = coordinator.begin(CycleId(2))
        for camera in (6, 1, 5, 2, 4, 3):
            coordinator.submit_frame(frame(2, camera))
        engine.release.set()
        result = second.result.result(WAIT)
        assert result.cycle_status is CycleStatus.COMPLETE
        assert all(c.frame_id and c.frame_id.cycle_id == 2 for c in result.cameras)
        assert first.result.result() is terminal
        assert coordinator.snapshot().late_results == 1
        assert engine.calls == [11, 26, 21, 25, 22, 24, 23]
    finally:
        close(coordinator, engine)
