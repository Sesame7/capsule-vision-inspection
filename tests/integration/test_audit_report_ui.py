"""Audit uses the existing compact, context-only Records export dialog."""

from concurrent.futures import Future
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication, QComboBox
from tests.integration.test_audit_ui import select_account
from tests.integration.test_history_report import choose_file
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data
from tests.unit.test_audit_report import fill

from capsule_vision.application.audit_report import AuditReportRequest
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_scope import HistoryQuery, ReportRequest
from capsule_vision.localization.languages import Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.report_dialog import ReportDialog


def test_audit_context_dialog_scope_export_and_empty_feedback(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    assert app.identity and app.history
    batch, _ = fill(app.identity, app.history)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.resize(1080, 1920)
        window.show()
        window.set_language("en")
        window.navigate(3)
        window.records_filters.select_batch(batch, "LOT-A · Yellow")
        window.history_tabs.setCurrentIndex(2)
        select_account(window.audit_page, "Former Operator")
        until(lambda: len(window.audit_page.rows) == 50)
        assert window.history.export_button.isEnabled()
        window.history.export_button.click()
        dialog = window.history.report_dialog
        assert dialog and dialog.windowTitle() == "Audit Records Report"
        assert dialog.audit and dialog.audit.account
        assert dialog.audit.account.username == "Former Operator"
        assert dialog.audit.filters.batch == batch
        assert "Batch-linked records: LOT-A · Yellow" in dialog.scope_label.text()
        assert dialog.format.currentData() == "pdf"
        assert len([w for w in dialog.findChildren(QComboBox) if w.isVisible()]) == 2
        assert not dialog.basis.isVisible() and not dialog.hint.isVisible()
        dialog.output_language.setCurrentIndex(0)
        window.set_language("zh")
        window.set_language("en")
        assert dialog.output_language.currentData() == "zh"
        output = tmp_path / "audit.pdf"
        choose_file(dialog, output)
        until(
            lambda: (
                output.exists() and dialog.feedback.text().startswith("Report saved")
            )
        )
        dialog.close()
        until(lambda: window.history.report_dialog is None)
        empty = ReportDialog(
            app,
            "en",
            window,
            HistoryQuery(),
            audit=AuditReportRequest(RecordsScope(until=1)),
        )
        empty.show()
        target = tmp_path / "empty.pdf"
        choose_file(empty, target)
        until(lambda: empty.feedback.text() == "No matching audit records")
        assert not target.exists() and empty.export.isEnabled()
        empty.close()
        empty.deleteLater()
        app.identity.logout()
        window.identity_controls.refresh()
        assert not window.history_tabs.isTabEnabled(2)
        app.identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        window.history_tabs.setCurrentIndex(3)
        assert window.history.export_button.isEnabled()
    finally:
        close_window(window)
        delivery.detach()


def test_audit_dialog_cancellation_and_late_delivery(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    pending: Future[Path] = Future()
    stops: list[Event] = []

    def held(
        batch: str | ReportRequest | AuditReportRequest,
        path: Path,
        language: Language,
        *,
        revision: int | None = None,
        cancel: Event | None = None,
    ) -> Future[Path]:
        assert isinstance(batch, AuditReportRequest) and cancel is not None
        assert revision == app.session_revision
        stops.append(cancel)
        return pending

    monkeypatch.setattr(app, "export_report", held)
    try:
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        window.history.export_button.click()
        dialog = window.history.report_dialog
        assert dialog
        choose_file(dialog, tmp_path / "pending.pdf")
        assert dialog.cancel.isVisible() and not dialog.export.isEnabled()
        dialog.close()
        until(lambda: window.history.report_dialog is None)
        assert stops[0].is_set()
        pending.set_result(tmp_path / "late.pdf")
        qapp.processEvents()
        assert window.history.report_dialog is None
    finally:
        close_window(window)
        delivery.detach()
