"""Additive production consumption of unchanged Phase 1 worker/cycle boundaries."""

from threading import enumerate as threads
from threading import get_ident

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.production_support import (
    ERROR,
    NG,
    OK,
    SUCCESS,
    UNKNOWN,
    Clock,
    inferred,
    layout,
    source_frame,
)

from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inspection import (
    CycleCollector,
    CycleId,
    CycleStatus,
    Frame,
    FrameId,
    inspect_cycle,
)
from vimo_demo.inspection.production import (
    Assessment,
    ProductionPipeline,
)
from vimo_demo.runtime import InferenceWorker, RuntimeCoordinator


class FakeEngine:
    def __init__(
        self, trace: list[tuple[str, int]], fail_marker: int | None = None
    ) -> None:
        self.trace = trace
        self.calls = 0
        self.fail_marker = fail_marker
        trace.append(("construct", get_ident()))

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls += 1
        self.trace.append(("infer", get_ident()))
        assert image_bgr.shape == (20, 60, 3)  # Full images, not 36 channel crops.
        assert image_bgr.dtype == np.uint8 and not image_bgr.flags.writeable
        if int(image_bgr[0, 0, 0]) == self.fail_marker:
            raise RuntimeError("fake full-camera failure")
        return inferred(BoundingRect(1, 1, 2, 2)) if self.calls == 1 else inferred()

    def close(self) -> None:
        self.trace.append(("close", get_ident()))


@pytest.mark.parametrize("fail_one", [False, True])
def test_collected_cycle_worker_and_production_results_compose(fail_one: bool) -> None:
    config, clock = layout(), Clock()
    collector = CycleCollector(config.camera_ids, timeout=10, clock=clock)
    pipeline = ProductionPipeline(config, clock=clock)
    collector.begin(CycleId(1))
    # These owned inputs belong only to this test; shared frame seeds stay unchanged.
    inputs = {
        index + 1: Frame(
            FrameId(CycleId(1), camera.camera_id, 5 - index),
            2000.0,
            np.full((20, 60, 3), index + 1, dtype=np.uint8),
        )
        for index, camera in enumerate(config.cameras)
    }
    frames_by_camera = {frame.id.camera_id: frame for frame in inputs.values()}
    expected_ids = tuple(frame.id for frame in inputs.values())
    snapshot = None
    for camera in reversed(config.cameras):
        snapshot = collector.accept(frames_by_camera[camera.camera_id])
    assert snapshot is not None
    assert tuple(item.frame.id for item in snapshot.frames) == expected_ids
    trace: list[tuple[str, int]] = []
    seen: list[FrameId] = []

    class Engine(FakeEngine):
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            marker = int(image_bgr[0, 0, 0])
            expected = inputs[marker]
            assert image_bgr is expected.image
            assert image_bgr.flags.owndata and image_bgr.flags.c_contiguous
            assert np.all(image_bgr == marker)
            seen.append(expected.id)
            return super().infer(image_bgr)

    # Fail CAM4 by its pixels, independently of reverse arrival or call count.
    failed_id = expected_ids[3]
    worker = InferenceWorker(lambda: Engine(trace, fail_marker=4 if fail_one else None))
    try:
        worker.start().result(5)
        assert [
            thread.ident for thread in threads() if thread.name == worker.thread_name
        ] == [trace[0][1]]
        raw = inspect_cycle(
            snapshot, lambda image: worker.submit(image).result(5), clock=clock
        )
        result = pipeline.from_cycle(raw)
    finally:
        worker.request_stop()
        assert worker.join(5)
    assert result.assessment == Assessment(ERROR if fail_one else SUCCESS, NG)
    assert raw.status is (CycleStatus.FAILED if fail_one else CycleStatus.COMPLETE)
    assert raw.cycle_id == result.cycle_id == CycleId(1)
    assert raw.expected_cameras == config.camera_ids and raw.missing_cameras == ()
    assert tuple(item.frame_id for item in raw.frames) == expected_ids
    assert [item.frame_id.sequence for item in raw.frames] == [5, 4, 3, 2, 1, 0]
    assert tuple(seen) == expected_ids
    failed = [item for item in raw.frames if item.failure is not None]
    assert len(failed) == int(fail_one)
    if fail_one:
        assert failed[0].frame_id == failed_id and failed[0].result is None
        assert failed[0].failure is not None
        assert failed[0].failure.exception_type == "builtins.RuntimeError"
        assert failed[0].failure.message == "fake full-camera failure"
    assert all(
        item.result is not None and item.failure is None
        for item in raw.frames
        if not fail_one or item.frame_id != failed_id
    )
    assert tuple(item.frame_id for item in result.cameras) == expected_ids
    assert tuple(item.camera_id for item in result.cameras) == config.camera_ids
    assert result.channels[0].assessment == Assessment(SUCCESS, NG)
    (finding,) = result.channels[0].vimo_findings
    assert finding.finding.frame_id == expected_ids[0]
    assert finding.channel_id == 1 and finding.intersection_area == 4
    assert result.cameras[3].assessment == (
        Assessment(ERROR, UNKNOWN) if fail_one else Assessment(SUCCESS, OK)
    )
    assert result.cycle_status is (
        CycleStatus.FAILED if fail_one else CycleStatus.COMPLETE
    )
    assert len(result.channels) == 36
    assert [item.channel_id for item in result.channels] == list(range(1, 37))
    assert [event for event, _ in trace] == ["construct", *["infer"] * 6, "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    assert worker.failure is None
    assert not any(thread.name == worker.thread_name for thread in threads())


def test_existing_coordinator_future_consumed_without_new_threads_or_inference_calls() -> (
    None
):
    config = layout()
    pipeline = ProductionPipeline(config)
    trace: list[tuple[str, int]] = []
    coordinator = RuntimeCoordinator(
        InferenceWorker(lambda: FakeEngine(trace)), config.camera_ids, timeout=10
    )
    try:
        coordinator.start().result(5)
        future = coordinator.begin_cycle(CycleId(1))
        for camera in reversed(config.cameras):
            frame = source_frame(camera.camera_id)
            coordinator.submit_frame(frame).result(5)
        # Ordinary non-GUI consumer, not a blocking Future callback.
        envelope = future.result(5)
        result = pipeline.from_cycle(envelope.result)
        assert result.assessment == Assessment(SUCCESS, NG)
        assert result.inspection_started_at is not None
        assert result.result_ready_at >= envelope.result_ready_at
        assert len(result.channels) == 36
    finally:
        coordinator.request_stop()
        assert coordinator.join(5)
    assert [event for event, _ in trace].count("infer") == 6
    assert coordinator.failure is None
    assert not set(coordinator.thread_names) & {thread.name for thread in threads()}
