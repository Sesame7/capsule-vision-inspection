"""One shared, actually nullable range and persisted Batch selector in Records."""

from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QPoint, QRect, Qt, QTimeZone
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCalendarWidget, QCheckBox, QTableView
from tests.support.application import WAIT, close
from tests.support.business import mixed_result
from tests.support.qt import close_window, until
from tests.support.startup import authenticated_startup, saved_data
from tests.unit.test_durable_batches import RECIPE

from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.application.audit import AuditOperation, AuditQuery
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.records_query import (
    BatchChoice,
    BatchChoices,
    RecordsScope,
)
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_event_table import local_day_bounds
from capsule_vision.ui.records_filters import OptionalDate, RecordsFilters


def test_nullable_calendar_keyboard_clear_and_dst(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QTimeZone, "systemTimeZone", lambda: QTimeZone(b"America/New_York")
    )
    date = OptionalDate("records_from")
    date.set_language("en")
    date.show()
    try:
        assert date.value is None and date.text() == ""
        assert date.placeholderText() == "Start Date"
        assert (
            date.calendar.verticalHeaderFormat()
            == QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        date.open_calendar.trigger()
        until(date.calendar.isVisible)
        QTest.keyClick(date.calendar, Qt.Key.Key_Escape)
        until(lambda: not date.popup.isVisible())
        assert date.value is None
        date.set_date(QDate(2026, 11, 1))
        start, end = local_day_bounds(date.value or QDate())
        assert end is not None and end - start == 25 * 3600
        assert datetime.fromtimestamp(start, UTC).hour == 4
        date.open_calendar.trigger()
        until(date.calendar.isVisible)
        view = date.calendar.findChild(QTableView, "qt_calendar_calendarview")
        assert view
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Right)
        QTest.keyClick(view, Qt.Key.Key_Return)
        until(lambda: not date.popup.isVisible())
        assert date.value == QDate(2026, 11, 2)
        date.setFocus()
        date.selectAll()
        QTest.keyClick(date, Qt.Key.Key_Backspace)
        assert date.value is None and date.text() == ""
        date.setText("2026-03-08")
        start, end = local_day_bounds(date.value or QDate())
        assert end is not None and end - start == 23 * 3600
    finally:
        date.popup.hide()
        date.close()
        date.deleteLater()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_blank_selected_cleared_dates_and_native_popup_bounds(
    qapp: QApplication, language: str
) -> None:
    for key in ("records_from", "records_to"):
        date = OptionalDate(key)
        date.set_language("zh" if language == "zh" else "en")
        available = date.screen().availableGeometry()
        date.move(
            available.right() - date.width(), available.bottom() - date.height() - 30
        )
        date.show()
        try:
            assert date.value is None and date.text() == ""
            assert date.placeholderText() in (
                "开始日期",
                "结束日期",
                "Start Date",
                "End Date",
            )
            for selected in (None, QDate(2026, 11, 5), None):
                date.set_date(selected)
                date.open_calendar.trigger()
                until(date.popup.isVisible)
                assert date.value == selected
                assert date.text() == ("2026-11-05" if selected else "")
                assert date.calendar.selectedDate() == (selected or QDate.currentDate())
                assert (
                    date.calendar.verticalHeaderFormat()
                    == QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
                )
                assert available.contains(date.popup.frameGeometry())
                QTest.keyClick(date.calendar, Qt.Key.Key_Escape)
                until(lambda date=date: not date.popup.isVisible())
        finally:
            date.popup.hide()
            date.close()
            date.deleteLater()


def test_pending_choice_validation_cannot_clear_a_later_batch_handoff(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    pending: Future[BatchChoices] = Future()
    requested: list[RecordsScope] = []

    def hold(
        scope: RecordsScope,
        search: str = "",
        cursor: tuple[float, str] | None = None,
        *,
        alarm_revision: int | None = None,
    ) -> Future[BatchChoices]:
        requested.append(scope)
        return pending

    monkeypatch.setattr(history, "batch_choices", hold)
    filters = RecordsFilters(history)
    try:
        filters.show()
        filters.select_batch("old", "Earlier selection")
        filters.start.set_date(QDate.currentDate())
        assert requested[0].batch == "old"
        filters.select_batch(
            "new", "Viewed Batch"
        )  # View inspections while the earlier read is pending.
        pending.set_result(BatchChoices((), False, False, False))
        until(filters.choices.isEnabled)
        assert (
            filters.scope.batch == "new" and filters.batch.toolTip() == "Viewed Batch"
        )
    finally:
        filters.dispose()
        filters.close()
        history.request_stop()
        assert history.join(WAIT)


def test_unavailable_picker_preserves_selection_without_retrying(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path), select_basler_batch=False)
    filters = RecordsFilters(None, startup.application.identity)
    try:
        filters.set_language("en")
        filters.show()
        filters.select_batch("known", "Selected Batch")
        filters.batch.click()
        until(filters.choices.isEnabled)
        assert filters.choices.count() == 2
        assert not filters.choices.item(1).flags()
        assert filters.scope.batch == "known"
    finally:
        filters.dispose()
        filters.close()
        close(startup.application)


