"""The real History destination, audit reader and session fence on disposable storage."""

from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QPoint, QRect, Qt, QTimeZone
from PySide6.QtGui import QTextTable
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox
from tests.support.application import WAIT, production
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)

from capsule_vision.application import ConfigurationIdentity
from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.application.audit import (
    AuditAccount,
    AuditOperation,
    AuditQuery,
    AuditRecord,
)
from capsule_vision.application.audit_presentation import outcome_caption
from capsule_vision.application.history import HistoryContext
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.localization.audit_wording import audit_text
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.audit_page import AuditPage
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_event_table import audit_utc_bound, local_day_bounds


def select_account(page: AuditPage, username: str) -> None:
    picker = page.accounts
    picker.click()
    picker.search.setText(username)
    until(lambda: picker.choices.isEnabled() and picker.choices.count() > 1)
    for row in range(1, picker.choices.count()):
        item = picker.choices.item(row)
        if item.text() == username:
            picker.choices.setCurrentItem(item)
            QTest.keyClick(picker.choices, Qt.Key.Key_Return)
            assert picker.selected and picker.selected.username == username
            return
    pytest.fail("Historical account was not discovered")


def test_historical_account_popup_search_disabled_accounts_and_late_delivery(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_test_fonts()
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity
    former = provision_account(
        identity, "Former & operator", Role.OPERATOR, TEST_PASSWORD
    )
    identity.set_enabled(former.account_id, False).result(WAIT)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page = window.audit_page
    picker = page.accounts
    closed = False
    try:
        for i in range(65):
            identity.audit.record(
                AuditOperation(f"old-{i}", f"Archived {i:03}", "recipe.save", "saved"),
                "success",
            )
        identity.audit.record(
            AuditOperation(
                former.account_id, former.username, "recipe.save", "Old Recipe"
            ),
            "success",
        )
        identity.audit_page(AuditQuery()).result(WAIT)
        for _ in range(70):
            identity.audit.record(
                AuditOperation("current", "Current", "session.login", "current"),
                "success",
            )
        identity.audit_page(AuditQuery()).result(WAIT)
        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentWidget(window.audit_page)
        until(lambda: len(page.rows) == 50)
        assert all(row.operation.username == "Current" for row in page.rows)
        assert not any(control.isVisible() for control in page.findChildren(QComboBox))
        assert picker.text() == "All Accounts" and picker.selected is None
        assert not page.search.isVisible()
        picker.click()
        until(lambda: picker.choices.isEnabled() and picker.choices.count() == 51)
        scroll = picker.choices.verticalScrollBar()
        until(lambda: scroll.maximum() > 0)
        scroll.setValue(scroll.maximum())
        until(lambda: picker.choices.isEnabled() and picker.choices.count() > 51)
        picker.popup.hide()
        select_account(page, "Archived 064")
        until(lambda: len(page.rows) == 1)
        assert page.rows[0].operation.actor_id == "old-64"
        select_account(page, former.username)
        until(
            lambda: (
                bool(page.rows)
                and all(r.operation.actor_id == former.account_id for r in page.rows)
            )
        )
        assert any(r.operation.target == "Old Recipe" for r in page.rows)
        assert picker.toolTip() == former.username and "&&" in picker.text()
        window.set_language("zh")
        assert picker.toolTip() == former.username
        picker.click()
        until(lambda: picker.choices.isEnabled())
        picker.choices.setCurrentRow(0)
        QTest.mouseClick(
            picker.choices.viewport(),
            Qt.MouseButton.LeftButton,
            pos=picker.choices.visualItemRect(picker.choices.item(0)).center(),
        )
        assert picker.selected is None and picker.text() == "所有账户"
        until(lambda: len(page.rows) == 50)
        late: Future[tuple[AuditAccount, ...]] = Future()

        def delayed(
            search: str = "",
            cursor: tuple[str, str] | None = None,
            revision: int | None = None,
        ) -> Future[tuple[AuditAccount, ...]]:
            return late

        monkeypatch.setattr(identity.audit, "accounts", delayed)
        picker.click()
        identity.logout()
        window.identity_controls.refresh()
        late.set_result((AuditAccount(former.account_id, former.username),))
        qapp.processEvents()
        assert (
            not picker.popup.isVisible()
            and picker.choices.count() == 0
            and picker.selected is None
        )
        assert not page.rows
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        late = Future()
        window.history_tabs.setCurrentWidget(window.audit_page)
        picker.click()
        close_window(window)
        closed = True
        late.set_result((AuditAccount(former.account_id, former.username),))
        qapp.processEvents()
    finally:
        if not closed:
            close_window(window)
        delivery.detach()


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
        provision_account(identity, "Shift", Role.ENGINEER, TEST_PASSWORD)
        provision_account(identity, "Line", Role.OPERATOR, TEST_PASSWORD)
        for i in range(58):
            op = AuditOperation(
                "stable",
                "Archived user",
                "recipe.save",
                "Lot " + str(i),
                "old",
                "new",
                utc=datetime(2025, 2, 1, 12)
                .astimezone(UTC)
                .isoformat(timespec="microseconds"),
            )
            identity.audit.record(op, "success")
        window.show()
        window.navigate(3)
        assert window.pages.count() == 5 and window.history_tabs.count() == 5
        window.history_tabs.setCurrentWidget(window.audit_page)
        until(lambda: len(page.rows) == 50)
        identity.login("Shift", TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        until(lambda: len(page.rows) == 50)
        select_account(page, "Archived user")
        window.records_filters.start.set_date(QDate(2025, 2, 1))
        window.records_filters.end.set_date(QDate(2025, 2, 1))
        page.refresh()
        until(lambda: bool(page.rows) and page.rows[0].operation.target == "Lot 57")
        columns = [page.table.columnWidth(i) for i in range(5)]
        page.older.click()
        until(lambda: len(page.rows) == 8)
        assert not page.older.isEnabled() and page.newer.isEnabled()
        page.table.selectRow(7)
        target = page.table.item(7, 3)
        result = page.table.item(7, 4)
        assert target and target.text() == target.toolTip() == text(
            "page_recipe", window.language
        )
        assert result and result.text() == text("records_success", window.language)
        assert (
            result.toolTip()
            == result.data(Qt.ItemDataRole.AccessibleTextRole)
            == result.text()
        )
        assert "old" in page.details.toPlainText()
        assert (
            page.rows[7].operation.target == "Lot 0"
            and "Lot 0" not in page.details.toPlainText()
        )
        assert columns == [page.table.columnWidth(i) for i in range(5)]
        page.newer.click()
        until(lambda: len(page.rows) == 50)
        assert not page.newer.isEnabled()
        window.records_filters.start.set_date(QDate(9999, 12, 31))
        window.records_filters.end.set_date(QDate(9999, 12, 31))
        until(lambda: not page.rows)
        assert not page.details.toPlainText()
        window.records_filters.start.set_date(QDate(2025, 2, 1))
        window.records_filters.end.set_date(QDate(2025, 2, 1))
        until(lambda: len(page.rows) == 50)
        private_rows = page.rows
        late: Future[tuple[AuditRecord, ...]] = Future()
        replacement: Future[tuple[AuditRecord, ...]] = Future()
        pending = iter((late, replacement))

        def delayed(
            query: AuditQuery, revision: int | None
        ) -> Future[tuple[AuditRecord, ...]]:
            return next(pending)

        monkeypatch.setattr(identity, "audit_page", delayed)
        page.refresh()
        assert page.message.text() == text("history_loading", window.language)
        assert not page.newer.isEnabled() and not page.older.isEnabled()
        late.set_exception(RuntimeError("private query details"))
        monkeypatch.setattr(
            identity.audit, "observation", lambda: (1, "Audit write failure")
        )
        unavailable = text("history_unavailable", window.language)
        until(lambda: unavailable in page.message.text())
        assert "RuntimeError" in page.message.accessibleDescription()
        assert "private query details" not in page.message.toolTip()
        assert "Audit write failure" in page.message.toolTip()
        page.refresh()
        identity.logout()
        window.identity_controls.refresh()
        replacement.set_result(private_rows)
        qapp.processEvents()
        for role in (Role.GUEST, Role.OPERATOR):
            if role == Role.OPERATOR:
                identity.login("Line", TEST_PASSWORD).result(WAIT)
                window.identity_controls.refresh()
            assert not window.history_tabs.isTabEnabled(
                window.history_tabs.indexOf(window.audit_page)
            )
            assert window.history_tabs.currentIndex() == 0 and not page.rows
            assert not page.details.toPlainText() and page.table.rowCount() == 0
            with pytest.raises(AuthorizationError):
                identity.audit.page(AuditQuery())
            with pytest.raises(AuthorizationError):
                identity.audit.accounts()
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize(
    ("day", "hours"), [(QDate(2025, 3, 9), 23), (QDate(2025, 11, 2), 25)]
)
def test_calendar_uses_both_local_midnights(
    day: QDate, hours: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    zone = QTimeZone(b"America/New_York")
    assert zone.isValid()
    monkeypatch.setattr(QTimeZone, "systemTimeZone", lambda: zone)
    start, end = local_day_bounds(day)
    assert end is not None and end - start == hours * 3600
    assert audit_utc_bound(start).endswith(
        "05:00:00+00:00" if hours == 23 else "04:00:00+00:00"
    )
    assert audit_utc_bound(end).endswith(
        "04:00:00+00:00" if hours == 23 else "05:00:00+00:00"
    )


def test_local_date_filters_use_recorded_instants_and_keep_full_details(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    identity, history = app.identity, app.history
    assert identity and history
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    # All four observations are retained. Only the two inside the local date match.
    midnight = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=2)
    start, end = midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()
    target = "<inspection & calibration> " + "assembly/" * 32
    before, after = (
        "limit =  15\nlabel = <original>",
        "limit =  18\nlabel = <requested>",
    )
    detail = "denied <change> & settings preserved"
    try:
        for index, stamp in enumerate((start - 0.001, start, end - 0.001, end), 1):
            context = HistoryContext(
                stamp,
                "segment",
                None,
                "boundary-run",
                ConfigurationIdentity("recipe", "model", "c" * 64),
                "Boundary",
            )
            assert history.offer(context, production(index))
            op = AuditOperation(
                "actor-id",
                "Boundary",
                "recipe.save",
                target,
                before,
                after,
                id=f"boundary-{index}",
                utc=datetime.fromtimestamp(stamp, UTC).isoformat(
                    timespec="microseconds"
                ),
            )
            identity.audit.intent(op).result(WAIT)
            identity.audit.complete(op, "denied", detail).result(WAIT)
            history.offer_alarms(
                (
                    AlarmEvent(
                        stamp,
                        f"fault-{index}",
                        "activated",
                        "software",
                        "软件故障",
                        "Software fault",
                        "fins_tcp://127.0.0.1:9600" if index == 2 else "application",
                        None,
                        None,
                        None,
                        "",
                        None,
                        None,
                        "startup · TimeoutError" if index == 3 else detail,
                    ),
                )
            )

        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentWidget(window.history)
        date = QDate(midnight.year, midnight.month, midnight.day)
        inspection, audit, alarms = window.history, window.audit_page, window.alarm_page
        window.records_filters.start.set_date(date)
        window.records_filters.end.set_date(date)
        until(lambda: not inspection.busy and inspection.cycles.rowCount() == 2)
        for row, expected in enumerate(("3", "2")):
            cycle = inspection.cycles.item(row, 1)
            assert cycle and cycle.text() == expected
        window.history_tabs.setCurrentWidget(window.audit_page)
        select_account(audit, "Boundary")
        audit.refresh()
        until(lambda: len(audit.rows) == 2)
        assert [r.operation.id for r in audit.rows] == ["boundary-3", "boundary-2"]
        audit_text = audit.details.toPlainText()
        assert all(
            v in audit_text
            for v in (
                "limit",
                " 15",
                " 18",
                "<original>",
                "<requested>",
                detail,
                "Requested value",
            )
        )
        assert audit.rows[0].operation.before == before
        assert audit.rows[0].operation.after == after
        assert "After" not in audit_text and audit.details.isReadOnly()
        assert all(v not in audit_text for v in ("Recipe", "Boundary", target))
        assert audit.rows[0].operation.target == target

        window.history_tabs.setCurrentWidget(window.alarm_page)
        alarms.refresh()
        until(lambda: len(alarms.rows) == 2)
        assert [r.event.signal for r in alarms.rows] == ["fault-3", "fault-2"]
        sources = [alarms.table.item(row, 1) for row in (0, 1)]
        assert [cell.text() for cell in sources if cell] == ["Software", "PLC"]
        alarm_text = alarms.details.toPlainText()
        assert "Startup failed · Timeout" in alarm_text
        assert all(
            v not in alarm_text
            for v in ("Software", "application", "fault-3", "TimeoutError", "Batch")
        )
        assert alarms.rows[0].event.detail == "startup · TimeoutError"
        alarms.table.selectRow(1)
        assert detail in alarms.details.toPlainText()
        for table in (inspection.cycles, audit.table, alarms.table):
            cell = table.item(0, 0)
            assert cell is not None
            value = datetime.fromisoformat(cell.toolTip())
            assert value.utcoffset() is not None and value.timestamp() == end - 0.001
            assert value.strftime("%m-%d %H:%M:%S") in cell.text()
    finally:
        close_window(window)
        delivery.detach()


def test_targets_show_recorded_names_and_details_only_add_meaningful_changes(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    identity = startup.application.identity
    assert identity
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page = window.audit_page
    actor, other = "a1" * 16, "7586613b-98d5-4129-83ee-9552800da1eb"
    digest = "b4" * 32
    recipe_name = "Yellow capsules for the secondary packaging line with the revised coating specification"
    # action, stored target, prior value, requested value, outcome, operator caption
    cases = (
        ("session.login", actor, "", "", "success", "Reviewer"),
        ("session.login", other, "", "", "failed", "Account"),
        ("account.role", other, "OPERATOR", "ENGINEER", "success", "Account"),
        ("account.enabled", other, "True", "False", "success", "Account"),
        ("account.create", other, "", "Lee · ENGINEER · enabled", "success", "Lee"),
        ("production.stop", other, "", "", "success", "Production Run"),
        ("batch.end", other, "LOT-4", "", "success", "LOT-4"),
        ("recipe.save", "recipe-2", "", "", "success", "Recipe"),
        (
            "recipe.save",
            "recipe-2",
            "",
            f"display_name = {recipe_name}",
            "success",
            recipe_name,
        ),
        (
            "recipe.save",
            "20261106-kmrdj2j7",
            "display_name = Blue",
            "display_name = Green",
            "failed",
            "Blue",
        ),
        ("model.stage", digest, "", "Capsule.vimosln", "success", "Capsule.vimosln"),
        (
            "model.publish",
            digest[:12],
            "",
            "Blue · Segmentation · " + digest,
            "success",
            "Blue",
        ),
        ("model.cache", digest[:12], "", "device 0", "success", "Model"),
        (
            "communication.save",
            "communication.json",
            "diagnostic.timeout = 2.0",
            "diagnostic.timeout = 1.0",
            "failed",
            text("page_communication", "en"),
        ),
        ("security.admission", "x7Nq" * 12, "", "", "denied", "—"),
        ("security.admission", "JD9-xyZ/" * 6, "", "", "denied", "—"),
    )
    try:
        for index, (action, target, before, after, outcome, _) in enumerate(cases):
            op = AuditOperation(
                actor, "Reviewer", action, target, before, after, id=f"target-{index}"
            )
            identity.audit.intent(op).result(WAIT)
            issue = "ConfigurationIOError" if action == "communication.save" else ""
            identity.audit.complete(op, outcome, issue).result(WAIT)
        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentWidget(window.audit_page)
        select_account(page, "Reviewer")
        page.refresh()
        until(lambda: len(page.rows) == len(cases))
        for row, case in enumerate(reversed(cases)):
            _, raw, _, _, _, expected = case
            cell = page.table.item(row, 3)
            assert cell and cell.text() == expected
            page.table.selectRow(row)
            if case[0] == "communication.save":
                assert all(
                    value in page.details.toPlainText()
                    for value in ("Timeout (seconds)", "Requested value", "2.0", "1.0")
                )
                assert text("records_config_io", "en") in page.details.toPlainText()
                assert page.rows[row].detail == "ConfigurationIOError"
            elif case[0] in ("account.role", "account.enabled"):
                (comparison,) = page.details.document().rootFrame().childFrames()
                assert isinstance(comparison, QTextTable)
                assert comparison.columns() == 3 and comparison.rows() == 2
            if case[0] == "account.enabled":
                action = page.table.item(row, 2)
                assert action and action.text() == action.toolTip() == "Disable account"
                assert all(
                    text(key, "en") in page.details.toPlainText()
                    for key in ("identity_enabled", "identity_disabled")
                )
            elif case[0] == "account.create":
                assert (
                    "Lee" not in page.details.toPlainText()
                    and "Engineer" in page.details.toPlainText()
                )
            elif case[0] == "model.publish":
                assert "Module ID Segmentation" in page.details.toPlainText()
            elif expected == recipe_name:
                (recorded,) = page.details.document().rootFrame().childFrames()
                assert isinstance(recorded, QTextTable)
                assert recorded.columns() == recorded.rows() == 2
                assert recorded.cellAt(0, 1).firstCursorPosition().block().text() == (
                    "Recorded value · After"
                )
                assert recorded.cellAt(1, 1).firstCursorPosition().block().text() == (
                    recipe_name
                )
                assert page.table.fontMetrics().horizontalAdvance(
                    recipe_name
                ) > page.table.columnWidth(3)
                assert (
                    cell.toolTip()
                    == cell.data(Qt.ItemDataRole.AccessibleTextRole)
                    == recipe_name
                )
            elif case[0] in (
                "session.login",
                "production.stop",
                "batch.end",
                "model.stage",
                "security.admission",
            ):
                assert page.details.isHidden() and not page.details.toPlainText()
            assert page.rows[row].operation.target == raw
            assert raw not in page.details.toPlainText()
        identity.logout()
        window.identity_controls.refresh()
        assert (
            not page.rows and page.details.isHidden() and not page.details.toPlainText()
        )
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_narrow_audit_details_keep_aligned_values_and_plain_fallback(
    qapp: QApplication, tmp_path: Path, language: str
) -> None:
    assert language in ("zh", "en")
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    identity = app.identity
    assert identity
    page = AuditPage(identity)
    long_note = (
        "线下样品核对记录" * 39 if language == "zh" else "UnbrokenReviewLabel" * 16
    )
    try:
        identity.audit.record(
            AuditOperation(
                "actor",
                "Reviewer",
                "batch.information",
                "REVIEW-LOT",
                "notes = Prior note\nproduct = Prior",
                f"product = New\nnotes = {long_note}",
            ),
            "success",
        )
        identity.audit.record(
            AuditOperation(
                "actor",
                "Reviewer",
                "recipe.save",
                "recipe",
                "product_code = Prior\n…",
                "display_name = New",
            ),
            "stale",
            "ConfigurationIOError",
        )
        ensure_test_fonts()
        page.set_language(language)
        page.resize(560, 430)
        page.show()
        page.refresh()
        until(lambda: len(page.rows) >= 2)
        for row, record in enumerate(page.rows[:2]):
            page.table.selectRow(row)
            QApplication.processEvents()
            frames = page.details.document().rootFrame().childFrames()
            assert page.details.isReadOnly()
            assert page.details.accessibleName() == text("audit_details", language)
            if record.operation.action == "batch.information":
                assert len(frames) == 1 and isinstance(frames[0], QTextTable)
                table = frames[0]
                assert table.columns() == table.rows() == 3
                expected = (
                    (
                        text("audit_field", language),
                        text("audit_before", language),
                        text("audit_after", language),
                    ),
                    (text("batch_notes", language), "Prior note", long_note),
                    (text("batch_product", language), "Prior", "New"),
                )
                for r, values in enumerate(expected):
                    for c, value in enumerate(values):
                        assert (
                            table.cellAt(r, c).firstCursorPosition().block().text()
                            == value
                        )
                assert page.details.horizontalScrollBar().maximum() == 0
                assert page.details.verticalScrollBar().maximum() > 0
            else:
                assert len(frames) == 2
                assert all(
                    isinstance(frame, QTextTable) and frame.columns() == 2
                    for frame in frames
                )
                details = page.details.toPlainText()
                assert text("audit_recorded_before", language) in details
                assert text("audit_recorded_requested", language) in details
                assert (
                    audit_text("product_code", language) in details
                    and "Prior" in details
                )
                assert text("display_name", language) in details and "New" in details
                incomplete = text("audit_config_summary_incomplete", language).format(
                    fields=16, value=96, side=2048
                )
                assert details.lstrip().startswith(incomplete)
                assert page.details.horizontalScrollBar().maximum() == 0
                for locale in ("zh-TW", "de", "hi", "ti"):
                    page.set_language(locale)
                    assert page.table.currentRow() == row
                    value = page.table.item(row, 4)
                    assert value and value.text() == outcome_caption("stale", locale)
                    assert value.toolTip() == value.data(
                        Qt.ItemDataRole.AccessibleTextRole
                    )
                    assert value.toolTip() == outcome_caption(
                        "stale", locale, tooltip=True
                    )
                    details = page.details.toPlainText()
                    assert text("audit_recorded_before", locale) in details
                    assert text("audit_recorded_requested", locale) in details
                    assert (
                        audit_text("product_code", locale) in details
                        and "Prior" in details
                    )
                    assert text("display_name", locale) in details and "New" in details
                    assert page.details.horizontalScrollBar().maximum() == 0
                page.set_language(language)
    finally:
        page.dispose()
        page.close()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_record_segments_filters_and_columns_stay_usable_in_both_profiles(
    qapp: QApplication, tmp_path: Path
) -> None:
    ensure_test_fonts()
    startup = authenticated_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.navigate(3)
        tabs = window.history_tabs
        bar = tabs.tabBar()
        for width, height in ((1080, 1920), (1920, 1080)):
            window.resize(width, height)
            window.set_theme("dark" if width < height else "light")
            qapp.processEvents()
            slots = tuple(bar.tabRect(i) for i in range(5))
            assert all(r.width() == 132 and r.height() == 44 for r in slots)
            base_filters: dict[int, tuple[QRect, ...]] = {}
            base_columns: dict[int, tuple[int, ...]] = {}
            export_slot: QRect | None = None
            common_pagers: set[tuple[tuple[int, int, int, int], ...]] = set()
            for lang in LANGUAGES:
                window.set_language(lang)
                for i, key in enumerate(
                    (
                        "records_batches",
                        "records_ng_capsules",
                        "short_cycles",
                        "records_audit",
                        "records_alarms",
                    )
                ):
                    QTest.mouseClick(
                        bar, Qt.MouseButton.LeftButton, pos=slots[i].center()
                    )
                    qapp.processEvents()
                    assert tabs.currentIndex() == i and slots[i] == bar.tabRect(i)
                    assert tabs.tabText(i) == ("NG" if i == 1 else text(key, lang))
                    assert bar.accessibleTabName(i) == text(
                        "audit_inspection" if i == 2 else key, lang
                    )
                    assert not tabs.tabIcon(i).isNull() and tabs.tabToolTip(i)
                    destination = (
                        "records_batches",
                        "records_ng_capsules",
                        "audit_inspection",
                        "audit_trail",
                        "alarm_history",
                    )[i]
                    assert tabs.tabToolTip(i) == text(destination, lang)
                    page = (
                        window.batches_page,
                        window.ng_capsules,
                        window.history,
                        window.audit_page,
                        window.alarm_page,
                    )[i]
                    table = (
                        window.batches_page.table,
                        window.ng_capsules.table,
                        window.history.cycles,
                        window.audit_page.table,
                        window.alarm_page.table,
                    )[i]
                    until(
                        lambda table=table: table.horizontalScrollBar().maximum() == 0
                    )
                    assert (
                        sum(table.columnWidth(c) for c in range(table.columnCount()))
                        == table.viewport().width()
                    )
                    columns = tuple(
                        table.columnWidth(c) for c in range(table.columnCount())
                    )
                    assert base_columns.setdefault(i, columns) == columns
                    filters = tuple(
                        QRect(w.mapTo(page, QPoint()), w.size())
                        for w in (
                            window.records_filters.start,
                            window.records_filters.end,
                            page.newer,
                            page.older,
                            page.refresh_button,
                        )
                    )
                    assert base_filters.setdefault(i, filters) == filters
                    assert filters[0].left() < filters[1].left() < filters[2].left()
                    pagers = (page.newer, page.older, page.refresh_button)
                    rectangles = tuple(
                        QRect(w.mapTo(tabs, QPoint()), w.size()) for w in pagers
                    )
                    common_pagers.add(
                        tuple((r.x(), r.y(), r.width(), r.height()) for r in rectangles)
                    )
                    assert len(common_pagers) == 1
                    report = window.records_export
                    if page is window.history:
                        assert report.isHidden() and not report.isEnabled()
                        continue
                    assert (
                        report.parentWidget() is tabs.cornerWidget()
                        and report.isVisible()
                    )
                    header = QRect(report.mapTo(tabs, QPoint()), report.size())
                    assert header.top() == bar.y()
                    assert report.visibleRegion().boundingRect() == report.rect()
                    if export_slot is None:
                        export_slot = header
                    assert header == export_slot
                    assert report.text() == "PDF" and not report.icon().isNull()
                    assert header.left() > bar.tabRect(4).right()
                    assert header.intersects(QRect(0, 0, tabs.width(), bar.height()))
                    assert header.right() == rectangles[-1].right()
                    assert header.bottom() < rectangles[-1].top()
            for i, event_page in enumerate((window.audit_page, window.alarm_page), 3):
                tabs.setCurrentIndex(i)
                qapp.processEvents()
                for language in LANGUAGES:
                    event_page.set_language(language)
                    table = event_page.table
                    assert (
                        tuple(table.columnWidth(c) for c in range(5)) == base_columns[i]
                    )
                    for c in range(5):
                        title = table.horizontalHeaderItem(c)
                        assert title and title.toolTip() == title.data(
                            Qt.ItemDataRole.AccessibleTextRole
                        )
                    if i == 3:
                        outcome_width = table.columnWidth(4)
                        for outcome in (
                            "success",
                            "failed",
                            "stale",
                            "denied",
                            "uncertain",
                            "cancelled",
                            "unresolved",
                        ):
                            assert (
                                table.fontMetrics().horizontalAdvance(
                                    outcome_caption(outcome, language)
                                )
                                < outcome_width
                            ), (language, outcome)
    finally:
        close_window(window)
        delivery.detach()
