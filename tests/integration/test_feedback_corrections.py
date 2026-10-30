"""Feedback regressions using isolated files and controlled engines."""

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ControlledEngine
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.recipe_ui import Screen, edit
from tests.support.recipe_ui import camera_screen as camera_screen
from tests.support.recipe_ui import configured_screen as configured_screen
from tests.support.startup import saved_data

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ProductionCounters,
)
from capsule_vision.application.folder_replay import FolderSourceError
from capsule_vision.application.startup import load_startup
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationIOError,
    RecipeDefinition,
    save_camera_sources,
    save_recipe,
)
from capsule_vision.configuration.drafts import CameraSourceDraft
from capsule_vision.inspection import CameraId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui import bootstrap
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.vision_page import VisionPage


@pytest.fixture(autouse=True)
def screen_fonts(qapp: QApplication) -> None:
    # Match existing screenshot fixtures; offscreen Qt has no native font discovery.
    ensure_test_fonts()


def show(window: MainWindow, language: Language) -> None:
    window.set_language(language)
    window.resize(1080, 1920) if language == "zh" else window.resize(1920, 1080)
    window.show()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_folder_warning_survives_apply_until_verified_source_save(
    qapp: QApplication, tmp_path: Path, language: Language
) -> None:
    data = saved_data(tmp_path)
    missing = str(tmp_path / "unavailable-images")
    save_camera_sources(
        data.sources,
        CameraSources(
            tuple(CameraFolder(CameraId(f"CAM{i}"), missing) for i in range(1, 7))
        ),
    )
    engine = ControlledEngine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    show(window, language)
    try:
        window.navigate(1)
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert text("startup_source_unavailable", language) in window.event_label.text()
        assert missing in window.event_label.toolTip()
        assert missing in window.recipe_page.status.toolTip()
        assert text("roi_applied", language) in window.recipe_page.status.text()
        assert not engine.calls
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.application.snapshot().counters == ProductionCounters()
        QApplication.processEvents()

        window.navigate(4)
        workspace = window.vision_workspace
        assert workspace is not None
        until(lambda: not window.recipe_page.busy)
        for card in workspace.camera_cards.values():
            card.click()
            until(lambda: not window.recipe_page.busy)
            workspace.folder.setText(str(tmp_path / "frames"))
            workspace.source_edited(str(tmp_path / "frames"))
            workspace.refresh_frame()
            until(lambda: not window.recipe_page.busy)
        # A single reference or a path edit is not proof that all saved sources work.
        assert missing in window.event_label.toolTip()
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert not window.configuration_issue and not window.event_label.text()
        assert not window.recipe_page.configuration_messages
        assert missing not in window.recipe_page.status.toolTip()
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.application.snapshot().active is None and not engine.calls
    finally:
        if startup.editor is not None:
            startup.editor.machine_editor.discard()
            if startup.editor.camera_sources is not None:
                startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()