def test_shared_scope_search_tabs_exact_association_and_pager_geometry(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path), select_basler_batch=False)
    app = startup.application
    history, identity = app.history, app.identity
    assert history and identity
    today = QDate.currentDate()
    start, end = local_day_bounds(today)
    assert end is not None
    batch = history.select_batch("Lot Alpha").result(WAIT)
    assert batch
    history.start_run(batch.uuid, "recorded-run", RECIPE, None).result(WAIT)
    assert history.offer(
        HistoryContext(
            datetime.now().timestamp(), batch.uuid, batch.label, "recorded-run", RECIPE
        ),
        mixed_result(1),
    )
    history.finish_run("recorded-run", "stopped", False).result(WAIT)
    other = history.select_batch("Lot Beta").result(WAIT)
    assert other
    history.start_run(other.uuid, "other-run", RECIPE, None).result(WAIT)
    assert history.offer(
        HistoryContext(
            datetime.now().timestamp(), other.uuid, other.label, "other-run", RECIPE
        ),
        mixed_result(2),
    )
    history.finish_run("other-run", "stopped", False).result(WAIT)
    for i, stored in enumerate((batch, other, None)):
        if stored is None:
            assert history.offer(
                HistoryContext(
                    start + 22, "unbatched-segment", None, "unbatched", RECIPE
                ),
                mixed_result(3),
            )
        history.offer_alarms(
            (
                AlarmEvent(
                    start + 20 + i,
                    f"alarm-{i}",
                    "observed_active",
                    "startup",
                    "气压",
                    "Air",
                    "PLC",
                    1,
                    18,
                    0,
                    "fixture",
                    stored.uuid if stored else None,
                    stored.label if stored else None,
                ),
            )
        )
    identity.audit.record(
        AuditOperation("a", "Shift", "production.start", "recorded-run"), "success"
    )
    identity.audit.record(
        AuditOperation("a", "Shift", "session.login", "recorded-run"), "success"
    )
    identity.audit_page(AuditQuery()).result(WAIT)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.resize(1080, 1920)
        window.show()
        window.set_language("en")
        window.navigate(3)
        filters = window.records_filters
        assert filters.start.value is filters.end.value is None
        assert not window.history_tabs.findChildren(QCheckBox)
        filters.start.set_date(today)
        filters.end.set_date(today)
        filters.batch.click()
        until(lambda: filters.choices.isEnabled() and filters.choices.count() == 4)
        filters.search.setText("alpha")
        until(lambda: filters.choices.isEnabled() and filters.choices.count() == 3)
        choice = filters.choices.item(2)
        assert choice.text() == "Lot Alpha" and isinstance(
            choice.data(Qt.ItemDataRole.UserRole), BatchChoice
        )
        filters.choices.setCurrentRow(2)
        QTest.keyClick(filters.choices, Qt.Key.Key_Return)
        until(lambda: not filters.popup.isVisible())
        assert filters.scope.batch == batch.uuid
        pager_positions: set[tuple[int, int, int, int]] = set()
        for index in range(4):
            window.history_tabs.setCurrentIndex(index)
            page = (
                window.batches_page,
                window.history,
                window.audit_page,
                window.alarm_page,
            )[index]
            until(lambda page=page: page.isVisible())
            qapp.processEvents()
            assert page.scope == filters.scope
            assert filters.batch.isVisible() and filters.batch.text() == "Lot Alpha"
            rect = QRect(
                page.refresh_button.mapTo(window.history_tabs, QPoint()),
                page.refresh_button.size(),
            )
            pager_positions.add((rect.x(), rect.y(), rect.width(), rect.height()))
        until(lambda: len(window.alarm_page.rows) == 1)
        assert window.alarm_page.rows[0].event.signal == "alarm-0"
        assert len(pager_positions) == 1
        window.history_tabs.setCurrentIndex(2)
        until(lambda: len(window.audit_page.rows) == 1)
        assert window.audit_page.rows[0].operation.action == "production.start"
        window.history_tabs.setCurrentIndex(1)
        until(lambda: not window.history.busy)
        assert window.history.cycles.rowCount() == 1, (
            window.history.warning.text(),
            window.history.status.text(),
            window.history.scope,
            history.query().result(WAIT),
        )
        assert window.history.scope.since == start and window.history.scope.until == end
        filters.end.clear()
        assert filters.scope.since == start and filters.scope.until is None
        filters.start.clear()
        filters.end.set_date(today)
        assert filters.scope.since is None and filters.scope.until == end
        filters.start.set_date(today.addDays(1))
        filters.end.set_date(today.addDays(1))
        until(lambda: filters.scope.batch is None)
        assert filters.batch.text() == "All Batches"
        until(lambda: not window.history.busy and not window.history.cycles.rowCount())
        identity.logout()
        window.identity_controls.refresh()
        assert filters.start.value == filters.end.value == today.addDays(1)
        until(
            lambda: (
                not window.history_tabs.isTabEnabled(2)
                and not window.history_tabs.isTabEnabled(3)
            )
        )
    finally:
        close_window(window)
        delivery.detach()
