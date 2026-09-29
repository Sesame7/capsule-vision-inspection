"""Deterministic cycle contracts using tiny owned frames and explicit clock steps."""

from dataclasses import replace
from typing import cast
from weakref import ref

import numpy as np
import pytest
from numpy.typing import NDArray

from vimo_demo.inference import BoundingRect, DefectRegion, ImageResult, RotatedRect
from vimo_demo.inspection import (
    CameraId,
    CollectedCycle,
    CollectionStatus,
    CycleCapacityError,
    CycleCollector,
    CycleDeadlineError,
    CycleId,
    CycleStateError,
    CycleStatus,
    DuplicateFrameError,
    Frame,
    FrameId,
    UnknownCameraError,
    inspect_cycle,
)

CAMERAS = tuple(CameraId(f"camera-{i}") for i in (3, 1, 6, 2, 5, 4))


class Clock:
    value: float = 10.0

    def __call__(self) -> float:
        return self.value


def frame(cycle: int, camera: CameraId, sequence: int = 0) -> Frame:
    return Frame(
        FrameId(CycleId(cycle), camera, sequence),
        2000.0,  # Camera clock deliberately differs from host monotonic time.
        np.full((2, 3, 3), sequence % 256, dtype=np.uint8),
    )


def image_result(image: NDArray[np.uint8], defect: bool = False) -> ImageResult:
    height, width, channels = image.shape
    rect = BoundingRect(0, 0, width, height)
    rotated = RotatedRect(width / 2, height / 2, width, height, 0)
    regions = (
        (DefectRegion("fake-defect", 6, rect, 3, 2, (), (), rotated),) if defect else ()
    )
    return ImageResult(
        (height, width, channels),
        {"fake-defect": 1},
        regions,
        np.zeros((height, width), dtype=np.uint8),
        rect,
        rotated,
        "fake",
    )


def ready_cycle(clock: Clock, cycle_id: int = 1) -> CollectedCycle:
    collector = CycleCollector(CAMERAS, timeout=2, clock=clock)
    collector.begin(CycleId(cycle_id))
    snapshot: CollectedCycle | None = None
    for i, camera in enumerate(reversed(CAMERAS)):
        snapshot = collector.accept(frame(cycle_id, camera, i))
    assert snapshot is not None
    assert collector.active_cycle_ids == ()
    return snapshot


def test_six_frames_out_of_order_inspect_in_configured_order() -> None:
    clock = Clock()
    cycle = ready_cycle(clock)
    seen: list[int] = []

    def inspect(image: NDArray[np.uint8]) -> ImageResult:
        seen.append(int(image[0, 0, 0]))
        clock.value += 0.25
        return image_result(image, defect=True)

    result = inspect_cycle(cycle, inspect, clock=clock)
    assert result.is_complete and result.status is CycleStatus.COMPLETE
    assert result.cycle_id == CycleId(1) and result.missing_cameras == ()
    assert result.expected_cameras == CAMERAS
    assert tuple(item.frame_id.camera_id for item in result.frames) == CAMERAS
    assert seen == list(reversed(range(6)))
    assert all(
        item.result is not None and item.result.has_defects for item in result.frames
    )
    assert all(item.failure is None for item in result.frames)
    assert result.started_at == result.collection_finished_at == 10
    assert result.finished_at == 11.5
    assert result.frames[0].started_at == 10 and result.frames[0].finished_at == 10.25
    assert result.frames[0].captured_at == 2000 and result.frames[0].arrived_at == 10


def test_duplicate_unknown_and_cycle_separation_preserve_first_frame() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=5, clock=clock)
    collector.begin(CycleId(10))
    collector.begin(CycleId(11))
    first = frame(10, CAMERAS[0], 7)
    assert collector.accept(first) is None
    for duplicate in (first, frame(10, CAMERAS[0], 8)):
        with pytest.raises(DuplicateFrameError):
            collector.accept(duplicate)
    with pytest.raises(UnknownCameraError):
        collector.accept(frame(10, CameraId("other")))
    with pytest.raises(CycleStateError, match="not active"):
        collector.accept(frame(12, CAMERAS[1]))
    next_cycle: CollectedCycle | None = None
    for camera in CAMERAS:
        next_cycle = collector.accept(frame(11, camera, 9))
    assert next_cycle is not None and next_cycle.cycle_id == CycleId(11)
    assert collector.active_cycle_ids == (CycleId(10),)
    previous: CollectedCycle | None = None
    for camera in CAMERAS[1:]:
        previous = collector.accept(frame(10, camera))
    assert previous is not None and previous.frames[0].frame is first
    assert all(item.frame.id.cycle_id == CycleId(10) for item in previous.frames)
    assert all(item.frame.id.cycle_id == CycleId(11) for item in next_cycle.frames)


