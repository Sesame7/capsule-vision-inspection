"""Alarm export reuses the shared action, category and PDF chooser."""

from pathlib import Path

from PySide6.QtWidgets import QApplication
from tests.integration.test_history_report import choose_file
from tests.support.qt import close_window, until
from tests.support.reports import pdf_text
from tests.support.startup import authenticated_startup, saved_data
from tests.unit.test_alarm_report import event, offer

from capsule_vision.application.alarm_report import AlarmReportRequest
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_alarm_pdf_scope_language_empty_and_identity(
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
        window.history_tabs.setCurrentWidget(window.alarm_page)
        window.set_language("en")
        page = window.alarm_page
        page.kind.setCurrentIndex(page.kind.findData("equipment"))
        page.kind.activated.emit(page.kind.currentIndex())
        action = window.records_export
        assert (
            action.request == AlarmReportRequest(RecordsScope(), "equipment")
            and action.isEnabled()
        )
        action.click()
        chooser = action.chooser
        assert chooser and chooser.windowTitle() == "Alarm Records Report"
        target = tmp_path / "alarm.pdf"
        choose_file(action, target)
        until(lambda: action.feedback == ("report_saved", str(target)))
        assert "Alarm Records Report" in pdf_text(target)
        page.kind.setCurrentIndex(page.kind.findData("software"))
        page.kind.activated.emit(page.kind.currentIndex())
        action.click()
        path = tmp_path / "empty.pdf"
        choose_file(action, path)
        until(lambda: action.feedback == ("alarm_no_matches", ""))
        assert not path.exists() and action.isEnabled()
        action.click()
        app.identity.logout()
        window.identity_controls.refresh()
        assert action.chooser is None and not window.history_tabs.isTabEnabled(
            window.history_tabs.indexOf(page)
        )
    finally:
        close_window(window)
        delivery.detach()
