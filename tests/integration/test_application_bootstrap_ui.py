"""Normal shell restoration and successful Apply preferences, with no real SDK."""

import json
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication, QWidget
from tests.support.application import WAIT
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.startup import close, saved_data

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ProductionCounters,
)
from capsule_vision.application.folder_replay import FolderSourceError
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    RecipeDefinition,
    save_camera_sources,
    save_recipe,
)
from capsule_vision.configuration.preferences import (
    ApplicationPreferences,
    load_preferences,
    save_preferences,
)
from capsule_vision.inspection import CameraId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_absent_data_opens_five_page_unconfigured_shell(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = load_startup(ApplicationData.at_entry(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.pages.count() == 5
        assert not window.cards
        assert window.snapshot.state is ApplicationState.UNCONFIGURED
        assert not window.buttons["operation"].isEnabled()
        assert window.event_label.toolTip() and startup.issues
        window.navigate(1)
        window.navigate(4)
        window.language_selector.activated.emit(
            window.language_selector.findData("en" if window.language == "zh" else "zh")
        )
        assert window.language == "en"
        preferences = load_preferences(startup.data.preferences)
        assert preferences.language == "en"
        assert preferences.last_applied_recipe_id is None
        assert not startup.data.models.exists() and not startup.data.machine.exists()
    finally:
        close_window(window)
        delivery.detach()


def test_sparse_source_file_opens_vision_without_implicit_repair(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    data.sources.write_text("{}", encoding="utf-8")
    original = data.sources.read_bytes()
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.navigate(4)
        until(lambda: not window.recipe_page.busy)
        workspace = window.vision_workspace
        assert workspace is not None and workspace.sources is not None
        source = workspace.sources
        assert source.saved.cameras == () and not source.dirty and source.persisted
        assert all(not card.folder.text() for card in workspace.camera_cards.values())
        assert data.sources.read_bytes() == original
        workspace.source_edited("../frames", CameraId("CAM3"))
        assert source.dirty and tuple(
            c.camera_id for c in source.definition.cameras
        ) == ("CAM3",)
        assert source.definition.camera(CameraId("CAM3")).folder == "../frames"
        assert source.saved.cameras == () and data.sources.read_bytes() == original
        workspace.set_source_mode("basler")
        assert source.definition.camera(CameraId("CAM3")).folder == "../frames"
        assert source.definition.camera(CameraId("CAM1")).basler_serial == ""
        assert data.sources.read_bytes() == original
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        if startup.editor is not None and startup.editor.camera_sources is not None:
            startup.editor.camera_sources.discard()
        close_window(window)
        delivery.detach()


def test_saved_apply_language_restart_and_no_production_side_effects(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_recipe(
        data.recipes / "second.json",
        RecipeDefinition("second", "Second", model_id="general"),
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.recipe_page.recipes.currentText() == "Local test"
        window.navigate(1)
        window.recipe_page.recipes.activated.emit(
            window.recipe_page.recipes.findText("Second")
        )
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        assert startup.editor is not None
        startup.editor.save()  # Save alone must not change restored selection.
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        window.recipe_page.save_and_apply()
        until(
            lambda: (
                load_preferences(data.preferences).last_applied_recipe_id == "second"
            )
        )
        assert startup.application.snapshot().candidate is not None
        window.language_selector.activated.emit(
            window.language_selector.findData("en" if window.language == "zh" else "zh")
        )
        assert load_preferences(data.preferences).language == "zh"
        snapshot = startup.application.snapshot()
        assert snapshot.state is ApplicationState.READY and snapshot.active is None
        assert snapshot.counters == ProductionCounters() and snapshot.batch_id is None
        assert not snapshot.runtime_owned and snapshot.latest_production_result is None
    finally:
        close_window(window)
        delivery.detach()
    restored = load_startup(data)
    try:
        assert restored.prepared is not None
        assert restored.prepared.configuration.recipe_id == "second"
        assert (
            restored.preferences is not None and restored.preferences.language == "zh"
        )
        assert restored.machine == startup.machine
    finally:
        close(restored)


def test_unchanged_apply_still_notifies_and_persists_preferences(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    writes: list[ApplicationPreferences] = []
    applied: list[object] = []
    snapshots: list[object] = []

    def persist(path: Path, preferences: ApplicationPreferences) -> None:
        assert path == data.preferences
        writes.append(preferences)
        save_preferences(path, preferences)

    try:
        until(lambda: not window.recipe_page.labels_loading)
        window.bridge.refresh()
        before = startup.application.snapshot()
        original_preferences = load_preferences(data.preferences)
        window.editing.applied.connect(applied.append)
        window.bridge.snapshot_changed.connect(snapshots.append)
        monkeypatch.setattr("capsule_vision.ui.bootstrap.save_preferences", persist)
        window.recipe_page.save_and_apply()
        until(lambda: bool(applied) and not window.editing.busy)
        assert startup.application.snapshot() == before
        assert not snapshots
        assert len(applied) == 1
        assert applied[0] is startup.application.snapshot().candidate_prepared
        assert writes == [original_preferences]
        assert load_preferences(data.preferences) == original_preferences
    finally:
        close_window(window)
        delivery.detach()


def test_failed_preparation_never_updates_last_recipe(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_recipe(
        data.recipes / "second.json",
        RecipeDefinition("second", "Second", model_id="general"),
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.recipe_page.recipes.activated.emit(
            window.recipe_page.recipes.findText("Second")
        )
        (data.models / "general" / "model.vimosln").write_bytes(
            b"changed after startup"
        )
        window.recipe_page.save_and_apply()
        until(
            lambda: (
                not window.recipe_page.busy and bool(window.recipe_page.status.text())
            )
        )
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        candidate = startup.application.snapshot().candidate
        assert candidate is not None and candidate.recipe_id == "local"
    finally:
        close_window(window)
        delivery.detach()


def test_no_recipe_fallback_in_widget_and_explicit_selection_can_apply(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_preferences(data.preferences, ApplicationPreferences(None, "zh"))
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.machine is not None
        assert window.snapshot.candidate_prepared is None
        assert (
            tuple(card.camera_layout for card in window.cards)
            == startup.machine.layout.cameras
        )
        assert startup.editor is not None and startup.editor.entry is None
        assert window.recipe_page.recipes.currentIndex() == -1
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        window.recipe_page.recipes.activated.emit(0)
        window.recipe_page.save_and_apply()
        until(
            lambda: load_preferences(data.preferences).last_applied_recipe_id == "local"
        )
        assert startup.application.snapshot().state is ApplicationState.READY
        assert not window.configuration_issue
    finally:
        close_window(window)
        delivery.detach()


def test_unavailable_folder_is_ready_but_start_preflight_prevents_inference(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_camera_sources(
        data.sources,
        CameraSources(
            tuple(CameraFolder(CameraId(f"CAM{i}"), "missing") for i in range(1, 7))
        ),
    )
    calls: list[object] = []

    def forbidden(config: object):
        calls.append(config)
        raise AssertionError("Unavailable acquisition must not create an engine")

    startup = load_startup(data, engine_factory=forbidden)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.application.snapshot().state is ApplicationState.READY
        assert any(
            issue.key == "startup_source_unavailable" for issue in startup.issues
        )
        window.open_vision()
        assert window.vision_workspace is not None
        until(lambda: not window.recipe_page.busy)
        assert window.recipe_page.editable()
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        with pytest.raises(ApplicationError, match="missing") as failure:
            startup.application.start().result(WAIT)
        assert isinstance(failure.value.__cause__, FolderSourceError)
        snapshot = startup.application.snapshot()
        assert snapshot.state is ApplicationState.READY
        assert snapshot.active is None and not snapshot.runtime_owned
        assert snapshot.counters == ProductionCounters()
        assert not calls
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize(
    "kind", ["malformed", "unsupported", "invalid_language", "external_change"]
)
def test_unreliable_preferences_are_never_overwritten_by_ui(
    qapp: QApplication, tmp_path: Path, kind: str
) -> None:
    data = saved_data(tmp_path)
    contents = {
        "malformed": "{",
        "unsupported": '{"schema_version":2,"default_model_id":"general","last_applied_recipe_id":"local"}',
        "invalid_language": '{"last_applied_recipe_id":"local","language":"invalid"}',
        "external_change": "{",
    }[kind]
    if kind != "external_change":
        data.preferences.write_text(contents, encoding="utf-8")
    startup = load_startup(data)
    if kind == "external_change":
        data.preferences.write_text(contents, encoding="utf-8")
    original = data.preferences.read_bytes()
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.pages.count() == 5
        for index in range(5):
            window.navigate(index)
        window.language_selector.activated.emit(
            window.language_selector.findData("en" if window.language == "zh" else "zh")
        )
        assert data.preferences.read_bytes() == original
        assert "ConfigurationError" in window.event_label.toolTip()
        if kind in ("invalid_language", "external_change"):
            window.recipe_page.save_and_apply()
            until(lambda: not window.recipe_page.busy)
            assert startup.application.snapshot().state is ApplicationState.READY
            assert data.preferences.read_bytes() == original
        else:
            assert not window.recipe_page.buttons["save_apply"].isEnabled()
            assert not window.buttons["operation"].isEnabled()
            assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_startup_feedback_is_localized_with_details_only_in_tooltip(
    qapp: QApplication, tmp_path: Path, language: str
) -> None:
    data = saved_data(tmp_path)
    value = json.loads(data.preferences.read_text(encoding="utf-8"))
    value["language"] = language
    data.preferences.write_text(json.dumps(value), encoding="utf-8")
    data.sources.write_text('{"cameras":{}}', encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        summary = "相机源配置" if language == "zh" else "Camera-source configuration"
        assert summary in window.recipe_page.status.text()
        assert str(data.sources) in window.recipe_page.status.toolTip()
        assert str(data.sources) in window.event_label.toolTip()
        assert str(data.sources) not in window.recipe_page.status.text()
        assert str(data.sources) not in window.event_label.text()
        window.open_vision()
        assert window.vision_workspace is not None
        assert startup.editor is not None and startup.editor.camera_sources is not None
        assert not startup.editor.camera_sources.persisted
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert "Unknown fields" in window.recipe_page.status.toolTip()
        assert data.sources.read_text(encoding="utf-8") == '{"cameras":{}}'
    finally:
        close_window(window)
        delivery.detach()


def test_bad_unrelated_recipe_is_visible_without_blocking_valid_apply(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    (data.recipes / "broken.json").write_text("{}", encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.editor is not None and startup.editor.entry is not None
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert startup.application.snapshot().state is ApplicationState.READY
        assert "broken.json" in window.recipe_page.status.toolTip()
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
    finally:
        close_window(window)
        delivery.detach()


def test_recipe_directory_permission_error_keeps_localized_navigable_shell(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    original_iterdir = Path.iterdir

    def denied(path: Path):
        if path == data.recipes:
            raise PermissionError("test Recipe directory denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.pages.count() == 5
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert "Recipe definitions unavailable" in window.recipe_page.status.text()
        assert "directory denied" in window.recipe_page.status.toolTip()
        assert str(data.recipes) not in window.recipe_page.status.text()
        assert not window.buttons["operation"].isEnabled()
        for index in range(5):
            window.navigate(index)
    finally:
        close_window(window)
        delivery.detach()


def test_preferences_write_error_is_visible_and_retains_selection(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    before = load_preferences(data.preferences)

    def fail(_source: object, _target: object) -> None:
        raise OSError("preferences replace denied")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", fail)
    try:
        window.language_selector.activated.emit(
            window.language_selector.findData("en" if window.language == "zh" else "zh")
        )
        assert load_preferences(data.preferences) == before
        assert "ConfigurationIOError" in window.event_label.toolTip()
    finally:
        close_window(window)
        delivery.detach()


def test_source_browsing_and_recipe_apply_do_not_rewrite(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    value = json.loads(data.sources.read_text(encoding="utf-8"))
    original = json.dumps(value, indent=3).encode()
    data.sources.write_bytes(original)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.navigate(4)
        until(lambda: not window.recipe_page.busy)
        workspace = window.vision_workspace
        assert workspace is not None and workspace.frame is not None
        workspace.buttons["reference_next"].click()
        until(lambda: not window.recipe_page.busy)
        assert data.sources.read_bytes() == original
        window.navigate(1)
        window.recipe_page.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert data.sources.read_bytes() == original
        assert startup.application.snapshot().counters == ProductionCounters()
        assert startup.application.snapshot().active is None
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("size", [(1080, 1857), (1920, 1017)])
def test_language_switches_persist_without_saving_other_drafts(
    qapp: QApplication, tmp_path: Path, size: tuple[int, int]
) -> None:
    ensure_test_fonts()
    data = saved_data(tmp_path)
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.resize(*size)
    window.show()
    window.navigate(1)
    session = startup.editor
    assert session is not None
    original = session.draft.display_name
    diagnostic_host = window.communication.host.text()
    session.draft.display_name = "Unsaved recipe name"
    window.recipe_page.show_draft()
    window.communication.host.setText("127.0.0.2")
    before = startup.application.snapshot()
    recipe_bytes = {p: p.read_bytes() for p in data.recipes.glob("*.json")}
    try:
        for language in LANGUAGES:
            index = window.language_selector.findData(language)
            assert index >= 0
            window.language_selector.activated.emit(index)
            qapp.processEvents()
            until(
                lambda: (
                    cast(QWidget, window.recipe_page.summary_scroll.widget()).width()
                    <= window.recipe_page.summary_scroll.viewport().width()
                )
            )
            assert window.language == language
            saved = load_preferences(data.preferences)
            assert saved == ApplicationPreferences("local", language)
            assert session.dirty and session.draft.display_name == "Unsaved recipe name"
            assert window.communication.dirty
            assert window.communication.host.text() == "127.0.0.2"
            assert startup.application.snapshot() == before
            assert {p: p.read_bytes() for p in recipe_bytes} == recipe_bytes
            assert window.navigation[0].accessibleName() == text(
                "page_production", language
            )
            if language == "zh-TW":
                assert window.history.date.locale().name() == "zh_TW"
            assert window.width() == size[0]
            page = window.recipe_page
            for button in page.buttons.values():
                if button.isVisible():
                    assert page.rect().contains(
                        button.mapTo(page, button.rect().bottomRight())
                    ), language
    finally:
        session.draft.display_name = original
        window.recipe_page.show_draft()
        window.communication.host.setText(diagnostic_host)
        close_window(window)
        delivery.detach()
    restored = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(restored, delivery)
    try:
        assert window.language == LANGUAGES[-1]
        assert window.language_selector.currentData() == LANGUAGES[-1]
    finally:
        close_window(window)
        delivery.detach()
