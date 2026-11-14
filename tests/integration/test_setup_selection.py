"""A saved selection is valid before its production dependencies are complete."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_process_preview import saved_target
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application import ApplicationState, ApplicationStateError
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import load_recipe, save_recipe
from capsule_vision.configuration.preferences import load_preferences
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


@pytest.mark.parametrize("missing", ["machine", "model", "model_definition"])
def test_valid_saved_selection_survives_incomplete_setup(
    qapp: QApplication, tmp_path: Path, missing: str
) -> None:
    data = saved_data(tmp_path)
    data.preferences.unlink()
    path = data.recipes / "local.json"
    if missing == "machine":
        data.machine.unlink()
    elif missing == "model_definition":
        (data.models / "general").rename(tmp_path / "unused-model-fixture")
    else:
        save_recipe(path, replace(load_recipe(path), model_id=None))
    startup = authenticated_startup(data)
    identity = startup.application.identity
    assert identity is not None
    provision_account(identity, "Operator", Role.OPERATOR, TEST_PASSWORD)
    identity.login("Operator", TEST_PASSWORD).result(5)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert not any(i.key == "startup_recipe_identity" for i in startup.issues)
        picker = window.production_recipes
        assert picker.recipes.currentIndex() == -1
        picker.select(picker.recipes.findData("local"))
        until(lambda: not window.editing.busy)
        assert picker.recipes.currentData() == "local"
        snapshot = startup.application.snapshot()
        assert snapshot.state is ApplicationState.UNCONFIGURED
        assert snapshot.candidate is None and snapshot.active is None
        assert not snapshot.process_required and not snapshot.process_applying
        assert picker.feedback is not None
        assert picker.feedback[0] == "configure_first"
        assert not window.buttons["operation"].isEnabled()
        with pytest.raises(ApplicationStateError):
            startup.application.start()
        for page in (2, 3, 4, 0):
            window.navigate(page)
            QApplication.processEvents()
            assert window.pages.currentIndex() == page
            assert picker.recipes.currentData() == "local"
            if page != 0:
                assert (
                    text("configure_first", window.language)
                    not in window.event_label.accessibleName()
                )
                assert (
                    text("camera_setup", window.language)
                    not in window.event_label.accessibleName()
                )
        assert load_preferences(data.preferences).last_applied_recipe_id is None
    finally:
        assert startup.editor is not None
        startup.editor.machine_editor.discard()
        if startup.editor.camera_sources is not None:
            startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()
    restarted = authenticated_startup(data)
    try:
        assert restarted.application.snapshot().selected_recipe is None
        assert restarted.prepared is None
    finally:
        close(restarted)


@pytest.mark.parametrize("missing", ["machine", "model", "sources"])
def test_setup_completion_requires_reselection_before_process_application(
    qapp: QApplication, tmp_path: Path, missing: str
) -> None:
    data = saved_data(tmp_path)
    original = load_recipe(data.recipes / "local.json")
    other = replace(
        original,
        recipe_id="other",
        display_name="Setup Recipe",
        process_parameters=(("running_speed", 73),),
    )
    target = data.recipes / "other.json"
    save_recipe(target, replace(other, model_id=None) if missing == "model" else other)
    absent = data.machine if missing == "machine" else data.sources
    content = absent.read_bytes()
    if missing != "model":
        absent.unlink()
    with TcpPeer() as plc:
        saved_target(data.root, plc.port)
        startup = authenticated_startup(
            data,
            engine_factory=lambda _: pytest.fail("selection must not create an engine"),
        )
        app = startup.application
        before = app.snapshot()
        preferences = data.preferences.read_bytes()
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        try:
            picker = window.production_recipes
            picker.select(picker.recipes.findData("other"))
            until(lambda: not window.editing.busy)
            incomplete = app.snapshot()
            assert (
                incomplete.selected_recipe
                and incomplete.selected_recipe.recipe_id == "other"
            )
            assert incomplete.state is ApplicationState.UNCONFIGURED
            assert incomplete.candidate_prepared is before.candidate_prepared
            assert not plc.packets and not plc.connections
            with pytest.raises(ApplicationStateError):
                app.start()
            assert data.preferences.read_bytes() == preferences
            # Neither saving the selected incomplete Recipe nor the old candidate
            # is an implicit selection or permission to write process values.
            save_recipe(target, other)
            assert app.observe_saved_recipe(other, app.session_revision) is None
            assert app.observe_saved_recipe(original, app.session_revision) is None
            if missing != "model":
                absent.write_bytes(content)
            QApplication.processEvents()
            assert app.snapshot() == incomplete and not plc.packets
            picker.select(picker.recipes.findData("other"))
            until(lambda: not window.editing.busy)
            ready = app.snapshot()
            assert ready.state is ApplicationState.READY
            assert ready.selected_recipe and ready.selected_recipe.recipe_id == "other"
            assert ready.candidate and ready.candidate.recipe_id == "other"
            assert (
                not ready.candidate_requires_preparation and not ready.process_required
            )
            assert ready.active is None and not ready.runtime_owned
            assert plc.words(102, 2, 0x82) == (73, 0)
            assert len(plc.packets) == 3  # Running read, DINT write, matching readback.
            assert load_preferences(data.preferences).last_applied_recipe_id == "other"
        finally:
            close_window(window)
            delivery.detach()
        restored = load_startup(data)
        try:
            assert (
                restored.application.snapshot().selected_recipe == ready.selected_recipe
            )
            assert (
                restored.application.snapshot().process_required
            )  # Guest never writes.
            assert len(plc.packets) == 3
        finally:
            close(restored)


@pytest.mark.parametrize("invalid", ["missing", "malformed", "ambiguous"])
def test_invalid_saved_selection_never_replaces_valid_identity(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    data = saved_data(tmp_path)
    target = data.recipes / "other.json"
    save_recipe(
        target, replace(load_recipe(data.recipes / "local.json"), recipe_id="other")
    )
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        before = startup.application.snapshot()
        if invalid == "missing":
            target.unlink()
        elif invalid == "malformed":
            target.write_text("{", encoding="utf-8")
        else:
            iterdir = Path.iterdir

            def ambiguous(path: Path):
                values = tuple(iterdir(path))
                return iter(
                    (*values, path / "OTHER.json") if path == data.recipes else values
                )

            monkeypatch.setattr(Path, "iterdir", ambiguous)
        picker = window.production_recipes
        picker.select(
            picker.recipes.findData("other")
        )  # Catalog became stale after display.
        until(lambda: not window.editing.busy)
        assert startup.application.snapshot() == before
        assert picker.recipes.currentData() == "local"
        assert picker.feedback and picker.feedback[0] == "recipe_prepare_error"
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("transition", ["supersede", "logout", "close"])
def test_selection_acceptance_cannot_outlive_its_admission(
    tmp_path: Path, transition: str
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app = startup.application
    assert startup.editor and app.identity
    recipe = startup.editor.read_saved("local").recipe
    try:
        request = app.begin_recipe_selection("local")
        before = app.snapshot()
        # Pending validation cannot start the old candidate either.
        with pytest.raises(ApplicationStateError):
            app.start()
        if transition == "supersede":
            newer = app.begin_recipe_selection("local")
            with pytest.raises(StaleSessionError):
                app.accept_recipe_selection(request, recipe)
            app.recipe_selection_failed(request, RuntimeError("late failure"))
            app.accept_recipe_selection(newer, recipe)
            assert app.snapshot().state is ApplicationState.UNCONFIGURED
        elif transition == "logout":
            app.identity.logout()
            with pytest.raises((StaleSessionError, AuthorizationError)):
                app.accept_recipe_selection(request, recipe)
            assert app.snapshot() == before
        else:
            app.close().result(5)
            with pytest.raises(ApplicationStateError):
                app.accept_recipe_selection(request, recipe)
            assert app.snapshot().selected_recipe == before.selected_recipe
    finally:
        close(startup)
