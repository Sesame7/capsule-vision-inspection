"""History uses persisted group membership and the admitted manual report command."""

import re
import sqlite3
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import time

import pytest
from openpyxl import load_workbook
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QListWidget, QWidget
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
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE, record_group

from capsule_vision.application import (
    ProductionApplication,
    production_report,
    report_identification,
)
from capsule_vision.application.alarm_report import AlarmReportRequest
from capsule_vision.application.audit_report import AuditReportRequest
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_scope import (
    DefectScope,
    HistoryQuery,
    ReportRequest,
)
from capsule_vision.localization.languages import Language
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


@pytest.mark.parametrize("extension", ["pdf", "xlsx"])
@pytest.mark.parametrize(
    ("kind", "label"),
    [
        ("batch", "LOT-QA"),
        ("defects", "LOT-QA"),
        ("defects", None),
        ("audit", "Historic Batch"),
        ("audit", None),
        ("alarm", "Historic Batch"),
        ("alarm", None),
    ],
)
def test_filename_suggestion_follows_effective_report_and_batch(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    label: str | None,
    extension: str,
) -> None:
    app = ProductionApplication(lambda _p, _g: FakeSession())
    parent = QWidget()
    batch_id = "private-batch-identity" if label else None
    pending: Future[DefectScope] = Future()
    pending.set_result(DefectScope(4, 1, label))

    def read_scope(
        _query: HistoryQuery, *, revision: int | None = None
    ) -> Future[DefectScope]:
        return pending

    def forbidden() -> str:
        raise AssertionError("Choosing a filename must not read Windows identity")

    monkeypatch.setattr(app, "defect_report_scope", read_scope)
    monkeypatch.setattr(report_identification, "workstation_label", forbidden)
    dialog = ReportDialog(
        app,
        "en",
        parent,
        HistoryQuery(batch_segment=batch_id),
        ("private-batch-identity", label) if kind == "batch" and label else None,
        audit=AuditReportRequest(RecordsScope(batch=batch_id))
        if kind == "audit"
        else None,
        audit_label=label or "",
        alarm=AlarmReportRequest(RecordsScope(batch=batch_id))
        if kind == "alarm"
        else None,
        alarm_label=label or "",
    )
    try:
        dialog.show()
        until(dialog.export.isEnabled)
        dialog.format.setCurrentIndex(dialog.format.findData(extension))
        dialog.export.click()
        picker = next(p for p in dialog.findChildren(QFileDialog) if p.isVisible())
        suggested = Path(picker.selectedFiles()[0]).name
        prefix = dialog.windowTitle() + ("_" + label if label else "")
        assert re.fullmatch(
            re.escape(prefix) + r"_\d{8}-\d{6}\." + extension, suggested
        )
        assert "private-batch-identity" not in suggested
        assert picker.acceptMode() == QFileDialog.AcceptMode.AcceptSave
        assert not picker.testOption(QFileDialog.Option.DontConfirmOverwrite)
        picker.reject()
        assert not dialog.cancel.isVisible()
    finally:
        dialog.reject()
        destroyed = Event()
        parent.destroyed.connect(destroyed.set)
        parent.deleteLater()
        until(destroyed.is_set)
        close(app)


