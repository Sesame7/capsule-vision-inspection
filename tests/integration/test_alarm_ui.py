"""Guest startup monitoring, authorized saves and History in the real shell."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from threading import enumerate as threads

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QMenu
from tests.support.application import WAIT, ReplayEngine
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)

from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery, AlarmRecord
from capsule_vision.application.audit import (
    AuditOperation,
    AuditQuery,
    AuditUnavailable,
)
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.startup import load_startup
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.configuration import ConfigurationError
from capsule_vision.configuration.alarm_definitions import TEMPLATE
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_guest_close_waits_for_the_inflight_alarm_owner(tmp_path: Path) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        peer.hold_next("read")
        saved = replace(
            CommunicationPreferences(),
            status=StatusPreferences(True, FinsTcpEndpoint(port=peer.port)),
        )
        save_communication_preferences(data.communication, saved)
        app = load_startup(data).application
        try:
            assert peer.entered.wait(3)
            done = Event()
            closed = app.close()
            closed.add_done_callback(lambda _: done.set())
            assert not done.wait(0.04)
        finally:
            peer.release.set()
            app.close().result(WAIT)
            assert app.join(WAIT)
        assert all(t.name != "PLCStatusService" for t in threads())


def test_saved_monitor_runs_across_guest_start_stop_logout_and_saved_changes(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    initialize_administrator(data.security, "Admin", TEST_PASSWORD)
    with TcpPeer() as first, TcpPeer() as second:
        saved = replace(
            CommunicationPreferences(),
            status=StatusPreferences(True, FinsTcpEndpoint(port=first.port)),
        )
        save_communication_preferences(data.communication, saved)
        first.set_words(18, (4,))
        second.set_words(211, (1 << 7,))
        startup = load_startup(data, engine_factory=lambda _: ReplayEngine())
        app = startup.application
        identity = app.identity
        assert identity and app.history
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        page, bar = window.communication, window.event_frame
        try:
            window.resize(1080, 1920)
            window.show()
            until(
                lambda: (
                    app.alarm_snapshot().state == "online"
                    and "Emergency stop" in bar.alarm.toolTip()
                )
            )
            assert identity.snapshot().role is Role.GUEST and bar.height() == 44
            width = bar.alarm.width()
            assert (
                not page.alarm_enabled.isEnabled()
                and not window.history_tabs.isTabEnabled(3)
            )
            for command in (app.create_alarm_definitions, app.alarm_definitions_path):
                with pytest.raises(AuthorizationError):
                    command()
            assert not page.alarm_panel.findChildren(QMenu)
            with pytest.raises(AuthorizationError):
                app.alarm_page(AlarmQuery())
            with pytest.raises(AuthorizationError):
                app.history.alarm_page(AlarmQuery())
            app.start().result(WAIT)
            app.stop().result(WAIT)
            assert app.alarm_snapshot().active[0].id == "emergency_stop"
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            provision_account(identity, "Line", Role.OPERATOR, TEST_PASSWORD)
            provision_account(identity, "Tech", Role.ENGINEER, TEST_PASSWORD)
            identity.login("Line", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            assert (
                not page.alarm_enabled.isEnabled()
                and window.history_tabs.isTabEnabled(3)
            )
            with pytest.raises(AuthorizationError):
                app.create_alarm_definitions()
            assert not window.history_tabs.isTabEnabled(2)
            app.start_batch("Batch P").result(WAIT)
            identity.login("Tech", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(app.create_alarm_definitions).result(WAIT)
            path = app.alarm_definitions_path()
            assert path.read_bytes() == TEMPLATE.read_bytes()
            assert len(app.alarm_page(AlarmQuery()).result(WAIT)) == 1
            # Editing while live does not reactivate it. A saved target change does.
            content = path.read_text(encoding="utf-8").replace(
                '"en": "Start inhibited: homing required"',
                '"en": "Start inhibited: homing required", "translations": {"de": "Referenzfahrt erforderlich"}',
            )
            path.write_text(content, encoding="utf-8")
            page.alarm_port.setValue(second.port)
            page.alarm_address.setValue(211)
            assert page.alarm_address.value() == 211
            assert (
                page.dirty
                and not second.packets
                and load_communication_preferences(data.communication) == saved
            )
            page.reload_settings()
            assert (
                page.alarm_port.value() == first.port
                and page.alarm_address.value() == 18
            )
            page.alarm_port.setValue(second.port)
            page.alarm_address.setValue(211)
            page.persist_settings()
            until(
                lambda: (
                    not page.busy
                    and bool(app.alarm_snapshot().active)
                    and app.alarm_snapshot().active[0].id == "homing_required"
                )
            )
            assert (
                load_communication_preferences(data.communication).status.word_address
                == 211
            )
            assert app.alarm_snapshot().selection.word_address == 211
            window.set_language("de")
            until(lambda: "Referenzfahrt erforderlich" in bar.alarm.toolTip())
            assert "Referenzfahrt erforderlich" in bar.alarm.accessibleDescription()
            ops = identity.audit_page(AuditQuery(action="communication.save")).result(
                WAIT
            )
            assert (
                ops[0].operation.username == "Tech"
                and "alarm.station" not in ops[0].operation.after
                and "status.word_address" in ops[0].operation.after
            )
            events = app.alarm_page(AlarmQuery()).result(WAIT)
            assert events[0].event.batch_label == "Batch P"
            assert events[0].event.label("de") == "Referenzfahrt erforderlich"
            assert events[-1].event.label("de") == "Emergency stop"
            assert events[0].event.batch_id == app.snapshot().batch_uuid
            assert events[-1].event.batch_id is None
            identity.logout()
            window.identity_controls.refresh()
            second.set_words(211, (0,))
            until(
                lambda: (
                    app.alarm_snapshot().state == "online"
                    and not app.alarm_snapshot().active
                )
            )
            assert bar.height() == 44 and bar.alarm.width() == width
            page.alarm_enabled.setChecked(False)
            page.persist_settings()
            assert load_communication_preferences(data.communication).status.enabled
            assert (
                app.alarm_snapshot().state == "online"
                and not window.history_tabs.isTabEnabled(3)
            )
            identity.login("Tech", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            page.persist_settings()
            until(lambda: not page.busy and app.alarm_snapshot().state == "off")
            assert not load_communication_preferences(data.communication).status.enabled
        finally:
            close_window(window)
            delivery.detach()


def test_alarm_file_commands_remain_authorized_without_a_card_menu(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app, identity = startup.application, startup.application.identity
    assert identity is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page, path = window.communication, app.alarm_definitions_path()
    try:
        assert not page.alarm_panel.findChildren(QMenu)
        page.alarm_port.setValue(10001)
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert (
                worker.submit(app.preview_alarm_definitions).result(WAIT).origin
                == "bundled"
            )
            worker.submit(app.create_alarm_definitions).result(WAIT)
            assert path.read_bytes() == TEMPLATE.read_bytes() and page.dirty
            with pytest.raises(FileExistsError):
                worker.submit(app.create_alarm_definitions).result(WAIT)
            path.write_bytes(b"{broken")
            with pytest.raises(ConfigurationError):
                worker.submit(app.create_alarm_definitions).result(WAIT)
            assert path.read_bytes() == b"{broken"
            with pytest.raises(ConfigurationError):
                worker.submit(app.preview_alarm_definitions).result(WAIT)
        ops = identity.audit_page(AuditQuery(action="alarm_definitions.create")).result(
            WAIT
        )
        assert [row.outcome for row in ops] == ["failed", "failed", "success"]
        assert all(
            row.operation.target == path.name and row.operation.username == "TestAdmin"
            for row in ops
        )
        assert not data.communication.exists() and app.alarm_snapshot().state == "off"
        identity.logout()
        with pytest.raises(AuthorizationError):
            app.alarm_definitions_path()
        with pytest.raises(AuthorizationError):
            app.create_alarm_definitions()
        assert path.read_bytes() == b"{broken"
    finally:
        close_window(window)
        delivery.detach()


def test_creating_alarm_file_requires_durable_intent_and_current_session(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, identity = startup.application, startup.application.identity
    assert identity is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    path = app.alarm_definitions_path()
    try:
        gate: Future[None] = Future()
        entered = Event()

        def pending(_op: AuditOperation) -> Future[None]:
            entered.set()
            return gate

        with monkeypatch.context() as held, ThreadPoolExecutor(max_workers=1) as worker:
            held.setattr(identity.audit, "intent", pending)
            creating = worker.submit(app.create_alarm_definitions)
            try:
                until(entered.is_set)
                identity.logout()
                window.identity_controls.refresh()
            finally:
                gate.set_result(None)
            with pytest.raises(StaleSessionError):
                creating.result(WAIT)
            assert not path.exists()
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)

        def no_audit(_op: AuditOperation) -> Future[None]:
            unavailable: Future[None] = Future()
            unavailable.set_exception(AuditUnavailable("isolated audit failure"))
            return unavailable

        monkeypatch.setattr(identity.audit, "intent", no_audit)
        with pytest.raises(AuditUnavailable):
            app.create_alarm_definitions()
        assert not path.exists()
    finally:
        close_window(window)
        delivery.detach()


def test_alarm_review_scoped_filters_pagination_and_stale_delivery(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, delivery = startup.application, FutureDelivery()
    history, identity = app.history, app.identity
    assert history and identity
    today = datetime.now().astimezone()
    when = today.timestamp()
    long_label = "Reciprocating axis stroke not set after the secondary transfer station returned to its reference position"
    provision_account(identity, "Line", Role.OPERATOR, TEST_PASSWORD)
    for i in range(57):
        history.offer_alarms(
            (
                AlarmEvent(
                    when,
                    "reciprocating_stroke_unset",
                    "observed_active",
                    "startup",
                    "往复轴行程未设置",
                    long_label if i == 55 else "Reciprocating axis stroke not set",
                    "fins_tcp://127.0.0.1:9600",
                    2,
                    118,
                    5,
                    "equipment/v1 SHA-256 fixture",
                    "stable-id" if i else "other-id",
                    "P" if i else "Q",
                    detail="Reciprocating axis stroke not set" if i == 56 else "",
                    translations=(("de", "Hub nicht eingestellt"),),
                ),
            )
        )
    identity.login("Line", TEST_PASSWORD).result(WAIT)
    window = create_window(startup, delivery)
    page = window.alarm_page
    try:
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(3)
        until(lambda: len(page.rows) == 50)
        page.kind.setCurrentIndex(page.kind.findData("startup"))
        window.records_filters.start.set_date(QDate(today.year, today.month, today.day))
        window.records_filters.end.set_date(QDate(today.year, today.month, today.day))
        window.records_filters.select_batch("stable-id", "P")
        page.refresh()
        until(lambda: len(page.rows) == 50 and page.older.isEnabled())
        newer = page.rows
        # Compare languages at one viewport. Other pages' translated minimum
        # sizes can grow a normally shown test window during a language switch.
        page.table.setFixedWidth(page.table.width())
        columns = [page.table.columnWidth(col) for col in range(5)]
        page.older.click()
        until(lambda: len(page.rows) == 6)
        assert all(r.event.batch_label == "P" for r in page.rows)
        assert columns == [page.table.columnWidth(col) for col in range(5)]
        page.newer.click()
        until(lambda: page.rows == newer)
        assert page.details.isHidden() and not page.details.toPlainText()
        assert page.rows[0].event.definition == "equipment/v1 SHA-256 fixture"
        window.set_language("zh")
        state = page.table.item(0, 3)
        assert state and state.text() == text("records_seen_active", "zh")
        assert (
            state.toolTip()
            == state.data(Qt.ItemDataRole.AccessibleTextRole)
            == text("alarm_observed_active", "zh")
        )
        source = page.table.item(0, 1)
        assert source and source.text() == "PLC · W118.5"
        item = page.table.item(0, 2)
        assert item is not None and item.text() == "往复轴行程未设置"
        assert page.table.horizontalScrollBar().maximum() == 0, (
            window.width(),
            page.table.viewport().width(),
            [page.table.columnWidth(i) for i in range(5)],
        )
        window.set_language("de")
        item = page.table.item(0, 2)
        assert item is not None and item.text() == "Hub nicht eingestellt"
        assert item.text() == item.toolTip() and page.details.isHidden()
        window.set_language("zh-TW")
        item = page.table.item(0, 2)
        assert item is not None and item.text() == "Reciprocating axis stroke not set"
        assert item.text() == item.toolTip() and page.details.isHidden()
        outlier = page.table.item(1, 2)
        assert (
            outlier
            and outlier.text()
            == outlier.toolTip()
            == outlier.data(Qt.ItemDataRole.AccessibleTextRole)
            == long_label
        )
        assert page.table.fontMetrics().horizontalAdvance(long_label) > columns[2]
        assert columns == [page.table.columnWidth(col) for col in range(5)]
        late: Future[tuple[AlarmRecord, ...]] = Future()

        def delayed(
            query: AlarmQuery, revision: int | None
        ) -> Future[tuple[AlarmRecord, ...]]:
            return late

        monkeypatch.setattr(app, "alarm_page", delayed)
        page.refresh()
        identity.logout()
        window.identity_controls.refresh()
        late.set_result(newer)
        qapp.processEvents()
        assert (
            not page.rows
            and not page.details.toPlainText()
            and page.details.isHidden()
            and not window.history_tabs.isTabEnabled(3)
        )
    finally:
        close_window(window)
        delivery.detach()
