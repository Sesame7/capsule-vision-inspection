from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QMessageBox
from tests.integration.test_parameters_ui import configured_screen as configured_screen
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession

from vimo_demo.application import ApplicationState
from vimo_demo.configuration import load_machine_layout
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_editor import RecipeEditorSession
from vimo_demo.ui.roi_editor import MachineRoiWorkspace

type Screen = tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]]


def publish(
    window: ProductionWindow, sequence: int = 1, size: tuple[int, int] = (120, 60)
) -> None:
    image = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    image[:, :, 0] = 40
    window.bridge.frames.publish(
        Frame(FrameId(CycleId(1), CameraId("CAM3"), sequence), 1.0, image), generation=0
    )


def open_editor(window: ProductionWindow) -> MachineRoiWorkspace:
    page = window.parameters
    window.navigate(4)
    dialog = page.roi_workspace
    assert dialog is not None
    dialog.camera_tabs.setCurrentIndex(2)
    dialog.select_roi(4)
    QApplication.processEvents()
    return dialog


@pytest.fixture
def roi_screen(
    configured_screen: Screen,
) -> Iterator[tuple[Screen, MachineRoiWorkspace]]:
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
    if window.parameters.roi_workspace is not None:
        session.machine_editor.discard()
        window.parameters.roi_workspace.request_back()


def point(dialog: MachineRoiWorkspace, x: float, y: float) -> QPoint:
    canvas = dialog.canvas
    t = canvas.model.transform(canvas.width(), canvas.height())
    assert t is not None
    return QPoint(round(t.x + x * t.scale), round(t.y + y * t.scale))


