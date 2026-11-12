"""Saved Recipe evidence and stopped refresh admission, without Qt or hardware."""

import json
from dataclasses import replace
from pathlib import Path
from threading import get_ident

import pytest
from tests.support.application import WAIT, FakeSession, close
from tests.support.recipe import editor
from tests.support.startup import saved_data

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
    ProductionApplication,
)
from capsule_vision.application.identity_models import StaleSessionError
from capsule_vision.application.recipe_dependencies import RecipeDependencies
from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import save_recipe


@pytest.mark.parametrize("change", ["changed", "corrupt", "missing", "unreadable"])
def test_start_verifies_restored_saved_recipe_before_session_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    candidate = startup.prepared
    assert candidate is not None and candidate.recipe_dependencies is not None
    close(startup.application)
    calls: list[PreparedConfiguration] = []

    def factory(value: PreparedConfiguration, _generation: int) -> FakeSession:
        calls.append(value)
        return FakeSession()

    threads: list[int] = []
    validate = RecipeDependencies.validate_current

    def checked(value: RecipeDependencies) -> None:
        threads.append(get_ident())
        validate(value)

    monkeypatch.setattr(RecipeDependencies, "validate_current", checked)
    entry = candidate.recipe_dependencies.entry
    if change == "changed":
        save_recipe(entry.path, replace(entry.recipe, display_name="External"))
    elif change == "corrupt":
        entry.path.write_text("{", encoding="utf8")
    elif change == "missing":
        entry.path.unlink()
    else:
        read = Path.read_text

        def denied(path: Path, *args: object, **kwargs: object) -> str:
            if path == entry.path:
                raise PermissionError("isolated unavailable Recipe")
            return read(path, encoding="utf8")

        monkeypatch.setattr(Path, "read_text", denied)
    app = ProductionApplication(factory, initial_candidate=candidate)
    try:
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert not calls
        state = app.snapshot()
        assert state.state is ApplicationState.READY and not state.runtime_owned
        assert state.candidate_prepared is candidate and state.active is None
        assert state.candidate_requires_preparation
        assert (
            state.last_issue
            and state.last_issue.exception_type == "RecipePreparationRequired"
        )
        assert threads and get_ident() not in threads
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
    finally:
        close(app)


def test_parsed_recipe_equality_and_failed_start_recovery(tmp_path: Path) -> None:
    session = editor(tmp_path)
    candidate = session.prepare_saved("product-a")
    assert session.entry is not None
    path = session.entry.path
    path.write_text(
        json.dumps(json.loads(path.read_text(encoding="utf8")), indent=4),
        encoding="utf8",
    )
    installed: list[PreparedConfiguration] = []

    def factory(value: PreparedConfiguration, _generation: int) -> FakeSession:
        installed.append(value)
        return FakeSession()

    app = ProductionApplication(factory, initial_candidate=candidate)
    try:
        app.start().result(WAIT)
        save_recipe(path, replace(session.entry.recipe, display_name="External"))
        assert app.snapshot().active_prepared is candidate  # Active is immutable.
        app.stop().result(WAIT)
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert installed == [candidate]
        request = app.begin_recipe_selection("product-a")
        refreshed = session.prepare_saved("product-a")
        app.install_recipe_selection(request, refreshed)
        assert not app.snapshot().candidate_requires_preparation
        assert app.snapshot().last_issue is None
        app.start().result(WAIT)
        assert installed == [candidate, refreshed]
        assert app.snapshot().active_prepared is refreshed
    finally:
        close(app)


def test_save_observation_blocks_start_before_release_and_supersedes_refresh(
    tmp_path: Path,
) -> None:
    session = editor(tmp_path)
    candidate = session.prepare_saved("product-a")
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), initial_candidate=candidate
    )
    try:
        assert session.save() is None  # Unchanged Save supplies no refresh evidence.
        session.draft.display_name = "First saved"
        with app.model_management():
            saved = session.save()
            assert saved is not None
            first = app.observe_saved_recipe(saved.recipe, None)
            assert first is not None and first.operation is None and first.automatic
            with pytest.raises(ApplicationStateError):
                app.start()
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
        session.draft.display_name = "Latest saved"
        saved = session.save()
        assert saved is not None
        second = app.observe_saved_recipe(saved.recipe, None)
        assert second is not None
        with pytest.raises(StaleSessionError):
            with app.recipe_preparation(first):
                pytest.fail("Superseded read admitted")
        with pytest.raises(StaleSessionError):
            app.install_recipe_selection(first, candidate)
        with app.recipe_preparation(second):
            refreshed = session.prepare_saved("product-a")
        app.install_recipe_selection(second, refreshed)
        assert not app.snapshot().candidate_requires_preparation
        assert app.snapshot().candidate_prepared is refreshed
        assert (
            app.observe_saved_recipe(replace(saved.recipe, recipe_id="other"), None)
            is None
        )
        assert app.snapshot().candidate_prepared is refreshed
        app.close().result(WAIT)
        with pytest.raises(ApplicationStateError):
            app.install_recipe_selection(second, refreshed)
    finally:
        close(app)
