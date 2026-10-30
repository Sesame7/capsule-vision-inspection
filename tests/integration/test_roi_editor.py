from collections.abc import Iterator

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QMessageBox
from tests.support.application import FakeSession
from tests.support.qt import until
from tests.support.recipe_ui import configured_screen as configured_screen

from capsule_vision.application import ApplicationState
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.configuration import load_machine_layout
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId, CycleId, Frame, FrameId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.vision_page import VisionPage

type Screen = tuple[MainWindow, RecipeEditorSession, list[FakeSession]]


def publish(
    window: MainWindow, sequence: int = 1, size: tuple[int, int] = (120, 60)
) -> None:
    image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    image[:, :, 0] = 40
    window.bridge.frames.publish(
        Frame(FrameId(CycleId(1), CameraId("CAM3"), sequence), 1.0, image), generation=0
    )


def open_editor(window: MainWindow) -> VisionPage:
    window.navigate(4)
    dialog = window.vision_workspace
    assert dialog is not None
    dialog.camera_cards[CameraId("CAM3")].click()
    dialog.select_roi(4)
    QApplication.processEvents()
    return dialog


@pytest.fixture
def roi_screen(
    configured_screen: Screen,
) -> Iterator[tuple[Screen, VisionPage]]:
    window, session, _ = configured_screen
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.recipe_id == "product-b"
    )
    window.recipe_page.recipes.activated.emit(index)
    publish(window)
    dialog = open_editor(window)
    yield configured_screen, dialog
    if window.vision_workspace is not None:
        session.machine_editor.discard()
        window.vision_workspace.request_back()


def point(dialog: VisionPage, x: float, y: float) -> QPoint:
    canvas = dialog.canvas
    t = canvas.model.transform(canvas.width(), canvas.height())
    assert t is not None
    return QPoint(round(t.x + x * t.scale), round(t.y + y * t.scale))