def drag(
    dialog: MachineRoiWorkspace,
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
    roi_screen: tuple[Screen, MachineRoiWorkspace],
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
    dialog.camera_tabs.setCurrentIndex(3)
    assert dialog.frame is None and not dialog.canvas.model.can_drag
    dialog.camera_tabs.setCurrentIndex(2)
    dialog.select_roi(4)
    assert dialog.canvas.model.rect == BoundingRect(42, 3, 10, 12)
    QTest.mouseClick(dialog.canvas, Qt.MouseButton.LeftButton, pos=point(dialog, 24, 5))
    assert dialog.selected_index == 2
    assert dialog.canvas.model.rect == BoundingRect(20, 0, 8, 10)
    draft.discard()
    dialog.request_back()


def test_invalid_batch_is_atomic(
    roi_screen: tuple[Screen, MachineRoiWorkspace],
) -> None:
    (_, session, _), dialog = roi_screen
    before = session.machine_editor.definition
    dialog.common_width.setText("999")
    dialog.common_height.setText("10")
    dialog.common_size()
    assert session.machine_editor.definition == before
    assert "rejected" in dialog.message.toolTip()
    assert (
        dialog.canvas.model.can_drag
    )  # Invalid batch does not disable visual recovery.
    dialog.request_back()


def test_save_apply_start_and_no_running_edits(
    roi_screen: tuple[Screen, MachineRoiWorkspace],
) -> None:
    (window, session, runtimes), dialog = roi_screen
    page, bridge = window.parameters, window.bridge
    before_choice = window.choices[0].prepared
    dialog.commit_geometry(BoundingRect(42, 0, 8, 10))
    assert window.choices[0].prepared == before_choice
    dialog.buttons["roi_save_apply"].click()
    assert session.machine_path is not None
    assert load_machine_layout(session.machine_path) == session.machine
    assert not session.machine_editor.dirty
    until(lambda: not page.busy)
    assert window.choices[-1].prepared.configuration.channels[16].roi.x == 42
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
    assert not dialog.buttons["roi_save_apply"].isEnabled()
    dialog.canvas.zoom(8)
    drag(dialog, QPoint(400, 200), QPoint(360, 180), Qt.MouseButton.MiddleButton)
    assert session.machine_editor.definition == before
    bridge.stop()
    until(lambda: bridge.application.snapshot().state is ApplicationState.READY)
    bridge.refresh()
    assert dialog.canvas.model.can_drag
    dialog.request_back()


def test_reference_frozen_and_bgr(
    roi_screen: tuple[Screen, MachineRoiWorkspace],
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
    assert dialog.reference_modes["runtime"].accessibleName() == text(
        "reference_runtime", language
    )
    assert not dialog.findChildren(QComboBox)
    assert len(dialog.findChildren(QLineEdit)) == 4
    assert dialog.canvas.width() >= 600 and dialog.canvas.height() >= 240
    for button in dialog.buttons.values():
        assert dialog.rect().contains(button.mapTo(dialog, QPoint(0, 0)))
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 12
        )
    assert dialog.canvas.height() > dialog.height() * 0.7
    assert not session.dirty
    dialog.request_back()


def test_fast_placement_buttons(roi_screen: tuple[Screen, MachineRoiWorkspace]) -> None:
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


@pytest.mark.parametrize("decision", ["cancel", "discard", "roi_save_apply"])
def test_dirty_navigation_requires_decision(
    roi_screen: tuple[Screen, MachineRoiWorkspace], decision: str
) -> None:
    (window, session, _), dialog = roi_screen
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
        assert window.pages.currentWidget() is window.parameters.settings
        assert (
            window.parameters.roi_workspace is dialog and session.machine_editor.dirty
        )
    else:
        until(lambda: window.parameters.roi_workspace is None)
        assert window.pages.currentIndex() == 0
        assert not session.machine_editor.dirty
        if decision == "roi_save_apply":
            assert session.machine_path is not None
            assert (
                load_machine_layout(session.machine_path)
                .layout.cameras[2]
                .slots[4]
                .rect.x
                == 42
            )
            assert window.bridge.application.snapshot().active is None


def test_compact_tools_wheel_and_pan_preserve_geometry(
    roi_screen: tuple[Screen, MachineRoiWorkspace],
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
    roi_screen: tuple[Screen, MachineRoiWorkspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    (window, session, _), dialog = roi_screen
    candidate = window.bridge.application.snapshot().candidate

    def fail(_recipe: object) -> None:
        raise ValueError("artifact unavailable")

    monkeypatch.setattr(session, "prepare", fail)
    dialog.commit_geometry(BoundingRect(42, 0, 8, 10))
    dialog.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert "artifact unavailable" in dialog.message.toolTip()
    assert not session.machine_editor.dirty
    assert window.bridge.application.snapshot().candidate == candidate
    assert window.parameters.roi_workspace is dialog


def test_camera_tabs_use_custom_configured_order(configured_screen: Screen) -> None:
    window, session, _ = configured_screen
    draft = session.machine_editor
    layout = replace(
        draft.definition.layout,
        cameras=tuple(reversed(draft.definition.layout.cameras)),
    )
    draft.definition = draft.saved = replace(draft.definition, layout=layout)
    publish(window)
    workspace = open_editor(window)
    assert [workspace.camera_tabs.tabText(i) for i in range(6)] == [
        str(c.camera_id).removeprefix("CAM") for c in layout.cameras
    ]
    QTest.mouseClick(
        workspace.camera_tabs,
        Qt.MouseButton.LeftButton,
        pos=workspace.camera_tabs.tabRect(3).center(),
    )
    assert workspace.camera == CameraId("CAM3")
    assert workspace.frame is not None and len(workspace.canvas.rois) == 6
    workspace.camera_tabs.setFocus()
    QTest.keyClick(workspace.camera_tabs, Qt.Key.Key_Right)
    assert workspace.camera == CameraId("CAM2")
    QTest.keyClick(workspace.camera_tabs, Qt.Key.Key_Left)
    assert workspace.camera == CameraId("CAM3")
    workspace.request_back()


def test_wheel_anchor_and_pan(roi_screen: tuple[Screen, MachineRoiWorkspace]) -> None:
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
    roi_screen: tuple[Screen, MachineRoiWorkspace], monkeypatch: pytest.MonkeyPatch
) -> None:
    (window, session, _), workspace = roi_screen
    saved = session.machine_editor.saved
    candidate = window.bridge.application.snapshot().candidate

    def fail() -> None:
        raise ValueError("machine file is read-only")

    monkeypatch.setattr(session.machine_editor, "save", fail)
    workspace.commit_geometry(BoundingRect(42, 0, 8, 10))
    workspace.save_and_apply()
    assert session.machine_editor.saved == saved and session.machine_editor.dirty
    assert "read-only" in workspace.message.toolTip()
    assert not window.parameters.busy
    assert window.bridge.application.snapshot().candidate == candidate


def test_workspace_open_refresh_and_reopen_never_reinitialize_saved_layout(
    roi_screen: tuple[Screen, MachineRoiWorkspace],
) -> None:
    (window, session, _), workspace = roi_screen
    # This fixture intentionally has tiny, nonpartitioned rectangles.
    saved = session.machine_editor.saved
    assert session.machine_path is not None
    payload = session.machine_path.read_bytes()
    for _ in range(2):
        workspace.refresh_frame()
        workspace.camera_tabs.setCurrentIndex(0)
        workspace.camera_tabs.setCurrentIndex(2)
        assert session.machine_editor.definition == saved
        workspace.request_back()
        workspace = open_editor(window)
    assert session.machine_editor.definition == saved
    assert not session.machine_editor.dirty
    assert session.machine_path.read_bytes() == payload
