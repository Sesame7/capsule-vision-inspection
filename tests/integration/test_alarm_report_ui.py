"""Alarm export reuses the shared action, category and two-choice dialog."""

from pathlib import Path

from PySide6.QtWidgets import QApplication, QComboBox
from tests.integration.test_history_report import choose_file
from tests.support.qt import close_window, until
from tests.support.startup import authenticated_startup, saved_data
from tests.unit.test_alarm_report import event, offer

from capsule_vision.application.alarm_report import AlarmReportRequest
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_scope import HistoryQuery
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.report_dialog import ReportDialog


def test_alarm_dialog_scope_language_empty_and_identity(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    assert app.history and app.identity
    offer(app.history, [event()])
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(3)
        window.set_language("en")
        page = window.alarm_page
        page.kind.setCurrentIndex(page.kind.findData("equipment"))
        page.kind.activated.emit(page.kind.currentIndex())
        assert window.history.export_button.isEnabled()
        window.history.export_button.click()
        dialog = window.history.report_dialog
        assert dialog and dialog.alarm and dialog.alarm.category == "equipment"
        assert dialog.windowTitle() == "Alarm Records Report"
        assert dialog.format.currentData() == "pdf"
        assert "Equipment alarm" in dialog.scope_label.text()
        assert len([c for c in dialog.findChildren(QComboBox) if c.isVisible()]) == 2
        assert dialog.basis.isHidden() and dialog.hint.isHidden()
        dialog.output_language.setCurrentIndex(0)
        window.set_language("zh")
        assert dialog.windowTitle() == "报警记录报告"
        window.set_language("en")
        assert dialog.output_language.currentData() == "zh"
        target = tmp_path / "alarm.pdf"
        choose_file(dialog, target)
        until(
            lambda: (
                target.exists() and dialog.feedback.text().startswith("Report saved")
            )
        )
        # A changed effective scope dismisses the old request, without resetting filters.
        page.kind.setCurrentIndex(page.kind.findData("software"))
        page.kind.activated.emit(page.kind.currentIndex())
        until(lambda: window.history.report_dialog is None)
        window.history.export_button.click()
        empty = window.history.report_dialog
        assert empty and empty.alarm and empty.alarm.category == "software"
        choose_file(empty, tmp_path / "empty.pdf")
        until(lambda: empty.feedback.text() == "No matching alarm records")
        assert not (tmp_path / "empty.pdf").exists() and empty.export.isEnabled()
        app.identity.logout()
        window.identity_controls.refresh()
        until(lambda: window.history.report_dialog is None)
        assert not window.history_tabs.isTabEnabled(3)
    finally:
        close_window(window)
        delivery.detach()


def test_alarm_dialog_batch_and_open_dates(qapp: QApplication, tmp_path: Path) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        dialog = ReportDialog(
            startup.application,
            "en",
            window,
            HistoryQuery(),
            alarm=AlarmReportRequest(RecordsScope(1_800_000_000, None, "exact-id")),
            alarm_label="LOT review",
        )
        dialog.show()
        assert (
            "LOT review" in dialog.scope_label.text()
            and "…" in dialog.scope_label.text()
        )
        assert "exact-id" not in dialog.scope_label.text()
        assert dialog.format.currentData() == "pdf"
        dialog.reject()
        assert not dialog.isVisible()
        dialog.deleteLater()
    finally:
        close_window(window)
        delivery.detach()
