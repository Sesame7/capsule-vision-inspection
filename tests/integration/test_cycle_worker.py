"""Six-frame serial inspection through the unchanged worker; no SDK or GPU."""

from threading import enumerate as threads
from threading import get_ident
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.unit.test_inspection_cycle import CAMERAS, Clock, image_result, ready_cycle

from vimo_demo.inference import ImageResult
from vimo_demo.inspection import CycleStatus, inspect_cycle
from vimo_demo.runtime import InferenceWorker


@pytest.mark.parametrize("fail_one", [False, True])
def test_cycle_uses_one_worker_and_preserves_camera_correlation(fail_one: bool) -> None:
    trace: list[tuple[str, int]] = []

    class Engine:
        def __init__(self) -> None:
            trace.append(("construct", get_ident()))

        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            trace.append(("infer", get_ident()))
            if fail_one and int(image_bgr[0, 0, 0]) == 2:
                raise RuntimeError("fake camera inspection error")
            assert image_bgr.dtype == np.uint8 and not image_bgr.flags.writeable
            return image_result(cast(NDArray[np.uint8], image_bgr))

        def close(self) -> None:
            trace.append(("close", get_ident()))

    clock = Clock()
    cycle = ready_cycle(clock)
    worker = InferenceWorker(Engine)
    try:
        worker.start().result(timeout=5)
        # This test/caller is not a GUI thread. Only one request is outstanding.
        result = inspect_cycle(
            cycle,
            lambda image: worker.submit(image).result(timeout=5),
            clock=clock,
        )
        assert result.status is (
            CycleStatus.FAILED if fail_one else CycleStatus.COMPLETE
        )
        assert tuple(item.frame_id.camera_id for item in result.frames) == CAMERAS
        assert [item.frame_id.sequence for item in result.frames] == [5, 4, 3, 2, 1, 0]
        assert sum(item.failure is not None for item in result.frames) == int(fail_one)
        if fail_one:
            assert result.frames[3].frame_id.camera_id == CAMERAS[3]
            assert result.frames[3].failure is not None
    finally:
        worker.request_stop()
        assert worker.join(timeout=5)
    assert worker.failure is None
    assert not any(t.name == worker.thread_name for t in threads())
    assert [action for action, _ in trace] == ["construct", *(["infer"] * 6), "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
