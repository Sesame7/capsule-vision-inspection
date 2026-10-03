from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QMessageBox,
)
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession
from tests.unit.test_recipe_editor import editor

from vimo_demo.application import (
    ApplicationState,
    PreparedConfiguration,
    ProductionApplication,
)
from vimo_demo.configuration import RecipeDefinition, load_recipe, save_recipe
from vimo_demo.inspection.production import ChannelId
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.parameters_page import DirtyDecision
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_editor import RecipeEditorSession


@pytest.fixture
def configured_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]]]:
    if not QFontDatabase.families():
        for filename in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / filename
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    session = editor(tmp_path)
    assert session.entry is not None
    ready = session.prepare(session.entry.recipe)
    runtimes: list[FakeSession] = []

    def factory(_p: PreparedConfiguration, _g: int) -> FakeSession:
        runtime = FakeSession(automatic_start=False)
        runtimes.append(runtime)
        return runtime

    app = ProductionApplication(factory)
    app.configure(ready)
    bridge = QtProductionBridge(
        app, LatestDisplayFrameStore(ready.configuration.layout.camera_ids), poll_ms=10
    )
    window = ProductionWindow(
        bridge, (RecipeChoice(ready, "A"),), recipe_editor=session
    )
    window.resize(1080, 1920)
    window.show()
    window.navigate(1)
    yield window, session, runtimes
    for runtime in runtimes:
        if not runtime.ready.done():
            runtime.ready.set_result(None)
    session.discard()
    window.close()
    until(lambda: bridge.closed)
    assert app.join(0)
    window.deleteLater()


def edit(line: QLineEdit, value: str) -> None:
    line.setText(value)
    line.textEdited.emit(value)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (900, 1200)])
