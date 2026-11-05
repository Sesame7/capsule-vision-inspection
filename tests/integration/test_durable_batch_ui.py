"""Normal composition with a temporary History, accounts and synthetic devices."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.integration.test_basler_production import Engine, PassiveBackend, save_basler
from tests.support.application import WAIT
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    saved_data,
)

from capsule_vision.application import ApplicationError, ApplicationState, _basler_sdk
from capsule_vision.application.identity_models import AuthorizationError
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.localization.catalog import text
from capsule_vision.ui import recipe_page
from capsule_vision.ui.batch_dialog import BatchDialog
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


@pytest.mark.parametrize(
    "mode", [None, ReportingMode.PER_CYCLE, ReportingMode.FOUR_CYCLE]
)
def test_unbatched_basler_run_keeps_its_cycles_and_eligible_group(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: ReportingMode | None,
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
    assert isinstance(app, SourceProductionApplication)
    cycles = 4 if mode is ReportingMode.FOUR_CYCLE else 1
    with TcpPeer() as peer:
        try:
            settings = ReportingSettings(
                FinsTcpEndpoint(port=peer.port), mode=mode or ReportingMode.FOUR_CYCLE
            )
            app.set_reporting_selection(ReportingSelection(mode is not None, settings))
            peer.set_words(48, (7,))
            app.start().result(WAIT)
            snapshot = app.snapshot()
            run = snapshot.run_id
            assert run and snapshot.batch_id is None and snapshot.batch_uuid is None
            assert not snapshot.unbatched_run_uncertain
            for ordinal in range(1, cycles + 1):
                backend.expose(ordinal)
                until(
                    lambda ordinal=ordinal: (
                        app.snapshot().counters.cycles_completed == ordinal
                    )
                )
                if mode is ReportingMode.FOUR_CYCLE and ordinal == 1:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.batch_observations
                            and s.batch_observations[-1].reads
                        )
                    )
            if mode is not None:
                until(
                    lambda: bool(
                        (s := app.reporting_snapshot()) and s.acknowledged_count == 1
                    )
                )
            else:
                assert not peer.packets
            app.stop().result(WAIT)
            assert not app.snapshot().unbatched_run_uncertain
            app.start_batch("Later").result(WAIT)
            assert app.snapshot().counters.cycles_completed == 0
            assert app.snapshot().pilot_counters.batches_completed == 0
        finally:
            close(startup)

    with closing(sqlite3.connect(data.history)) as db:
        assert db.execute("SELECT label FROM batches").fetchall() == [("Later",)]
        assert db.execute(
            "SELECT uuid,batch,outcome,reporting_mode,uncertain FROM runs"
        ).fetchall() == [(run, None, "stopped", mode.value if mode else "disabled", 0)]
        assert db.execute(
            "SELECT run_id,batch_label,cycle_id FROM cycles ORDER BY id"
        ).fetchall() == [(run, None, str(i)) for i in range(1, cycles + 1)]
        groups = db.execute(
            "SELECT run,first_cycle,plc_counter FROM plc_groups"
        ).fetchall()
        members = db.execute(
            "SELECT run,cycle,slot FROM group_members ORDER BY slot"
        ).fetchall()
        assert groups == ([(run, "1", 7)] if cycles == 4 else [])
        assert members == (
            [(run, str(i), i) for i in range(1, 5)] if cycles == 4 else []
        )


def test_unbatched_start_with_unavailable_history_is_visible(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    data.history.write_bytes(b"unreadable test database")
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    engine = Engine()
    engine.release_warm.set()
    startup = authenticated_startup(
        data, engine_factory=lambda _: engine, select_basler_batch=False
    )
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    app = startup.application
    try:
        window.show()
        until(lambda: window.buttons["operation"].isEnabled())
        window.buttons["operation"].click()
        until(lambda: window.snapshot.state is ApplicationState.RUNNING)
        assert window.snapshot.unbatched_run_uncertain and window.snapshot.run_id
        assert window.event_label.accessibleName() == text(
            "run_uncertain", window.language
        )
        backend.expose(1)
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        app.stop().result(WAIT)
        assert app.snapshot().unbatched_run_uncertain
        assert app.history and app.history.snapshot().persisted == 0
    finally:
        close_window(window)
        delivery.detach()
    assert data.history.read_bytes() == b"unreadable test database"


def test_named_basler_start_requires_available_storage_and_committed_run(
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