def test_successful_start_clears_source_warning_but_not_manifest_recovery(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    preferences = json.loads(data.preferences.read_text(encoding="utf-8"))
    preferences["language"] = "invalid"
    data.preferences.write_text(json.dumps(preferences), encoding="utf-8")
    original = data.preferences.read_bytes()
    images = {p: p.read_bytes() for p in (tmp_path / "frames").glob("*.png")}
    for path in images:
        path.unlink()
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert "startup_source_unavailable" in window.configuration_messages
        with pytest.raises(ApplicationError) as failure:
            startup.application.start().result(WAIT)
        assert (
            isinstance(failure.value.__cause__, FolderSourceError) and not engine.calls
        )
        for path, contents in images.items():
            path.write_bytes(contents)
        window.bridge.start()
        until(lambda: window.snapshot.state is ApplicationState.RUNNING)
        assert "startup_source_unavailable" not in window.configuration_messages
        assert "startup_language" in window.configuration_messages
        assert text("preferences_error", "zh") in window.event_label.text()
        assert "invalid language" in window.event_label.toolTip()
        assert data.preferences.read_bytes() == original
        assert engine.calls
    finally:
        close_window(window)
        delivery.detach()
    assert engine.closed


@pytest.mark.parametrize("language", ["zh", "en"])
def test_invalid_language_apply_preserves_both_outcomes_and_recovery(
    qapp: QApplication, tmp_path: Path, language: Language
) -> None:
    data = saved_data(tmp_path)
    preferences = json.loads(data.preferences.read_text(encoding="utf-8"))
    preferences["language"] = "invalid"
    data.preferences.write_text(json.dumps(preferences), encoding="utf-8")
    original = data.preferences.read_bytes()
    save_recipe(
        data.recipes / "second.json",
        RecipeDefinition("second", "Second", model_id="general"),
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    show(window, language)
    window.resize(1920, 1080) if language == "zh" else window.resize(1080, 1920)

    def assert_preferences_feedback(current_language: Language) -> None:
        QApplication.processEvents()
        expected = text("preferences_error", current_language)
        label = window.event_label
        full = label.accessibleName()
        visible = label.text()
        # The single row may elide; recovery text must remain fully accessible.
        assert full.startswith(expected)
        assert label.toolTip().splitlines()[0] == full
        assert visible and "\n" not in visible and not label.wordWrap()
        assert visible == full or (
            visible.endswith("…") and full.startswith(visible.removesuffix("…"))
        )
        assert label.fontMetrics().horizontalAdvance(visible) <= label.width()

    try:
        window.navigate(1)
        window.recipe_page.recipes.activated.emit(
            window.recipe_page.recipes.findText("Second")
        )
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        snapshot = startup.application.snapshot()
        assert (
            snapshot.candidate is not None and snapshot.candidate.recipe_id == "second"
        )
        assert snapshot.active is None and snapshot.state is ApplicationState.READY
        assert snapshot.counters == ProductionCounters() and not snapshot.runtime_owned
        assert text("roi_applied", language) in window.recipe_page.status.text()
        assert text("startup_language", language) in window.recipe_page.status.text()
        assert_preferences_feedback(language)
        assert "app.json" in window.event_label.toolTip()
        assert "Repair app.json externally and restart" in window.event_label.toolTip()
        assert "invalid language" in window.event_label.toolTip()
        assert data.preferences.read_bytes() == original
        finished: list[str] = []
        window.bridge.command_finished.connect(finished.append)
        window.bridge.stop()  # An unrelated successful command cannot repair the manifest.
        until(lambda: "stop" in finished)
        assert_preferences_feedback(language)
        QApplication.processEvents()
        other: Language = "en" if language == "zh" else "zh"
        window.set_language(other)
        assert_preferences_feedback(other)
        assert data.preferences.read_bytes() == original
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_recipe_summaries_keep_full_diagnostics_and_failure_semantics(
    configured_screen: Screen,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    window, session, runtimes = configured_screen
    show(window, language)
    window.navigate(1)
    page = window.recipe_page
    assert session.entry is not None
    original = session.entry.path.read_bytes()
    snapshot = window.bridge.application.snapshot()
    edit(page.metadata["display_name"], "")
    page.save_and_apply()
    assert page.status.text() == text("recipe_validation_error", language)
    assert "display_name" in page.status.toolTip()
    assert session.dirty and session.entry.path.read_bytes() == original
    assert window.bridge.application.snapshot() == snapshot
    if language == "zh":
        QApplication.processEvents()
    session.discard()
    page.reload()
    assert not page.status.toolTip()

    detail = f"Save {session.entry.path}: [WinError 5] Access is denied"

    def fail_save(*, new_identity: tuple[str, str] | None = None) -> None:
        raise ConfigurationIOError(detail)

    with monkeypatch.context() as patch:
        patch.setattr(session, "save", fail_save)
        edit(page.metadata["display_name"], "Unsaved")
        page.save_and_apply()
    assert page.status.text() == text("recipe_save_error", language)
    assert page.status.toolTip() == detail and detail not in page.status.text()
    assert session.dirty and session.entry.path.read_bytes() == original
    assert window.bridge.application.snapshot() == snapshot
    session.discard()
    page.reload()

    artifact = Path(session.registry.get("general").model_path)
    contents = artifact.read_bytes()
    try:
        artifact.write_bytes(b"mismatched isolated fake model")
        page.save_and_apply()
        until(lambda: not page.busy)
        assert page.status.text() == text("model_prepare_error", language)
        assert str(artifact) in page.status.toolTip()
        assert (
            "SHA-256" in page.status.toolTip() and "observed" in page.status.toolTip()
        )
        assert window.bridge.application.snapshot() == snapshot and not runtimes
        QApplication.processEvents()
    finally:
        artifact.write_bytes(contents)
    page.save_and_apply()
    until(lambda: not page.busy)
    assert page.status.text() == text("roi_applied", language)
    assert not page.status.toolTip()


def test_preferences_retry_clears_its_own_error_without_clearing_recipe_error(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        original = data.preferences.read_bytes()
        detail = f"Save {data.preferences}: [WinError 5] Access is denied"

        def fail(*_args: object) -> None:
            raise ConfigurationIOError(detail)

        with monkeypatch.context() as patch:
            patch.setattr(bootstrap, "save_preferences", fail)
            window.set_language("zh")
        assert text("preferences_error", "zh") in window.event_label.toolTip()
        assert detail in window.event_label.toolTip()
        assert data.preferences.read_bytes() == original
        window.set_language("en")
        assert not window.event_label.text() and not window.event_label.toolTip()
        window.navigate(1)
        window.bridge.command_failed.emit("recipe", ValueError("isolated command"))
        window.set_language("zh")
        assert text("recipe", "zh") in window.event_label.text()
        assert "ValueError" in window.event_label.toolTip()
        window.bridge.command_finished.emit("recipe")
        assert not window.event_label.text()
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_vision_folder_unavailability_does_not_block_save_or_apply(
    configured_screen: Screen, tmp_path: Path, language: Language
) -> None:
    window, session, _ = configured_screen
    folder = str(tmp_path / ("unavailable-folder-" * 6))
    sources = CameraSources(
        tuple(CameraFolder(c, folder) for c in session.machine.layout.camera_ids)
    )
    path = tmp_path / "camera_sources.json"
    save_camera_sources(path, sources)
    session.camera_sources = CameraSourceDraft(sources, path)
    original = path.read_bytes()
    snapshot = window.bridge.application.snapshot()
    show(window, language)
    window.navigate(4)
    until(lambda: not window.recipe_page.busy)
    workspace = window.vision_workspace
    assert workspace is not None and workspace.frame is None
    assert window.event_label.accessibleName() == text(
        "folder_reference_error", language
    )
    assert folder in window.event_label.toolTip()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not window.event_label.text()
    assert workspace.status.accessibleName() == text("saved", language)
    assert not workspace.status.toolTip()
    assert workspace.status.text() == "" and workspace.status.pixmap() is not None
    assert (
        path.read_bytes() == original
        and window.bridge.application.snapshot() == snapshot
    )
    workspace.refresh_frame()
    until(lambda: not window.recipe_page.busy)
    assert window.event_label.accessibleName() == text(
        "folder_reference_error", language
    )
    assert (
        folder in window.event_label.toolTip()
        and folder not in window.event_label.text()
    )
    assert workspace.frame is None and not workspace.vision_dirty
    QApplication.processEvents()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_vision_save_and_partial_save_keep_diagnostics_and_clear_after_retry(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    window, workspace, _ = camera_screen
    show(window, language)
    sources = workspace.sources
    assert sources is not None
    candidate = window.bridge.application.snapshot().candidate
    geometry = workspace.draft.definition
    workspace.coordinates["X"].setText("99999")
    workspace.apply_geometry()
    assert workspace.draft.definition == geometry
    assert window.event_label.accessibleName() == text("roi_geometry_error", language)
    assert "rejected" in window.event_label.toolTip()
    if language == "en":
        QApplication.processEvents()
    workspace.operation(
        lambda: workspace.set_rect(geometry.layout.cameras[0].slots[0].rect)
    )
    assert not window.event_label.text() and not window.event_label.toolTip()
    details = f"Write {sources.path}: [WinError 5] Access is denied"

    def fail() -> None:
        raise ConfigurationIOError(details)

    with monkeypatch.context() as patch:
        patch.setattr(workspace.draft, "save", fail)
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
    assert window.event_label.accessibleName() == text("vision_error", language)
    assert (
        window.event_label.toolTip()
        == f"{text('vision_error', language)}\n{text('saved', language)}: CameraSources\nMachineLayout: ConfigurationIOError: {details}"
    )
    with monkeypatch.context() as patch:
        patch.setattr(sources, "save", fail)
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
    assert window.event_label.accessibleName() == text("source_partial_save", language)
    assert (
        window.event_label.toolTip()
        == f"{text('source_partial_save', language)}\n{text('saved', language)}: MachineLayout\nCameraSources: ConfigurationIOError: {details}"
    )
    assert window.bridge.application.snapshot().candidate == candidate
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not window.event_label.text() and not window.event_label.toolTip()