def test_parameters_layout_language_and_viewing_is_clean(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    qapp: QApplication,
    language: Language,
    size: tuple[int, int],
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.resize(*size)
    qapp.processEvents()
    page = window.parameters
    assert window.pages.currentWidget() is page
    assert page.recipes.count() == 2 and not session.dirty
    page.scope.setCurrentIndex(1)
    page.camera.setCurrentIndex(2)
    page.scope.setCurrentIndex(2)
    page.channel.setCurrentIndex(4)
    window.navigate(0)
    window.navigate(1)
    assert not session.dirty and not runtimes
    for button in page.buttons.values():
        assert page.rect().contains(button.geometry())
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    assert page.scroll_area.viewport().width() > 650
    assert page.editor.width() <= page.scroll_area.viewport().width()


def test_hierarchy_effective_values_roi_and_distinct_explicit_values(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    page.camera.setCurrentIndex(2)
    page.channel.setCurrentIndex(4)
    page.scope.setCurrentIndex(2)
    maximum = page.groups["blob"].bounds["max_area"]
    assert maximum[0].isChecked() and maximum[1].text() == "1900"
    assert "1900" in page.groups["blob"].effective.text()
    maximum[0].setChecked(False)
    assert "1850" in page.groups["blob"].effective.text()
    assert session.draft.channels[ChannelId(17)]["blob"].max_area is None
    page.scope.setCurrentIndex(1)
    edit(page.groups["blob"].bounds["max_area"][1], "1852")
    assert "1852" in page.groups["blob"].effective.text()
    page.scope.setCurrentIndex(2)
    page.groups["blob"].booleans["enabled"].setCurrentIndex(2)  # explicit False
    page.groups["blob"].booleans["required"].setCurrentIndex(2)
    minimum, minimum_line = page.groups["blob"].bounds["min_area"]
    minimum.setChecked(True)
    edit(minimum_line, "0")
    page.groups["vimo"].label_override.setChecked(True)
    page.groups["vimo"].label_values.setPlainText("")
    assert page.validate()
    assert page.save()
    assert session.entry is not None
    checks = load_recipe(session.entry.path).channel_overrides[0].checks
    assert checks.blob.enabled is False and checks.blob.required is False
    assert checks.blob.min_area == 0 and checks.blob.max_area is None
    assert checks.vimo.disabled_labels == ()
    page.scope.setCurrentIndex(2)
    page.camera.setCurrentIndex(2)
    page.channel.setCurrentIndex(4)
    page.roi_override.setChecked(False)
    assert ChannelId(17) not in session.draft.rois
    assert "width=8" in page.roi_effective.text()
    page.roi_override.setChecked(True)
    edit(page.roi_values[2], "0")
    assert not page.validate() and not page.save()
    assert session.dirty and "ROI" in page.status.text()
    edit(page.roi_values[2], "7")
    assert page.save()


def test_save_apply_and_failed_candidate_preserve_active(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
) -> None:
    window, session, runtimes = configured_screen
    page, app = window.parameters, window.bridge.application
    page.apply()
    until(lambda: not page.busy)
    assert app.snapshot().candidate is not None and app.snapshot().active is None
    a = app.snapshot().candidate
    window.bridge.start()
    until(lambda: len(runtimes) == 1)
    assert not page.metadata["display_name"].isEnabled()
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    window.bridge.refresh()
    assert (
        not page.buttons["save"].isEnabled() and not page.buttons["apply"].isEnabled()
    )
    assert page.camera.isEnabled()  # Inspection without editing remains available.
    window.bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    window.bridge.refresh()
    assert page.metadata["display_name"].isEnabled()
    edit(page.metadata["display_name"], "新产品")
    assert session.dirty and not page.buttons["apply"].isEnabled()
    assert page.save(("candidate-b", "候选乙"))
    assert app.snapshot().candidate == a == app.snapshot().active
    before = app.snapshot().counters
    page.apply()
    until(lambda: not page.busy)
    assert app.snapshot().active == a
    candidate = app.snapshot().candidate
    assert candidate is not None and candidate.recipe_id == "candidate-b"
    assert app.snapshot().counters == before
    assert "candidate-b" in page.identity.text() and "product-a" in page.identity.text()
    window.bridge.start()
    until(lambda: len(runtimes) == 2)
    runtimes[1].ready.set_exception(RuntimeError("fake startup failed"))
    until(lambda: app.snapshot().state is ApplicationState.READY)
    window.bridge.refresh()
    assert app.snapshot().active == a and app.snapshot().candidate == candidate
    assert (
        "candidate-b" in window.identity_label.text()
        and "product-a" in window.identity_label.text()
    )


@pytest.mark.parametrize("action", ["select", "new", "close"])
@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_dirty_decisions(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    decision: DirtyDecision,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "有修改")
    assert text("unsaved", "zh") in page.identity.text()
    decisions: list[DirtyDecision] = []

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        decisions.append(decision)
        callback(decision)

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    if action == "select":
        target = next(
            i
            for i, e in enumerate(session.catalog.discover().entries)
            if e.path != original.path
        )
        page.recipes.activated.emit(target)
    elif action == "new":
        page.buttons["new_recipe"].click()
    else:
        window.close()
        if decision != "cancel":
            until(lambda: window.bridge.closed)
    assert decisions == [decision]
    if decision == "cancel":
        assert session.dirty and window.isVisible()
        assert session.entry == original
    elif action == "new":
        assert session.entry is None and not session.draft.cameras
    elif action == "select":
        assert session.entry is not None and session.entry.path != original.path
    assert load_recipe(original.path).display_name == (
        "有修改" if decision == "save" else original.recipe.display_name
    )


def test_invalid_save_does_not_continue_dirty_navigation(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    edit(page.metadata["display_name"], "")

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        callback("save")

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    original = session.entry
    page.buttons["new_recipe"].click()
    assert session.entry == original and session.dirty
    assert text("invalid", "zh") in page.status.text()


def test_async_preparation_close_wait_and_no_late_apply(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    entered, release, heartbeat = Event(), Event(), Event()
    prepare = session.prepare
    thread_ids: list[int] = []
    before = window.bridge.application.snapshot().candidate

    def held(recipe: RecipeDefinition) -> PreparedConfiguration:
        thread_ids.append(get_ident())
        entered.set()
        assert release.wait(5)
        return prepare(recipe)

    monkeypatch.setattr(session, "prepare", held)
    try:
        page.apply()
        until(entered.is_set)
        QTimer.singleShot(0, heartbeat.set)
        until(heartbeat.is_set)
        assert page.busy and thread_ids != [get_ident()]
        window.close()
        assert window.isVisible() and not window.bridge.closed
        release.set()
        until(lambda: window.bridge.closed)
        assert window.bridge.application.snapshot().candidate == before
    finally:
        release.set()


def test_apply_artifact_failure_does_not_change_candidate(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    before = window.bridge.application.snapshot()
    Path(session.registry.get(session.default_model_id).model_path).write_bytes(
        b"wrong SHA"
    )
    page.apply()
    until(lambda: not page.busy)
    assert "SHA-256" in page.status.text()
    assert window.bridge.application.snapshot() == before and not runtimes
    assert not session.dirty and page.metadata["display_name"].isEnabled()


@pytest.mark.parametrize(
    "state",
    [
        ApplicationState.STARTING,
        ApplicationState.RUNNING,
        ApplicationState.STOPPING,
        ApplicationState.FAULT,
        ApplicationState.CLOSED,
    ],
)
def test_locked_state_mapping(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    monkeypatch: pytest.MonkeyPatch,
    state: ApplicationState,
) -> None:
    window, _, _ = configured_screen
    app, page = window.bridge.application, window.parameters
    snapshot = replace(app.snapshot(), state=state)
    with monkeypatch.context() as patch:
        patch.setattr(app, "snapshot", lambda: snapshot)
        page.refresh_state()
        assert page.recipes.isEnabled() and page.scope.isEnabled()
        assert not page.metadata["display_name"].isEnabled()
        assert all(
            not page.buttons[k].isEnabled()
            for k in ("validate", "save", "save_as", "apply", "new_recipe")
        )
    page.refresh_state()


def test_unknown_model_stays_invalid_on_unrelated_edit(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    save_recipe(
        session.entry.path,
        replace(session.entry.recipe, model_override_id="unknown-model"),
    )
    page = window.parameters
    page.reload()
    assert "unknown-model" in page.model.currentText()
    edit(page.metadata["display_name"], "名称")
    assert session.draft.model_override_id == "unknown-model"
    assert not page.validate() and not page.save()


def test_save_as_actual_dialog_validation_and_persistence(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    before = window.bridge.application.snapshot()
    page.buttons["save_as"].click()
    dialog = page.findChildren(QDialog)[-1]
    identity, name = dialog.findChildren(QLineEdit)
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons is not None
    assert session.entry is not None
    identity.setText(session.entry.recipe.recipe_id)
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    assert dialog.isVisible()  # Duplicate identity does not close or overwrite.
    identity.setText("variant-dialog")
    name.setText("对话框变体")
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    assert not dialog.isVisible()
    assert session.entry.recipe.recipe_id == "variant-dialog" and not session.dirty
    assert page.recipes.currentText() == "对话框变体"
    assert window.bridge.application.snapshot() == before


@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_actual_dirty_dialog_buttons(
    configured_screen: tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]],
    decision: DirtyDecision,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "未保存")
    page.buttons["new_recipe"].click()
    dialog = page.findChildren(QMessageBox)[-1]
    button = next(
        b for b in dialog.buttons() if b.text() == text(decision, page.language)
    )
    button.click()
    assert not dialog.isVisible()
    if decision == "cancel":
        assert session.dirty and session.entry == original
    else:
        assert session.entry is None
    assert load_recipe(original.path).display_name == (
        "未保存" if decision == "save" else original.recipe.display_name
    )
