"""One opt-in six-frame runtime wiring check, not a performance/accuracy test."""

from threading import enumerate as threads
from threading import get_ident
from typing import cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from tests.integration.test_reference import (
    ROOT,
    short_cases,
    unavailable,
    verify_files,
    verify_result,
)
from tests.integration.test_worker_vimo import (
    TIMEOUT,
    RecordedEngine,
    reference_image,
)

from vimo_demo.inference import VimoInitializationError
from vimo_demo.inspection import CameraId, CycleId, CycleStatus, Frame, FrameId
from vimo_demo.runtime import CoordinatorState, InferenceWorker, RuntimeCoordinator


@pytest.mark.gpu
def test_real_six_frame_cycle_and_shutdown(pytestconfig: pytest.Config) -> None:
    reference, _, _ = reference_image(pytestconfig)
    candidates = short_cases(reference)
    rows = [
        next(row for row in candidates if not row["regions_per_label"]),
        next(row for row in candidates if row["regions_per_label"]),
    ]
    rows.extend(row for row in candidates if row not in rows)
    rows = sorted(rows[:6], key=lambda row: row["filename"])
    verify_files(rows, pytestconfig, full=False)
    cameras = tuple(CameraId(f"camera-{index}") for index in range(6))
    frames: list[Frame] = []
    for index, row in enumerate(rows):
        image = cv2.imdecode(
            np.fromfile(ROOT / "images" / row["filename"], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert image is not None and image.dtype == np.uint8
        if not image.flags.owndata or not image.flags.c_contiguous:
            image = image.copy()
        frames.append(
            Frame(
                FrameId(CycleId(1), cameras[index], index),
                0.0,
                cast(NDArray[np.uint8], image),
            )
        )
    trace: list[tuple[str, int]] = []
    coordinator = RuntimeCoordinator(
        InferenceWorker(lambda: RecordedEngine(trace)), cameras, timeout=30
    )
    try:
        try:
            coordinator.start().result(TIMEOUT)
        except VimoInitializationError as exc:
            cause = exc.__cause__
            if isinstance(cause, (ImportError, OSError)) or (
                type(cause).__name__ == "VimoException"
                and getattr(cause, "code", None) == 40000
            ):
                unavailable(pytestconfig, f"ViMo runtime/license unavailable: {exc}")
            raise
        future = coordinator.begin_cycle(CycleId(1))
        for frame in reversed(frames):
            coordinator.submit_frame(frame).result(TIMEOUT)
        envelope = future.result(TIMEOUT)
        assert envelope.result.status is CycleStatus.COMPLETE
        assert envelope.result.missing_cameras == ()
        assert (
            tuple(row.frame_id.camera_id for row in envelope.result.frames) == cameras
        )
        for outcome, row in zip(envelope.result.frames, rows, strict=True):
            assert outcome.result is not None and outcome.failure is None
            verify_result(outcome.result, row, reference["image_shape"])
    finally:
        coordinator.request_stop()
        assert coordinator.join(TIMEOUT), coordinator.thread_names
    assert coordinator.snapshot.state is CoordinatorState.STOPPED
    assert coordinator.failure is None
    assert [action for action, _ in trace] == ["construct", *["infer"] * 6, "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    remaining = set(coordinator.thread_names) & {thread.name for thread in threads()}
    assert not remaining
    print("Coordinator GPU: 6/6 golden results; one engine owner; no residual threads")
