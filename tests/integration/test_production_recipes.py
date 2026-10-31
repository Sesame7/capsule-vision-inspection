"""Saved definitions, prepared candidates and active sessions remain independent."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOption,
    QStyleOptionComboBox,
    QStylePainter,
)
from tests.support.qt import close_window, until
from tests.support.recipe_ui import Screen, assert_inside
from tests.support.recipe_ui import configured_screen as configured_screen
from tests.support.startup import saved_data

from capsule_vision.application import ApplicationState, PreparedConfiguration
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import RecipeDefinition, load_recipe, save_recipe
from capsule_vision.configuration.preferences import load_preferences
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_startup_restores_latest_saved_same_id_without_start(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    path = data.recipes / "local.json"
    save_recipe(path, replace(load_recipe(path), display_name="Latest saved"))
    startup = load_startup(data)
    calls: list[str] = []
    prepare = RecipeEditorSession.prepare_saved

    def counted(owner: RecipeEditorSession, identity: str) -> PreparedConfiguration:
        calls.append(identity)
        return prepare(owner, identity)

    monkeypatch.setattr(RecipeEditorSession, "prepare_saved", counted)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        snapshot = startup.application.snapshot()
        assert snapshot.candidate_prepared is not None
        assert snapshot.candidate_prepared.configuration.display_name == "Latest saved"
        assert snapshot.active is None and snapshot.state is ApplicationState.READY
        assert not snapshot.runtime_owned
        picker = window.production_recipes
        assert picker.recipes.currentData() == "local"
        assert picker.recipes.currentText() == "Latest saved"
        preferences = data.preferences.read_bytes()
        save_recipe(path, replace(load_recipe(path), display_name="New saved version"))
        picker.reload()
        window.navigate(0)
        window.set_theme("light")
        window.bridge.refresh()
        assert picker.recipes.currentText() == "Latest saved"
        assert not calls and not window.editing.busy
        assert startup.application.snapshot() == snapshot
        assert data.preferences.read_bytes() == preferences
        # Only activated (including the same ID) requests preparation.
        picker.recipes.activated.emit(picker.recipes.currentIndex())
        until(lambda: not window.editing.busy)
        assert calls == ["local"]
        assert picker.recipes.currentText() == "New saved version"
        assert startup.application.snapshot().active is None
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("size", [(1080, 1857), (1920, 1017)])
def test_selector_geometry_and_localization_fallback(
    configured_screen: Screen, size: tuple[int, int]
) -> None:
    window, _session, _runtimes = configured_screen
    window.navigate(0)
    window.resize(*size)
    picker = window.production_recipes
    expected_refresh = picker.refresh_button.size()
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert picker.refresh_button.size() == expected_refresh
        assert picker.recipes.minimumWidth() <= picker.recipes.width() <= 180
        if language in ("zh", "en"):
            assert 160 <= picker.recipes.width() <= 180
        assert window.size().toTuple() == size
        assert picker.recipes.accessibleName().startswith(
            text("select_saved_recipe", language)
        )
        assert picker.refresh_button.toolTip() == text("short_refresh", language)
        assert not picker.refresh_button.icon().isNull()
        assert_inside(picker, window)
        assert_inside(picker.recipes, picker)
        assert_inside(picker.refresh_button, picker)
        layout = window.identity_group.layout()
        assert layout is not None and layout.count() == 2
        assert window.identity_heading.geometry().bottom() < picker.geometry().top()
        center = window.summary_header.itemAtPosition(0, 1)
        assert center is not None and center.widget() is window.identity_group


@pytest.mark.parametrize("width", [1080, 1920])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_compact_selector_elides_paint_without_changing_identity(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    theme: str,
) -> None:
    window, _session, _runtimes = configured_screen
    window.navigate(0)
    window.resize(width, 1017)
    window.set_language("en")
    window.set_theme("light" if theme == "light" else "dark")
    app = window.bridge.application
    prepared = app.snapshot().candidate_prepared
    assert prepared is not None
    name = "Long Recipe / 很长的配方名称 " * 8
    app.configure(
        replace(
            prepared, configuration=replace(prepared.configuration, display_name=name)
        )
    )
    window.bridge.refresh()
    before = app.snapshot()
    combo = window.production_recipes.recipes
    painted: list[str] = []
    original = QStylePainter.drawControl

    def draw_control(
        painter: QStylePainter, element: QStyle.ControlElement, option: QStyleOption
    ) -> None:
        if painter.device() is combo and isinstance(option, QStyleOptionComboBox):
            painted.append(option.currentText)
        original(painter, element, option)

    monkeypatch.setattr(QStylePainter, "drawControl", draw_control)
    QApplication.processEvents()
    combo.repaint()
    assert painted and painted[-1].endswith("…")
    assert combo.currentText() == combo.itemText(combo.currentIndex()) == name
    assert combo.currentData() == prepared.configuration.recipe_id
    assert name in combo.toolTip() and name in combo.accessibleName()
    assert app.snapshot() == before and not window.editing.busy

    # A compact placeholder is painted without inventing a selection.
    window.bridge.snapshot_changed.emit(
        replace(before, candidate=None, candidate_prepared=None)
    )
    combo.repaint()
    assert combo.currentIndex() == -1 and combo.currentText() == ""
    assert painted[-1] and combo.placeholderText().startswith(
        painted[-1].removesuffix("…")
    )
    assert app.snapshot() == before


def test_save_and_selection_keep_editor_candidate_and_preferences_independent(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_recipe(data.recipes / "other.json", RecipeDefinition("other", "Other"))
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        session = startup.editor
        assert session is not None and session.entry is not None
        initial = startup.application.snapshot()
        assert initial.candidate_prepared is not None
        # Save requires neither valid setup drafts nor a registered model.
        session.machine_editor.remove_camera_six()
        assert session.camera_sources is not None
        session.camera_sources.set_loop(not session.camera_sources.definition.loop)
        session.draft.display_name = "Saved latest"
        window.recipe_page.save()
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Saved latest"
        assert startup.application.snapshot() == initial
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        session.draft.display_name = "Keep unsaved"
        draft = session.draft.to_recipe()
        entry = session.entry
        window.production_recipes.select(
            window.production_recipes.recipes.findData("local")
        )
        until(lambda: not window.editing.busy)
        selected = startup.application.snapshot()
        assert selected.candidate_prepared is not None
        assert selected.candidate_prepared.configuration.display_name == "Saved latest"
        assert (
            selected.candidate_prepared.configuration.layout
            == initial.candidate_prepared.configuration.layout
        )
        assert selected.active is None and selected.counters == initial.counters
        assert session.entry is entry and session.draft.to_recipe() == draft
        assert session.machine_editor.dirty and session.camera_sources.dirty
        # An incomplete saved Recipe can be listed, but cannot replace the candidate.
        window.production_recipes.select(
            window.production_recipes.recipes.findData("other")
        )
        until(lambda: not window.editing.busy)
        assert startup.application.snapshot() == selected
        assert window.production_recipes.feedback is not None
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
    finally:
        assert startup.editor is not None
        startup.editor.discard()
        startup.editor.machine_editor.discard()
        assert startup.editor.camera_sources is not None
        startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()


def test_catalog_refresh_never_selects_or_prepares_and_same_id_reads_disk(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    picker = window.production_recipes
    before = window.bridge.application.snapshot()
    assert picker.recipes.currentData() == "product-a"
    entry = next(
        e
        for e in session.catalog.discover().entries
        if e.recipe.recipe_id == "product-a"
    )
    save_recipe(entry.path, replace(entry.recipe, display_name="Externally updated"))
    save_recipe(session.catalog.directory / "new.json", RecipeDefinition("new", "New"))
    (session.catalog.directory / "broken.json").write_text("{", encoding="utf-8")
    window.navigate(0)
    assert (
        picker.recipes.findData("new") >= 0
        and picker.recipes.currentData() == "product-a"
    )
    assert window.bridge.application.snapshot() == before
    assert "broken.json" in window.event_label.toolTip()
    picker.select(picker.recipes.findData("product-a"))
    until(lambda: not window.editing.busy)
    first = window.bridge.application.snapshot()
    assert first.candidate_prepared is not None
    assert first.candidate_prepared.configuration.display_name == "Externally updated"
    save_recipe(entry.path, replace(entry.recipe, display_name="Updated again"))
    picker.refresh_button.click()
    assert window.bridge.application.snapshot() == first
    picker.recipes.activated.emit(picker.recipes.findData("product-a"))
    until(lambda: not window.editing.busy)
    second = window.bridge.application.snapshot()
    assert second.candidate_prepared is not None
    assert second.candidate_prepared.configuration.display_name == "Updated again"
    assert session.entry is not None and session.entry.recipe == entry.recipe
    entry.path.unlink()
    picker.reload()
    assert picker.recipes.currentData() == "product-a"
    assert picker.recipes.currentText() == "Updated again"
    model = picker.recipes.model()
    assert (
        not model.flags(model.index(picker.recipes.currentIndex(), 0))
        & Qt.ItemFlag.ItemIsEnabled
    )
    assert window.bridge.application.snapshot() == second
    # Restore the test-owned file so the fixture can reload its original editor.
    save_recipe(entry.path, entry.recipe)


@pytest.mark.parametrize("completion", ["success", "failure", "obsolete", "close"])
def test_async_selection_is_bounded_and_stale_results_cannot_install(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, completion: str
) -> None:
    window, _session, runtimes = configured_screen
    window.navigate(0)
    picker = window.production_recipes
    before = window.bridge.application.snapshot()
    entered, release = Event(), Event()
    workers: list[int] = []
    original = RecipeEditorSession.prepare_saved

    def held(owner: RecipeEditorSession, identity: str) -> PreparedConfiguration:
        workers.append(get_ident())
        entered.set()
        assert release.wait(5)
        if completion == "failure":
            raise ValueError("Preparation failed")
        return original(owner, identity)

    monkeypatch.setattr(RecipeEditorSession, "prepare_saved", held)
    if completion == "obsolete":
        run = window.editing.run_model_io

        def change_before_delivery(
            work: Callable[[], object],
            completed: Callable[[object, BaseException | None], None],
        ) -> bool:
            def delivered(value: object, error: BaseException | None) -> None:
                assert before.candidate_prepared is not None
                window.bridge.application.configure(replace(before.candidate_prepared))
                completed(value, error)

            return run(work, delivered)

        monkeypatch.setattr(window.editing, "run_model_io", change_before_delivery)
    target = picker.recipes.findData("product-b")
    requested_name = picker.recipes.itemText(target)
    picker.recipes.setCurrentIndex(target)
    picker.recipes.activated.emit(target)
    try:
        until(entered.is_set)
        assert workers != [get_ident()]
        assert window.editing.busy and not picker.recipes.isEnabled()
        assert not picker.refresh_button.isEnabled()
        assert not window.buttons["operation"].isEnabled()
        assert picker.recipes.currentData() == "product-a"
        assert window.identity_label.isVisible()
        assert (
            text("recipe_preparing_short", window.language)
            in window.identity_label.text()
        )
        assert requested_name in picker.recipes.toolTip()
        assert requested_name in window.identity_label.toolTip()
        picker.select(picker.recipes.findData("product-a"))
        assert len(workers) == 1 and not runtimes
        if completion == "close":
            window.editing.drain(lambda: None)
    finally:
        release.set()
    until(lambda: not window.editing.busy)
    after = window.bridge.application.snapshot()
    assert after.active is None and after.counters == before.counters
    if completion == "success":
        assert after.candidate is not None and after.candidate.recipe_id == "product-b"
        assert picker.recipes.currentData() == "product-b"
    else:
        assert after.candidate == before.candidate and picker.feedback is not None
        assert picker.recipes.currentData() == "product-a"
        assert requested_name in picker.feedback[1]
    assert not window.identity_label.isVisible()
    window.editing.resume()


def test_start_alone_activates_and_running_selection_is_rejected(
    configured_screen: Screen,
) -> None:
    window, session, runtimes = configured_screen
    picker = window.production_recipes
    picker.select(picker.recipes.findData("product-b"))
    until(lambda: not window.editing.busy)
    app = window.bridge.application
    assert app.snapshot().active is None
    window.bridge.start()
    until(lambda: bool(runtimes))
    window.bridge.refresh()
    assert not picker.recipes.isEnabled()
    picker.select(picker.recipes.findData("product-a"))
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    window.bridge.refresh()
    active = app.snapshot().active
    assert active is not None and active.recipe_id == "product-b"
    picker.reload()  # Reading the catalog while running cannot change configuration.
    picker.select(picker.recipes.findData("product-a"))
    assert not window.editing.busy and app.snapshot().candidate == app.snapshot().active
    assert session.entry is not None and session.entry.recipe.recipe_id == "product-a"
    window.bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
