"""History uses persisted group membership and the admitted manual report command."""

from collections.abc import Iterator
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from time import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFileDialog
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.reports import pdf_text
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    saved_data,
)
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE, record_group

from capsule_vision.application import ApplicationState
from capsule_vision.application.batch_review import BatchList
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import Role
from capsule_vision.application.report_scope import (
    ReportRequest,
)
from capsule_vision.communication.reporting import ReportingMode
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_page import HistoryPage
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.records_export import RecordsExport


@pytest.mark.parametrize("mode", [ReportingMode.SINGLE_CYCLE, ReportingMode.FOUR_CYCLE])
def test_group_column_disambiguates_runs_and_has_no_inferred_members(
    qapp: QApplication, tmp_path: Path, mode: ReportingMode
) -> None:
    ensure_test_fonts()
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    page.resize(1080, 950)
    page.show()
    try:
        batch = history.select_batch("Lot").result(WAIT)
        assert batch
        context = HistoryContext(time(), batch.uuid, batch.label, "run-1", RECIPE)
        for run, first in (("run-1", 2265), ("run-2", 2264)):
            history.start_run(
                batch.uuid, run, RECIPE, replace(FOUR_CYCLE, mode=mode)
            ).result(WAIT)
            if mode is ReportingMode.FOUR_CYCLE:
                record_group(history, replace(context, run_id=run), first, 5, 8)
            else:
                result = mixed_result(first)
                from capsule_vision.communication.reporting import (
                    CompletedReportingBatch,
                    FirstStationReport,
                )

                report = FirstStationReport.from_result(1, result)
                history.offer_group(
                    run,
                    CompletedReportingBatch(
                        report.identity, 8, (report,), report.reject_words
                    ),
                )
                assert history.offer(replace(context, run_id=run), result)
            history.finish_run(run, "stopped", False).result(WAIT)
        history.start_run(batch.uuid, "run-3", RECIPE, None).result(WAIT)
        for cycle in range(2265, 2269):
            assert history.offer(replace(context, run_id="run-3"), mixed_result(cycle))
        page.refresh()
        until(lambda: not page.busy)
        assert (
            page.cycles.rowCount() == 2 * (mode.group_size or 0) + 4
            and page.cycles.columnCount() == 6
        )
        for row in range(page.cycles.rowCount()):
            item, result = page.cycles.item(row, 2), page.cycles.item(row, 5)
            assert item and result
            record = result.data(Qt.ItemDataRole.UserRole)
            run_id = record.context.run_id
            first = 2265 if run_id == "run-1" else 2264
            expected = (
                "—"
                if run_id == "run-3"
                else f"{first} · {record.cycle_id - first + 1}/{mode.group_size}"
            )
            assert item.text() == expected and run_id in item.toolTip()
            assert (
                item.data(Qt.ItemDataRole.AccessibleDescriptionRole) == item.toolTip()
            )
            assert page.cycles.fontMetrics().horizontalAdvance(
                item.text()
            ) + 12 <= page.cycles.columnWidth(2)
    finally:
        page.dispose()
        page.close()
        finish(history)
        page.deleteLater()


def choose_file(action: RecordsExport, target: Path | None) -> None:
    picker = action.chooser
    assert picker is not None and picker.isVisible()
    assert picker.acceptMode() == QFileDialog.AcceptMode.AcceptSave
    assert picker.nameFilters() == ["PDF (*.pdf)"] and picker.defaultSuffix() == "pdf"
    if target is None:
        picker.reject()
    else:
        picker.setDirectory(str(target.parent))
        picker.selectFile(target.name)
        picker.accept()


