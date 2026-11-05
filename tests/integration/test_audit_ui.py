"""The real History destination, audit reader and session fence on disposable storage."""

from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QDate
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data

from capsule_vision.application.audit import AuditOperation, AuditQuery, AuditRecord
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_audit_tab_filters_pages_and_revokes_pending_delivery(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page = window.audit_page
    try:
        identity.create_account("Shift", Role.ENGINEER, TEST_PASSWORD).result(WAIT)
        identity.create_account("Line", Role.OPERATOR, TEST_PASSWORD).result(WAIT)
        for i in range(58):
            op = AuditOperation(
                "stable",
                "Archived user",
                "recipe.save",
                "Lot " + str(i),
                "old",
                "new",
                utc="2025-02-01T01:02:03.000000+00:00",
            )
            identity.audit.record(op, "success")
        window.show()
        window.navigate(3)
        assert window.pages.count() == 5 and window.history_tabs.count() == 3
        window.history_tabs.setCurrentIndex(1)
        until(lambda: len(page.rows) == 50)
        identity.login("Shift", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        until(lambda: len(page.rows) == 50)
        page.actor.setText("Archived user")
        page.action.setCurrentIndex(page.action.findData("recipe.save"))
        page.date.setDate(QDate(2025, 2, 1))
        page.all_dates.setChecked(False)
        page.refresh()
        until(lambda: bool(page.rows) and page.rows[0].operation.target == "Lot 57")
        page.older.click()
        until(lambda: len(page.rows) == 8)
        assert not page.older.isEnabled() and page.newer.isEnabled()
        page.table.selectRow(7)
        assert (
            "Lot 0" in page.details.toPlainText()
            and "old" in page.details.toPlainText()
        )
        page.newer.click()
        until(lambda: len(page.rows) == 50)
        assert not page.newer.isEnabled()
        page.date.setDate(QDate(9999, 12, 31))
        until(lambda: not page.rows)
        assert not page.details.toPlainText()
        page.date.setDate(QDate(2025, 2, 1))
        until(lambda: len(page.rows) == 50)
        private_rows = page.rows
        late: Future[tuple[AuditRecord, ...]] = Future()

        def delayed(
            query: AuditQuery, revision: int | None
        ) -> Future[tuple[AuditRecord, ...]]:
            return late

        monkeypatch.setattr(identity, "audit_page", delayed)
        page.refresh()
        identity.logout()
        window.identity_controls.refresh()
        late.set_result(private_rows)
        qapp.processEvents()
        for role in (Role.GUEST, Role.OPERATOR):
            if role == Role.OPERATOR:
                identity.login("Line", TEST_PASSWORD).result(WAIT)
                window.identity_controls.refresh()
            assert not window.history_tabs.isTabEnabled(1)
            assert window.history_tabs.currentIndex() == 0 and not page.rows
            assert not page.details.toPlainText() and page.table.rowCount() == 0
            with pytest.raises(AuthorizationError):
                identity.audit.page(AuditQuery())
    finally:
        close_window(window)
        delivery.detach()
