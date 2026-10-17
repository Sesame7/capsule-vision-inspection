from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLineEdit,
    QMessageBox,
    QScrollArea,
)
from tests.qt_support import until
from tests.recipe_ui_support import Screen, assert_inside, edit
from tests.recipe_ui_support import configured_screen as configured_screen

from vimo_demo.application import (
    ApplicationState,
    PreparedConfiguration,
)
from vimo_demo.configuration import (
    ConfigurationIOError,
    ModelRegistry,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)
from vimo_demo.ui.parameters_page import DirtyDecision
from vimo_demo.ui.production_style import Language, Style, text


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (900, 1200)])
def test_compact_recipe_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    page = window.parameters
    assert page.recipes.count() == 2 and not session.dirty and not runtimes
    assert set(page.metadata) == {"display_name", "product_code"}
    assert page.findChildren(QComboBox) == [page.recipes, page.model]
    assert page.findChildren(QScrollArea) == [page.summary_scroll]
    assert not page.findChildren(QGroupBox)
    assert len(page.findChildren(QLineEdit)) == 2
    assert set(page.buttons) == {
        "new_recipe",
        "duplicate_recipe",
        "delete_recipe",
        "reload",
        "recipe_discard",
        "save_apply",
    }
    assert window.navigation[1].text() == text("parameters", language)
    for button in page.buttons.values():
        assert page.rect().contains(button.mapTo(page, QPoint(0, 0)))
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    assert session.entry is not None
    model = session.registry.get(session.default_model_id)
    assert not page.model_details.isVisible()
    assert model.model_id in page.model_status.text()
    page.model_more.click()
    assert (
        page.model_details.isVisible()
        and model.expected_sha256 in page.model_details.text()
    )
    page.model_more.click()
    assert page.roi_workspace is None
    window.navigate(4)
    assert (
        page.roi_workspace is not None
        and page.roi_workspace.isVisible()
        and window.pages.currentWidget() is page.settings
    )
    window.navigate(1)


def test_metadata_model_and_hidden_overrides_preserved(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    assert session.entry is not None
    original = session.entry.recipe
    assert not page.notice.isVisible()
    page.advanced.click()
    assert page.notice.isVisible()
    edit(page.metadata["display_name"], "新产品")
    edit(page.metadata["product_code"], "SKU-中文")
    page.buttons["save_apply"].click()
    until(lambda: not page.busy)
    saved = load_recipe(session.entry.path)
    assert saved == replace(original, display_name="新产品", product_code="SKU-中文")
    assert window.bridge.application.snapshot().active is None
    assert window.choices[-1].prepared.configuration.recipe_id == original.recipe_id
    # Explicit default model IDs survive an unrelated edit; changing the selector
    # deliberately uses the registry/default mapping, never the display name as ID.
    default_index = next(
        i
        for i, m in enumerate(session.registry.models)
        if m.model_id == session.default_model_id
    )
    page.model.setCurrentIndex(default_index)
    page.model.activated.emit(default_index)
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path).model_override_id is None
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if not e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    assert page.notice.isVisible() and "required" in page.notice.text()


def test_save_apply_start_and_running_view_only(configured_screen: Screen) -> None:
    window, session, runtimes = configured_screen
    page, app = window.parameters, window.bridge.application
    assert session.entry is not None
    original = session.entry.recipe
    edit(page.metadata["display_name"], "Updated")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path).display_name == "Updated"
    assert app.snapshot().active is None
    assert "next successful Start" in text("roi_applied", "en")
    assert page.status.text() == text("roi_applied", page.language)
    assert text("model_verified", page.language) in page.model_status.text()
    window.bridge.start()
    until(lambda: len(runtimes) == 1)
    window.bridge.refresh()
    assert text("model_running", page.language) not in page.model_status.text()
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    window.bridge.refresh()
    assert text("model_running", page.language) in page.model_status.text()
    active = app.snapshot().active
    assert active is not None and active.recipe_id == original.recipe_id
    assert not page.buttons["save_apply"].isEnabled()
    assert page.recipes.isEnabled() and not page.metadata["display_name"].isEnabled()
    before = session.draft.to_recipe()
    page.save_and_apply()
    assert session.draft.to_recipe() == before
    window.bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    window.bridge.refresh()
    assert text("model_running", page.language) not in page.model_status.text()


