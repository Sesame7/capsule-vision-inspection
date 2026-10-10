"""Preview shares production math but has no application/runtime side effects."""

from dataclasses import replace
from threading import Event
from threading import enumerate as threads

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRectF, QSizeF
from PySide6.QtGui import QFont, QFontMetricsF
from PySide6.QtWidgets import QApplication, QLabel
from tests.integration.test_parameters_ui import Screen
from tests.integration.test_parameters_ui import configured_screen as configured_screen
from tests.qt_support import until
from tests.unit.test_capsule import SETTINGS, bodies

from vimo_demo.configuration import MachineLayoutDefinition, load_recipe
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelROI,
    InspectionLayout,
    ProductJudgment,
)
from vimo_demo.inspection.production.capsule import CapsuleSettings, judge_capsule
from vimo_demo.ui.display_frames import DisplayFrame
from vimo_demo.ui.machine_editor import MachineLayoutDraft
from vimo_demo.ui.production_style import Language
from vimo_demo.ui.traditional_preview import TraditionalPreview, measurement_label_rect


def setup(screen: Screen, *, pixel_scale: int = 1) -> TraditionalPreview:
    window, session, _ = screen
    cameras = tuple(
        replace(
            camera,
            slots=tuple(
                ChannelROI(
                    slot.slot,
                    slot.channel_id,
                    BoundingRect(
                        slot.slot * 80 * pixel_scale,
                        0,
                        80 * pixel_scale,
                        120 * pixel_scale,
                    ),
                )
                for slot in camera.slots
            ),
        )
        for camera in session.machine.layout.cameras
    )
    session.machine_editor = MachineLayoutDraft(
        MachineLayoutDefinition(InspectionLayout(cameras)), session.machine_path
    )
    for index, camera in enumerate(cameras):
        image = np.concatenate([bodies(30 + index) for _ in range(6)], axis=1)
        image = image.repeat(pixel_scale, axis=0).repeat(pixel_scale, axis=1)
        window.bridge.frames.publish(
            Frame(FrameId(CycleId(1), camera.camera_id, 1), 1.0, image), generation=0
        )
    session.draft.capsule = SETTINGS
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    return preview


def test_cached_judgment_and_isolation_and_save(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    window, session, runtimes = configured_screen
    before = window.bridge.application.snapshot()
    preview = setup(configured_screen)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    for row, result in zip(preview.measurements, preview.results, strict=True):
        assert row.measured is not None
        assert result == judge_capsule(row.measured, session.draft.capsule)

    def forbidden(*args: object) -> None:
        pytest.fail("Threshold edit must not segment again")

    monkeypatch.setattr(module, "measure_capsule", forbidden)
    preview.spins["target_width"].setValue(25)
    assert all(
        result and result.assessment.judgment is ProductJudgment.NG
        for result in preview.results
    )
    preview.spins["width_tolerance"].setValue(5)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    preview.mode_tabs.setCurrentIndex(1)
    session.draft.capsule = replace(
        session.draft.capsule, color=replace(session.draft.capsule.color, target_hue=50)
    )
    preview.show_settings()
    assert all(
        result and result.assessment.judgment is ProductJudgment.NG
        for result in preview.results
    )
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 30
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    preview.tolerance.setValue(10)
    after = window.bridge.application.snapshot()
    assert (
        before.counters == after.counters
        and before.active == after.active
        and before.candidate == after.candidate
    )
    assert not runtimes and after.latest_production_result is None
    assert (
        session.entry
        and load_recipe(session.entry.path).capsule != session.draft.capsule
    )
    expected = session.draft.capsule
    preview.back.click()
    assert window.parameters.preview is None
    window.parameters.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert session.entry and load_recipe(session.entry.path).capsule == expected
    assert window.choices[-1].prepared.configuration.capsule == expected
    assert not runtimes and window.bridge.application.snapshot().active is before.active


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_camera_refresh_and_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    assert [preview.camera_tabs.tabText(i) for i in range(6)] == [
        str(c).removeprefix("CAM") for c in session.machine.layout.camera_ids
    ]
    for index, camera in enumerate(session.machine.layout.camera_ids):
        assert not preview.camera_tabs.tabIcon(index).isNull()
        assert str(camera) in preview.camera_tabs.tabToolTip(index)
        assert preview.camera_tabs.accessibleTabName(
            index
        ) == preview.camera_tabs.tabToolTip(index)
    assert preview.canvas.width() > 500 and preview.canvas.height() > 240
    for widget in (
        preview.back,
        preview.refresh_button,
        preview.controls,
    ):
        assert preview.rect().contains(widget.mapTo(preview, QPoint(0, 0)))
        assert preview.rect().contains(
            widget.mapTo(preview, QPoint(widget.width() - 1, widget.height() - 1))
        )
    assert not preview.message.isVisible()
    original = preview.frame
    preview.camera_tabs.setCurrentIndex(2)
    until(lambda: not preview.busy)
    assert preview.frame is not None and preview.frame is not original
    assert preview.frame.frame_id.camera_id == "CAM3"
    preview.mode_tabs.setCurrentIndex(1)
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 32
    camera = CameraId("CAM3")
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), camera, 2), 2, np.zeros((120, 480, 3), dtype=np.uint8)
        ),
        generation=0,
    )
    assert preview.frame.frame_id.cycle_id == 1
    preview.refresh_button.click()
    until(lambda: not preview.busy)
    assert preview.frame is not None and preview.frame.frame_id.cycle_id == 2
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 32
    assert "unchanged" in preview.message.text() or "未更改" in preview.message.text()
    preview.back.click()


