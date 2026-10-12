"""Additive production consumption of unchanged Phase 1 worker/cycle boundaries."""

from threading import enumerate as threads
from threading import get_ident

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.production_support import (
    ERROR,
    NG,
    SUCCESS,
    Clock,
    inferred,
    layout,
    source_frame,
)

from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inspection import CycleCollector, CycleId, CycleStatus, inspect_cycle
from vimo_demo.inspection.production import (
    Assessment,
    ProductionPipeline,
)
from vimo_demo.runtime import InferenceWorker, RuntimeCoordinator


class FakeEngine:
    def __init__(self, trace: list[tuple[str, int]], fail_one: bool = False) -> None:
        self.trace = trace
        self.calls = 0
        self.fail_one = fail_one
        trace.append(("construct", get_ident()))

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls += 1
        self.trace.append(("infer", get_ident()))
        assert image_bgr.shape == (20, 60, 3)  # Full images, not 36 channel crops.
        if self.fail_one and self.calls == 2:
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
    snapshot = None
    for camera in reversed(config.cameras):
        frame = source_frame(camera.camera_id)
        snapshot = collector.accept(frame)
    assert snapshot is not None
    trace: list[tuple[str, int]] = []
    worker = InferenceWorker(lambda: FakeEngine(trace, fail_one))
    try:
        worker.start().result(5)
        raw = inspect_cycle(
            snapshot, lambda image: worker.submit(image).result(5), clock=clock
        )
        result = pipeline.from_cycle(raw)
    finally:
        worker.request_stop()
        assert worker.join(5)
    assert result.assessment == Assessment(ERROR if fail_one else SUCCESS, NG)
    assert result.cycle_status is (
        CycleStatus.FAILED if fail_one else CycleStatus.COMPLETE
    )
    assert len(result.channels) == 36
    assert [item.channel_id for item in result.channels] == list(range(1, 37))
    assert [event for event, _ in trace] == ["construct", *["infer"] * 6, "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    assert worker.failure is None


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
