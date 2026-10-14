"""One optional real-local cycle with additive capsule checks, no calibration claim."""

from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from run_local_production_ui import compose
from tests.integration.test_local_production_ui import input_files
from tests.integration.test_reference import ROOT, short_cases, verify_files
from tests.integration.test_worker_vimo import MODEL, TIMEOUT, reference_image
from tests.qt_support import until

from vimo_demo.configuration import (
    load_machine_layout,
    load_model_definition,
    load_recipe,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import (
    CameraLayout,
    OccupancyState,
    ProductJudgment,
    TechnicalStatus,
    TraditionalSkipReason,
)
from vimo_demo.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
)


@pytest.mark.gpu
def test_real_local_capsule(
    qapp: QApplication, pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    reference, _, _ = reference_image(pytestconfig)
    rows = short_cases(reference)[:6]
    verify_files(rows, pytestconfig, full=False)
    model, machine, recipe, _ = input_files(tmp_path)
    save_model_definition(
        model,
        replace(
            load_model_definition(model),
            model_path=str(MODEL),
            expected_sha256=reference["model_sha256"],
            module_id="2",
        ),
    )
    paths = tuple(ROOT / "images" / row["filename"] for row in rows)
    layout = load_machine_layout(machine)
    cameras: list[CameraLayout] = []
    for camera, path in zip(layout.layout.cameras, paths, strict=True):
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        h, w = image.shape[:2]
        cameras.append(
            replace(
                camera,
                slots=tuple(
                    replace(
                        slot,
                        rect=BoundingRect(
                            slot.slot * w // 6,
                            0,
                            (slot.slot + 1) * w // 6 - slot.slot * w // 6,
                            h,
                        ),
                    )
                    for slot in camera.slots
                ),
            )
        )
    save_machine_layout(
        machine, replace(layout, layout=replace(layout.layout, cameras=tuple(cameras)))
    )
    # Deliberately impossible size target exercises NG, not production requirements.
    save_recipe(
        recipe,
        replace(
            load_recipe(recipe),
            capsule=CapsuleSettings(
                SizeSettings(True, 1, 0, 1, 0), ColorSettings(True, 0, 90)
            ),
        ),
    )
    before = {t.ident for t in threads()}
    composition = compose(model, machine, recipe, paths)
    app = composition.application
    try:
        start = app.start()
        until(start.done, int(TIMEOUT * 1000))
        start.result()
        until(
            lambda: app.snapshot().counters.cycles_completed >= 1, int(TIMEOUT * 1000)
        )
        stop = app.stop()
        until(stop.done, int(TIMEOUT * 1000))
        stop.result()
        result = app.snapshot().latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.assessment.technical_status is TechnicalStatus.SUCCESS
        assert result.assessment.judgment is ProductJudgment.NG
        for channel in result.channels:
            assert channel.occupancy is not None
            if channel.occupancy.state is OccupancyState.EMPTY:
                assert channel.traditional_skip is TraditionalSkipReason.CONFIRMED_EMPTY
                assert (
                    channel.checks[0].assessment.technical_status
                    is TechnicalStatus.DISABLED
                )
                assert channel.checks[0].measurements == channel.checks[0].reasons == ()
            else:
                assert channel.traditional_skip is None
                assert channel.checks[0].assessment.judgment is ProductJudgment.NG
        assert any(channel.checks[0].measurements for channel in result.channels)
        for camera, row in zip(result.cameras, rows, strict=True):
            findings = {
                assignment.finding.region_index
                for assignment in camera.assigned_findings
            }
            assert len(findings) + len(camera.unassigned_findings) == sum(
                row["regions_per_label"].values()
            )
        print(
            "Real six-frame ViMo + Size/Color: non-EMPTY traditional NG, confirmed EMPTY skipped; ViMo findings preserved; Stop completed"
        )
    finally:
        close = app.close()
        until(close.done, int(TIMEOUT * 1000))
        close.result()
        assert app.join(0)
    assert not {t.ident for t in threads()} - before