def test_missing_deadline_boundary_late_frame_and_exactly_once_finalization() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=2, clock=clock)
    collector.begin(CycleId(1))
    for camera in CAMERAS[:-1]:
        assert collector.accept(frame(1, camera)) is None
    clock.value = 11.999
    assert collector.expire() == ()
    clock.value = 12
    with pytest.raises(CycleDeadlineError):
        collector.accept(frame(1, CAMERAS[-1]))
    (cycle,) = collector.expire()
    assert cycle.status is CollectionStatus.TIMED_OUT
    assert cycle.missing_cameras == (CAMERAS[-1],)
    assert cycle.finalized_at == cycle.deadline == 12
    assert collector.expire() == ()
    collector.begin(CycleId(2))
    with pytest.raises(CycleStateError):
        collector.accept(frame(1, CAMERAS[-1]))
    with pytest.raises(CycleStateError):
        collector.begin(CycleId(1))
    assert collector.active_cycle_ids == (CycleId(2),)

    def unexpected(image: NDArray[np.uint8]) -> ImageResult:
        pytest.fail("Incomplete cycles must not invoke inference")

    result = inspect_cycle(cycle, unexpected, clock=clock)
    assert result.status is CycleStatus.TIMED_OUT and not result.is_complete
    assert len(result.frames) == 5 and result.missing_cameras == (CAMERAS[-1],)
    assert all(item.result is None and item.failure is None for item in result.frames)
    assert all(
        item.started_at is None and item.finished_at is None for item in result.frames
    )


def test_ready_before_deadline_is_not_retroactively_timed_out() -> None:
    clock = Clock()
    cycle = ready_cycle(clock)
    clock.value = 100  # Timeout covers collection, not synchronous native calls.
    result = inspect_cycle(cycle, image_result, clock=clock)
    assert result.is_complete and result.finished_at == 100
    assert all(
        item.result is not None and not item.result.has_defects
        for item in result.frames
    )


def test_no_frames_timeout_and_shutdown_are_distinct() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=2, clock=clock)
    collector.begin(CycleId(0))
    clock.value = 11
    collector.begin(CycleId(1))
    collector.accept(frame(1, CAMERAS[2]))
    clock.value = 12
    expired, stopped = collector.shutdown()
    assert (
        expired.cycle_id == CycleId(0) and expired.status is CollectionStatus.TIMED_OUT
    )
    assert expired.missing_cameras == CAMERAS
    assert stopped.status is CollectionStatus.SHUTDOWN
    assert stopped.missing_cameras == tuple(c for c in CAMERAS if c != CAMERAS[2])
    result = inspect_cycle(stopped, image_result, clock=clock)
    assert result.status is CycleStatus.SHUTDOWN and not result.is_complete
    assert collector.shutdown() == collector.expire() == ()
    assert collector.active_cycle_ids == ()
    with pytest.raises(CycleStateError, match="shut down"):
        collector.begin(CycleId(2))
    with pytest.raises(CycleStateError, match="shut down"):
        collector.accept(frame(1, CAMERAS[0]))


def test_inspection_failure_keeps_identity_cause_and_other_results() -> None:
    clock = Clock()
    cycle = ready_cycle(clock)
    calls = 0

    def inspect(image: NDArray[np.uint8]) -> ImageResult:
        nonlocal calls
        calls += 1
        clock.value += 0.1
        if calls == 2:
            try:
                raise ValueError("underlying cause")
            except ValueError as exc:
                raise RuntimeError("inspection failed") from exc
        return image_result(image)

    result = inspect_cycle(cycle, inspect, clock=clock)
    assert calls == 6 and result.status is CycleStatus.FAILED and not result.is_complete
    assert result.missing_cameras == ()
    failed = result.frames[1]
    assert failed.frame_id == cycle.frames[1].frame.id and failed.result is None
    assert failed.failure is not None
    assert failed.failure.exception_type == "builtins.RuntimeError"
    assert failed.failure.message == "inspection failed"
    assert "underlying cause" in failed.failure.traceback
    assert sum(item.result is not None for item in result.frames) == 5


def test_wrong_result_shape_is_technical_failure() -> None:
    clock = Clock()
    result = inspect_cycle(
        ready_cycle(clock),
        lambda image: replace(image_result(image), image_shape=(9, 9, 3)),
        clock=clock,
    )
    assert result.status is CycleStatus.FAILED
    assert all(
        item.failure is not None and item.result is None for item in result.frames
    )


def test_capacity_rejects_without_eviction_or_consuming_id() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=1, max_active=1, clock=clock)
    collector.begin(CycleId(1))
    collector.accept(frame(1, CAMERAS[0]))
    with pytest.raises(CycleCapacityError):
        collector.begin(CycleId(2))
    assert collector.active_cycle_ids == (CycleId(1),)
    clock.value += 1
    (cycle,) = collector.expire()
    assert len(cycle.frames) == 1
    collector.begin(CycleId(2))  # Rejected ID wasn't consumed.
    assert collector.active_cycle_ids == (CycleId(2),)
    with pytest.raises(CycleStateError):
        collector.begin(CycleId(2))


def test_expiration_is_cycle_id_order_not_frame_arrival_order() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=1, clock=clock)
    collector.begin(CycleId(10))
    collector.begin(CycleId(20))  # IDs need not be contiguous.
    collector.accept(frame(20, CAMERAS[-1]))
    collector.accept(frame(10, CAMERAS[0]))
    clock.value += 2
    assert tuple(c.cycle_id for c in collector.expire()) == (CycleId(10), CycleId(20))
    with pytest.raises(CycleStateError):
        collector.begin(CycleId(15))