def drag(
    dialog: VisionPage,
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


def test_six_rois_selection_move_resize_and_camera_switch(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (_, session, _), dialog = roi_screen
    before = session.machine
    recipe = session.draft.to_recipe()
    assert len(dialog.canvas.rois) == 6
    assert [s.channel_id for s in dialog.canvas.rois] == list(range(13, 19))
    assert dialog.canvas.model.rect == BoundingRect(40, 0, 8, 10)
    drag(dialog, point(dialog, 44, 5), point(dialog, 46, 8))
    assert dialog.canvas.model.rect == BoundingRect(42, 3, 8, 10)
    drag(dialog, point(dialog, 50, 13), point(dialog, 52, 15))
    assert dialog.canvas.model.rect == BoundingRect(42, 3, 10, 12)
    draft = session.machine_editor
    assert draft.dirty and not session.dirty and session.draft.to_recipe() == recipe
    assert session.machine == before
    for a, b in zip(
        before.layout.cameras[2].slots,
        draft.camera(CameraId("CAM3")).slots,
        strict=True,
    ):
        if a.channel_id != 17:
            assert a == b
    dialog.camera_cards[CameraId("CAM4")].click()
    assert dialog.frame is None and not dialog.canvas.model.can_drag
    dialog.camera_cards[CameraId("CAM3")].click()
    dialog.select_roi(4)
    assert dialog.canvas.model.rect == BoundingRect(42, 3, 10, 12)
    QTest.mouseClick(dialog.canvas, Qt.MouseButton.LeftButton, pos=point(dialog, 24, 5))
    assert dialog.selected_index == 2
    assert dialog.canvas.model.rect == BoundingRect(20, 0, 8, 10)
    draft.discard()
    dialog.request_back()


def test_invalid_batch_is_atomic(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (window, session, _), dialog = roi_screen
    before = session.machine_editor.definition
    dialog.common_width.setText("999")
    dialog.common_height.setText("10")
    dialog.common_size()
    assert session.machine_editor.definition == before
    assert "rejected" in window.event_label.toolTip()
    assert (
        dialog.canvas.model.can_drag
    )  # Invalid batch does not disable visual recovery.
    dialog.request_back()


def test_save_apply_start_and_no_running_edits(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (window, session, runtimes), dialog = roi_screen
    page, bridge = window.recipe_page, window.bridge
    before = bridge.application.snapshot().candidate_prepared
    dialog.commit_geometry(BoundingRect(42, 0, 8, 10))
    assert bridge.application.snapshot().candidate_prepared is before
    dialog.buttons["save_apply"].click()
    until(lambda: not page.busy)
    assert session.machine_path is not None
    assert load_machine_layout(session.machine_path) == session.machine
    assert not session.machine_editor.dirty
    candidate = bridge.application.snapshot().candidate_prepared
    assert candidate is not None
    assert candidate.configuration.layout.cameras[2].slots[4].rect.x == 42
    assert bridge.application.snapshot().active is None
    bridge.start()
    before = session.machine_editor.definition
    dialog.commit_geometry(BoundingRect(44, 0, 8, 10))
    dialog.common_size()
    assert session.machine_editor.definition == before
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(lambda: bridge.application.snapshot().state is ApplicationState.RUNNING)
    bridge.refresh()
    assert (
        bridge.application.snapshot().active == bridge.application.snapshot().candidate
    )
    assert all(
        line.isReadOnly() for line in (dialog.common_width, dialog.common_height)
    )
    assert not dialog.buttons["save_apply"].isEnabled()
    dialog.canvas.zoom(8)
    drag(dialog, QPoint(400, 200), QPoint(360, 180), Qt.MouseButton.MiddleButton)
    assert session.machine_editor.definition == before
    bridge.stop()
    until(lambda: bridge.application.snapshot().state is ApplicationState.READY)
    bridge.refresh()
    assert dialog.canvas.model.can_drag
    dialog.request_back()


def test_reference_frozen_and_bgr(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (window, session, _), dialog = roi_screen
    first = dialog.frame
    assert first is not None
    color = dialog.canvas.grab().toImage().pixelColor(point(dialog, 100, 30))
    assert (color.red(), color.green(), color.blue()) == (0, 0, 40)
    publish(window, 2)
    assert dialog.frame is first
    dialog.refresh_frame()
    assert dialog.frame is not first
    assert not session.machine_editor.dirty
    dialog.request_back()


def test_fast_placement_buttons(roi_screen: tuple[Screen, VisionPage]) -> None:
    (_, session, _), dialog = roi_screen
    dialog.common_width.setText("6")
    dialog.common_height.setText("8")
    dialog.buttons["roi_common"].click()
    assert all((s.rect.width, s.rect.height) == (6, 8) for s in dialog.canvas.rois)
    dialog.select_roi(5)
    dialog.commit_geometry(BoundingRect(61, 21, 6, 8))
    dialog.buttons["roi_distribute"].click()
    assert [s.rect.x for s in dialog.canvas.rois] == [1, 13, 25, 37, 49, 61]
    assert [s.rect.y for s in dialog.canvas.rois] == [1, 5, 9, 13, 17, 21]
    session.machine_editor.discard()
    dialog.request_back()


@pytest.mark.parametrize("decision", ["cancel", "discard", "save"])
def test_dirty_navigation_requires_decision(
    roi_screen: tuple[Screen, VisionPage], decision: str
) -> None:
    (window, session, _), dialog = roi_screen
    candidate = window.bridge.application.snapshot().candidate
    dialog.commit_geometry(BoundingRect(42, 0, 8, 10))
    window.navigate(0)
    boxes = dialog.findChildren(QMessageBox)
    assert len(boxes) == 1 and session.machine_editor.dirty
    box = boxes[0]
    button = next(
        b for b in box.buttons() if b.text() == text(decision, dialog.language)
    )
    button.click()
    if decision == "cancel":
        assert window.pages.currentWidget() is window.vision_page
        assert window.vision_workspace is dialog and session.machine_editor.dirty
    else:
        until(lambda: window.vision_workspace is None)
        assert window.pages.currentIndex() == 0
        assert not session.machine_editor.dirty
        if decision == "save":
            assert session.machine_path is not None
            assert (
                load_machine_layout(session.machine_path)
                .layout.cameras[2]
                .slots[4]
                .rect.x
                == 42
            )
            assert window.bridge.application.snapshot().active is None
            assert window.bridge.application.snapshot().candidate == candidate


def test_compact_tools_wheel_and_pan_preserve_geometry(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (window, session, _), dialog = roi_screen
    before = session.machine_editor.definition
    canvas = dialog.canvas
    for key, scale in (("roi_actual", 1), ("roi_fit", None)):
        dialog.buttons[key].click()
        assert canvas.model.scale == scale
    center = QPointF(canvas.width() / 2, canvas.height() / 2)
    wheel = QWheelEvent(
        center,
        center,
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(canvas, wheel)
    assert canvas.model.scale is not None
    canvas.zoom(8)
    drag(dialog, QPoint(400, 200), QPoint(360, 180), Qt.MouseButton.MiddleButton)
    publish(window, 7)
    dialog.buttons["roi_refresh"].click()
    assert dialog.frame is not None and dialog.frame.frame_id.sequence == 7
    assert canvas.model.scale is None
    assert session.machine_editor.definition == before


def test_prepare_failure_keeps_saved_machine_and_workspace(
    roi_screen: tuple[Screen, VisionPage], monkeypatch: pytest.MonkeyPatch
) -> None:
    (window, session, _), dialog = roi_screen
    candidate = window.bridge.application.snapshot().candidate

    def fail(_recipe: object) -> None:
        raise ValueError("artifact unavailable")

    monkeypatch.setattr(session, "prepare", fail)
    dialog.commit_geometry(BoundingRect(42, 0, 8, 10))
    dialog.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert "artifact unavailable" in window.event_label.toolTip()
    assert not session.machine_editor.dirty
    assert window.bridge.application.snapshot().candidate == candidate
    assert window.vision_workspace is dialog


def test_camera_cards_use_persisted_canonical_order(configured_screen: Screen) -> None:
    window, session, _ = configured_screen
    assert session.machine_path is not None
    layout = load_machine_layout(session.machine_path).layout
    publish(window)
    workspace = open_editor(window)
    assert (
        list(workspace.camera_cards)
        == list(layout.camera_ids)
        == [f"CAM{i}" for i in range(1, 7)]
    )
    QTest.mouseClick(
        workspace.camera_cards[layout.camera_ids[3]],
        Qt.MouseButton.LeftButton,
    )
    assert workspace.camera == CameraId("CAM4") and workspace.frame is None
    QTest.mouseClick(
        workspace.camera_cards[layout.camera_ids[2]],
        Qt.MouseButton.LeftButton,
    )
    assert workspace.camera == CameraId("CAM3")
    assert workspace.frame is not None and len(workspace.canvas.rois) == 6
    workspace.camera_cards[CameraId("CAM2")].setFocus()
    QTest.keyClick(workspace.camera_cards[CameraId("CAM2")], Qt.Key.Key_Space)
    assert workspace.camera == CameraId("CAM2")
    workspace.camera_cards[CameraId("CAM3")].setFocus()
    QTest.keyClick(workspace.camera_cards[CameraId("CAM3")], Qt.Key.Key_Space)
    assert workspace.camera == CameraId("CAM3")
    workspace.request_back()


def test_wheel_anchor_and_pan(roi_screen: tuple[Screen, VisionPage]) -> None:
    (window, session, _), workspace = roi_screen
    publish(window, 9, (1600, 1200))
    workspace.refresh_frame()
    canvas = workspace.canvas
    before = session.machine_editor.definition
    canvas.zoom(2)
    point = QPointF(canvas.width() / 2 + 40, canvas.height() / 2 + 30)
    transform = canvas.model.transform(canvas.width(), canvas.height())
    assert transform is not None
    source = transform.source_point(point.x(), point.y())
    event = QWheelEvent(
        point,
        point,
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(canvas, event)
    zoomed = canvas.model.transform(canvas.width(), canvas.height())
    assert zoomed is not None and zoomed.scale == pytest.approx(2.4)
    assert zoomed.source_point(point.x(), point.y()) == pytest.approx(source)
    pan = canvas.model.pan
    drag(workspace, QPoint(400, 400), QPoint(360, 380), Qt.MouseButton.MiddleButton)
    assert canvas.model.pan == pytest.approx((pan[0] - 40, pan[1] - 20))
    assert session.machine_editor.definition == before


def test_machine_save_failure_does_not_prepare(
    roi_screen: tuple[Screen, VisionPage], monkeypatch: pytest.MonkeyPatch
) -> None:
    (window, session, _), workspace = roi_screen
    saved = session.machine_editor.saved
    candidate = window.bridge.application.snapshot().candidate

    def fail() -> None:
        raise ValueError("machine file is read-only")

    monkeypatch.setattr(session.machine_editor, "save", fail)
    workspace.commit_geometry(BoundingRect(42, 0, 8, 10))
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert session.machine_editor.saved == saved and session.machine_editor.dirty
    assert "read-only" in window.event_label.toolTip()
    assert not window.recipe_page.busy
    assert window.bridge.application.snapshot().candidate == candidate


def test_workspace_open_refresh_and_reopen_never_reinitialize_saved_layout(
    roi_screen: tuple[Screen, VisionPage],
) -> None:
    (window, session, _), workspace = roi_screen
    # This fixture intentionally has tiny, nonpartitioned rectangles.
    saved = session.machine_editor.saved
    assert session.machine_path is not None
    payload = session.machine_path.read_bytes()
    for _ in range(2):
        workspace.refresh_frame()
        workspace.camera_cards[CameraId("CAM1")].click()
        workspace.camera_cards[CameraId("CAM3")].click()
        assert session.machine_editor.definition == saved
        workspace.request_back()
        workspace = open_editor(window)
    assert session.machine_editor.definition == saved
    assert not session.machine_editor.dirty
    assert session.machine_path.read_bytes() == payload


def _assert_source_free_roi_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, _ = configured_screen
    window.set_language(language)
    window.resize(*size)
    publish(window)
    dialog = open_editor(window)
    assert dialog.buttons["vision_runtime"].accessibleName() == text(
        "vision_runtime", language
    )
    assert set(dialog.findChildren(QComboBox)) == {
        dialog.reference,
        dialog.channel,
        *(card.device for card in dialog.camera_cards.values()),
    }
    source_fields = {
        field for card in dialog.camera_cards.values() for field in (card.folder,)
    }
    assert set(dialog.findChildren(QLineEdit)) == set(
        dialog.coordinates.values()
    ) | source_fields | {dialog.fps}
    assert dialog.canvas.width() >= 600 and dialog.canvas.height() >= 240
    for button in dialog.buttons.values():
        assert dialog.rect().contains(button.mapTo(dialog, QPoint(0, 0)))
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
        )
    assert dialog.canvas.height() > dialog.source_cards.height()
    assert not session.dirty
    dialog.request_back()


def test_source_free_roi_layout_matrix(configured_screen: Screen) -> None:
    window, session, runtimes = configured_screen
    assert session.camera_sources is None
    before = window.bridge.application.snapshot()
    languages: tuple[Language, Language] = ("zh", "en")
    for size in ((1080, 1920), (1920, 1080)):
        for language in languages:
            try:
                _assert_source_free_roi_layout(configured_screen, language, size)
                until(lambda: window.vision_workspace is None)
                assert window.bridge.application.snapshot() == before
                assert not runtimes and not session.dirty
            except Exception as exc:
                exc.add_note(f"Source-free ROI: size={size}, language={language}")
                raise
