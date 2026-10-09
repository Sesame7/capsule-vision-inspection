"""Opt-in real local Production UI wiring; fixture ROIs are NOT calibrated."""

from dataclasses import replace
from pathlib import Path
from shutil import copyfile
from threading import enumerate as threads

import pytest
from _tool_helpers import decode
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from run_local_production_ui import compose
from tests.integration.test_local_production_ui import input_files
from tests.integration.test_reference import (
    ROOT,
    short_cases,
    unavailable,
    verify_files,
)
from tests.integration.test_worker_vimo import MODEL, TIMEOUT, reference_image
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    load_model_definition,
    save_camera_sources,
    save_model_definition,
)
from vimo_demo.inference import BoundingRect, VimoInitializationError
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_presentation import matching_camera
from vimo_demo.ui.production_window import ProductionWindow


@pytest.mark.gpu
def test_real_local_ui(
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
            display_name="Verified ViMo (uncalibrated wiring fixture)",
            model_path=str(MODEL),
            expected_sha256=reference["model_sha256"],
            module_id="2",
        ),
    )
    paths = tuple(ROOT / "images" / row["filename"] for row in rows)
    before = {t.ident for t in threads()}
    assignments: list[CameraFolder] = []
    for index, path in enumerate(paths):
        folder = tmp_path / f"camera-{index + 1}"
        folder.mkdir()
        copyfile(path, folder / path.name)
        assignments.append(CameraFolder(CameraId(f"CAM{index + 1}"), folder.name))
    save_camera_sources(
        tmp_path / "camera_sources.json", CameraSources(tuple(assignments))
    )
    composition = compose(model, machine, recipe)
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=10)
    if not QFontDatabase.families():
        for font in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / font
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.resize(1080, 1920)
    window.show()
    try:
        assert composition.store.cameras == tuple(f"CAM{i}" for i in range(1, 7))
        # Explicit wiring ROI, not calibration: slot 0 covers each reference,
        # guaranteeing that known real findings exercise channel NG assignment.
        draft = composition.editor.machine_editor
        for camera, path in zip(composition.store.cameras, paths, strict=True):
            height, width = decode(path).shape[:2]
            draft.bounds[camera] = (width, height)
            rects = [s.rect for s in draft.camera(camera).slots]
            rects[0] = BoundingRect(0, 0, width, height)
            draft.replace_rects(camera, tuple(rects))
        draft.save()
        window.parameters.apply()
        until(lambda: not window.parameters.busy)
        assert window.choices[-1].prepared.configuration.layout == draft.saved.layout
        startup = app.start()
        until(startup.done, int(TIMEOUT * 1000))
        error = startup.exception()
        if error is not None:
            cause = error.__cause__
            if isinstance(cause, VimoInitializationError):
                vendor = cause.__cause__
                if isinstance(vendor, (ImportError, OSError)) or (
                    type(vendor).__name__ == "VimoException"
                    and getattr(vendor, "code", None) == 40000
                ):
                    unavailable(
                        pytestconfig, f"ViMo runtime/license unavailable: {cause}"
                    )
            raise error
        until(
            lambda: app.snapshot().counters.cycles_completed >= 2, int(TIMEOUT * 1000)
        )
        until(lambda: any(card.viewport.boxes for card in window.cards))
        window.buttons["stop"].click()
        until(
            lambda: app.snapshot().state is ApplicationState.READY, int(TIMEOUT * 1000)
        )
        snapshot = app.snapshot()
        result = snapshot.latest_production_result
        assert result is not None and result.cycle_id >= 2
        assert result.assessment.technical_status is TechnicalStatus.SUCCESS
        for frame, camera, row in zip(
            composition.store.snapshot().frames, result.cameras, rows, strict=True
        ):
            assert matching_camera(frame, snapshot) is camera
            if row["regions_per_label"]:
                assert camera.channels[0].assessment.judgment is ProductJudgment.NG
            findings = {
                item.finding.region_index: item.finding
                for item in camera.assigned_findings
            }
            assert len(findings) + len(camera.unassigned_findings) == sum(
                row["regions_per_label"].values()
            )
        bridge.refresh()
        qapp.processEvents()
        assert any(card.viewport.boxes for card in window.cards)
        assert window.grab().save(str(tmp_path / "local-production.png"))
        print(
            "CAM1 -> CAM6; saved machine ROI -> Apply -> Start; known findings -> slot-0 channel NG"
        )
        print(
            f"Real Folder UI: {snapshot.counters.cycles_completed} cycles; six real frames/cycle; matched overlays; Stop -> READY"
        )
        print(
            f"Wiring-only configuration: {model.parent}; images: {' '.join(str(p) for p in paths)}"
        )
    finally:
        window.close()
        until(lambda: bridge.closed, int(TIMEOUT * 1000))
        assert app.join(0)
        window.deleteLater()
    assert not {t.ident for t in threads()} - before
    print("Close -> CLOSED; no residual application/source/coordinator/worker threads")
