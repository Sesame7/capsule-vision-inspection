"""Guest startup monitoring, authorized saves and History in the real shell."""

from concurrent.futures import Future
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from threading import enumerate as threads

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ReplayEngine
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data

from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery, AlarmRecord
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.startup import load_startup
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.configuration.communication_preferences import (
    AlarmPreferences,
    CommunicationPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_guest_close_waits_for_the_inflight_alarm_owner(tmp_path: Path) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        peer.hold_next("read")
        saved = replace(
            CommunicationPreferences(),
            alarm=AlarmPreferences(True, 1, FinsTcpEndpoint(port=peer.port)),
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
        assert all(t.name != "PLCAlarmMonitor" for t in threads())


def test_saved_monitor_runs_across_guest_start_stop_logout_and_saved_changes(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    initialize_administrator(data.security, "Admin", TEST_PASSWORD)
    with TcpPeer() as first, TcpPeer() as second:
        saved = replace(
            CommunicationPreferences(),
            alarm=AlarmPreferences(True, 1, FinsTcpEndpoint(port=first.port)),
        )
        save_communication_preferences(data.communication, saved)
        first.set_words(18, (4,))
        second.set_words(118, (1 << 7,))
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
                and not window.history_tabs.isTabEnabled(2)
            )
            with pytest.raises(AuthorizationError):
                app.alarm_page(AlarmQuery())
            with pytest.raises(AuthorizationError):
                app.history.alarm_page(AlarmQuery())
            app.start().result(WAIT)
            app.stop().result(WAIT)
            assert app.alarm_snapshot().active[0].id == "emergency_stop"
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            identity.create_account("Line", Role.OPERATOR, TEST_PASSWORD).result(WAIT)
            identity.create_account("Tech", Role.ENGINEER, TEST_PASSWORD).result(WAIT)
            identity.login("Line", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            assert (
                not page.alarm_enabled.isEnabled()
                and window.history_tabs.isTabEnabled(2)
            )
            assert not window.history_tabs.isTabEnabled(1)
            app.start_batch("Batch P").result(WAIT)
            identity.login("Tech", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            page.alarm_port.setValue(second.port)
            page.alarm_station.setCurrentIndex(1)
            assert (
                page.dirty
                and not second.packets
                and load_communication_preferences(data.communication) == saved
            )
            page.reload_settings()
            assert (
                page.alarm_port.value() == first.port
                and page.alarm_station.currentIndex() == 0
            )
            page.alarm_port.setValue(second.port)
            page.alarm_station.setCurrentIndex(1)
            page.persist_settings()
            until(
                lambda: (
                    not page.busy
                    and bool(app.alarm_snapshot().active)
                    and app.alarm_snapshot().active[0].id == "homing_required"
                )
            )
            assert load_communication_preferences(data.communication).alarm.station == 2
            ops = identity.audit_page(AuditQuery(action="communication.save")).result(
                WAIT
            )
            assert (
                ops[0].operation.username == "Tech"
                and "alarm.station" in ops[0].operation.after
            )
            events = app.alarm_page(AlarmQuery()).result(WAIT)
            assert events[0].event.batch_label == "Batch P"
            assert events[0].event.batch_id == app.snapshot().batch_uuid
            assert events[-1].event.batch_id is None
            identity.logout()
            window.identity_controls.refresh()
            second.set_words(118, (0,))
            until(
                lambda: (
                    app.alarm_snapshot().state == "online"
                    and not app.alarm_snapshot().active
                )
            )
            assert bar.height() == 44 and bar.alarm.width() == width
            page.alarm_enabled.setChecked(False)
            page.persist_settings()
            assert load_communication_preferences(data.communication).alarm.enabled
            assert (
                app.alarm_snapshot().state == "online"
                and not window.history_tabs.isTabEnabled(2)
            )
            identity.login("Tech", TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            page.persist_settings()
            until(lambda: not page.busy and app.alarm_snapshot().state == "off")
            assert not load_communication_preferences(data.communication).alarm.enabled
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
    today = datetime.now(UTC)
    when = today.timestamp()
    identity.create_account("Line", Role.OPERATOR, TEST_PASSWORD).result(WAIT)
    for i in range(57):
        history.offer_alarms(
            (
                AlarmEvent(
                    when,
                    "reciprocating_stroke_unset",
                    "observed_active",
                    "startup",
                    "往复轴行程未设置",
                    "Reciprocating axis stroke not set",
                    "fins_tcp://127.0.0.1:9600",
                    2,
                    118,
                    5,
                    "equipment/v1 SHA-256 fixture",
                    "stable-id",
                    "P" if i else "Q",
                ),
            )
        )
    identity.login("Line", TEST_PASSWORD).result(WAIT)
    window = create_window(startup, delivery)
    page = window.alarm_page
    try:
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        until(lambda: len(page.rows) == 50)
        page.search.setText("P")
        page.kind.setCurrentIndex(page.kind.findData("startup"))
        page.date.setDate(QDate(today.year, today.month, today.day))
        page.all_dates.setChecked(False)
        page.refresh()
        until(lambda: len(page.rows) == 50 and page.older.isEnabled())
        newer = page.rows
        page.older.click()
        until(lambda: len(page.rows) == 6)
        assert all(r.event.batch_label == "P" for r in page.rows)
        page.newer.click()
        until(lambda: page.rows == newer)
        assert (
            "W118.5" in page.details.toPlainText()
            and "stable-id" in page.details.toPlainText()
        )
        window.set_language("zh")
        item = page.table.item(0, 1)
        assert item is not None and item.text() == "往复轴行程未设置"
        assert page.table.horizontalScrollBar().maximum() == 0
        window.set_language("zh-TW")
        item = page.table.item(0, 1)
        assert item is not None and item.text() == "Reciprocating axis stroke not set"
        assert item.text() in page.details.toPlainText()
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
            and not window.history_tabs.isTabEnabled(2)
        )
    finally:
        close_window(window)
        delivery.detach()