@pytest.fixture
def batch_pdf_window(qapp: QApplication, tmp_path: Path) -> Iterator[MainWindow]:
    startup = authenticated_startup(saved_data(tmp_path), select_basler_batch=False)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        history = startup.application.history
        assert history is not None
        batch = history.select_batch("Close recovery lot").result(WAIT)
        assert batch
        window.show()
        window.navigate(3)
        window.records_filters.select_batch(batch.uuid, batch.label)
        until(window.records_export.isEnabled)
        yield window
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize(
    "pending", [False, True], ids=["open-chooser", "active-export"]
)
def test_failed_close_restores_batch_pdf_after_fresh_login(
    batch_pdf_window: MainWindow, tmp_path: Path, pending: bool
) -> None:
    window = batch_pdf_window
    app, action = window.bridge.application, window.records_export
    identity, history = app.identity, app.history
    assert identity and history
    original_revision = identity.snapshot().revision
    batch = action.batch_label
    action.click()
    chooser = action.chooser
    assert chooser
    enabled_on_dismissal: list[bool] = []
    chooser.finished.connect(lambda: enabled_on_dismissal.append(action.isEnabled()))
    capture = Future[None]()
    interrupted = tmp_path / "interrupted.pdf"
    try:
        with patch.object(history, "capture_report", return_value=capture) as snapshot:
            if pending:
                choose_file(action, interrupted)
                until(lambda: snapshot.called)
            with patch.object(
                app, "_close_resources", side_effect=OSError("cleanup failed")
            ):
                window.close()
                until(
                    lambda: (
                        window.snapshot.state is ApplicationState.FAULT
                        and not window.bridge.closing
                        and window.navbar.user.isEnabled()
                    )
                )
            guest = identity.snapshot()
            assert window.isVisible() and guest.role is Role.GUEST
            assert guest.revision > original_revision
            assert action.chooser is None and not action.isEnabled()
            if pending:
                cancel = snapshot.call_args.args[2]
                assert isinstance(cancel, Event) and cancel.is_set()
            else:
                assert enabled_on_dismissal == [False]
                snapshot.assert_not_called()
            renewed = identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
            assert renewed.revision > guest.revision
            window.identity_controls.refresh()
            if pending:
                assert action.text() == "Cancel" and not action.isEnabled()
    finally:
        capture.set_result(None)

    until(lambda: action.text() == "PDF" and action.isEnabled())
    assert action.feedback is None and not interrupted.exists()
    assert batch == "Close recovery lot"
    for name in ("first", "second"):
        output = tmp_path / f"{name}.pdf"
        action.click()
        choose_file(action, output)
        until(output.is_file, 12000)
        assert batch in pdf_text(output)
        until(lambda: action.text() == "PDF" and action.isEnabled())
        assert action.feedback == ("report_saved", str(output))
    window.close()
    until(lambda: not window.isVisible())
    assert window.bridge.closed and window.communication.closed and app.join(0)


@pytest.mark.parametrize(
    "queued", [False, True], ids=["after-close", "queued-at-disposal"]
)
def test_final_window_close_ignores_late_pdf_completion(
    batch_pdf_window: MainWindow, tmp_path: Path, queued: bool
) -> None:
    window = batch_pdf_window
    action = window.records_export
    pending = Future[Path]()
    output = tmp_path / "previous-session.pdf"
    with patch.object(
        window.bridge.application, "export_report", return_value=pending
    ) as export:
        action.click()
        choose_file(action, output)
    cancel = export.call_args.kwargs["cancel"]
    changed = QSignalSpy(action.feedback_changed)
    dispose = action.dispose

    def final_disposal() -> None:
        if queued:
            pending.set_result(output)  # The signal is already in the Qt event queue.
        dispose()

    with patch.object(action, "dispose", side_effect=final_disposal):
        window.close()
        until(lambda: not window.isVisible())
    assert window.bridge.closed and isinstance(cancel, Event) and cancel.is_set()
    if not queued:
        pending.set_result(output)
    QCoreApplication.sendPostedEvents(action, QEvent.Type.MetaCall)
    assert changed.count() == 0 and action.feedback != ("report_saved", str(output))


