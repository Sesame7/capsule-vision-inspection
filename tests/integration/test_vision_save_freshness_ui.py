"""Qt Vision persists independently; Production alone installs saved candidates."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.recipe_ui import select_production_recipe
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data

from capsule_vision.application import ApplicationState, StartBlocker
from capsule_vision.application.audit import AuditQuery
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
    save_machine_layout,
)
from capsule_vision.configuration.preferences import load_preferences
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
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
        window.editing.pending_roi_input = None
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


@pytest.mark.parametrize(
    "preview, change_loop", [(False, False), (False, True), (True, False)]
)
def test_save_uses_visible_coordinates_for_selected_channel(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    preview: bool,
    change_loop: bool,
) -> None:
    window, workspace, data = saved_screen
    camera = CameraId("CAM2")
    workspace.camera_cards[camera].click()
    until(lambda: not window.editing.busy)
    workspace.select_roi(2)
    before = load_machine_layout(data.machine)
    sources = load_camera_sources(data.sources)
    original = before.layout.cameras[before.layout.camera_ids.index(camera)].slots[2]
    x = workspace.coordinates["X"]
    x.setFocus()
    x.selectAll()
    QTest.keyClicks(x, str(original.rect.x + 1))
    save = workspace.buttons["save"]
    assert save.isEnabled() and workspace.buttons["vision_discard"].isEnabled()
    assert workspace.status.accessibleName() == text("unsaved", window.language)
    assert workspace.draft.definition == before
    assert workspace.canvas.rois[2] == original
    if preview:
        QTest.keyClick(x, Qt.Key.Key_Return)
        assert workspace.canvas.rois[2].rect.x == original.rect.x + 1
        assert load_machine_layout(data.machine) == before
    if change_loop:
        workspace.loop.click()
    save.setFocus()
    QTest.keyClick(save, Qt.Key.Key_Space)
    until(lambda: not window.editing.busy)
    expected = replace(original, rect=replace(original.rect, x=original.rect.x + 1))
    after = load_machine_layout(data.machine)
    assert after.layout.camera_ids == before.layout.camera_ids
    for new, old in zip(after.layout.cameras, before.layout.cameras, strict=True):
        assert new.slots == tuple(
            expected if roi == original else roi for roi in old.slots
        )
    assert load_camera_sources(data.sources) == replace(
        sources, loop=not sources.loop if change_loop else sources.loop
    )
    assert workspace.canvas.rois[2] == expected and not save.isEnabled()


@pytest.mark.parametrize("invalid", ["", "bad", "-1", "999"])
def test_invalid_visible_coordinates_block_both_publications_and_allow_correction(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData], invalid: str
) -> None:
    window, workspace, data = saved_screen
    before = data.machine.read_bytes(), data.sources.read_bytes()
    region = workspace.draft.rois(workspace.camera)[0]
    workspace.loop.click()
    x = workspace.coordinates["X"]
    x.setText(invalid)
    saved = QSignalSpy(workspace.setup_saved)
    workspace.buttons["save"].click()
    assert not window.editing.busy and saved.count() == 0
    assert (data.machine.read_bytes(), data.sources.read_bytes()) == before
    assert (
        workspace.feedback is not None and workspace.feedback[0] == "roi_geometry_error"
    )
    assert x.text() == invalid and not x.isReadOnly()
    assert workspace.canvas.rois[0] == region
    workspace.refresh_frame()
    until(lambda: not window.editing.busy)
    assert x.text() == invalid
    x.setText(str(region.rect.x + 1))
    workspace.buttons["save"].click()
    until(lambda: saved.count() == 1)
    assert workspace.feedback is None and not workspace.vision_dirty
    assert (
        data.machine.read_bytes() != before[0]
        and data.sources.read_bytes() != before[1]
    )


def test_pending_input_follows_displayed_roi_without_applying_on_selection(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    baseline = load_machine_layout(data.machine)
    x = workspace.coordinates["X"]
    original = x.text()
    x.setText("17")
    workspace.channel.setFocus()
    assert x.text() == "17" and workspace.draft.definition == baseline
    x.setText(original)
    assert not workspace.vision_dirty and not workspace.buttons["save"].isEnabled()
    x.setText("invalid")
    workspace.select_roi(2)
    assert x.text() == str(workspace.draft.rois(workspace.camera)[2].rect.x)
    x.setText("99")
    workspace.camera_cards[CameraId("CAM2")].click()
    until(lambda: not window.editing.busy)
    assert workspace.selected_index == 0 and not workspace.vision_dirty
    assert workspace.draft.definition == load_machine_layout(data.machine) == baseline


def test_read_only_navigation_retains_unapplied_coordinates_and_rechecks_save_admission(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    owner = window.bridge.application.identity
    assert owner is not None
    camera = CameraId("CAM2")
    workspace.camera_cards[camera].click()
    until(lambda: not window.editing.busy)
    workspace.select_roi(3)
    before = data.machine.read_bytes()
    workspace.coordinates["X"].setText("31")
    owner.logout()
    window.identity_controls.refresh()
    workspace.save_setup()
    assert not workspace.draft.dirty and data.machine.read_bytes() == before
    assert not workspace.buttons["save"].isEnabled()
    window.navigate(1)
    assert window.pages.currentWidget() is window.recipe_page
    assert not window.close()
    until(
        lambda: (
            window.vision_workspace is not None
            and any(
                box.isVisible()
                for box in window.vision_workspace.findChildren(QMessageBox)
            )
        )
    )
    restored = window.vision_workspace
    assert restored is not None and restored is not workspace
    assert restored.camera == camera and restored.selected_index == 3
    assert (
        restored.coordinates["X"].isReadOnly()
        and restored.coordinates["X"].text() == "31"
    )
    box = next(box for box in restored.findChildren(QMessageBox) if box.isVisible())
    assert not next(
        b
        for b in box.buttons()
        if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
    ).isEnabled()
    box.reject()
    owner.login("TestAdmin", TEST_PASSWORD).result(WAIT)
    window.identity_controls.refresh()
    window.navigate(4)
    restored.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert load_machine_layout(data.machine).layout.cameras[1].slots[3].rect.x == 31


def test_unapplied_coordinates_with_a_saved_roi_and_no_reference_image(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    workspace.source_edited("")
    assert workspace.frame is None and workspace.canvas.model.schematic_size is not None
    workspace.coordinates["X"].setText("81")
    workspace.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert load_machine_layout(data.machine).layout.cameras[0].slots[0].rect.x == 81
    assert not load_camera_sources(data.sources).camera(workspace.camera).folder


def test_equivalent_numeric_spelling_leaves_without_publishing(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    paths = (data.sources, data.machine)
    before = tuple((p.read_bytes(), p.stat().st_mtime_ns) for p in paths)
    workspace.coordinates["X"].setText("00")
    saved = QSignalSpy(workspace.setup_saved)
    window.navigate(1)
    box = next(b for b in workspace.findChildren(QMessageBox) if b.isVisible())
    next(
        b
        for b in box.buttons()
        if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
    ).click()
    until(lambda: window.pages.currentWidget() is window.recipe_page)
    assert saved.count() == 0
    assert tuple((p.read_bytes(), p.stat().st_mtime_ns) for p in paths) == before


@pytest.mark.parametrize("closing", [False, True])
def test_navigation_and_close_save_validate_pending_coordinates(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    closing: bool,
) -> None:
    window, workspace, data = saved_screen
    before = load_machine_layout(data.machine)
    x = workspace.coordinates["X"]
    x.setText("invalid")

    def choose_save() -> None:
        if closing:
            window.close()
        else:
            window.navigate(1)
        until(
            lambda: any(box.isVisible() for box in workspace.findChildren(QMessageBox))
        )
        box = next(
            box for box in workspace.findChildren(QMessageBox) if box.isVisible()
        )
        next(
            b
            for b in box.buttons()
            if box.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole
        ).click()

    choose_save()
    assert x.text() == "invalid" and window.vision_workspace is workspace
    assert load_machine_layout(data.machine) == before and not window.bridge.closed
    x.setText("1")
    choose_save()
    until(
        lambda: (
            window.bridge.closed
            if closing
            else window.pages.currentWidget() is window.recipe_page
        )
    )
    assert load_machine_layout(data.machine).layout.cameras[0].slots[0].rect.x == 1


@pytest.mark.parametrize("edited", ["machine", "sources", "both"])
def test_save_only_marks_candidate_stale_and_same_id_selection_recovers(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
    edited: str,
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
    assert not workspace.buttons["save"].isEnabled()
    if edited in ("machine", "both"):
        edit_geometry(workspace)
    if edited in ("sources", "both"):
        workspace.loop.click()
    assert workspace.buttons["save"].isEnabled()
    # Save is independent of the Recipe editor and its unsaved input.
    workspace.session.draft.display_name = "Unsaved Recipe input"
    workspace.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert not workspace.vision_dirty and applied.count() == 0
    assert not workspace.buttons["save"].isEnabled()
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
    assert window.event_label.accessibleName() == text("recipe_reprepare_hint", "en")
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


def test_unchanged_persisted_vision_has_no_ui_save_or_audit(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData],
) -> None:
    window, workspace, data = saved_screen
    app = window.bridge.application
    owner = app.identity
    assert owner is not None
    before = app.snapshot()
    preferences = data.preferences.read_bytes()
    files = (data.machine, data.sources)
    saved_files = tuple((path.read_bytes(), path.stat().st_mtime_ns) for path in files)
    applied = QSignalSpy(window.editing.applied)
    saved = QSignalSpy(workspace.setup_saved)
    save = workspace.buttons["save"]
    assert not save.isEnabled()
    workspace.loop.click()
    assert save.isEnabled()
    workspace.loop.click()
    assert not workspace.vision_dirty and not save.isEnabled()
    for language in ("zh-TW", "de", "th", "hi", "ka", "ti"):
        window.set_language(language)
        assert save.accessibleName() == save.toolTip() == text("save", language)
        assert workspace.status.accessibleName() == text("saved", language)
        assert not workspace.vision_dirty and not save.isEnabled()
    window.set_language("en")
    save.click()
    workspace.save_setup()
    assert not window.editing.busy and saved.count() == 0
    assert app.snapshot() == before
    assert (
        tuple((path.read_bytes(), path.stat().st_mtime_ns) for path in files)
        == saved_files
    )
    for action in ("vision.layout", "vision.sources"):
        assert not owner.audit_page(AuditQuery(action=action)).result(WAIT)
    assert data.preferences.read_bytes() == preferences and applied.count() == 0
    assert window.buttons["operation"].isEnabled()


@pytest.mark.parametrize("missing", ["machine", "sources"])
def test_unchanged_supplied_definition_can_create_missing_file(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData], missing: str
) -> None:
    window, workspace, data = saved_screen
    assert workspace.sources is not None
    path = data.machine if missing == "machine" else data.sources
    saved = path.read_bytes()
    path.unlink()
    # An explicit composition can supply an in-memory baseline for a new file.
    if missing == "machine":
        workspace.draft.load_saved(workspace.draft.saved)
    else:
        workspace.sources.load_saved(workspace.sources.saved)
    workspace.refresh()
    assert workspace.draft.persisted and workspace.sources.persisted
    assert not workspace.vision_dirty and not path.exists()
    assert workspace.buttons["save"].isEnabled()
    workspace.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert path.read_bytes() == saved
    assert not workspace.buttons["save"].isEnabled()


@pytest.mark.parametrize("conflict", ["machine", "sources"])
def test_pending_vision_save_preserves_external_file_conflict(
    saved_screen: tuple[MainWindow, VisionPage, ApplicationData], conflict: str
) -> None:
    window, workspace, data = saved_screen
    assert workspace.sources is not None
    if conflict == "machine":
        edit_geometry(workspace)
        path = data.machine
        save_machine_layout(path, workspace.draft.definition)
    else:
        workspace.loop.click()
        path = data.sources
        original = workspace.sources.saved
        save_camera_sources(path, replace(original, target_fps=original.target_fps + 1))
    external = path.read_bytes(), path.stat().st_mtime_ns
    assert workspace.buttons["save"].isEnabled()
    workspace.buttons["save"].click()
    until(lambda: not window.editing.busy)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == external
    assert workspace.draft.dirty == (conflict == "machine")
    assert workspace.sources.dirty == (conflict == "sources")
    assert workspace.buttons["save"].isEnabled()
    assert "changed/invalid on disk" in window.event_label.toolTip()


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
    assert workspace.buttons["save"].isEnabled()
    assert "partial Save failure" in window.event_label.toolTip()
    assert not window.buttons["operation"].isEnabled()
    key = "vision_sources_only_saved" if failed == "machine" else "source_partial_save"
    for language in ("zh-TW", "de", "hi"):
        window.set_language(language)
        assert window.event_label.accessibleName() == text(key, language)
        assert (
            "isolated partial Save failure"
            in window.event_label.accessibleDescription()
        )
        assert workspace.buttons["save"].isEnabled()
        assert workspace.draft.dirty == (failed == "machine")
        assert workspace.sources.dirty == (failed == "sources")


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
    assert window.bridge.application.snapshot().start_blocker is None
    assert window.buttons["operation"].isEnabled()
    window.buttons["operation"].click()
    until(
        lambda: failed.count() == 1 and window.snapshot.candidate_requires_preparation
    )
    assert window.snapshot.state is ApplicationState.READY
    assert window.snapshot.last_issue is not None
    assert window.snapshot.start_blocker is StartBlocker.RECIPE_STALE
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