@pytest.mark.parametrize(
    "outcome", ["cancel", "delete", "candidate_changed", "failure"]
)
def test_recipe_delete_confirmation_identity_and_live_protection(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    assert not page.buttons["delete_recipe"].isEnabled()  # Current candidate.
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    entry = session.entry
    assert entry is not None
    original = entry.path.read_bytes()
    before = window.bridge.application.snapshot()
    page.buttons["delete_recipe"].click()
    dialog = next(d for d in page.findChildren(QMessageBox) if d.isVisible())
    assert (
        entry.recipe.recipe_id in dialog.text()
        and entry.recipe.display_name in dialog.text()
    )
    assert dialog.defaultButton() is not None and dialog.defaultButton().text() == text(
        "cancel", page.language
    )
    if outcome == "candidate_changed":
        window.bridge.application.configure(session.prepare(entry.recipe))
    if outcome == "failure":

        def denied(*, protected_ids: frozenset[str]) -> None:
            raise ConfigurationIOError("Delete denied")

        monkeypatch.setattr(session, "delete", denied)
    caption = text("cancel" if outcome == "cancel" else "delete_recipe", page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    if outcome == "delete":
        assert not entry.path.exists() and session.entry is None
        assert page.recipes.currentIndex() == -1 and not session.dirty
        assert window.bridge.application.snapshot() == before
    else:
        assert entry.path.read_bytes() == original and session.entry == entry
        if outcome != "cancel":
            assert page.status.text() == text("recipe_delete_error", page.language)
    assert not runtimes


@pytest.mark.parametrize("outcome", ["cancel", "reload", "failure"])
def test_recipe_discard_reads_disk_and_preserves_other_domains(
    configured_screen: Screen, outcome: str
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    assert session.entry is not None
    entry = session.entry
    assert not page.buttons["recipe_discard"].isEnabled()
    edit(page.metadata["display_name"], "Unsaved")
    page.capsule_enabled["size"].click()
    draft = replace(session.draft)
    machine = session.machine_editor.definition
    before = window.bridge.application.snapshot()
    persisted = replace(
        entry.recipe, display_name="Changed on disk", product_code="disk-code"
    )
    save_recipe(entry.path, persisted)
    if outcome == "failure":
        entry.path.write_text("{", encoding="utf8")
    payload = entry.path.read_bytes()
    page.buttons["recipe_discard"].click()
    dialog = next(d for d in page.findChildren(QMessageBox) if d.isVisible())
    assert entry.recipe.recipe_id in dialog.text()
    caption = text("cancel" if outcome == "cancel" else "recipe_discard", page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    if outcome == "reload":
        assert session.entry.recipe == persisted and not session.dirty
        assert page.metadata["display_name"].text() == "Changed on disk"
    else:
        assert session.draft == draft and session.entry == entry
        if outcome == "failure":
            assert page.status.text() == text("recipe_discard_error", page.language)
            save_recipe(
                entry.path, entry.recipe
            )  # Restore this test's file for teardown.
    if outcome != "failure":
        assert entry.path.read_bytes() == payload
    assert session.machine_editor.definition == machine
    assert window.bridge.application.snapshot() == before and not runtimes


def test_active_recipe_stays_protected_without_candidate(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    snapshot = window.bridge.application.snapshot()
    assert snapshot.candidate is not None
    payload = session.entry.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(
            window.bridge.application,
            "snapshot",
            lambda: replace(snapshot, active=snapshot.candidate, candidate=None),
        )
        window.parameters.refresh_state()
        assert not window.parameters.buttons["delete_recipe"].isEnabled()
        window.parameters.request_delete()
        assert not any(
            d.isVisible() for d in window.parameters.findChildren(QMessageBox)
        )
    assert session.entry.path.read_bytes() == payload


def test_expanded_model_details_preserve_canvas_and_save_access(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    window.resize(1920, 1080)
    page = window.parameters
    page.open_preview("size")
    page.model_more.click()
    page.advanced.click()
    QApplication.processEvents()
    assert window.size().height() == 1080
    assert page.model_details.isVisible() and page.notice.isVisible()
    assert page.preview is not None and page.preview.canvas.height() >= 240
    assert_inside(page.buttons["save_apply"], page)
    assert_inside(page.buttons["recipe_discard"], page)
    assert not session.dirty
    page.leave_preview(lambda: None)
    until(lambda: page.preview is None)


@pytest.mark.parametrize("duplicate", [False, True])
def test_new_duplicate_dialog_and_save(
    configured_screen: Screen, duplicate: bool
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    if duplicate:
        page.recipes.activated.emit(
            next(
                i
                for i, e in enumerate(session.catalog.discover().entries)
                if e.recipe.channel_overrides
            )
        )
    assert session.entry is not None
    original = session.entry
    page.buttons["duplicate_recipe" if duplicate else "new_recipe"].click()
    dialog = page.findChildren(QDialog)[-1]
    lines = dialog.findChildren(QLineEdit)
    assert len(lines) == 1  # IDs are generated, never an operator form field.
    name = lines[0]
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    name.setText("")
    box.button(QDialogButtonBox.StandardButton.Ok).click()
    assert dialog.isVisible() and session.entry == original
    name.setText("新品种")
    box.button(QDialogButtonBox.StandardButton.Ok).click()
    assert not dialog.isVisible() and session.entry is None and session.dirty
    new = session.draft.to_recipe()
    assert new.recipe_id != original.recipe.recipe_id
    expected = (
        replace(original.recipe, recipe_id=new.recipe_id, display_name="新品种")
        if duplicate
        else RecipeDefinition(new.recipe_id, "新品种")
    )
    assert new == expected
    assert (
        len(session.catalog.discover().entries) == 2
    )  # Draft only until Save and apply.
    page.save_and_apply()
    until(lambda: not page.busy)
    assert session.entry is not None and load_recipe(session.entry.path) == expected
    assert load_recipe(original.path) == original.recipe
    assert len(session.catalog.discover().entries) == 3
    assert window.bridge.application.snapshot().active is None


@pytest.mark.parametrize(
    "action", ["select", "new_recipe", "duplicate_recipe", "close"]
)
@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_dirty_decisions(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    decision: DirtyDecision,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "有修改")

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        callback(decision)

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    if action == "select":
        target = next(
            i
            for i, e in enumerate(session.catalog.discover().entries)
            if e.path != original.path
        )
        page.recipes.activated.emit(target)
    elif action == "close":
        window.close()
    else:
        page.buttons[action].click()
    until(lambda: not page.busy)
    if decision == "cancel":
        assert session.dirty and session.entry == original and window.isVisible()
    elif action == "select":
        assert session.entry is not None and session.entry.path != original.path
    elif action == "close":
        until(lambda: window.bridge.closed)
    else:
        dialogs = [d for d in page.findChildren(QDialog) if d.isVisible()]
        assert len(dialogs) == 1
        dialogs[0].reject()
    assert load_recipe(original.path).display_name == (
        "有修改" if decision == "save" else original.recipe.display_name
    )


def test_invalid_save_blocks_navigation(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    original = session.entry
    edit(page.metadata["display_name"], "")

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        callback("save")

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    page.buttons["new_recipe"].click()
    assert session.entry == original and session.dirty
    assert page.status.text() == text("recipe_validation_error", page.language)
    assert "display_name" in page.status.toolTip()
    assert not page.findChildren(QDialog)


def test_async_preparation_close_wait_and_no_late_apply(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
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
        page.save_and_apply()
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


def test_prepare_failure_preserves_saved_edits_but_not_promoted(
    configured_screen: Screen,
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    before = window.bridge.application.snapshot()
    assert session.entry is not None
    Path(session.registry.get(session.default_model_id).model_path).write_bytes(
        b"wrong SHA"
    )
    edit(page.metadata["display_name"], "Saved before preparation")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert page.status.text() == text("model_prepare_error", page.language)
    assert "SHA-256" in page.status.toolTip()
    assert window.bridge.application.snapshot() == before and not runtimes
    assert (
        not session.dirty
        and load_recipe(session.entry.path).display_name == "Saved before preparation"
    )
    assert page.metadata["display_name"].isEnabled()


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
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, state: ApplicationState
) -> None:
    window, _, _ = configured_screen
    app, page = window.bridge.application, window.parameters
    snapshot = replace(app.snapshot(), state=state)
    with monkeypatch.context() as patch:
        patch.setattr(app, "snapshot", lambda: snapshot)
        page.refresh_state()
        assert page.recipes.isEnabled()
        assert not page.metadata["display_name"].isEnabled()
        assert all(
            not page.buttons[k].isEnabled()
            for k in (
                "save_apply",
                "new_recipe",
                "duplicate_recipe",
                "recipe_discard",
                "delete_recipe",
            )
        )
        page.request_discard()
        page.request_delete()
        assert not any(box.isVisible() for box in page.findChildren(QMessageBox))
    page.refresh_state()


def test_unknown_model_preserved_on_unrelated_edit(configured_screen: Screen) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    save_recipe(
        session.entry.path,
        replace(session.entry.recipe, model_override_id="unknown-model"),
    )
    page = window.parameters
    page.reload()
    assert page.model.currentText() == text("unavailable_model", page.language)
    edit(page.metadata["display_name"], "名称")
    assert session.draft.model_override_id == "unknown-model"
    page.save_and_apply()
    assert "unknown-model" in page.status.toolTip() and session.dirty


@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_actual_dirty_dialog(
    configured_screen: Screen, decision: DirtyDecision
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "未保存")
    page.buttons["reload"].click()
    dialog = page.findChildren(QMessageBox)[-1]
    caption = text("save_apply" if decision == "save" else decision, page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    until(lambda: not page.busy)
    assert not any(d.isVisible() for d in page.findChildren(QMessageBox))
    assert session.dirty == (decision == "cancel")
    assert load_recipe(original.path).display_name == (
        "未保存" if decision == "save" else original.recipe.display_name
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_single_model_and_unrelated_edit_preserve_model_semantics(
    configured_screen: Screen, explicit: bool
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    session.registry = ModelRegistry((session.registry.get(session.default_model_id),))
    baseline = replace(
        session.entry.recipe,
        model_override_id=session.default_model_id if explicit else None,
    )
    save_recipe(session.entry.path, baseline)
    page = window.parameters
    page.reload()
    assert page.model.count() == 1 and not page.model.isEnabled()
    assert page.model.currentText() == session.registry.models[0].display_name
    edit(page.metadata["product_code"], "new-code")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path) == replace(baseline, product_code="new-code")


def test_save_failure_keeps_draft_file_and_application(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    original = session.entry.path.read_bytes()
    before = window.bridge.application.snapshot()

    def fail(*, new_identity: tuple[str, str] | None = None) -> None:
        raise ConfigurationIOError("Recipe file is read-only")

    monkeypatch.setattr(session, "save", fail)
    page = window.parameters
    edit(page.metadata["display_name"], "Keep draft")
    page.save_and_apply()
    assert page.status.text() == text("recipe_save_error", page.language)
    assert "read-only" in page.status.toolTip() and session.dirty and not page.busy
    assert session.entry.path.read_bytes() == original
    assert window.bridge.application.snapshot() == before


def test_failed_preparation_blocks_dirty_switch(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    original = session.entry
    page = window.parameters
    before = window.bridge.application.snapshot()

    def fail(recipe: RecipeDefinition) -> PreparedConfiguration:
        raise ConfigurationIOError("model temporarily unavailable")

    def save(callback: Callable[[DirtyDecision], None]) -> None:
        callback("save")

    monkeypatch.setattr(session, "prepare", fail)
    monkeypatch.setattr(page, "confirm_dirty", save)
    edit(page.metadata["display_name"], "Persisted")
    target = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.path != original.path
    )
    page.recipes.activated.emit(target)
    until(lambda: not page.busy)
    assert session.entry.path == original.path
    assert load_recipe(original.path).display_name == "Persisted"
    assert page.status.text() == text("recipe_prepare_error", page.language)
    assert "unavailable" in page.status.toolTip()
    assert window.bridge.application.snapshot() == before


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (900, 1200)])
def test_vertical_sections_and_responsive_fields(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    page = window.parameters
    sections = (page.recipe_section, page.model_section, page.inspection_section)
    for above, below in zip(sections, sections[1:], strict=False):
        assert above.geometry().bottom() < below.y()
    for control in (
        *sections,
        page.recipes,
        page.model,
        *page.metadata.values(),
        *page.buttons.values(),
    ):
        assert_inside(control, page)
    name, product = page.metadata.values()
    assert abs(name.width() - product.width()) <= 1
    assert page.model.width() > page.width() // 2
    assert (
        page.buttons["save_apply"].geometry().right() >= page.width() - 2 * Style.space
    )
    before = window.bridge.application.snapshot()
    page.expand_tuning["size"].click()
    preview = page.preview
    assert preview is not None and page.tuning_section.isVisible()
    assert (
        page.expand_tuning["size"].isChecked()
        and not page.expand_tuning["color"].isChecked()
    )
    assert_inside(preview, page)
    assert preview.canvas.height() >= 240
    assert window.size().width() == size[0]
    page.expand_tuning["color"].click()
    assert page.preview is preview and preview.mode == "color"
    assert (
        not session.dirty
        and not runtimes
        and window.bridge.application.snapshot() == before
    )
    page.expand_tuning["color"].click()
    until(lambda: page.preview is None)
