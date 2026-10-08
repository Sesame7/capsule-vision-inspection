"""One real image validates extraction/ownership, never ROI calibration accuracy."""

from time import monotonic
from weakref import ref

import pytest
from tests.integration.test_reference import unavailable
from tests.integration.test_worker_vimo import TIMEOUT, RecordedEngine, reference_image

from vimo_demo.inference import BoundingRect, VimoInitializationError
from vimo_demo.inspection import CameraId, CycleId, FrameId, FrameInspectionResult
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
    ProductionPipeline,
    ProductJudgment,
    TechnicalStatus,
)
from vimo_demo.runtime import InferenceWorker


@pytest.mark.gpu
def test_real_image_to_lightweight_production_evidence(
    pytestconfig: pytest.Config,
) -> None:
    _, _, image = reference_image(pytestconfig)
    height, width, _ = image.shape
    camera_id = CameraId("CAM1")
    # Deliberately synthetic partitions; no claim of physical channel correctness.
    camera = CameraLayout(
        camera_id,
        tuple(
            ChannelROI(
                slot,
                ChannelId(slot + 1),
                BoundingRect(
                    slot * width // 6,
                    0,
                    (slot + 1) * width // 6 - slot * width // 6,
                    height,
                ),
            )
            for slot in range(6)
        ),
    )
    pipeline = ProductionPipeline(InspectionLayout((camera,)))
    trace: list[tuple[str, int]] = []
    worker = InferenceWorker(lambda: RecordedEngine(trace))
    try:
        try:
            worker.start().result(TIMEOUT)
        except VimoInitializationError as exc:
            cause = exc.__cause__
            if isinstance(cause, (ImportError, OSError)) or (
                type(cause).__name__ == "VimoException"
                and getattr(cause, "code", None) == 40000
            ):
                unavailable(pytestconfig, f"ViMo runtime/license unavailable: {exc}")
            raise
        start = monotonic()
        raw = worker.submit(image).result(TIMEOUT)
        end = monotonic()
        assert raw.regions  # Existing reference helper selects a known defect image.
        labels = {region.label for region in raw.regions}
        region_count = len(raw.regions)
        mask = ref(raw.mask)
        frame_result = FrameInspectionResult(
            FrameId(CycleId(1), camera_id, 0),
            start,
            start,
            raw.image_shape,
            start,
            end,
            raw,
            None,
        )
        result = pipeline.camera_result(camera, frame_result)
    finally:
        worker.request_stop()
        assert worker.join(TIMEOUT)
    del raw, frame_result, image
    assert mask() is None
    assert result.assessment.technical_status is TechnicalStatus.SUCCESS
    assert result.assessment.judgment is ProductJudgment.NG
    findings = (
        *result.unassigned_findings,
        *(item.finding for item in result.assigned_findings),
    )
    assert {finding.label for finding in findings} == labels
    assert len({finding.region_index for finding in findings}) == region_count
    assert worker.failure is None
    assert [event for event, _ in trace] == ["construct", "infer", "close"]
    print(
        f"Production conversion: {region_count} real regions preserved; mask released after worker cleanup; synthetic ROIs only"
    )
