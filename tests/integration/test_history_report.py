"""History uses persisted group membership and the admitted manual report command."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from time import time

import pytest
from openpyxl import load_workbook
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE, record_group

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_page import HistoryPage
from capsule_vision.ui.report_dialog import ReportDialog


def test_group_column_disambiguates_runs_and_has_no_inferred_members(
    qapp: QApplication, tmp_path: Path
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    page.resize(1080, 950)
    page.show()
    try:
        batch = history.select_batch("Lot").result(WAIT)
        assert batch
        context = HistoryContext(time(), batch.uuid, batch.label, "run-1", RECIPE)
        for run, first in (("run-1", 2265), ("run-2", 2264)):
            history.start_run(batch.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            record_group(history, replace(context, run_id=run), first, 5, 8)
            history.finish_run(run, "stopped", False).result(WAIT)
        history.start_run(batch.uuid, "run-3", RECIPE, None).result(WAIT)
        for cycle in range(2265, 2269):
            assert history.offer(replace(context, run_id="run-3"), mixed_result(cycle))
        page.activate()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 12 and page.cycles.columnCount() == 6
        for row in range(12):
            item, result = page.cycles.item(row, 2), page.cycles.item(row, 5)
            assert item and result
            record = result.data(Qt.ItemDataRole.UserRole)
            run_id = record.context.run_id
            first = 2265 if run_id == "run-1" else 2264
            expected = (
                "—"
                if run_id == "run-3"
                else f"{first} · {record.cycle_id - first + 1}/4"
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


def choose_file(dialog: ReportDialog, target: Path | None) -> None:
    dialog.export.click()
    picker = next((p for p in dialog.findChildren(QFileDialog) if p.isVisible()), None)
    assert picker and picker.isVisible()
    if target is None:
        picker.reject()
    else:
        picker.setDirectory(str(target.parent))
        picker.selectFile(target.name)
        picker.accept()


def test_export_picker_reaches_ended_batches_and_enforces_session(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    history = ProductionHistory(data.history)
    try:
        for i in range(105):
            history.select_batch(f"LOT-{i:03}").result(WAIT)
        history.select_batch(None).result(WAIT)
    finally:
        finish(history)
    startup = authenticated_startup(data)
    app = startup.application
    owner = app.identity
    assert owner is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.set_language("en")
        window.show()
        window.navigate(3)
        page = window.history
        until(lambda: not page.busy)
        assert page.batch.count() == 1  # None of these Batches has a Cycle row.
        owner.create_account("Worker", Role.OPERATOR, TEST_PASSWORD).result(WAIT)
        owner.create_account("Process", Role.ENGINEER, TEST_PASSWORD).result(WAIT)
        owner.logout()
        until(lambda: not page.export_button.isEnabled())
        target = tmp_path / "operator.xlsx"
        with pytest.raises(AuthorizationError):
            app.export_report("invalid", target, "en")
        with pytest.raises(AuthorizationError):
            app.report_batches()
        owner.login("Worker", TEST_PASSWORD).result(WAIT)
        until(page.export_button.isEnabled)
        page.export_button.click()
        dialog = page.report_dialog
        assert dialog
        until(dialog.export.isEnabled)
        assert (
            dialog.batches.count() == 50 and dialog.batches.item(0).text() == "LOT-000"
        )
        for first in ("LOT-050", "LOT-100"):
            dialog.next.click()
            until(dialog.export.isEnabled)
            assert dialog.batches.item(0).text() == first
        assert not dialog.next.isEnabled() and dialog.batches.count() == 5
        dialog.previous.click()
        until(dialog.export.isEnabled)
        assert dialog.batches.item(0).text() == "LOT-050"
        dialog.search.setText("LOT-104")
        until(dialog.export.isEnabled)
        assert dialog.batches.count() == 1
        uuid = dialog.batches.currentItem().data(Qt.ItemDataRole.UserRole)
        choose_file(dialog, None)
        assert not target.exists()
        qapp.processEvents()
        choose_file(dialog, target)
        until(lambda: dialog.feedback.text().startswith("Report saved"))
        with closing(load_workbook(target)) as book:
            assert (
                book.worksheets[0]["B2"].value == "LOT-104"
                and book.worksheets[0]["B3"].value == uuid
            )
        assert app.snapshot().batch_uuid is None
        revision = dialog.revision
        owner.login("Process", TEST_PASSWORD).result(WAIT)
        until(lambda: page.report_dialog is None)
        with pytest.raises(StaleSessionError):
            app.export_report(uuid, target, "en", revision=revision)
        for account in ("Process", "TestAdmin"):
            if account == "TestAdmin":
                owner.login(account, TEST_PASSWORD).result(WAIT)
            assert (
                app.export_report(uuid, tmp_path / f"{account}.xlsx", "en")
                .result(WAIT)
                .exists()
            )
        page.export_button.click()
        failed = page.report_dialog
        assert failed
        until(failed.export.isEnabled)

        def locked(_source: Path, _target: Path) -> Path:
            raise PermissionError("Destination is locked")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", locked)
            choose_file(failed, tmp_path / "failure.xlsx")
            until(lambda: failed.feedback.text().startswith("Export failed"))
        assert "Destination is locked" in failed.feedback.text()
        assert not (tmp_path / "failure.xlsx").exists()
    finally:
        close_window(window)
        delivery.detach()
