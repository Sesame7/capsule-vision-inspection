from collections.abc import Iterator

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from tests.integration.test_parameters_ui import configured_screen as configured_screen
from tests.integration.test_parameters_ui import edit
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession

from vimo_demo.application import ApplicationState, PreparedConfiguration
from vimo_demo.configuration import load_recipe
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import ChannelId
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_editor import RecipeEditorSession
from vimo_demo.ui.roi_editor import RoiEditorDialog

type Screen = tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]]


def publish(
    window: ProductionWindow, sequence: int = 1, size: tuple[int, int] = (120, 60)
) -> None:
    image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    image[:, :, 0] = 40
    window.bridge.frames.publish(
        Frame(FrameId(CycleId(1), CameraId("CAM3"), sequence), 1.0, image), generation=0
    )


def open_editor(window: ProductionWindow) -> RoiEditorDialog:
    page = window.parameters
    page.scope.setCurrentIndex(2)
    page.camera.setCurrentIndex(2)
    page.channel.setCurrentIndex(4)
    page.open_roi.click()
    dialog = page.roi_dialog
    assert dialog is not None
    QApplication.processEvents()
    return dialog


@pytest.fixture
def roi_screen(configured_screen: Screen) -> Iterator[tuple[Screen, RoiEditorDialog]]:
    window, session, _ = configured_screen
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    window.parameters.recipes.activated.emit(index)
    publish(window)
    dialog = open_editor(window)
    yield configured_screen, dialog
    if window.parameters.roi_dialog is not None:
        window.parameters.roi_dialog.close()


def point(dialog: RoiEditorDialog, x: float, y: float) -> QPoint:
    canvas = dialog.canvas
    t = canvas.model.transform(canvas.width(), canvas.height())
    assert t is not None
    return QPoint(round(t.x + x * t.scale), round(t.y + y * t.scale))


