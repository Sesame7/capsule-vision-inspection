"""Committed Recipe Save, bounded automatic preparation and queued UI feedback."""

from pathlib import Path
from threading import Event, get_ident

import pytest
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

from capsule_vision.application import ApplicationStateError, PreparedConfiguration
from capsule_vision.application.application import RecipeSelection
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.identity_models import Role
from capsule_vision.configuration import load_recipe, save_machine_layout
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_changed_selected_save_refreshes_next_start_without_selection_audit(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Save/refresh and isolated replay must not contact a PLC")

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
        before = app.snapshot()
        session.draft.display_name = "Latest saved"
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        refreshed = app.snapshot()
        candidate = refreshed.candidate_prepared
        assert candidate is not None and candidate is not before.candidate_prepared
        assert candidate.configuration.display_name == "Latest saved"
        assert (
            refreshed.active == before.active and refreshed.counters == before.counters
        )
        assert not refreshed.candidate_requires_preparation and not engines
        assert not session.dirty
        assert window.production_recipes.recipes.currentData() == "local"
        assert window.production_recipes.recipes.currentText() == "Latest saved"
        assert window.recipe_page.status.text() == " · ".join(
            text(key, window.language) for key in ("saved", "READY")
        )
        assert len(owner.audit_page(AuditQuery(action="recipe.save")).result(WAIT)) == 1
        assert not owner.audit_page(AuditQuery(action="production.recipe")).result(WAIT)
        app.start().result(WAIT)
        assert engines == [candidate]
        assert app.snapshot().active_prepared is candidate
        app.stop().result(WAIT)
    finally:
        close_window(window)
        delivery.detach()


def test_logout_between_committed_save_and_refresh_does_not_undo_save(
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

        def logout() -> None:
            assert text("saved", window.language) in window.recipe_page.status.text()
            owner.logout()

        window.recipe_page.save(logout)
        until(lambda: not window.editing.busy)
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Committed before logout"
        assert app.snapshot().candidate_prepared is initial.candidate_prepared
        assert app.snapshot().candidate_requires_preparation
        # Identity changes legitimately reload the read-only editor's feedback.
        assert window.recipe_page.status.text() != text(
            "recipe_save_error", window.language
        )
        assert (
            window.editing.recipe_refresh
            and window.editing.recipe_refresh[2] is not None
        )
        owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        picker = window.production_recipes
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        assert not app.snapshot().candidate_requires_preparation
        assert app.snapshot().candidate_prepared is not initial.candidate_prepared
        assert window.editing.recipe_refresh is None
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
    assert window.bridge.application.snapshot() == before
    assert window.editing.recipe_refresh is None


def test_coordinator_captures_current_revision_before_save(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, session = startup.application, startup.editor
    assert session is not None
    owner = app.identity
    assert owner is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        before = app.snapshot()
        session.draft.display_name = "Saved before reauthentication"

        def completed(_value: object, error: BaseException | None) -> None:
            assert error is None
            owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)

        assert window.editing.save_recipe(completed, revision=None)
        until(lambda: not window.editing.busy)
        assert not session.dirty
        assert app.snapshot().candidate_requires_preparation
        assert app.snapshot().candidate_prepared is before.candidate_prepared
        assert window.editing.recipe_refresh and window.editing.recipe_refresh[2]
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("failure", ["model", "unknown_model", "vision", "prepare"])
def test_saved_recipe_failure_keeps_evidence_and_blocks_start(
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
        state = app.snapshot()
        assert (
            state.candidate_prepared is before.candidate_prepared
            and state.active is None
        )
        assert state.candidate_requires_preparation and not runtimes, (
            window.recipe_page.status.text(),
            window.recipe_page.status.toolTip(),
            session.dirty,
        )
        assert not session.dirty
        assert load_recipe(session.entry.path).display_name == "Saved independently"
        assert text("saved", window.language) in window.recipe_page.status.text()
        assert (
            text("recipe_prepare_error", window.language)
            in window.recipe_page.status.text()
        )
        assert window.recipe_page.status.toolTip()
        assert window.production_recipes.recipes.currentData() == "product-a"
        assert window.production_recipes.feedback is not None
        window.navigate(0)
        assert window.production_recipes.feedback is not None
        assert window.production_recipes.feedback[0] == "recipe_prepare_error"
        assert (
            text("recipe_prepare_error", window.language)
            in window.production_recipes.recipes.toolTip()
        )
        window.bridge.refresh()
        assert not window.buttons["operation"].isEnabled()
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
    finally:
        if failure == "vision":
            assert session.machine_path is not None
            save_machine_layout(session.machine_path, session.machine)


@pytest.mark.parametrize(
    "transition", ["logout", "switch", "demote", "disable", "close"]
)
def test_refresh_late_completion_cannot_outlive_saving_session(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, session = startup.application, startup.editor
    assert session is not None
    owner = app.identity
    assert owner is not None
    author = provision_account(owner, "Author", Role.ENGINEER, TEST_PASSWORD)
    owner.login(author.username, TEST_PASSWORD).result(WAIT)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    entered, release = Event(), Event()
    workers: list[int] = []
    prepare = RecipeEditorSession.prepare_saved

    def held(editor: RecipeEditorSession, identity: str) -> PreparedConfiguration:
        workers.append(get_ident())
        entered.set()
        assert release.wait(10)
        return prepare(editor, identity)

    monkeypatch.setattr(RecipeEditorSession, "prepare_saved", held)
    before = app.snapshot()
    try:
        session.draft.display_name = "Saved while authenticated"
        window.recipe_page.save()
        until(entered.is_set)
        assert workers and get_ident() not in workers
        assert app.snapshot().candidate_requires_preparation
        assert window.editing.busy and not session.dirty
        assert text("preparing", window.language) in window.recipe_page.status.text()
        assert not window.recipe_page.identity.text()
        assert (
            window.production_recipes.feedback
            and window.production_recipes.feedback[0] == "preparing"
        )
        window.recipe_page.save()  # No second queued Save/preparation.
        assert len(workers) == 1
        if transition == "logout":
            owner.logout()
        elif transition == "close":
            window.close()
            until(lambda: window.editing.closing)
        else:
            owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
            if transition == "demote":
                owner.set_role(author.account_id, Role.OPERATOR).result(WAIT)
            elif transition == "disable":
                owner.set_enabled(author.account_id, False).result(WAIT)
        release.set()
        until(lambda: not window.editing.busy)
        assert app.snapshot().candidate_prepared is before.candidate_prepared
        assert app.snapshot().active is None
        assert app.snapshot().candidate_requires_preparation
        assert (
            window.editing.recipe_refresh
            and window.editing.recipe_refresh[2] is not None
        )
    finally:
        release.set()
        close_window(window)
        delivery.detach()


def test_automatic_refresh_cannot_replace_newer_candidate(
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
    assert app.snapshot().candidate_prepared is other
    assert not app.snapshot().candidate_requires_preparation
