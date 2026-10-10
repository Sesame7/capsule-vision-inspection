"""Normal shell restoration and successful Apply preferences, with no real SDK."""

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.qt_support import until
from tests.unit.test_application_bootstrap import close, saved_data
from tests.unit.test_production_application import WAIT

from vimo_demo.application import ApplicationError, ApplicationState, ProductionCounters
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.application.preferences import (
    ApplicationPreferences,
    load_preferences,
    save_preferences,
)
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    RecipeDefinition,
    save_camera_sources,
    save_recipe,
)
from vimo_demo.inspection import CameraId
from vimo_demo.ui.bootstrap import ApplicationData, create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_window import ProductionWindow


def close_window(window: ProductionWindow) -> None:
    window.close()
    until(lambda: window.bridge.closed)
    assert window.bridge.application.join(1)
    window.deleteLater()


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
        window.language_button.click()
        assert window.language == "en"
        preferences = load_preferences(startup.data.preferences)
        assert preferences.language == "en"
        assert (
            preferences.default_model_id is preferences.last_applied_recipe_id is None
        )
        assert not startup.data.models.exists() and not startup.data.machine.exists()
    finally:
        close_window(window)
        delivery.detach()


def test_saved_apply_language_restart_and_no_production_side_effects(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_recipe(
        data.recipes / "second.recipe.json", RecipeDefinition("second", "Second")
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.parameters.recipes.currentText() == "Local test"
        window.navigate(1)
        window.parameters.recipes.activated.emit(
            window.parameters.recipes.findText("Second")
        )
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        assert startup.editor is not None
        startup.editor.save()  # Save alone must not change restored selection.
        assert load_preferences(data.preferences).last_applied_recipe_id == "local"
        window.parameters.save_and_apply()
        until(
            lambda: (
                load_preferences(data.preferences).last_applied_recipe_id == "second"
            )
        )
        assert startup.application.snapshot().candidate is not None
        window.language_button.click()
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
        assert restored.choice is not None
        assert restored.choice.prepared.configuration.recipe_id == "second"
        assert (
            restored.preferences is not None and restored.preferences.language == "zh"
        )
        assert restored.machine == startup.machine
    finally:
        close(restored)


def test_failed_preparation_never_updates_last_recipe(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_recipe(
        data.recipes / "second.recipe.json", RecipeDefinition("second", "Second")
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.parameters.recipes.activated.emit(
            window.parameters.recipes.findText("Second")
        )
        (data.models / "general.vimosln").write_bytes(b"changed after startup")
        window.parameters.save_and_apply()
        until(
            lambda: not window.parameters.busy and bool(window.parameters.status.text())
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
    save_preferences(data.preferences, ApplicationPreferences("general", None, "zh"))
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.editor is not None and startup.editor.entry is None
        assert window.parameters.recipes.currentIndex() == -1
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        window.parameters.recipes.activated.emit(0)
        window.parameters.save_and_apply()
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
        window.parameters.open_roi_editor()
        assert window.parameters.roi_workspace is not None
        until(lambda: not window.parameters.busy)
        assert window.parameters.editable()
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
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
        "invalid_language": '{"schema_version":1,"default_model_id":"general","last_applied_recipe_id":"local","language":"invalid"}',
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
        window.language_button.click()
        assert data.preferences.read_bytes() == original
        assert "ConfigurationError" in window.event_label.toolTip()
        if kind in ("invalid_language", "external_change"):
            window.parameters.save_and_apply()
            until(lambda: not window.parameters.busy)
            assert startup.application.snapshot().state is ApplicationState.READY
            assert data.preferences.read_bytes() == original
        else:
            assert not window.parameters.buttons["save_apply"].isEnabled()
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
    data.sources.write_text("{}", encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        summary = "相机源配置" if language == "zh" else "Camera-source configuration"
        assert summary in window.parameters.status.text()
        assert str(data.sources) in window.parameters.status.toolTip()
        assert str(data.sources) in window.event_label.toolTip()
        assert str(data.sources) not in window.parameters.status.text()
        assert str(data.sources) not in window.event_label.text()
        window.parameters.open_roi_editor()
        assert window.parameters.roi_workspace is not None
        assert startup.editor is not None and startup.editor.camera_sources is None
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert "Saved camera sources" in window.parameters.status.toolTip()
    finally:
        close_window(window)
        delivery.detach()


def test_bad_recipe_catalog_cannot_be_bypassed_by_applying_valid_entry(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    (data.recipes / "broken.recipe.json").write_text("{}", encoding="utf-8")
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.editor is not None and startup.editor.entry is not None
        window.parameters.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert "invalid or duplicate" in window.parameters.status.toolTip()
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
        assert "Recipe definitions unavailable" in window.parameters.status.text()
        assert "directory denied" in window.parameters.status.toolTip()
        assert str(data.recipes) not in window.parameters.status.text()
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

    monkeypatch.setattr("vimo_demo.application.preferences.os.replace", fail)
    try:
        window.language_button.click()
        assert load_preferences(data.preferences) == before
        assert "ConfigurationIOError" in window.event_label.toolTip()
    finally:
        close_window(window)
        delivery.detach()