def drag(
    dialog: RoiEditorDialog,
    start: QPoint,
    end: QPoint,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> None:
    canvas = dialog.canvas
    QTest.mousePress(canvas, button, pos=start)
    # Direct mouse event keeps offscreen tests independent of OS cursor movement.
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(end),
        QPointF(canvas.mapToGlobal(end)),
        Qt.MouseButton.NoButton,
        button,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(canvas, event)
    QTest.mouseRelease(canvas, button, pos=end)


def test_move_resize_numeric_navigation_reset_and_save(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, _), dialog = roi_screen
    page = window.parameters
    base = session.machine
    before = session.draft.to_recipe()
    other_rois = {
        c.channel_id: c.roi for c in session.resolve().channels if c.channel_id != 17
    }
    assert dialog.canvas.model.rect == BoundingRect(41, 1, 7, 9)
    assert "CAM3" in dialog.identity.text() and "CH17" in dialog.identity.text()
    drag(dialog, point(dialog, 44.5, 5.5), point(dialog, 46.5, 8.5))
    assert dialog.canvas.model.rect == BoundingRect(43, 4, 7, 9)
    drag(dialog, point(dialog, 50, 13), point(dialog, 53, 15))
    assert dialog.canvas.model.rect == BoundingRect(43, 4, 10, 11)
    drag(dialog, point(dialog, 53, 9.5), point(dialog, 55, 9.5))
    assert dialog.canvas.model.rect == BoundingRect(43, 4, 12, 11)
    assert (
        tuple(line.text() for line in page.roi_values)
        == session.draft.rois[ChannelId(17)]
    )
    edit(dialog.values[0], "44")
    assert dialog.canvas.model.rect == BoundingRect(44, 4, 12, 11)
    assert session.dirty
    assert {
        c.channel_id: c.roi for c in session.resolve().channels if c.channel_id != 17
    } == other_rois
    edited = session.draft.rois[ChannelId(17)]
    assert session.draft.to_recipe().camera_overrides == before.camera_overrides
    assert (
        session.draft.to_recipe().channel_overrides[0].checks
        == before.channel_overrides[0].checks
    )
    dialog.close()
    page.channel.setCurrentIndex(5)
    page.camera.setCurrentIndex(3)
    page.camera.setCurrentIndex(2)
    page.channel.setCurrentIndex(4)
    assert tuple(line.text() for line in page.roi_values) == edited
    dialog = open_editor(window)
    dialog.reset_override()
    assert ChannelId(17) not in session.draft.rois
    assert dialog.canvas.model.rect == BoundingRect(40, 0, 8, 10)
    assert not dialog.canvas.model.can_drag
    unchanged = session.draft.to_recipe()
    drag(dialog, point(dialog, 44, 5), point(dialog, 46, 6))
    assert session.draft.to_recipe() == unchanged
    dialog.create_override()
    assert session.draft.rois[ChannelId(17)] == ("40", "0", "8", "10")
    drag(dialog, point(dialog, 44, 5), point(dialog, 45, 6))
    expected = dialog.canvas.model.rect
    dialog.close()
    candidate = window.bridge.application.snapshot().candidate
    assert page.validate() and page.save()
    assert session.entry is not None
    assert load_recipe(session.entry.path).channel_overrides[0].roi == expected
    assert window.bridge.application.snapshot().candidate == candidate
    assert session.machine == base


def test_invalid_text_no_clamping_and_recovery(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, _), dialog = roi_screen
    previous = dialog.canvas.model.rect
    for value in ("", "not a number", "0", "-1"):
        edit(dialog.values[2], value)
        assert dialog.values[2].text() == value
        assert session.draft.rois[ChannelId(17)][2] == value
        assert dialog.canvas.model.rect == previous
        assert not dialog.canvas.model.can_drag
        assert not window.parameters.validate()
    edit(dialog.values[2], "8")
    assert dialog.canvas.model.rect == BoundingRect(41, 1, 8, 9)
    assert dialog.canvas.model.can_drag and window.parameters.validate()
    edit(dialog.values[0], "999")
    assert dialog.values[0].text() == "999" and not dialog.canvas.model.can_drag
    assert text("roi_outside", dialog.language) in dialog.status.text()


def test_no_frame_refresh_frozen_ownership_and_resolution(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    dialog = open_editor(window)
    before = session.draft.to_recipe()
    assert dialog.frame is None and dialog.canvas.model.size is None
    assert text("roi_no_frame", dialog.language) in dialog.reference.text()
    assert not dialog.canvas.model.can_drag and session.draft.to_recipe() == before
    dialog.create_override()
    edit(dialog.values[0], "41")
    assert not dialog.values[0].isReadOnly()
    publish(window)
    assert dialog.frame is None
    dialog.refresh_frame()
    assert dialog.frame is not None
    original = dialog.frame
    assert not original.image.flags.writeable
    assert dialog.canvas.model.can_drag
    before_roi = session.draft.rois.copy()
    publish(window, 2, (20, 20))
    window.bridge.refresh()
    assert dialog.frame is original
    assert dialog.canvas.model.size == (120, 60)
    dialog.refresh_frame()
    assert dialog.frame is not None and dialog.frame.frame_id.sequence == 2
    assert dialog.canvas.model.size == (20, 20) and not dialog.canvas.model.can_drag
    assert text("roi_outside", dialog.language) in dialog.status.text()
    assert session.draft.rois == before_roi
    assert int(original.image[0, 0, 0]) == 40
    dialog.close()
    assert dialog.frame is None


def test_zoom_resize_pan_do_not_modify_draft(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (_, session, _), dialog = roi_screen
    before = session.draft.to_recipe()
    for scale in (None, 0.5, 1, 2):
        dialog.canvas.zoom(scale)
        dialog.resize(820, 700)
        QApplication.processEvents()
        assert session.draft.to_recipe() == before
    dialog.canvas.zoom(8)
    before_pan = dialog.canvas.model.pan
    drag(dialog, QPoint(400, 200), QPoint(360, 180), Qt.MouseButton.MiddleButton)
    assert dialog.canvas.model.pan != before_pan
    assert session.draft.to_recipe() == before
    dialog.buttons["roi_fit"].click()
    assert dialog.canvas.model.scale is None and dialog.canvas.model.pan == (0, 0)


def test_save_apply_start_and_running_read_only(
    roi_screen: tuple[Screen, RoiEditorDialog],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (window, session, runtimes), dialog = roi_screen
    page, bridge = window.parameters, window.bridge
    app = bridge.application
    received: list[PreparedConfiguration] = []

    def factory(prepared: PreparedConfiguration, _generation: int) -> FakeSession:
        received.append(prepared)
        runtime = FakeSession(automatic_start=False)
        runtimes.append(runtime)
        return runtime

    # Capture the actual startup boundary, not just the UI's candidate identity.
    monkeypatch.setattr(app, "_factory", factory)
    edit(dialog.values[0], "42")
    expected = session.resolve()
    counters = app.snapshot().counters
    original = app.snapshot().candidate
    dialog.close()
    assert page.save()
    assert app.snapshot().candidate == original
    page.apply()
    until(lambda: not page.busy)
    candidate = app.snapshot().candidate
    assert candidate is not None and candidate.recipe_id == expected.recipe_id
    assert app.snapshot().active is None
    assert window.choices[-1].prepared.configuration == expected
    dialog = open_editor(window)
    bridge.start()
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    bridge.refresh()
    assert app.snapshot().active == candidate
    assert received[0].configuration == expected
    assert page.open_roi.isEnabled()
    before = session.draft.to_recipe()
    assert all(line.isReadOnly() for line in dialog.values)
    assert not dialog.buttons["roi_create"].isEnabled()
    assert not dialog.buttons["roi_reset"].isEnabled()
    assert not dialog.canvas.model.can_drag
    drag(dialog, point(dialog, 45, 5), point(dialog, 47, 6))
    drag(dialog, point(dialog, 49, 10), point(dialog, 51, 12))
    dialog.create_override()
    dialog.reset_override()
    dialog.commit_geometry(BoundingRect(0, 0, 2, 2))
    dialog.numeric_changed()
    assert session.draft.to_recipe() == before
    dialog.buttons["roi_actual"].click()
    assert dialog.canvas.model.scale == 1
    dialog.canvas.zoom(8)
    drag(dialog, QPoint(400, 200), QPoint(380, 200), Qt.MouseButton.MiddleButton)
    assert session.draft.to_recipe() == before
    bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    bridge.refresh()
    assert dialog.canvas.model.can_drag and not dialog.values[0].isReadOnly()
    assert app.snapshot().counters == counters
    dialog.close()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (900, 1200)])
def test_responsive_bilingual_editor(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, _ = configured_screen
    window.set_language(language)
    window.resize(*size)
    publish(window)
    dialog = open_editor(window)
    assert dialog.windowTitle() == text("roi_editor", language)
    assert dialog.canvas.width() >= 600 and dialog.canvas.height() >= 240
    for button in dialog.buttons.values():
        assert dialog.rect().contains(button.geometry())
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    for line in dialog.values:
        assert dialog.rect().contains(line.geometry())
    assert not session.dirty
    dialog.close()


def test_existing_numeric_reset_creates_effective_roi(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, _), dialog = roi_screen
    dialog.close()
    page = window.parameters
    page.roi_override.setChecked(False)
    assert tuple(line.text() for line in page.roi_values) == ("40", "0", "8", "10")
    page.roi_override.setChecked(True)
    assert session.draft.rois[ChannelId(17)] == ("40", "0", "8", "10")


def test_failed_roi_candidate_start_preserves_active(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, runtimes), dialog = roi_screen
    bridge, page = window.bridge, window.parameters
    app = bridge.application
    bridge.start()
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    active = app.snapshot().active
    bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    bridge.refresh()
    edit(dialog.values[0], "42")
    dialog.close()
    assert page.save()
    page.apply()
    until(lambda: not page.busy)
    candidate = app.snapshot().candidate
    assert candidate != active
    bridge.start()
    until(lambda: len(runtimes) == 2)
    runtimes[1].ready.set_exception(RuntimeError("ROI candidate fake warmup failure"))
    until(lambda: app.snapshot().state is ApplicationState.READY)
    bridge.refresh()
    assert app.snapshot().active == active and app.snapshot().candidate == candidate
    dialog = open_editor(window)
    assert dialog.canvas.model.rect == BoundingRect(42, 1, 7, 9)
    assert dialog.canvas.model.can_drag and not session.dirty
    dialog.close()


def test_state_guard_before_qt_notification_and_bgr_ownership(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, runtimes), dialog = roi_screen
    image = dialog.canvas.grab().toImage()
    color = image.pixelColor(point(dialog, 100, 30))
    assert (color.red(), color.green(), color.blue()) == (0, 0, 40)
    before = session.draft.to_recipe()
    window.bridge.application.start()
    # Deliberately do not deliver the snapshot signal before attempted gestures.
    drag(dialog, point(dialog, 44.5, 5.5), point(dialog, 46.5, 8.5))
    dialog.reset_override()
    dialog.numeric_changed()
    assert session.draft.to_recipe() == before
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(
        lambda: window.bridge.application.snapshot().state is ApplicationState.RUNNING
    )
    dialog.close()


def test_running_pan_survives_snapshot_refresh(
    roi_screen: tuple[Screen, RoiEditorDialog],
) -> None:
    (window, session, runtimes), dialog = roi_screen
    window.bridge.start()
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(
        lambda: window.bridge.application.snapshot().state is ApplicationState.RUNNING
    )
    dialog.resize(800, 700)
    QApplication.processEvents()
    dialog.canvas.zoom(8)
    canvas = dialog.canvas
    before = session.draft.to_recipe()
    QTest.mousePress(canvas, Qt.MouseButton.MiddleButton, pos=QPoint(400, 200))
    window.parameters.refresh_state()  # New production snapshot during an active pan.
    end = QPoint(360, 180)
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(end),
        QPointF(canvas.mapToGlobal(end)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.MiddleButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(canvas, event)
    assert canvas.model.pan == (-40, -20)
    QTest.mouseRelease(canvas, Qt.MouseButton.MiddleButton, pos=end)
    assert session.draft.to_recipe() == before
    dialog.close()
