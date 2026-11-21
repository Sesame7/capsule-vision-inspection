"""History uses persisted group membership and the admitted manual report command."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import time

import pytest
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QFileDialog
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.reports import pdf_text
from tests.support.startup import (
    authenticated_startup,
    saved_data,
)
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE, record_group

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.report_scope import (
    ReportRequest,
)
from capsule_vision.communication.reporting import ReportingMode
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_page import HistoryPage
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
        page.activate()
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
        ng.table.clearSelection()
        assert not action.isEnabled()
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
        tabs.setCurrentWidget(window.batches_page)
        until(action.isEnabled)
        action.click()
        choose_file(action, None)
        assert action.chooser is None and action.isEnabled()
    finally:
        close_window(window)
        delivery.detach()
