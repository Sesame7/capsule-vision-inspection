"""Qt Vision persists independently; Production alone installs saved candidates."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from tests.support.qt import close_window, until
from tests.support.recipe_ui import select_production_recipe
from tests.support.startup import authenticated_startup, saved_data

from capsule_vision.application import ApplicationState
from capsule_vision.application.basler_connection import SourceConnectionSnapshot
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.startup import ApplicationData
from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    ConfigurationPublication,
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
)
from capsule_vision.configuration.preferences import load_preferences
from capsule_vision.inference import BoundingRect
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.vision_page import VisionPage


@pytest.fixture
def saved_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[MainWindow, VisionPage, ApplicationData]]:
    data = saved_data(tmp_path)

    def forbidden(_prepared: object) -> Never:
        raise AssertionError(
            "Vision Save/Production selection must not create an engine"
        )

    startup = authenticated_startup(data, engine_factory=forbidden)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.set_language("en")
    window.resize(1080, 1000)
    window.show()
    window.navigate(4)
    workspace = window.vision_workspace
    assert workspace is not None
    until(lambda: not window.editing.busy)
    try:
        yield window, workspace, data
    finally:
        if window.editing.session is not None:
            window.editing.session.discard()
            window.editing.session.machine_editor.discard()
            if window.editing.session.camera_sources is not None:
                window.editing.session.camera_sources.discard()
        close_window(window)
        delivery.detach()


def edit_geometry(workspace: VisionPage) -> None:
    rect = workspace.draft.camera(workspace.camera).slots[0].rect
    workspace.operation(
        lambda: workspace.set_rect(
            BoundingRect(rect.x, rect.y, rect.width - 1, rect.height)
        )
    )


def test_save_only_marks_candidate_stale_and_same_id_selection_recovers(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    app = window.bridge.application
    before = app.snapshot()
    preferences = data.preferences.read_bytes()
    applied = QSignalSpy(window.editing.applied)
    assert "save_apply" not in workspace.buttons
    assert not hasattr(workspace, "save_and_apply")
    assert workspace.buttons["save"].text() == text("save", window.language)
    assert workspace.sources is not None
    edit_geometry(workspace)
    workspace.sources.set_loop(not workspace.sources.saved.loop)
    # Save is independent of the Recipe editor and its unsaved input.
    workspace.session.draft.display_name = "Unsaved Recipe input"
    workspace.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert not workspace.vision_dirty and applied.count() == 0
    assert load_machine_layout(data.machine) == workspace.draft.definition
    assert load_camera_sources(data.sources) == workspace.sources.definition
    current = app.snapshot()
    assert current.candidate_prepared is before.candidate_prepared
    assert current.candidate_requires_preparation and current.active == before.active
    assert (
        current.counters == before.counters and current.state is ApplicationState.READY
    )
    assert data.preferences.read_bytes() == preferences
    assert not window.buttons["operation"].isEnabled()
    assert text("recipe_reprepare_hint", "en") in window.buttons["operation"].toolTip()
    window.navigate(0)
    assert before.candidate is not None
    assert window.production_recipes.recipes.currentData() == before.candidate.recipe_id
    assert (
        text("recipe_reprepare_hint", "en")
        in window.production_recipes.recipes.toolTip()
    )
    assert window.event_label.accessibleName() == text("recipe_reprepare", "en")
    select_production_recipe(window)
    until(lambda: not window.editing.busy)
    recovered = app.snapshot()
    assert recovered.candidate_prepared is not before.candidate_prepared
    assert not recovered.candidate_requires_preparation and applied.count() == 1
    assert recovered.candidate_prepared is not None
    assert (
        recovered.candidate_prepared.configuration.layout
        == workspace.draft.saved.layout
    )
    dependencies = recovered.candidate_prepared.vision_dependencies
    assert dependencies is not None and dependencies.sources == workspace.sources.saved
    assert workspace.session.draft.display_name == "Unsaved Recipe input"
    assert window.buttons["operation"].isEnabled() and recovered.active is None
    assert (
        load_preferences(data.preferences).last_applied_recipe_id
        == recovered.candidate_prepared.configuration.recipe_id
    )


def test_unchanged_save_never_installs_or_invalidates_candidate(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    before = window.bridge.application.snapshot()
    preferences = data.preferences.read_bytes()
    applied = QSignalSpy(window.editing.applied)
    workspace.save_setup()
    until(lambda: not window.editing.busy)
    assert window.bridge.application.snapshot() == before
    assert data.preferences.read_bytes() == preferences and applied.count() == 0
    assert window.buttons["operation"].isEnabled()


@pytest.mark.parametrize("failed", ["machine", "sources"])
def test_partial_save_invalidates_only_successfully_changed_dependencies(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    monkeypatch: pytest.MonkeyPatch,
    failed: str,
) -> None:
    window, workspace, data = saved_screen
    assert workspace.sources is not None
    app = window.bridge.application
    before = app.snapshot()
    machine, sources = data.machine.read_bytes(), data.sources.read_bytes()
    applied = QSignalSpy(window.editing.applied)
    edit_geometry(workspace)
    workspace.sources.set_loop(not workspace.sources.saved.loop)

    def fail(**_kwargs: object) -> None:
        raise ConfigurationIOError("isolated partial Save failure")

    monkeypatch.setattr(
        workspace.draft if failed == "machine" else workspace.sources, "save", fail
    )
    workspace.save_setup()
    until(lambda: not window.editing.busy)
    assert app.snapshot().candidate_prepared is before.candidate_prepared
    assert app.snapshot().candidate_requires_preparation and applied.count() == 0
    assert (data.machine.read_bytes() == machine) == (failed == "machine")
    assert (data.sources.read_bytes() == sources) == (failed == "sources")
    assert workspace.draft.dirty == (failed == "machine")
    assert workspace.sources.dirty == (failed == "sources")
    assert "partial Save failure" in window.event_label.toolTip()
    assert not window.buttons["operation"].isEnabled()


def test_failed_same_id_preparation_cannot_clear_stale_candidate(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, data = saved_screen
    before = window.bridge.application.snapshot()
    preferences = data.preferences.read_bytes()
    edit_geometry(workspace)
    workspace.save_setup()
    until(lambda: not window.editing.busy)
    applied = QSignalSpy(window.editing.applied)

    def failed(_session: RecipeEditorSession, _recipe_id: str) -> None:
        raise ConfigurationError("isolated preparation failure")

    monkeypatch.setattr(RecipeEditorSession, "prepare_saved", failed)
    window.navigate(0)
    select_production_recipe(window)
    until(lambda: not window.editing.busy)
    current = window.bridge.application.snapshot()
    assert current.candidate_prepared is before.candidate_prepared
    assert (
        current.candidate_requires_preparation
        and not window.buttons["operation"].isEnabled()
    )
    assert current.active == before.active and applied.count() == 0
    assert data.preferences.read_bytes() == preferences
    assert window.production_recipes.feedback is not None
    assert window.production_recipes.feedback[0] == "recipe_prepare_error"
    assert "isolated preparation failure" in window.event_label.toolTip()


def test_external_start_preflight_failure_and_reselection_clear_recovery_feedback(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _workspace, data = saved_screen
    window.navigate(0)
    source = load_camera_sources(data.sources)
    save_camera_sources(data.sources, replace(source, target_fps=source.target_fps + 1))
    failed = QSignalSpy(window.bridge.command_failed)
    assert window.buttons["operation"].isEnabled()
    window.buttons["operation"].click()
    until(
        lambda: failed.count() == 1 and window.snapshot.candidate_requires_preparation
    )
    assert window.snapshot.state is ApplicationState.READY
    assert window.snapshot.last_issue is not None
    assert not window.buttons["operation"].isEnabled()
    previous = window.snapshot.candidate_prepared

    def unavailable(_session: RecipeEditorSession, _recipe_id: str) -> None:
        raise ConfigurationError("stale recovery preparation failed")

    with monkeypatch.context() as patch:
        patch.setattr(RecipeEditorSession, "prepare_saved", unavailable)
        select_production_recipe(window)
        until(lambda: not window.editing.busy)
    assert window.snapshot.candidate_prepared is previous
    assert window.snapshot.candidate_requires_preparation
    assert (
        "stale recovery preparation failed"
        in window.production_recipes.recipes.toolTip()
    )
    assert (
        "stale recovery preparation failed"
        in window.production_recipes.recipes.accessibleName()
    )
    select_production_recipe(window)
    until(lambda: not window.editing.busy)
    assert not window.snapshot.candidate_requires_preparation
    assert window.snapshot.last_issue is None and window.snapshot.active is None
    assert window.buttons["operation"].isEnabled()
    assert window.event_label.accessibleName() != text("command_error", window.language)


@pytest.mark.parametrize("during", ["validation", "after_sources"])
def test_vision_save_revalidates_each_independent_publication(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    monkeypatch: pytest.MonkeyPatch,
    during: str,
) -> None:
    window, workspace, data = saved_screen
    app = window.bridge.application
    owner = app.identity
    assert owner is not None and workspace.sources is not None
    edit_geometry(workspace)
    workspace.sources.set_loop(not workspace.sources.saved.loop)
    before = data.machine.read_bytes(), data.sources.read_bytes()
    if during == "validation":
        original = workspace.session.validate_vision_save

        def validate(connection: SourceConnectionSnapshot | None) -> None:
            original(connection)
            owner.logout()

        monkeypatch.setattr(workspace.session, "validate_vision_save", validate)
    else:
        save = workspace.sources.save

        def save_and_logout(
            *, publish: Callable[[ConfigurationPublication], None] | None = None
        ) -> None:
            save(publish=publish)
            owner.logout()

        monkeypatch.setattr(workspace.sources, "save", save_and_logout)
    workspace.save_setup()
    until(lambda: not window.editing.busy)
    assert data.machine.read_bytes() == before[0] and workspace.draft.dirty
    assert (data.sources.read_bytes() != before[1]) == (during == "after_sources")
    assert app.snapshot().candidate_requires_preparation == (during == "after_sources")
    assert workspace.feedback is not None and "superseded" in workspace.feedback[1]
