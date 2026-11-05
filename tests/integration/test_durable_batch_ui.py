"""Normal composition with a temporary History, accounts and synthetic devices."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.integration.test_basler_production import Engine, PassiveBackend, save_basler
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    saved_data,
)

from capsule_vision.application import ApplicationError, ApplicationState, _basler_sdk
from capsule_vision.application.identity_models import AuthorizationError
from capsule_vision.ui import recipe_page
from capsule_vision.ui.batch_dialog import BatchDialog
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_formal_start_requires_selected_batch_and_committed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    engine = Engine()
    engine.release_warm.set()
    startup = authenticated_startup(
        data, engine_factory=lambda _: engine, select_basler_batch=False
    )
    app = startup.application
    try:
        with pytest.raises(ApplicationError, match="Select a Production Batch"):
            app.start().result(WAIT)
        assert not backend.prepared and app.snapshot().run_id is None
        app.start_batch("Test Lot").result(WAIT)
        app.start().result(WAIT)
        assert app.snapshot().run_id is not None
        app.stop().result(WAIT)
        assert app.history is not None
        app.history.request_stop()
        assert app.history.join(WAIT)
        with pytest.raises(ApplicationError, match="storage"):
            app.start().result(WAIT)
        assert (
            app.snapshot().state is ApplicationState.READY
            and app.snapshot().run_id is None
        )
    finally:
        close(startup)


def test_batch_dialog_revision_confirmation_and_restart(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def labels(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("Chip",)

    monkeypatch.setattr(recipe_page, "read_model_labels", labels)
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app = startup.application
    actor = app.identity
    assert actor is not None
    app.start_batch("Current").result(WAIT)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.bridge.refresh()

        def stale(dialog: BatchDialog) -> int:
            assert dialog.label.text() == "Current"
            dialog.label.setText("Superseded")
            actor.logout()
            return 1

        monkeypatch.setattr(BatchDialog, "exec", stale)
        window.buttons["batch"].click()
        assert app.snapshot().batch_id == "Current"
        with pytest.raises(AuthorizationError):
            app.end_batch()
        actor.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        until(lambda: window.buttons["batch"].isEnabled())

        def choose(dialog: BatchDialog) -> int:
            dialog.label.setText("  Next  ")
            return 1

        monkeypatch.setattr(BatchDialog, "exec", choose)
        window.buttons["batch"].click()
        until(lambda: window.snapshot.batch_id == "Next")
        assert window.batch_label.accessibleName() == "Batch Next"
    finally:
        close_window(window)
        delivery.detach()

    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        until(lambda: window.snapshot.batch_id == "Next")

        def end(dialog: BatchDialog) -> int:
            dialog.end.click()
            return int(dialog.end_requested)

        monkeypatch.setattr(BatchDialog, "exec", end)

        def confirm(prompt: QMessageBox) -> int:
            button = next(b for b in prompt.buttons() if prompt.buttonRole(b) == role)
            button.click()
            return 0

        monkeypatch.setattr(QMessageBox, "exec", confirm)
        role = QMessageBox.ButtonRole.RejectRole
        window.buttons["batch"].click()
        assert window.snapshot.batch_id == "Next"
        role = QMessageBox.ButtonRole.AcceptRole
        window.buttons["batch"].click()
        until(lambda: window.snapshot.batch_id is None)
    finally:
        close_window(window)
        delivery.detach()