def test_published_pdf_feedback_does_not_cross_login_revisions(
    batch_pdf_window: MainWindow, tmp_path: Path
) -> None:
    window = batch_pdf_window
    app, action = window.bridge.application, window.records_export
    identity = app.identity
    assert identity
    pending = Future[Path]()
    output = tmp_path / "prior-session.pdf"
    with patch.object(app, "export_report", return_value=pending):
        action.click()
        choose_file(action, output)
    pending.set_result(output)
    window.identity_controls.logout()
    identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
    window.identity_controls.refresh()
    QCoreApplication.sendPostedEvents(action, QEvent.Type.MetaCall)
    until(lambda: action.text() == "PDF" and action.isEnabled())
    assert action.feedback is None


def test_batch_pdf_requires_current_query_and_keeps_admitted_batch(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path), select_basler_batch=False)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        history = startup.application.history
        assert history is not None
        for label in ("Lot Alpha", "Lot Beta"):
            lot = history.select_batch(label).result(WAIT)
            assert lot
            run = f"run-{label}"
            history.start_run(lot.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            history.finish_run(run, "stopped", False).result(WAIT)
        alpha_page = history.batch_page("Lot Alpha").result(WAIT)
        beta_page = history.batch_page("Lot Beta").result(WAIT)
        alpha, beta = alpha_page.rows[0], beta_page.rows[0]
        window.show()
        window.set_language("en")
        window.navigate(3)
        filters, page, action = (
            window.records_filters,
            window.batches_page,
            window.records_export,
        )
        filters.select_batch(alpha.uuid, alpha.label)
        until(
            lambda: (
                page.selected_batch == (alpha.uuid, alpha.label) and action.isEnabled()
            )
        )

        stale, latest = Future[BatchList](), Future[BatchList]()
        with patch.object(history, "batch_page", side_effect=(stale, latest)) as read:
            filters.select_batch(beta.uuid, beta.label)
            assert page.selected_batch is None and action.request is None
            assert not action.isEnabled()
            action.click()
            assert action.chooser is None
            filters.select_batch(alpha.uuid, alpha.label)
            stale.set_result(beta_page)
            until(lambda: read.call_count == 2)
            assert [query.kwargs["batch"] for query in read.call_args_list] == [
                beta.uuid,
                alpha.uuid,
            ]
            assert page.selected_batch is None and action.request is None
            assert not action.isEnabled()
            latest.set_result(alpha_page)
            until(action.isEnabled)
        assert action.request == ReportRequest("batch", batch=alpha.uuid)

        filters.start.set_date(QDate.currentDate().addDays(-1))
        assert not action.isEnabled() and action.request is None
        until(action.isEnabled)
        action.click()
        chooser = action.chooser
        assert chooser
        output = tmp_path / Path(chooser.selectedFiles()[0]).name
        assert alpha.label in output.name
        page.table.clearSelection()
        filters.select_batch(beta.uuid, beta.label)
        until(lambda: page.selected_batch == (beta.uuid, beta.label))
        assert action.request == ReportRequest("batch", batch=beta.uuid)
        window.history_tabs.setCurrentWidget(window.history)
        filters.select_batch(alpha.uuid, alpha.label)
        assert page.selected_batch is None
        window.set_language("zh")
        assert action.chooser is chooser and alpha.label in action.toolTip()
        choose_file(action, output)
        until(lambda: action.feedback == ("report_saved", str(output)), 12000)
        body = pdf_text(output)
        assert text("customer_batch", "en") in body
        assert alpha.label in body and beta.label not in body
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ["zh", "en", "zh-TW", "de"])
def test_direct_pdf_freezes_selected_ng_batch_and_language(
    qapp: QApplication, tmp_path: Path, language: Language
) -> None:
    data = saved_data(tmp_path)
    today = datetime.now().astimezone()
    observed = today.timestamp()
    history = ProductionHistory(data.history, clock=lambda: observed)
    try:
        lot = history.select_batch("Archived lot").result(WAIT)
        assert lot
        for run, start in (
            ("old-run", today.timestamp() - 86400),
            ("visible-run", today.timestamp()),
        ):
            observed = start
            history.start_run(lot.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            record_group(
                history,
                HistoryContext(start, lot.uuid, lot.label, run, RECIPE),
                11,
                5,
                1,
            )
            history.finish_run(run, "stopped", False).result(WAIT)
        history.select_batch(None).result(WAIT)
        history.start_run(None, "anonymous", RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(
            history,
            HistoryContext(today.timestamp(), "anonymous", None, "anonymous", RECIPE),
            90,
            5,
            1,
        )
        history.finish_run("anonymous", "stopped", False).result(WAIT)
        other = history.select_batch("Current lot").result(WAIT)
        assert other and other.uuid != lot.uuid
    finally:
        finish(history)
    startup = authenticated_startup(data, select_basler_batch=False)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.set_language(language)
        window.navigate(3)
        tabs, action, ng = (
            window.history_tabs,
            window.records_export,
            window.ng_capsules,
        )
        assert [tabs.widget(i) for i in range(5)] == [
            window.batches_page,
            ng,
            window.history,
            window.audit_page,
            window.alarm_page,
        ]
        tabs.setCurrentWidget(ng)
        window.records_filters.start.set_date(QDate.currentDate())
        window.records_filters.select_batch(lot.uuid, lot.label)
        until(lambda: ng.selected_capsule is not None and action.isEnabled())
        assert ng.selected_capsule and ng.selected_capsule.key.run == "visible-run"
        assert action.request == ReportRequest("defects", batch=lot.uuid)
        assert action.text() == "PDF" and not action.icon().isNull()
        for description in (action.accessibleName(), action.toolTip()):
            assert text("customer_defects", language) in description
            assert text("records_entire_batch", language) in description
            assert (
                lot.label in description and today.date().isoformat() not in description
            )
        ng.table.clearSelection()
        assert not action.isEnabled()
        assert text("customer_defects", language) in action.accessibleName()
        assert action.accessibleDescription() == text("unavailable", language)
        ng.table.selectRow(0)
        until(action.isEnabled)
        action.click()
        chooser = action.chooser
        lang = "zh" if language == "zh" else "en"
        assert chooser and Path(chooser.selectedFiles()[0]).name.startswith(
            f"{text('customer_defects', lang)}_{lot.label}_"
        )
        # Even a context/language switch while the native chooser is open cannot redirect the write.
        window.set_language("en" if language == "zh" else "zh")
        window.records_filters.select_batch(None, "")
        tabs.setCurrentWidget(window.history)
        assert action.isHidden()
        output = tmp_path / "selected.pdf"
        choose_file(action, output)
        until(lambda: action.feedback == ("report_saved", str(output)), 12000)
        body = pdf_text(output)
        assert text("customer_defects", lang) in body and lot.label in body
        assert other.label not in body
        for number in (1, 2):
            assert f"{text('export_run', lang)} {number}" in body
        tabs.setCurrentWidget(ng)
        until(lambda: bool(ng.table.findItems("90", Qt.MatchFlag.MatchExactly)))
        ng.table.setCurrentItem(ng.table.findItems("90", Qt.MatchFlag.MatchExactly)[0])
        assert ng.selected_capsule is not None and not ng.selected_capsule.batch_uuid
        assert action.isVisible() and not action.isEnabled()
        assert action.accessibleDescription() == text(
            "records_no_batch", window.language
        )
        tabs.setCurrentWidget(window.batches_page)
        until(action.isEnabled)
        assert window.batches_page.selected_batch
        assert text("customer_batch", window.language) in action.accessibleName()
        assert window.batches_page.selected_batch[1] in action.toolTip()
        action.click()
        choose_file(action, None)
        assert action.chooser is None and action.isEnabled()
    finally:
        close_window(window)
        delivery.detach()
