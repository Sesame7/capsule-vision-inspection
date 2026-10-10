"""Normal shell restoration and successful Apply preferences, with no real SDK."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.qt_support import until
from tests.unit.test_application_bootstrap import close, saved_data

from vimo_demo.application import ApplicationState, ProductionCounters
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


def test_unavailable_sources_cannot_become_ready_by_apply(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    save_camera_sources(
        data.sources,
        CameraSources(
            tuple(CameraFolder(CameraId(f"CAM{i}"), "missing") for i in range(1, 7))
        ),
    )
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        window.parameters.save_and_apply()
        until(
            lambda: (
                not window.parameters.busy
                and "missing" in window.parameters.status.text()
            )
        )
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
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