def test_frame_transfer_no_copy_and_collector_releases_source_pixels() -> None:
    clock = Clock()
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    owned = Frame(FrameId(CycleId(1), CAMERAS[0], 0), 0, image)
    assert owned.image is image and not image.flags.writeable
    with pytest.raises(ValueError):
        image[0, 0, 0] = 1
    collector = CycleCollector([CAMERAS[0]], timeout=1, clock=clock)
    collector.begin(CycleId(1))
    cycle = collector.accept(owned)
    assert cycle is not None and cycle.frames[0].frame.image is image
    result = inspect_cycle(cycle, image_result, clock=clock)
    pixels = ref(image)
    del image, owned, cycle
    assert pixels() is None  # Neither collector nor final result retains source pixels.
    assert result.frames[0].result is not None
    with pytest.raises(CycleStateError):
        collector.accept(frame(1, CAMERAS[0]))


@pytest.mark.parametrize("kind", ["view", "strided", "dtype", "shape", "empty"])
def test_bad_frame_storage_is_rejected(kind: str) -> None:
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    if kind == "view":
        image = image.view()
    elif kind == "strided":
        image = image[:, ::-1, :]
    elif kind == "dtype":
        image = cast(NDArray[np.uint8], np.zeros((2, 3, 3), dtype=np.float32))
    elif kind == "shape":
        image = np.zeros((2, 3, 1), dtype=np.uint8)
    else:
        image = np.zeros((0, 3, 3), dtype=np.uint8)
    with pytest.raises((TypeError, ValueError)):
        Frame(FrameId(CycleId(1), CAMERAS[0], 0), 0, image)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_timeout_configuration_must_be_positive_finite(timeout: float) -> None:
    with pytest.raises(ValueError):
        CycleCollector(CAMERAS, timeout=timeout)


def test_invalid_identity_configuration_and_clock_do_not_accept_frames() -> None:
    with pytest.raises(ValueError):
        FrameId(CycleId(-1), CAMERAS[0], 0)
    with pytest.raises(ValueError):
        FrameId(CycleId(1), CameraId(" "), 0)
    with pytest.raises(ValueError):
        FrameId(CycleId(1), CAMERAS[0], -1)
    for cameras in ((), (CAMERAS[0], CAMERAS[0]), (CameraId(""),)):
        with pytest.raises(ValueError):
            CycleCollector(cameras, timeout=1)
    with pytest.raises(ValueError):
        CycleCollector(CAMERAS, timeout=1, max_active=0)
    clock = Clock()
    collector = CycleCollector(CAMERAS, timeout=1, clock=clock)
    collector.begin(CycleId(1))
    clock.value -= 1
    with pytest.raises(ValueError, match="backwards"):
        collector.accept(frame(1, CAMERAS[0]))
    clock.value = float("nan")
    with pytest.raises(ValueError, match="finite"):
        collector.expire()
    clock.value = 10
    (cycle,) = collector.shutdown()
    assert cycle.frames == ()


def test_inspection_requires_same_monotonic_time_domain() -> None:
    clock = Clock()
    cycle = ready_cycle(clock)
    clock.value -= 1
    with pytest.raises(ValueError, match="predates"):
        inspect_cycle(cycle, image_result, clock=clock)


def test_fatal_inspector_exception_is_not_disguised_as_normal_failure() -> None:
    clock = Clock()

    def interrupt(image: NDArray[np.uint8]) -> ImageResult:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        inspect_cycle(ready_cycle(clock), interrupt, clock=clock)


def test_snapshot_cannot_claim_ready_with_missing_mixed_or_reordered_frames() -> None:
    cycle = ready_cycle(Clock())
    with pytest.raises(ValueError, match="every camera"):
        replace(cycle, frames=cycle.frames[:-1])
    with pytest.raises(ValueError, match="expected camera order"):
        replace(cycle, frames=tuple(reversed(cycle.frames)))
    with pytest.raises(ValueError, match="expected camera order"):
        replace(cycle, cycle_id=CycleId(2))
    with pytest.raises(ValueError, match="before the deadline"):
        replace(cycle, finalized_at=cycle.deadline)
    with pytest.raises(ValueError, match="collection interval"):
        replace(
            cycle, frames=(replace(cycle.frames[0], arrived_at=9), *cycle.frames[1:])
        )


def test_configured_camera_count_and_shapes_are_not_hard_coded() -> None:
    clock = Clock()
    collector = CycleCollector(CAMERAS[:2], timeout=1, clock=clock)
    collector.begin(CycleId(1))
    assert collector.accept(frame(1, CAMERAS[0])) is None
    second = Frame(
        FrameId(CycleId(1), CAMERAS[1], 5), 1, np.zeros((4, 7, 3), dtype=np.uint8)
    )
    cycle = collector.accept(second)
    assert cycle is not None
    result = inspect_cycle(cycle, image_result, clock=clock)
    assert result.is_complete
    assert [r.image_shape for r in result.frames] == [(2, 3, 3), (4, 7, 3)]
