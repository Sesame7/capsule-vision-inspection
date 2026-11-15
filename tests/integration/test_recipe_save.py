"""Recipe publication, stopped invalidation and explicit Production reselection."""

from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ReplayEngine
from tests.support.qt import close_window, until
from tests.support.recipe_ui import Screen
from tests.support.recipe_ui import configured_screen as configured_screen
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)

from capsule_vision.application import (
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
)
from capsule_vision.application.application import RecipeSelection
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.identity_models import Role
from capsule_vision.configuration import load_recipe, save_machine_layout
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_changed_selected_save_blocks_start_until_explicit_reselection(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Save and isolated replay must not contact a PLC")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.sendto", forbidden)
    data = saved_data(tmp_path)
    engines: list[PreparedConfiguration] = []

    def factory(value: PreparedConfiguration) -> ReplayEngine:
        engines.append(value)
        return ReplayEngine()

    startup = authenticated_startup(data, engine_factory=factory)
    app, session = startup.application, startup.editor
    assert session is not None and session.entry is not None
    owner = app.identity
    assert owner is not None
    author = provision_account(owner, "Engineer", Role.ENGINEER, TEST_PASSWORD)
    owner.login(author.username, TEST_PASSWORD).result(WAIT)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        app.start().result(WAIT)
        app.stop().result(WAIT)
        before = app.snapshot()
        assert before.active_prepared is before.candidate_prepared
        assert engines == [before.active_prepared]
        preferences = data.preferences.read_bytes()
        applied = QSignalSpy(window.editing.applied)
        calls: list[tuple[str, int]] = []
        prepare = RecipeEditorSession.prepare_saved

        def counted(
            editor: RecipeEditorSession, identity: str
        ) -> PreparedConfiguration:
            calls.append((identity, get_ident()))
            return prepare(editor, identity)

        monkeypatch.setattr(RecipeEditorSession, "prepare_saved", counted)
        session.draft.display_name = "Latest saved"
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        saved = app.snapshot()
        assert saved == replace(before, candidate_requires_preparation=True)
        assert saved.candidate_prepared is before.candidate_prepared
        assert saved.active_prepared is before.active_prepared
        assert not calls and applied.count() == 0
        assert data.preferences.read_bytes() == preferences
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Latest saved"
        assert window.recipe_page.status.text() == text("saved", window.language)
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
        assert engines == [before.active_prepared]
        (save,) = owner.audit_page(AuditQuery(action="recipe.save")).result(WAIT)
        assert (
            save.outcome == "success" and save.operation.actor_id == author.account_id
        )
        assert not owner.audit_page(AuditQuery(action="production.recipe")).result(WAIT)

        window.navigate(0)
        picker = window.production_recipes
        assert before.selected_recipe is not None
        assert picker.recipes.currentData() == before.selected_recipe.recipe_id
        assert picker.recipes.currentText() == before.selected_recipe.display_name
        hint = text("recipe_reprepare_hint", window.language)
        assert (
            hint in picker.recipes.toolTip() and hint in picker.recipes.accessibleName()
        )
        assert hint in window.buttons["operation"].toolTip()
        assert not window.buttons["operation"].isEnabled()
        assert window.state_label.text() == text("report_stopped", window.language)
        assert window.event_frame.state.text() == window.state_label.text()
        assert window.event_label.accessibleName() == hint
        assert not calls  # Opening Production and refreshing the list did not select.

        picker.recipes.activated.emit(picker.recipes.currentIndex())
        until(lambda: not window.editing.busy)
        assert len(calls) == 1 and calls[0][0] == "local" and calls[0][1] != get_ident()
        assert applied.count() == 1
        ready = app.snapshot()
        candidate = ready.candidate_prepared
        assert candidate is not None and candidate is not before.candidate_prepared
        assert candidate.configuration.display_name == "Latest saved"
        assert ready.active_prepared is before.active_prepared
        assert (
            not ready.candidate_requires_preparation
            and window.buttons["operation"].isEnabled()
        )
        assert picker.recipes.currentText() == "Latest saved"
        assert window.recipe_page.status.text() == text("saved", window.language)
        app.start().result(WAIT)
        assert engines == [before.active_prepared, candidate]
        assert app.snapshot().active_prepared is candidate
        app.stop().result(WAIT)
    finally:
        close_window(window)
        delivery.detach()