def test_unavailable_reference_no_invented_measurement(
    configured_screen: Screen,
) -> None:
    window, _, _ = configured_screen
    window.parameters.open_preview("color")
    preview = window.parameters.preview
    assert preview is not None and preview.frame is None
    assert preview.measurements == () and preview.results == () and not preview.busy
    assert not preview.sample.isEnabled()
    preview.back.click()


def test_stale_job_and_observable_close(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    preview = setup(configured_screen)
    original = module.measure_reference
    entered, release = Event(), Event()

    def blocked(
        frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
    ) -> tuple[module.PreviewMeasurement, ...]:
        entered.set()
        assert release.wait(5)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", blocked)
    preview.refresh_reference()
    until(entered.is_set)
    preview.camera_tabs.setCurrentIndex(1)
    assert not preview.measurements
    release.set()
    until(lambda: not preview.busy)
    assert preview.frame and preview.frame.frame_id.camera_id == "CAM2"
    assert preview.measurements[0].measured
    assert preview.measurements[0].measured.views[0].body
    assert preview.measurements[0].measured.views[0].color
    assert preview.measurements[0].measured.views[0].color.hue == 31
    entered.clear()
    release.clear()
    preview.refresh_reference()
    until(entered.is_set)
    closed: list[bool] = []

    def finished() -> None:
        closed.append(True)
        preview.leave()

    preview.request_close(finished)
    assert not closed and preview.busy
    release.set()
    until(lambda: bool(closed))
    assert not any(t.name == "CapsulePreview" for t in threads())


def test_preview_read_only_during_runtime_and_navigation_releases(
    configured_screen: Screen,
) -> None:
    from vimo_demo.application import ApplicationState

    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    before = session.draft.capsule
    future = window.bridge.application.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(future.done)
    window.bridge.refresh()
    assert not preview.enabled.isEnabled() and not preview.controls.isEnabled()
    preview.sample_color()
    assert session.draft.capsule == before
    window.navigate(4)
    until(lambda: window.parameters.preview is None)
    assert window.pages.currentWidget() is window.parameters.settings
    stop = window.bridge.application.stop()
    until(stop.done)
    assert window.bridge.application.snapshot().state is ApplicationState.READY


def test_disabled_preview_never_segments(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module
    from vimo_demo.inspection.production.capsule import CapsuleSettings

    window, session, _ = configured_screen
    preview = setup(configured_screen)
    preview.back.click()
    session.draft.capsule = CapsuleSettings()
    calls: list[bool] = []
    original = module.measure_reference

    def observed(
        frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
    ) -> tuple[module.PreviewMeasurement, ...]:
        calls.append(True)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", observed)
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None and not preview.busy and not calls
    preview.enabled.setChecked(True)
    until(lambda: not preview.busy)
    assert calls == [True]
    preview.back.click()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_no_repeated_text_but_errors_remain(
    configured_screen: Screen, language: Language
) -> None:
    window, _, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language(language)
    for mode in (0, 1):
        preview.mode_tabs.setCurrentIndex(mode)
        assert not preview.message.isVisible() and preview.message.text() == ""
        labels = " ".join(label.text() for label in preview.findChildren(QLabel))
        assert "CH01: OK" not in labels
        assert "未标定" not in labels and "Uncalibrated demo" not in labels
    assert preview.frame is not None
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.zeros((1, 1, 3), dtype=np.uint8),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert preview.message.isVisible() and "ValueError" in preview.message.text()
    preview.back.click()


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
@pytest.mark.parametrize("scale", [None, 1.0, 1.5])
def test_twelve_annotation_placements(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    scale: float | None,
) -> None:
    import vimo_demo.ui.traditional_preview as module

    window, _, _ = configured_screen
    preview = setup(configured_screen, pixel_scale=3)
    window.resize(*size)
    QApplication.processEvents()
    records: list[tuple[QRectF, QRectF, QRectF | None]] = []

    def record(body: QRectF, available: QRectF, label_size: QSizeF) -> QRectF | None:
        result = measurement_label_rect(body, available, label_size)
        records.append((body, available, result))
        return result

    monkeypatch.setattr(module, "measurement_label_rect", record)
    preview.canvas.zoom(scale)
    preview.canvas.grab()
    assert len(records) == 12
    rectangles: list[QRectF] = []
    for body, available, label in records:
        if not body.intersects(available):
            assert label is None
            continue
        assert label is not None and available.contains(label)
        assert (
            label.height() >= 30
        )  # Two readable logical-pixel text lines plus padding.
        assert not any(label.intersects(previous) for previous in rectangles)
        rectangles.append(label)
    assert len(rectangles) == 12 if scale is None else len(rectangles) > 0
    records.clear()
    preview.mode_tabs.setCurrentIndex(1)
    preview.canvas.grab()
    assert not records  # No Size labels in Color mode.
    preview.back.click()


def test_narrow_real_frame_labels_and_extreme_zoom(qapp: QApplication) -> None:
    from vimo_demo.ui.production_presentation import image_transform

    font = QFont()
    font.setPixelSize(12)
    metrics = QFontMetricsF(font)
    size = QSizeF(
        max(metrics.horizontalAdvance("W 108"), metrics.horizontalAdvance("H 292")) + 8,
        2 * metrics.lineSpacing() + 6,
    )
    for width, height in ((1032, 1680), (1872, 760)):
        transform = image_transform(1056, 960, width, height)
        labels: list[QRectF] = []
        for channel in range(6):
            for view in range(2):
                body = QRectF(
                    *transform.rectangle(
                        BoundingRect(channel * 176 + 28, view * 480 + 80, 108, 292)
                    )
                )
                available = QRectF(
                    *transform.rectangle(
                        BoundingRect(channel * 176, view * 480, 176, 480)
                    )
                ).adjusted(3, 22 if view == 0 else 3, -3, -3)
                label = measurement_label_rect(body, available, size)
                assert label is not None and available.contains(label)
                assert not any(label.intersects(other) for other in labels)
                labels.append(label)
        assert len(labels) == 12
    assert (
        measurement_label_rect(QRectF(0, 0, 5, 10), QRectF(0, 0, 8, 20), size) is None
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_preview_scopes_ng_event_but_preserves_errors(
    configured_screen: Screen, language: Language
) -> None:
    from tests.unit.test_production_application import production

    from vimo_demo.application import ApplicationState
    from vimo_demo.inspection.production import TechnicalStatus
    from vimo_demo.ui.production_style import text

    window, _, runtimes = configured_screen
    window.set_language(language)
    app = window.bridge.application
    start = app.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(start.done)
    receipt = app.begin_cycle(CycleId(1))
    runtimes[0].cycles[0].accepted.set_result(None)
    runtimes[0].cycles[0].result.set_result(production(judgment=ProductJudgment.NG))
    until(receipt.result.done)
    stop = app.stop()
    until(stop.done)
    window.bridge.refresh()
    window.navigate(0)
    assert window.event_frame.isVisible() and not window.event_label.text()
    before = app.snapshot()
    window.navigate(1)
    preview = setup(configured_screen)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    assert window.event_frame.isVisible() and not window.event_label.text()
    assert app.snapshot() == before
    # Snapshot notification only: exercise real rendering branches without changing app state.
    window.bridge.snapshot_changed.emit(
        replace(
            before,
            latest_production_result=production(
                status=TechnicalStatus.ERROR, judgment=ProductJudgment.UNKNOWN
            ),
        )
    )
    assert (
        window.event_frame.isVisible()
        and text("inspection_issue", language) in window.event_label.text()
    )
    window.bridge.snapshot_changed.emit(replace(before, state=ApplicationState.FAULT))
    assert (
        window.event_frame.isVisible()
        and text("runtime_error", language) in window.event_label.text()
    )
    window.bridge.snapshot_changed.emit(before)
    window.bridge.command_failed.emit(
        "start", RuntimeError("simulated command failure")
    )
    assert (
        window.event_frame.isVisible()
        and text("operation_error", language) in window.event_label.text()
    )
    window.bridge.command_finished.emit("start")
    assert window.event_frame.isVisible() and not window.event_label.text()
    preview.back.click()
    window.navigate(0)
    assert window.event_frame.isVisible() and not window.event_label.text()
    assert app.snapshot() == before


def test_color_only_sampling_without_body_geometry(configured_screen: Screen) -> None:
    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    preview.mode_tabs.setCurrentIndex(1)
    session.draft.capsule = replace(
        SETTINGS,
        size=replace(SETTINGS.size, enabled=False),
        color=replace(SETTINGS.color, target_hue=80),
    )
    preview.show_settings()
    until(lambda: not preview.busy)
    # Entire colored views touch all edges: Size would reject each body.
    image = np.tile(bodies()[10, 20], (120, 480, 1)).copy()
    # A channel with only one usable view must not contribute a target sample.
    image[:60, :80] = bodies(80)[10, 20]
    image[60:, :80] = 0
    before = window.bridge.application.snapshot()
    window.bridge.frames.publish(
        Frame(FrameId(CycleId(2), CameraId("CAM1"), 2), 2, image), generation=0
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert all(
        row.measured
        and row.measured.color_measured
        and not row.measured.size_measured
        and all(v.body is None for v in row.measured.views)
        for row in preview.measurements
    )
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 30
    assert (
        preview.results[0]
        and preview.results[0].assessment.judgment is ProductJudgment.NG
    )
    assert all(
        r and r.assessment.judgment is ProductJudgment.OK for r in preview.results[1:]
    )
    assert all(
        r and not any(m.name.endswith((".width", ".height")) for m in r.measurements)
        for r in preview.results
    )
    assert window.bridge.application.snapshot() == before and not runtimes
    preview.back.click()


def test_preview_capability_changes_recompute_but_thresholds_rejudge(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    _, session, _ = configured_screen
    preview = setup(configured_screen)
    original = module.measure_reference
    calls: list[tuple[bool, bool]] = []

    def observed(frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings):
        calls.append((settings.size.enabled, settings.color.enabled))
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", observed)
    preview.enabled.setChecked(False)  # Color-only
    until(lambda: not preview.busy)
    assert calls == [(False, True)]
    preview.mode_tabs.setCurrentIndex(1)
    preview.tolerance.setValue(12)
    preview.sample.click()
    assert calls == [(False, True)]  # Targets and tolerances only rejudge.
    preview.enabled.setChecked(False)  # Neither: clear stale evidence, no job.
    assert not preview.busy and not preview.measurements and not preview.results
    assert calls == [(False, True)]
    preview.mode_tabs.setCurrentIndex(0)
    preview.enabled.setChecked(True)  # Size-only
    until(lambda: not preview.busy)
    assert calls[-1] == (True, False)
    assert all(
        row.measured
        and row.measured.size_measured
        and not row.measured.color_measured
        and all(v.color is None for v in row.measured.views)
        for row in preview.measurements
    )
    session.draft.capsule = replace(
        session.draft.capsule, color=replace(session.draft.capsule.color, enabled=True)
    )
    preview.show_settings()
    until(lambda: not preview.busy)
    assert calls == [(False, True), (True, False), (True, True)]
    preview.back.click()


def test_capability_change_discards_inflight_job_and_uses_latest_snapshot(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    _, session, _ = configured_screen
    preview = setup(configured_screen)
    original = module.measure_reference
    entered, release = Event(), Event()
    calls: list[tuple[bool, bool]] = []

    def blocked(frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings):
        calls.append((settings.size.enabled, settings.color.enabled))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", blocked)
    preview.refresh_reference()
    until(entered.is_set)
    try:
        session.draft.capsule = replace(
            SETTINGS, size=replace(SETTINGS.size, enabled=False)
        )
        preview.show_settings()
        assert preview.busy and not preview.measurements and len(calls) == 1
    finally:
        release.set()
    until(lambda: not preview.busy)
    assert calls == [(True, True), (False, True)]
    assert all(
        row.measured and not row.measured.size_measured for row in preview.measurements
    )
    assert not any(t.name == "CapsulePreview" for t in threads())
    preview.back.click()