def test_context_export_defaults_permissions_language_and_failure(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    history = ProductionHistory(data.history)
    try:
        batch = history.select_batch("LOT-104").result(WAIT)
        assert batch
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
        until(lambda: window.batches_page.selected_batch is not None)
        provision_account(owner, "Worker", Role.OPERATOR, TEST_PASSWORD)
        provision_account(owner, "Process", Role.ENGINEER, TEST_PASSWORD)
        owner.logout()
        until(lambda: not page.export_button.isEnabled())
        target = tmp_path / "operator.xlsx"
        with pytest.raises(AuthorizationError):
            app.export_report(batch.uuid, target, "en")
        with pytest.raises(AuthorizationError):
            app.defect_report_scope(HistoryQuery())
        owner.login("Worker", TEST_PASSWORD).result(WAIT)
        until(page.export_button.isEnabled)
        selected = window.batches_page.selected_batch
        assert selected
        page.export_button.click()
        dialog = page.report_dialog
        assert dialog and dialog.export.isEnabled()
        assert dialog.batch == selected and dialog.basis.isHidden()
        assert dialog.format.currentData() == "pdf"
        assert not dialog.findChildren(QListWidget)
        assert (
            len(dialog.findChildren(QComboBox)) == 3
        )  # no type or repeated Batch chooser
        choose_file(dialog, None)
        assert not target.exists()
        dialog.format.setCurrentIndex(dialog.format.findData("xlsx"))
        choose_file(dialog, target)
        until(
            lambda: (
                dialog is not None and dialog.feedback.text().startswith("Report saved")
            )
        )
        with closing(load_workbook(target)) as book:
            assert book.worksheets[0]["B3"].value == selected[1]
            assert len(book.worksheets) == 1
        assert (
            app.snapshot().batch_uuid != selected[0]
            or app.snapshot().counters.cycles_started == 0
        )
        dialog.reject()
        # Inspection captures effective filters and ignores pagination.
        window.history_tabs.setCurrentWidget(page)
        page.result.set_selected(frozenset(("ERROR",)))
        page.export_button.click()
        dialog = page.report_dialog
        assert dialog
        until(dialog.export.isEnabled)
        assert dialog.batch is None and dialog.filters.result == frozenset(("ERROR",))
        assert dialog.basis.count() == 1 and dialog.basis.currentData() == "detections"
        assert dialog.hint.isVisible() and dialog.format.currentData() == "pdf"
        dialog.output_language.setCurrentIndex(dialog.output_language.findData("zh"))
        window.set_language("zh")
        window.set_language("en")
        assert dialog.output_language.currentData() == "zh"
        pdf = tmp_path / "defects.pdf"
        choose_file(dialog, pdf)
        until(
            lambda: pdf.exists() and dialog.feedback.text().startswith("Report saved")
        )
        assert pdf.read_bytes().startswith(b"%PDF-")
        revision = dialog.revision
        owner.login("Process", TEST_PASSWORD).result(WAIT)
        until(lambda: page.report_dialog is None)
        with pytest.raises(StaleSessionError):
            app.export_report(batch.uuid, target, "en", revision=revision)
        window.history_tabs.setCurrentIndex(3)
        assert page.export_button.isEnabled()
        assert page.export_button.parentWidget() is window.history_tabs.cornerWidget()
        page.export_button.click()
        assert page.report_dialog is not None and page.report_dialog.alarm is not None
        page.report_dialog.reject()
        window.history_tabs.setCurrentWidget(page)
        page.export_button.click()
        failed = page.report_dialog
        assert failed
        until(failed.export.isEnabled)
        failed.format.setCurrentIndex(failed.format.findData("xlsx"))

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


def test_defect_basis_selection_late_scope_and_export_dismissal(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    history = ProductionHistory(data.history)
    try:
        batch = history.select_batch("Complete").result(WAIT)
        assert batch
        history.start_run(batch.uuid, "run", RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(
            history,
            HistoryContext(time(), batch.uuid, batch.label, "run", RECIPE),
            1,
            5,
            1,
        )
        history.finish_run("run", "stopped", False).result(WAIT)
    finally:
        finish(history)
    startup = authenticated_startup(data)
    app = startup.application
    identity = app.identity
    assert identity
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    release = Event()
    try:
        window.show()
        window.navigate(3)
        page = window.history
        window.history_tabs.setCurrentWidget(page)
        page.export_button.click()
        dialog = page.report_dialog
        assert dialog
        until(dialog.export.isEnabled)
        assert dialog.basis.currentData() == "capsule" and dialog.basis.count() == 2
        dialog.basis.setCurrentIndex(dialog.basis.findData("detections"))
        window.set_language("zh")
        window.set_language("en")
        assert dialog.basis.currentData() == "detections"
        dialog.format.setCurrentIndex(dialog.format.findData("xlsx"))
        entered = Event()
        original = production_report.write_workbook

        def held(
            db: sqlite3.Connection, path: Path, language: Language, stop: Event
        ) -> None:
            entered.set()
            assert release.wait(WAIT)
            original(db, path, language, stop)

        monkeypatch.setattr(production_report, "write_workbook", held)
        target = tmp_path / "cancelled.xlsx"
        choose_file(dialog, target)
        until(entered.is_set)
        dialog.reject()
        until(lambda: page.report_dialog is None)
        release.set()
        until(lambda: not list(tmp_path.glob("*.tmp")))
        assert not target.exists() and not list(tmp_path.glob("*.tmp"))
        late: Future[DefectScope] = Future()

        def pending_scope(
            _q: HistoryQuery, *, revision: int | None = None
        ) -> Future[DefectScope]:
            return late

        monkeypatch.setattr(app, "defect_report_scope", pending_scope)
        page.export_button.click()
        pending = page.report_dialog
        assert pending and not pending.export.isEnabled()
        identity.logout()
        until(lambda: page.report_dialog is None)
        late.set_result(DefectScope(4, 1, "Complete"))
        qapp.processEvents()
        assert page.report_dialog is None and not page.export_button.isEnabled()
    finally:
        release.set()
        close_window(window)
        delivery.detach()


def test_inspection_selected_batch_exports_entire_classification_not_discovery_filters(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    history = ProductionHistory(data.history)
    try:
        batch = history.select_batch("LOT-Historical").result(WAIT)
        assert batch
        history.start_run(batch.uuid, "first", RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(
            history,
            HistoryContext(time(), batch.uuid, batch.label, "first", RECIPE),
            11,
            5,
            1,
        )
        history.finish_run("first", "stopped", False).result(WAIT)
    finally:
        finish(history)
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentWidget(window.history)
        # An exact Batch selector, even when its current query matches no Cycles.
        window.records_filters.select_batch(batch.uuid, batch.label)
        window.history.result.set_selected(frozenset())
        window.history.export_button.click()
        dialog = window.history.report_dialog
        assert dialog
        until(dialog.export.isEnabled)
        assert dialog.windowTitle() == "Defect Classification Report"
        assert dialog.scope_label.text() == "Entire Batch · LOT-Historical"
        assert dialog.basis.isHidden() and dialog.hint.isHidden()
        requests: list[ReportRequest] = []
        original = startup.application.export_report

        def observe(
            request: str | ReportRequest,
            path: Path,
            language: Language,
            *,
            revision: int | None = None,
            cancel: Event | None = None,
        ) -> Future[Path]:
            assert isinstance(request, ReportRequest)
            requests.append(request)
            return original(request, path, language, revision=revision, cancel=cancel)

        monkeypatch.setattr(startup.application, "export_report", observe)
        dialog.format.setCurrentIndex(dialog.format.findData("xlsx"))
        target = tmp_path / "entire-batch.xlsx"
        choose_file(dialog, target)
        until(lambda: dialog.feedback.text().startswith("Report saved"))
        assert requests[0].scope == "defects" and requests[0].batch == batch.uuid
        assert (
            requests[0].basis == "capsule" and requests[0].filters.result == frozenset()
        )
        with closing(load_workbook(target)) as book:
            values = list(book.worksheets[0].values)
            assert any(row[0] == "Batch Total" and row[1] == 20 for row in values)
            assert any(str(row[0]).startswith("Run 1\n") for row in values)
    finally:
        close_window(window)
        delivery.detach()