def test_logout_after_committed_save_preserves_publication_and_stale_candidate(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, session = startup.application, startup.editor
    assert session is not None and session.entry is not None
    owner = app.identity
    assert owner is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        initial = app.snapshot()
        session.draft.display_name = "Committed before logout"
        applied = QSignalSpy(window.editing.applied)

        def logout() -> None:
            assert text("saved", window.language) in window.recipe_page.status.text()
            owner.logout()

        window.recipe_page.save(logout)
        until(lambda: not window.editing.busy)
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Committed before logout"
        assert app.snapshot() == replace(initial, candidate_requires_preparation=True)
        assert applied.count() == 0
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        picker = window.production_recipes
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        assert not app.snapshot().candidate_requires_preparation
        assert app.snapshot().candidate_prepared is not initial.candidate_prepared
        assert applied.count() == 1
    finally:
        close_window(window)
        delivery.detach()


def test_unrelated_and_unchanged_saves_do_not_prepare(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    before = window.bridge.application.snapshot()

    def forbidden(_owner: RecipeEditorSession, _identity: str) -> PreparedConfiguration:
        pytest.fail("Unrelated or unchanged Save must not prepare")

    monkeypatch.setattr(RecipeEditorSession, "prepare_saved", forbidden)
    window.recipe_page.save()
    assert not window.editing.busy
    assert window.bridge.application.snapshot() == before
    session.load(
        next(
            e
            for e in session.catalog.discover().entries
            if e.recipe.recipe_id == "product-b"
        )
    )
    session.draft.display_name = "Unrelated saved"
    window.recipe_page.save()
    until(lambda: not window.editing.busy)
    assert session.entry is not None
    assert load_recipe(session.entry.path).display_name == "Unrelated saved"
    assert window.bridge.application.snapshot() == before
    assert window.buttons["operation"].isEnabled()


def test_coordinator_invalidates_after_publication_before_releasing_admission(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, session = startup.application, startup.editor
    assert session is not None and session.entry is not None
    owner = app.identity
    assert owner is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    entered, release = Event(), Event()
    observe = app.observe_saved_recipe

    def held(recipe_id: str) -> None:
        observe(recipe_id)
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(app, "observe_saved_recipe", held)
    outcomes: list[BaseException | None] = []
    try:
        before = app.snapshot()
        revision = app.session_revision
        session.draft.display_name = "Saved before reauthentication"

        def completed(_value: object, error: BaseException | None) -> None:
            outcomes.append(error)

        assert window.editing.save_recipe(completed, revision=None)
        until(entered.is_set)
        assert (
            load_recipe(session.entry.path).display_name
            == "Saved before reauthentication"
        )
        assert app.snapshot() == replace(before, candidate_requires_preparation=True)
        assert window.editing.busy and not session.dirty and not outcomes
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        assert app.session_revision != revision
        with pytest.raises(ApplicationStateError, match="Command unavailable"):
            app.start()
        release.set()
        until(lambda: not window.editing.busy)
        assert outcomes == [None]
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
        (save,) = owner.audit_page(AuditQuery(action="recipe.save")).result(WAIT)
        assert save.outcome == "success"
    finally:
        release.set()
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("failure", ["model", "unknown_model", "vision", "prepare"])
def test_failed_reselection_keeps_saved_definition_and_blocks_start(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    window, session, runtimes = configured_screen
    app = window.bridge.application
    before = app.snapshot()
    assert session.entry is not None
    if failure == "model":
        Path(session.registry.get("general").model_path).write_bytes(b"bad artifact")
    elif failure == "unknown_model":
        session.draft.model_id = "missing-model"
    elif failure == "vision":
        assert session.machine_path is not None
        session.machine_path.write_text("broken", encoding="utf8")
    else:

        def fail(_owner: RecipeEditorSession, _identity: str) -> PreparedConfiguration:
            raise ValueError("isolated preparation failure")

        monkeypatch.setattr(RecipeEditorSession, "prepare_saved", fail)
    session.draft.display_name = "Saved independently"
    window.recipe_page.save()
    until(lambda: not window.editing.busy)
    try:
        assert app.snapshot() == replace(before, candidate_requires_preparation=True)
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Saved independently"
        assert window.recipe_page.status.text() == text("saved", window.language)
        window.navigate(0)
        picker = window.production_recipes
        assert picker.recipes.currentData() == "product-a"
        assert not window.buttons["operation"].isEnabled()
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()

        picker.recipes.activated.emit(picker.recipes.currentIndex())
        until(lambda: not window.editing.busy)
        state = app.snapshot()
        assert state.candidate_prepared is before.candidate_prepared
        assert (
            state.active is None
            and state.candidate_requires_preparation
            and not runtimes
        )
        assert state.state is ApplicationState.UNCONFIGURED
        assert picker.recipes.currentText() == "Saved independently"
        key = (
            "recipe_setup_required"
            if failure == "unknown_model"
            else "recipe_prepare_error"
        )
        assert picker.feedback and picker.feedback[0] == key
        assert text(key, window.language) in picker.recipes.toolTip()
        assert window.recipe_page.status.text() == text("saved", window.language)
        assert not window.buttons["operation"].isEnabled()
        with pytest.raises(ApplicationStateError):
            app.start()
    finally:
        if failure == "vision":
            assert session.machine_path is not None
            save_machine_layout(session.machine_path, session.machine)


def test_close_waits_for_committed_recipe_save(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    app = window.bridge.application
    before = app.snapshot()
    committed, release = Event(), Event()
    observe = app.observe_saved_recipe
    applied = QSignalSpy(window.editing.applied)

    def held(recipe_id: str) -> None:
        observe(recipe_id)
        committed.set()
        assert release.wait(10)

    monkeypatch.setattr(app, "observe_saved_recipe", held)
    outcomes: list[BaseException | None] = []
    session.draft.display_name = "Committed before Close"
    assert window.editing.save_recipe(
        lambda _, error: outcomes.append(error), revision=None
    )
    try:
        until(committed.is_set)
        window.close()
        assert window.editing.closing and window.editing.busy
        assert window.isVisible() and not window.bridge.closed and not outcomes
        assert app.snapshot() == replace(before, candidate_requires_preparation=True)
    finally:
        release.set()
    until(lambda: window.bridge.closed)
    assert outcomes == [None] and applied.count() == 0
    assert (
        session.entry
        and load_recipe(session.entry.path).display_name == "Committed before Close"
    )


def test_reselection_cannot_replace_newer_candidate(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    app = window.bridge.application
    other = session.production_preparation().prepare_saved("product-b")
    # Replace at the queued installation boundary, after the preparation slot releases.
    install = app.install_recipe_selection

    def install_newer(
        request: RecipeSelection, candidate: PreparedConfiguration
    ) -> None:
        app.configure(other)
        install(request, candidate)

    monkeypatch.setattr(app, "install_recipe_selection", install_newer)
    session.draft.display_name = "Saved"
    window.recipe_page.save()
    until(lambda: not window.editing.busy)
    assert app.snapshot().candidate_requires_preparation
    picker = window.production_recipes
    picker.recipes.activated.emit(picker.recipes.currentIndex())
    until(lambda: not window.editing.busy)
    assert app.snapshot().candidate_prepared is other
    assert not app.snapshot().candidate_requires_preparation
    assert picker.feedback and picker.feedback[0] == "identity_stale"
