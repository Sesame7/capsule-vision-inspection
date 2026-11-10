"""Page-local feedback and evidence-based retirement of startup warnings."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.recipe_ui import edit, select_production_recipe
from tests.support.startup import authenticated_startup, saved_data

from capsule_vision.application import ApplicationState, ProductionCounters
from capsule_vision.application.startup import Startup
from capsule_vision.configuration import ConfigurationIOError, load_recipe
from capsule_vision.configuration.communication_preferences import (
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow

INVALID_DOCUMENTS = ('{"schema_version":99}', '{"status":{"enabled":false}}')


@contextmanager
def feedback_window(
    root: Path, document: str, *, source_error: bool = False
) -> Generator[tuple[Startup, MainWindow]]:
    data = saved_data(root)
    data.communication.write_text(document, encoding="utf-8")
    if source_error:
        data.sources.write_text('{"cameras":{}}', encoding="utf-8")
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    ensure_test_fonts()
    window = create_window(startup, delivery)
    window.resize(1080, 1920)
    window.show()
    try:
        until(lambda: not window.recipe_page.busy)
        yield startup, window
    finally:
        until(lambda: not window.editing.busy and not window.communication.busy)
        if startup.editor is not None:
            startup.editor.discard()
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("document", ("{}", *INVALID_DOCUMENTS))
@pytest.mark.parametrize("language", ("zh", "en"))
def test_recipe_save_feedback_is_independent_of_communication(
    qapp: QApplication, tmp_path: Path, document: str, language: Language
) -> None:
    with feedback_window(tmp_path, document) as (startup, window):
        window.set_language(language)
        window.navigate(1)
        page = window.recipe_page
        candidate = startup.application.snapshot().candidate_prepared
        before = startup.data.communication.read_bytes()
        edit(page.metadata["display_name"], "Saved independently")
        page.buttons["save"].click()
        until(lambda: not page.busy)
        assert load_recipe(startup.data.recipes / "local.json").display_name == (
            "Saved independently"
        )
        assert page.status.text() == text("saved", language)
        assert not page.status.toolTip()
        assert startup.data.communication.read_bytes() == before
        snapshot = startup.application.snapshot()
        assert snapshot.candidate_prepared is candidate and snapshot.active is None
        assert snapshot.counters == ProductionCounters()
        assert snapshot.state is ApplicationState.READY
        if startup.communication_error:
            assert "startup_communication" in window.configuration_messages
            assert startup.communication_error in window.event_label.toolTip()
            assert window.event_label.isVisible() and window.event_label.text()
            assert text("startup_communication", language) in (
                window.event_label.accessibleName()
            )
            assert startup.communication_error in (
                window.communication.preferences_status.toolTip()
            )
        else:
            assert "startup_communication" not in window.configuration_messages


@pytest.mark.parametrize("succeeds", (True, False))
def test_recipe_preparation_cannot_resolve_communication_warning(
    qapp: QApplication, tmp_path: Path, succeeds: bool
) -> None:
    with feedback_window(tmp_path, INVALID_DOCUMENTS[0]) as (startup, window):
        candidate = startup.application.snapshot().candidate_prepared
        applied: list[object] = []
        window.editing.applied.connect(applied.append)
        if not succeeds:
            (startup.data.models / "general/model.vimosln").write_bytes(b"invalid")
        select_production_recipe(window)
        until(lambda: not window.recipe_page.busy)
        assert bool(applied) is succeeds
        if not succeeds:
            assert startup.application.snapshot().candidate_prepared is candidate
        window.navigate(1)
        assert "startup_communication" in window.configuration_messages
        assert startup.communication_error in window.event_label.toolTip()
        assert startup.communication_error not in window.recipe_page.status.toolTip()
        assert text("startup_communication", window.language) not in (
            window.recipe_page.status.text()
        )


@pytest.mark.parametrize("operation", ("save", "reload"))
@pytest.mark.parametrize("document", INVALID_DOCUMENTS)
@pytest.mark.parametrize("source_error", (False, True))
def test_validated_communication_resolution_updates_only_its_warning(
    qapp: QApplication,
    tmp_path: Path,
    operation: str,
    document: str,
    source_error: bool,
) -> None:
    with feedback_window(tmp_path, document, source_error=source_error) as (
        startup,
        window,
    ):
        page = window.communication
        other_messages = tuple(
            key
            for key in window.configuration_messages
            if key != "startup_communication"
        )
        window.navigate(2)
        assert startup.communication_error in window.event_label.toolTip()
        assert startup.communication_error not in window.recipe_page.status.toolTip()
        if source_error:
            assert str(startup.data.sources) in window.recipe_page.status.toolTip()
        if operation == "save":
            page.save_settings.click()
            until(lambda: not page.busy)
        else:
            # Simulate an explicit external repair, then the existing confirmed reload.
            save_communication_preferences(
                startup.data.communication, startup.communication
            )
            page.reload_settings()
        assert load_communication_preferences(startup.data.communication) == (
            startup.communication
        )
        # No timer, page change, new window or Recipe preparation is needed.
        assert window.configuration_messages == other_messages
        assert startup.communication_error not in window.event_label.toolTip()
        assert not page.preferences_status.toolTip()
        if source_error:
            assert "startup_sources" in other_messages
            assert str(startup.data.sources) in window.event_label.toolTip()
            assert str(startup.data.sources) in window.recipe_page.status.toolTip()
        else:
            assert not window.configuration_issue
            assert not window.event_label.toolTip()
        window.set_language("zh")
        assert "startup_communication" not in window.configuration_messages
        assert startup.communication_error not in window.event_label.toolTip()


@pytest.mark.parametrize(
    "operation",
    ("draft", "invalid_save", "failed_write", "invalid_reload", "missing_reload"),
)
def test_unconfirmed_communication_changes_keep_startup_warning(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with feedback_window(tmp_path, INVALID_DOCUMENTS[1]) as (startup, window):
        page = window.communication
        before = startup.data.communication.read_bytes()
        page.web_panel.port.setValue(9000)
        if operation == "invalid_save":
            page.web_panel.host.setText("0.0.0.0")
            page.save_settings.click()
        elif operation == "failed_write":

            def denied(*_args: object) -> None:
                raise ConfigurationIOError("Controlled write failure")

            monkeypatch.setattr(
                "capsule_vision.ui.communication_page.save_communication_preferences",
                denied,
            )
            page.save_settings.click()
        elif operation in ("invalid_reload", "missing_reload"):
            if operation == "missing_reload":
                startup.data.communication.unlink()  # Isolated test fixture only.
            page.reload_settings()
        until(lambda: not page.busy)
        assert page.dirty and page.web_panel.port.value() == 9000
        assert "startup_communication" in window.configuration_messages
        assert startup.communication_error in window.event_label.toolTip()
        if operation != "draft":
            assert page.preferences_status.toolTip()
        if operation != "missing_reload":
            assert startup.data.communication.read_bytes() == before


def test_recipe_save_failure_keeps_its_local_error_and_global_communication_warning(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with feedback_window(tmp_path, INVALID_DOCUMENTS[0]) as (startup, window):
        page = window.recipe_page
        window.navigate(1)
        before = (startup.data.recipes / "local.json").read_bytes()
        edit(page.metadata["display_name"], "Must not be saved")

        def denied(*_args: object, **_kwargs: object) -> None:
            raise ConfigurationIOError("Recipe write denied")

        monkeypatch.setattr(
            "capsule_vision.configuration.catalog.RecipeCatalog.save", denied
        )
        page.save()
        until(lambda: not page.busy)
        assert page.status.text() == text("recipe_save_error", window.language)
        assert "Recipe write denied" in page.status.toolTip()
        assert startup.communication_error not in page.status.toolTip()
        assert (startup.data.recipes / "local.json").read_bytes() == before
        assert startup.communication_error in window.event_label.toolTip()
