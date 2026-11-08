"""Batch review is public, isolated from Production selection, and edits are fenced."""

import sqlite3
from concurrent.futures import Future
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QPoint, Qt, QTimeZone
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidgetItem,
)
from tests.support.application import WAIT, FakeSession, close
from tests.support.business import mixed_result
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_four_cycle_presentation import completed
from tests.unit.test_history import finish

from capsule_vision.application import ProductionApplication
from capsule_vision.application.batch_review import (
    BatchDetail,
    BatchInformation,
    StartAccount,
)
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import Role
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.localization.catalog import text
from capsule_vision.ui.batches_page import BatchesPage
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_event_table import local_day_bounds, record_time


def cell(table: QTableWidget, row: int, column: int) -> QTableWidgetItem:
    item = table.item(row, column)
    assert item is not None
    return item


def parent(page: BatchesPage, row: int = 0) -> QTreeWidgetItem:
    item = page.table.topLevelItem(row)
    assert item is not None
    return item


def child(item: QTreeWidgetItem, row: int = 0) -> QTreeWidgetItem:
    result = item.child(row)
    assert result is not None
    return result


def test_batch_search_and_paging_on_a_25_hour_local_day(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    zone = QTimeZone(b"America/New_York")
    monkeypatch.setattr(QTimeZone, "systemTimeZone", lambda: zone)
    # 23:30 local time is still November 1, after more than 24 hours of this day.
    now = datetime(2026, 11, 2, 4, 30, tzinfo=UTC).timestamp()
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    page = BatchesPage(app)
    try:
        for i in range(52):
            label = f"Line-{i:02}"
            if i == 51:
                now += 1800  # The following local midnight is exclusive.
            batch = history.select_batch(label).result(WAIT)
            assert batch
            history.start_run(batch.uuid, label, RECIPE, None).result(WAIT)
            history.finish_run(label, "stopped", False).result(WAIT)
        page.show()
        page.scope = RecordsScope(*local_day_bounds(QDate(2026, 11, 1)))
        page.refresh()
        until(lambda: page.detail is not None and page.detail.batch.label == "Line-50")
        assert page.table.topLevelItemCount() == 50 and page.older.isEnabled()
        page.older.click()
        until(lambda: page.detail is not None and page.detail.batch.label == "Line-00")
        assert page.table.topLevelItemCount() == 1 and page.newer.isEnabled()
        page.newer.click()
        until(lambda: page.detail is not None and page.detail.batch.label == "Line-50")
        page.table.setCurrentItem(parent(page, 2))
        until(lambda: page.detail is not None and page.detail.batch.label == "Line-48")
        page.refresh_button.click()
        until(lambda: page.older.isEnabled() and page.detail is not None)
        assert page.detail and page.detail.batch.label == "Line-48"
        page.scope = RecordsScope(*local_day_bounds(QDate(2026, 11, 2)))
        page.refresh()
        until(lambda: page.detail is not None and page.detail.batch.label == "Line-51")
        assert page.table.topLevelItemCount() == 1
    finally:
        page.dispose()
        page.close()
        close(app)


def test_public_readonly_batch_metadata_and_export_context(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    day = QDate.currentDate().addDays(-4)
    first, _ = local_day_bounds(day)
    later, _ = local_day_bounds(day.addDays(2))
    now = first + 10
    history = ProductionHistory(data.history, clock=lambda: now)
    try:
        batch = history.select_batch("Lot A").result(WAIT)
        assert batch
        for i in range(2):
            now = (later if i else first) + 20
            run = f"run-{i}"
            actor = StartAccount("shift-1", "Lin") if i else None
            history.start_run(batch.uuid, run, RECIPE, None, actor).result(WAIT)
            now += 1
            ctx = HistoryContext(now, batch.uuid, batch.label, run, RECIPE, "Sample")
            for cycle in range(11, 15):
                assert history.offer(ctx, mixed_result(cycle))
            history.offer_group(run, completed(11))
            history.finish_run(run, "stopped", False).result(WAIT)
        other = history.select_batch("lot a").result(WAIT)
        assert other
        history.start_run(other.uuid, "other", RECIPE, None).result(WAIT)
        assert history.offer(
            HistoryContext(now, other.uuid, other.label, "other", RECIPE, "Sample"),
            mixed_result(11),
        )
        history.finish_run("other", "stopped", False).result(WAIT)
        empty_batch = history.select_batch("Empty lot").result(WAIT)
        assert empty_batch
        history.select_batch(other.label).result(WAIT)
    finally:
        finish(history)

    startup = authenticated_startup(data, select_basler_batch=False)
    app = startup.application
    identity = app.identity
    assert identity is not None
    provision_account(identity, "Shift", Role.OPERATOR, TEST_PASSWORD)
    identity.logout()
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.resize(1080, 1920)
        window.show()
        window.set_language("en")
        window.navigate(3)
        tabs, page = window.history_tabs, window.batches_page
        assert tabs.currentWidget() is page
        assert [tabs.tabText(i) for i in range(4)] == [
            "Batches",
            "Inspection",
            "Audit",
            "Alarms",
        ]
        assert [tabs.isTabEnabled(i) for i in range(4)] == [True, True, False, False]
        assert (
            window.history.export_button.isVisible()
            and not window.history.export_button.isEnabled()
        )
        until(lambda: page.detail is not None)
        assert page.detail is not None and page.detail.batch.uuid == other.uuid
        assert all(parent(page).text(c) == "—" for c in range(4, 8))
        assert not parent(page).isExpanded()
        assert not hasattr(page, "edit") and not hasattr(page, "view")
        assert 60 <= page.metadata.height() <= 90
        assert all(not label.text() for label in page.metadata_labels)
        filters = window.records_filters
        assert filters.start.value is filters.end.value is None
        filters.start.set_date(day)
        filters.end.set_date(day)
        until(lambda: page.detail is not None and page.detail.batch.uuid == batch.uuid)
        assert page.table.topLevelItemCount() == 1
        assert parent(page).text(1) == record_time(first + 20)[0][5:]
        assert parent(page).text(2) == record_time(later + 21)[0][5:]
        assert "Stopped" in parent(page).toolTip(2)
        filters.start.set_date(day.addDays(1))
        filters.end.set_date(day.addDays(1))
        until(lambda: page.table.topLevelItemCount() == 0 and page.detail is None)
        filters.start.set_date(day)
        filters.end.set_date(day)
        until(lambda: page.detail is not None and page.detail.batch.uuid == batch.uuid)
        parent(page).setExpanded(True)
        until(lambda: parent(page).childCount() == 2)
        assert page.table.columnCount() == 8
        assert sorted(child(parent(page), i).text(3) for i in range(2)) == ["Lin", "—"]
        assert page.detail
        for i, run in enumerate(page.detail.runs):
            assert run.totals is not None
            row = child(parent(page), i)
            assert "Stopped" not in row.text(0) and "Stopped" in row.toolTip(0)
            assert [row.text(c) for c in range(4, 8)] == [
                str(run.totals.inspected),
                str(run.totals.channel_ok),
                str(run.totals.channel_ng),
                f"{100 * run.totals.channel_ok / run.totals.inspected:.2f}%",
            ]
            assert all(
                row.textAlignment(c) & Qt.AlignmentFlag.AlignRight for c in range(4, 8)
            )
        totals = page.detail.batch.totals
        assert totals.batches_completed == 2
        assert [parent(page).text(c) for c in range(4, 8)] == [
            str(totals.inspected),
            str(totals.channel_ok),
            str(totals.channel_ng),
            f"{100 * totals.channel_ok / totals.inspected:.2f}%",
        ]
        assert (
            sum(page.table.columnWidth(i) for i in range(8))
            == page.table.viewport().width()
        )
        assert page.table.horizontalScrollBar().maximum() == 0
        assert page.table.geometry().bottom() < page.metadata.geometry().top()
        assert page.metadata.geometry().bottom() == page.contentsRect().bottom()

        identity.login("Shift", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        until(window.history.export_button.isEnabled)
        page.table.setCurrentItem(child(parent(page)))
        assert page.selected_batch == (batch.uuid, batch.label)
        assert window.history.export_batch == (batch.uuid, batch.label)
        window.history.export_button.click()
        report = window.history.report_dialog
        assert report and report.batch == (batch.uuid, batch.label)
        report.reject()
        assert app.history is not None
        app.update_batch_information(
            batch.uuid,
            BatchInformation(),
            BatchInformation("Capsules A", "Size 0", "Long notes " * 30 + "end"),
        ).result(WAIT)
        page.refresh()
        until(
            lambda: page.detail is not None and page.detail.info.product == "Capsules A"
        )
        height = page.metadata.height()
        for language in ("zh", "en", "de"):
            page.set_language(language)
            qapp.processEvents()
            assert page.metadata.height() == height
            assert page.metadata_labels[0].accessibleName().endswith("Capsules A")
            assert page.metadata_labels[2].toolTip().endswith("end")
            assert page.metadata_labels[2].text() != page.metadata_labels[2].toolTip()
        assert "Size 0" in parent(page).toolTip(0)
        assert app.snapshot().batch_uuid == other.uuid
        tabs.setCurrentWidget(page)
        filters.start.clear()
        filters.end.clear()
        filters.select_batch(None)
        until(lambda: page.table.topLevelItemCount() == 3)
        item = next(
            parent(page, i)
            for i in range(3)
            if parent(page, i).text(0) == empty_batch.label
        )
        page.table.setCurrentItem(item)
        until(
            lambda: (
                page.detail is not None and page.detail.batch.uuid == empty_batch.uuid
            )
        )
        assert item.childCount() == 0 and [item.text(c) for c in (1, 2)] == ["—", "—"]
        assert all(not label.text() for label in page.metadata_labels)
        assert page.metadata.height() == height
        assert app.snapshot().batch_uuid == other.uuid
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("count", [0, 1, 10, 50, 100])
def test_all_runs_expand_without_changing_batch_selection(
    qapp: QApplication, tmp_path: Path, count: int
) -> None:
    now = 2_000_000_000.0
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    page = BatchesPage(app)
    try:
        lot = history.select_batch("Many Runs").result(WAIT)
        assert lot
        for n in range(count):
            now += 1
            history.start_run(
                lot.uuid, f"run-{n}", RECIPE, None, StartAccount("actor", "Lin")
            ).result(WAIT)
            history.offer_group(f"run-{n}", completed(11))
            history.finish_run(
                f"run-{n}", "interrupted" if n == 0 else "stopped", n == 0
            ).result(WAIT)
        other = history.select_batch("Other").result(WAIT)
        assert other
        history.start_run(other.uuid, "other", RECIPE, None).result(WAIT)
        history.finish_run("other", "stopped", False).result(WAIT)
        page.resize(1056, 900)
        page.set_language("en")
        page.show()
        page.refresh()
        until(lambda: page.detail is not None)
        many = next(
            parent(page, i) for i in range(2) if parent(page, i).text(0) == lot.label
        )
        other_item = next(
            parent(page, i) for i in range(2) if parent(page, i) is not many
        )
        page.table.setCurrentItem(other_item)
        assert page.selected_batch == (other.uuid, other.label)
        assert not many.isExpanded() and not other_item.isExpanded()
        widths = [page.table.columnWidth(i) for i in range(8)]
        if count:
            # Actual branch-click behavior, independent of the selected Batch.
            rect = page.table.visualItemRect(many)
            QTest.mouseClick(
                page.table.viewport(),
                Qt.MouseButton.LeftButton,
                pos=QPoint(rect.left() - 11, rect.center().y()),
            )
            until(lambda: many.childCount() == count)
            assert page.selected_batch == (other.uuid, other.label)
            assert many.isExpanded() and not other_item.isExpanded()
            assert [
                child(many, i).data(0, Qt.ItemDataRole.UserRole + 1)
                for i in range(count)
            ] == [f"run-{n}" for n in range(count - 1, -1, -1)]
            assert "Interrupted" in child(many, count - 1).text(0)
            assert "incomplete" in child(many, count - 1).toolTip(4)
            for lang in ("zh", "en", "de", "it"):
                page.set_language(lang)
                qapp.processEvents()
                assert many.childCount() == count
                assert [page.table.columnWidth(i) for i in range(8)] == widths
                assert page.table.horizontalScrollBar().maximum() == 0
            if count >= 50:
                assert page.table.verticalScrollBar().maximum() > 0
                page.table.scrollToItem(child(many, count - 1))
                assert page.table.visualItemRect(child(many, count - 1)).intersects(
                    page.table.viewport().rect()
                )
            page.table.setCurrentItem(child(many, 0))
            until(
                lambda: page.detail is not None and page.detail.batch.uuid == lot.uuid
            )
            assert page.selected_batch == (lot.uuid, lot.label)
            assert page.detail and page.detail.batch.totals.batches_completed == count
            # Expanding another Batch collapses the first without selecting it.
            other_item.setExpanded(True)
            until(lambda: other_item.childCount() == 1)
            assert not many.isExpanded() and many.childCount() == 0
            assert page.selected_batch == (lot.uuid, lot.label)
            page.table.setCurrentItem(other_item)
            page.table.setFocus()
            QTest.keyClick(page.table, Qt.Key.Key_Left)
            assert not other_item.isExpanded()
            QTest.keyClick(page.table, Qt.Key.Key_Right)
            until(lambda: other_item.isExpanded() and other_item.childCount() == 1)
        else:
            assert (
                many.childIndicatorPolicy()
                == QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator
            )
            assert [many.text(c) for c in (1, 2)] == ["—", "—"]
        assert page.table.topLevelItemCount() == 2
    finally:
        page.dispose()
        page.close()
        close(app)


def test_legacy_run_counts_unavailable_without_moving_columns(
    qapp: QApplication, tmp_path: Path
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path)
    try:
        lot = owner.select_batch("Historical lot").result(WAIT)
        assert lot
        owner.start_run(lot.uuid, "old", RECIPE, None).result(WAIT)
        owner.offer_group("old", completed(11))
        owner.finish_run("old", "stopped", False).result(WAIT)
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE run_positions")
        db.execute("UPDATE runs SET detail_expired=1")
    owner = ProductionHistory(path)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=owner)
    page = BatchesPage(app)
    try:
        page.resize(1056, 900)
        page.show()
        page.refresh()
        until(lambda: page.detail is not None)
        parent(page).setExpanded(True)
        until(lambda: parent(page).childCount() == 1)
        widths = [page.table.columnWidth(c) for c in range(8)]
        for language in ("zh", "en", "de", "it"):
            page.set_language(language)
            qapp.processEvents()
            row = child(parent(page))
            assert [row.text(c) for c in range(4, 8)] == ["—"] * 4
            assert text("run_statistics_unavailable", language) in row.toolTip(4)
            assert row.data(4, Qt.ItemDataRole.AccessibleTextRole) == row.toolTip(4)
            assert [page.table.columnWidth(c) for c in range(8)] == widths
        assert page.detail and page.detail.batch.totals.batches_completed == 1
    finally:
        page.dispose()
        page.close()
        close(app)


def test_failed_child_page_requires_explicit_retry(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    page = BatchesPage(app)
    try:
        lot = history.select_batch("Many Runs").result(WAIT)
        assert lot
        for n in range(51):
            history.start_run(lot.uuid, f"run-{n}", RECIPE, None).result(WAIT)
            history.finish_run(f"run-{n}", "stopped", False).result(WAIT)
        read = history.batch_detail
        pending: list[Future[BatchDetail]] = []
        requests: list[tuple[str, int, int | None]] = []

        def delayed(
            batch: str, offset: int = 0, run_anchor: int | None = None
        ) -> Future[BatchDetail]:
            if offset == 0:
                return read(batch, offset, run_anchor)
            requests.append((batch, offset, run_anchor))
            future: Future[BatchDetail] = Future()
            pending.append(future)
            return future

        monkeypatch.setattr(history, "batch_detail", delayed)
        page.show()
        page.refresh()
        until(lambda: page.detail is not None)
        parent(page).setExpanded(True)
        until(lambda: len(pending) == 1)
        assert parent(page).childCount() == 50
        pending[0].set_exception(OSError("Summary read unavailable"))
        until(lambda: page.message.isVisible())
        assert "Summary read unavailable" in page.message.text()
        for language in ("en", "zh"):
            page.set_language(language)
            qapp.processEvents()
        assert len(pending) == 1  # No continuous retry from rendering/status updates.
        parent(page).setExpanded(False)
        parent(page).setExpanded(True)
        until(lambda: len(pending) == 2)
        assert requests[0] == requests[1] and requests[1][1] == 50
        result = read(*requests[1]).result(WAIT)
        pending[1].set_result(result)
        until(lambda: parent(page).childCount() == 51)
        assert not page.message.isVisible()
        assert (
            len(
                {
                    child(parent(page), i).data(0, Qt.ItemDataRole.UserRole + 1)
                    for i in range(51)
                }
            )
            == 51
        )
    finally:
        page.dispose()
        page.close()
        close(app)


def test_stale_child_reads_collapse_and_close_do_not_change_context(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000.0
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    page = BatchesPage(app)
    try:
        older = history.select_batch("Older").result(WAIT)
        assert older
        history.start_run(older.uuid, "older", RECIPE, None).result(WAIT)
        history.finish_run("older", "stopped", False).result(WAIT)
        now += 100
        active = history.select_batch("Active").result(WAIT)
        assert active
        history.start_run(active.uuid, "active", RECIPE, None).result(WAIT)
        facts = {
            lot.uuid: history.batch_detail(lot.uuid).result(WAIT)
            for lot in (older, active)
        }
        pending: list[tuple[str, Future[BatchDetail]]] = []

        def delayed(
            batch: str, offset: int = 0, run_anchor: int | None = None
        ) -> Future[BatchDetail]:
            assert offset == 0 and run_anchor is None
            assert not pending or pending[-1][1].done()  # One admitted read at a time.
            future: Future[BatchDetail] = Future()
            pending.append((batch, future))
            return future

        monkeypatch.setattr(history, "batch_detail", delayed)
        page.set_language("en")
        page.show()
        page.refresh()
        until(lambda: len(pending) == 1)
        assert pending[0][0] == active.uuid
        page.table.setCurrentItem(parent(page, 1))
        parent(page, 1).setExpanded(True)
        pending[0][1].set_result(facts[active.uuid])
        until(lambda: len(pending) == 2)
        assert page.detail is None and pending[1][0] == older.uuid
        old_item = parent(page, 1)
        page.refresh()
        until(lambda: parent(page, 1) is not old_item)
        pending[1][1].set_result(facts[older.uuid])
        until(lambda: len(pending) == 3)
        assert page.detail is None and parent(page, 1).childCount() == 0
        pending[2][1].set_result(facts[older.uuid])
        until(lambda: parent(page, 1).childCount() == 1)
        parent(page).setExpanded(True)
        until(lambda: len(pending) == 4)
        parent(page).setExpanded(False)
        pending[3][1].set_result(facts[active.uuid])
        qapp.processEvents()
        assert parent(page).childCount() == 0 and len(pending) == 4
        assert page.selected_batch == (older.uuid, older.label)
        parent(page).setExpanded(True)
        until(lambda: len(pending) == 5)
        pending[4][1].set_result(facts[active.uuid])
        until(lambda: parent(page).childCount() == 1)
        assert "Running" in child(parent(page)).text(0)
        assert child(parent(page)).text(2) == "—"  # No fabricated Run end.
        assert parent(page).text(2) == record_time(now)[0][5:]
        assert "Running" in parent(page).toolTip(2)
        page.table.setCurrentItem(child(parent(page)))
        assert page.selected_batch == (active.uuid, active.label)
        page.refresh()
        until(lambda: len(pending) == 6)
        page.dispose()
        page.close()
        pending[5][1].set_result(facts[active.uuid])
        qapp.processEvents()
        assert len(pending) == 6 and parent(page).childCount() == 0
    finally:
        page.dispose()
        page.close()
        close(app)
