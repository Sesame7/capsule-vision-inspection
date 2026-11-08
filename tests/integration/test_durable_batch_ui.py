"""Normal composition with a temporary History, accounts and synthetic devices."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.integration.test_basler_production import Engine, PassiveBackend, save_basler
from tests.support.application import WAIT
from tests.support.fins_tcp import TcpPeer
from tests.support.production import inferred
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    saved_data,
)
from tests.unit.test_alarm_monitor import event as alarm_event

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
    _basler_sdk,
)
from capsule_vision.application.alarm_records import AlarmQuery
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.identity_models import AuthorizationError
from capsule_vision.application.image_archive import camera_archive_status
from capsule_vision.application.models import PilotCounters
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.inference import BoundingRect, ImageResult
from capsule_vision.localization.catalog import text
from capsule_vision.ui import recipe_page
from capsule_vision.ui.batch_dialog import BatchDialog
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


class ExposureFindings(Engine):
    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        raw = super().infer(image_bgr)
        exposure = int(image_bgr[0, 0, 0]) // 10
        # One background region on every exposure. The first slot also has
        # a finding on exposure 1 (empty) and 3 (occupied), but not on 4.
        regions = [BoundingRect(58, 15, 2, 2)]
        if exposure in (1, 3):
            regions.append(BoundingRect(1, 1, 2, 2))
        return replace(raw, regions=inferred(*regions).regions)


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
    engine = ExposureFindings()
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
            with pytest.raises(ApplicationStateError):
                app.clear_statistics()
            for ordinal in range(1, cycles + 1):
                backend.expose(ordinal)
                until(
                    lambda ordinal=ordinal: (
                        app.snapshot().counters.cycles_completed == ordinal
                    )
                )
                observed = app.snapshot().latest_production_result
                assert observed is not None
                if ordinal in (1, 3, 4):
                    assert all(
                        camera_archive_status(camera) == {1: 3, 3: 1, 4: 0}[ordinal]
                        for camera in observed.cameras
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
            before = app.snapshot()
            assert (
                before.counters.cycles_completed == cycles
                and before.latest_production_result
            )
            assert before.pilot_counting is (mode is ReportingMode.FOUR_CYCLE)
            if before.pilot_counting:
                assert before.pilot_counters == PilotCounters(
                    batches_completed=1, channel_ok=30, channel_ng=6
                )
                assert before.batch_duration_ms is not None
                assert before.group_progress is not None
                assert peer.words(50, 3) == (0x1041, 0x4104, 0)
            packets = tuple(peer.packets)
            with pytest.raises(ApplicationStateError, match="before confirmation"):
                app.clear_statistics(expected_generation=before.generation - 1)
            assert app.snapshot() == before
            app.clear_statistics(
                revision=app.session_revision, expected_generation=before.generation
            )
            cleared = app.snapshot()
            assert (
                cleared.counters == ProductionCounters()
                and cleared.pilot_counters == PilotCounters()
            )
            assert (
                cleared.latest_production_result
                is cleared.batch_duration_ms
                is cleared.group_progress
                is None
            )
            assert cleared.pilot_counting is before.pilot_counting
            assert (
                cleared.batch_id is cleared.batch_uuid is None
                and not cleared.unbatched_run_uncertain
            )
            assert tuple(peer.packets) == packets
            assert app.history is not None
            history = sorted(
                app.history.query().result(WAIT).records,
                key=lambda record: record.cycle_id,
            )
            assert len(history) == cycles
            assert history[0].judgment == "OK" and all(
                c.business == "EMPTY" and not c.reasons for c in history[0].channels
            )
            if cycles == 4:
                assert history[2].judgment == "NG" and history[3].judgment == "OK"
                assert [
                    c.channel
                    for c in history[2].channels
                    if "defect_region" in c.reasons
                ] == [1, 7, 13, 19, 25, 31]
            app.start_batch("Later").result(WAIT)
            with pytest.raises(ApplicationStateError):
                app.clear_statistics()
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
        if cycles == 4:
            finals = db.execute("SELECT final_channels FROM plc_groups").fetchone()
            assert json.loads(finals[0]) == [
                [c, "NG" if c % 6 == 1 else "OK"] for c in range(1, 37)
            ]
        assert members == (
            [(run, str(i), i) for i in range(1, 5)] if cycles == 4 else []
        )
    with closing(sqlite3.connect(data.audit)) as audit:
        assert audit.execute(
            "SELECT username,target,old_value,new_value FROM operations JOIN completions USING(id) "
            "WHERE action='production.clear_statistics' AND outcome='success'"
        ).fetchall() == [
            (
                "TestAdmin",
                "unbatched",
                f"cycles={cycles}, groups={int(cycles == 4)}",
                "cycles=0, groups=0",
            )
        ]


def test_unbatched_clear_preserves_prior_audit_and_alarm_records(
    tmp_path: Path,
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    try:
        history, identity = app.history, app.identity
        assert history is not None and identity is not None
        history.offer_alarms((alarm_event(history.clock(), "prior-alarm"),))
        alarms = app.alarm_page(AlarmQuery()).result(WAIT)
        audit = identity.audit.page(AuditQuery()).result(WAIT)
        assert alarms and audit
        app.clear_statistics()
        assert app.alarm_page(AlarmQuery()).result(WAIT) == alarms
        latest, *prior = identity.audit.page(AuditQuery()).result(WAIT)
        assert tuple(prior) == audit
        assert (
            latest.operation.action == "production.clear_statistics"
            and latest.outcome == "success"
        )
    finally:
        close(startup)


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
        app.clear_statistics()
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
            dialog.secondary.click()
            return int(dialog.action == "end")

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


def test_production_batch_setup_metadata_enter_continue_and_dismissal(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from capsule_vision.application.batch_review import BatchInformation

    def labels(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("Chip",)

    monkeypatch.setattr(recipe_page, "read_model_labels", labels)
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    assert app.history is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        until(lambda: window.buttons["batch"].isEnabled())

        def create(dialog: BatchDialog) -> int:
            dialog.show()
            dialog.label.setText(" New Batch ")
            until(lambda: dialog.select.isEnabled())
            assert dialog.expected is not None and dialog.expected.uuid is None
            assert dialog.mode.text() == text("batch_create", dialog.language)
            for field, value in zip(
                dialog.inputs, ("Capsules A", "Size 0", "Day shift"), strict=True
            ):
                field.setText(value)
            QTest.keyClick(dialog.inputs[-1], Qt.Key.Key_Return)
            return dialog.result()

        monkeypatch.setattr(BatchDialog, "exec", create)
        window.buttons["batch"].click()
        until(
            lambda: (
                window.snapshot.batch_id == "New Batch"
                and not window.snapshot.batch_pending
            )
        )
        uid = app.snapshot().batch_uuid
        assert uid
        saved = app.history.batch_detail(uid).result(WAIT)
        assert saved.info == BatchInformation("Capsules A", "Size 0", "Day shift")

        def edit(dialog: BatchDialog) -> int:
            dialog.show()
            until(lambda: dialog.select.isEnabled())
            assert dialog.expected and dialog.expected.uuid == uid
            assert [f.text() for f in dialog.inputs] == [
                "Capsules A",
                "Size 0",
                "Day shift",
            ]
            assert dialog.mode.text() == text("batch_continue", dialog.language)
            dialog.inputs[2].setText("Evening shift")
            QTest.mouseClick(dialog.select, Qt.MouseButton.LeftButton)
            return dialog.result()

        monkeypatch.setattr(BatchDialog, "exec", edit)
        window.buttons["batch"].click()
        until(lambda: not app.snapshot().batch_pending)
        assert app.snapshot().batch_uuid == uid
        edited = app.history.batch_detail(uid).result(WAIT)
        assert (
            edited.info.notes == "Evening shift"
            and edited.batch.totals == saved.batch.totals
        )
        for closing in ("escape", "close"):

            def dismiss(dialog: BatchDialog, how: str = closing) -> int:
                dialog.show()
                until(lambda: dialog.select.isEnabled())
                dialog.inputs[0].setText("Unsaved")
                if how == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    dialog.close()
                return dialog.result()

            monkeypatch.setattr(BatchDialog, "exec", dismiss)
            window.buttons["batch"].click()
            assert app.history.batch_detail(uid).result(WAIT) == edited
            assert app.snapshot().batch_uuid == uid
    finally:
        close_window(window)
        delivery.detach()


def test_batch_metadata_lookup_is_bounded_and_cancel_does_not_publish(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import Future

    from capsule_vision.application.batch_review import BatchInformation, BatchSetup

    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app = startup.application
    assert app.history is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    pending: list[tuple[str, Future[BatchSetup]]] = []

    def lookup(label: str) -> Future[BatchSetup]:
        assert not pending or pending[-1][1].done()
        future: Future[BatchSetup] = Future()
        pending.append((label, future))
        return future

    monkeypatch.setattr(app.history, "batch_setup", lookup)
    before = app.snapshot()
    dialog = BatchDialog(before, "en", window, app)
    try:
        dialog.show()
        dialog.label.setText("A")
        until(lambda: len(pending) == 1)
        dialog.label.setText("B")
        assert not dialog.select.isEnabled()
        pending[0][1].set_result(BatchSetup("old", BatchInformation("Wrong product")))
        until(lambda: len(pending) == 2)
        assert pending[1][0] == "B" and not dialog.inputs[0].text()
        pending[1][1].set_result(
            BatchSetup("batch-b", BatchInformation("Correct product"))
        )
        until(dialog.select.isEnabled)
        assert dialog.inputs[0].text() == "Correct product"
        dialog.label.setText("C")
        until(lambda: len(pending) == 3)
        pending[2][1].set_exception(OSError("Unavailable"))
        until(lambda: dialog.mode.text() == text("history_unavailable", "en"))
        assert not dialog.select.isEnabled()
        qapp.processEvents()
        assert len(pending) == 3  # A failed read never continuously retries.
        dialog.label.setText("D")
        until(lambda: len(pending) == 4)
        dialog.close()
        pending[3][1].set_result(BatchSetup(None, BatchInformation()))
        qapp.processEvents()
        assert app.snapshot() == before
    finally:
        dialog.close()
        dialog.deleteLater()
        close_window(window)
        delivery.detach()
