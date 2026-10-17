"""Phase 3L feedback regressions using isolated files and existing fake engines."""

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.bootstrap_support import saved_data
from tests.integration.test_camera_setup import camera_screen as camera_screen
from tests.integration.test_local_production_ui import ControlledEngine
from tests.qt_support import close_window, ensure_test_fonts, until
from tests.recipe_ui_support import Screen, edit
from tests.recipe_ui_support import configured_screen as configured_screen

from vimo_demo.application import ApplicationError, ApplicationState, ProductionCounters
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationIOError,
    RecipeDefinition,
    save_camera_sources,
    save_recipe,
)
from vimo_demo.inspection import CameraId
from vimo_demo.ui import bootstrap
from vimo_demo.ui.bootstrap import create_window, load_startup
from vimo_demo.ui.camera_source_editor import CameraSourceDraft
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.roi_editor import MachineRoiWorkspace


@pytest.fixture(autouse=True)
def screen_fonts(qapp: QApplication) -> None:
    # Match existing screenshot fixtures; offscreen Qt has no native font discovery.
    ensure_test_fonts()


def capture(window: ProductionWindow, directory: Path, scenario: str) -> None:
    """Use the existing offscreen screenshot workflow; keep full tooltip evidence."""
    QApplication.processEvents()
    name = f"after_{scenario}_{window.language}_{window.width()}x{window.height()}"
    assert window.grab().save(str(directory / f"{name}.png"))
    workspace = window.parameters.roi_workspace
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "state": window.snapshot.state.name,
                "language": window.language,
                "event": window.event_label.text(),
                "event_tooltip": window.event_label.toolTip(),
                "strip_height": window.event_frame.height(),
                "recipe": window.parameters.status.text(),
                "recipe_tooltip": window.parameters.status.toolTip(),
                "vision": window.event_label.text() if workspace else "",
                "vision_tooltip": window.event_label.toolTip() if workspace else "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def show(window: ProductionWindow, language: Language) -> None:
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
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert text("startup_source_unavailable", language) in window.event_label.text()
        assert missing in window.event_label.toolTip()
        assert missing in window.parameters.status.toolTip()
        assert text("roi_applied", language) in window.parameters.status.text()
        assert not engine.calls
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.application.snapshot().counters == ProductionCounters()
        capture(window, tmp_path, "recipe_apply_unresolved_folder")

        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None
        until(lambda: not window.parameters.busy)
        for card in workspace.camera_cards.values():
            card.click()
            until(lambda: not window.parameters.busy)
            workspace.folder.setText(str(tmp_path / "frames"))
            workspace.source_edited(str(tmp_path / "frames"))
            workspace.refresh_frame()
            until(lambda: not window.parameters.busy)
        # A single reference or a path edit is not proof that all saved sources work.
        assert missing in window.event_label.toolTip()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert not window.configuration_issue and not window.event_label.text()
        assert not window.parameters.configuration_messages
        assert missing not in window.parameters.status.toolTip()
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
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
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
        data.recipes / "second.recipe.json", RecipeDefinition("second", "Second")
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    show(window, language)
    window.resize(1920, 1080) if language == "zh" else window.resize(1080, 1920)
    try:
        window.navigate(1)
        window.parameters.recipes.activated.emit(
            window.parameters.recipes.findText("Second")
        )
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
        snapshot = startup.application.snapshot()
        assert (
            snapshot.candidate is not None and snapshot.candidate.recipe_id == "second"
        )
        assert snapshot.active is None and snapshot.state is ApplicationState.READY
        assert snapshot.counters == ProductionCounters() and not snapshot.runtime_owned
        assert text("roi_applied", language) in window.parameters.status.text()
        assert text("startup_language", language) in window.parameters.status.text()
        assert text("preferences_error", language) in window.event_label.text()
        assert "app.json" in window.event_label.toolTip()
        assert "Repair app.json externally and restart" in window.event_label.toolTip()
        assert "invalid language" in window.event_label.toolTip()
        assert data.preferences.read_bytes() == original
        finished: list[str] = []
        window.bridge.command_finished.connect(finished.append)
        window.bridge.stop()  # An unrelated successful command cannot repair the manifest.
        until(lambda: "stop" in finished)
        assert text("preferences_error", language) in window.event_label.text()
        capture(window, tmp_path, "invalid_language_after_apply")
        other: Language = "en" if language == "zh" else "zh"
        window.set_language(other)
        assert text("preferences_error", other) in window.event_label.text()
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
    page = window.parameters
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
        capture(window, tmp_path, "recipe_invalid_name")
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

    artifact = Path(session.registry.get(session.default_model_id).model_path)
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
        capture(window, tmp_path, "recipe_model_hash_failure")
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
def test_vision_folder_summaries_and_tooltips_preserve_configuration(
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
    until(lambda: not window.parameters.busy)
    workspace = window.parameters.roi_workspace
    assert workspace is not None and workspace.frame is None
    assert window.event_label.accessibleName() == text(
        "folder_reference_error", language
    )
    assert folder in window.event_label.toolTip()
    workspace.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert window.event_label.accessibleName() == text(
        "source_validation_error", language
    )
    assert (
        folder in window.event_label.toolTip()
        and folder not in window.event_label.text()
    )
    assert workspace.status.accessibleName() == text("setup_failed", language)
    assert workspace.status.text() == "" and workspace.status.pixmap() is not None
    assert (
        path.read_bytes() == original
        and window.bridge.application.snapshot() == snapshot
    )
    capture(window, tmp_path, "vision_unavailable_folder_failed_save")


@pytest.mark.parametrize("language", ["zh", "en"])
def test_vision_save_and_partial_save_keep_diagnostics_and_clear_after_retry(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
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
        capture(window, sources.path.parent, "vision_invalid_roi")
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
        until(lambda: not window.parameters.busy)
    assert window.event_label.accessibleName() == text("vision_save_error", language)
    assert (
        window.event_label.toolTip()
        == f"{text('vision_save_error', language)}\nConfigurationIOError: {details}"
    )
    with monkeypatch.context() as patch:
        patch.setattr(sources, "save", fail)
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
    assert window.event_label.accessibleName() == text("source_partial_save", language)
    assert (
        window.event_label.toolTip()
        == f"{text('source_partial_save', language)}\nConfigurationIOError: {details}"
    )
    assert window.bridge.application.snapshot().candidate == candidate
    workspace.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert not window.event_label.text() and not window.event_label.toolTip()
