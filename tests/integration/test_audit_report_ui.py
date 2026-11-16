"""Audit uses the existing compact, context-only Records export dialog."""

from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from openpyxl import load_workbook
from openpyxl.styles import Alignment
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument, QTextTable
from PySide6.QtWidgets import QApplication, QComboBox
from reportlab.pdfgen.canvas import Canvas
from tests.integration.test_audit_ui import select_account
from tests.integration.test_history_report import choose_file
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data
from tests.unit.test_audit_report import fill

from capsule_vision.application import ProductionApplication
from capsule_vision.application.audit import (
    AUDIT_CHANGE_LIMIT,
    AuditOperation,
    AuditQuery,
)
from capsule_vision.application.audit_report import AuditReportRequest
from capsule_vision.application.identity_models import AuthenticationError
from capsule_vision.application.manual_plc import execute_manual, manual_target
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_scope import HistoryQuery, ReportRequest
from capsule_vision.communication.endpoints import FinsUdpEndpoint
from capsule_vision.communication.fins import MAX_WRITE_WORDS, FinsAddress
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)
from capsule_vision.configuration import load_recipe, save_recipe
from capsule_vision.localization.audit_wording import audit_text
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui.audit_page import AuditPage
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.report_dialog import ReportDialog


def audit_report_texts(
    app: ProductionApplication,
    directory: Path,
    language: Language,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    drawn: list[str] = []
    draw = Canvas.drawString

    def capture(canvas: Canvas, x: float, y: float, value: str) -> None:
        drawn.append(value)
        draw(canvas, x, y, value)

    request = AuditReportRequest()
    pdf, excel = (
        directory / f"audit-{language}.pdf",
        directory / f"audit-{language}.xlsx",
    )
    with monkeypatch.context() as capture_pdf:
        capture_pdf.setattr(Canvas, "drawString", capture)
        app.export_report(request, pdf, language).result(WAIT)
    assert pdf.read_bytes().startswith(b"%PDF") and drawn
    app.export_report(request, excel, language).result(WAIT)
    with closing(load_workbook(excel)) as book:
        cells = "\n".join(
            str(value)
            for row in book.worksheets[0].values
            for value in row
            if value is not None
        )
    return "\n".join(drawn), cells


@pytest.mark.parametrize("language", ("zh", "en"))
def test_audit_wording_follows_recorded_evidence_in_qt_and_customer_exports(
    language: Language,
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = authenticated_startup(saved_data(tmp_path)).application
    identity = app.identity
    assert identity
    page = AuditPage(identity)
    plc = "FINS/TCP 192.0.2.43:9600 130 220 × 2"
    private_name = "C:/PrivatePathSentinel/engineering.txt"
    cases = (
        (
            "pending",
            "communication.save",
            "communication.json",
            "diagnostic.timeout = 1.0",
            "diagnostic.timeout = 2.0",
            "unresolved",
            "",
        ),
        ("ack", "plc.write", plc, "", "[17, 65535]", "success", ""),
        (
            "uncertain",
            "plc.write",
            plc,
            "",
            "[15, 65535]",
            "uncertain",
            "FinsTimeoutError",
        ),
        (
            "blocked",
            "plc.write",
            "FINS/TCP 192.0.2.43:9600 177 300 × 2",
            "",
            "",
            "denied",
            "Manual write overlaps active PLC status W301, W302",
        ),
        ("denied", "plc.write", plc, "", "", "denied", "AuthorizationError"),
        ("rejection", "plc.write", plc, "", "[11, 65535]", "failed", "FinsPlcError"),
        (
            "legacy",
            "plc.write",
            plc,
            "",
            "[9, 65535]",
            "failed",
            "FinsTcpEnvelopeError",
        ),
        (
            "process-value",
            "recipe.process_value",
            "recipe-internal",
            "Application " + "a1" * 16,
            "PrivateParameterSentinel: D108–D109 = 320",
            "success",
            "Register readback matched",
        ),
        (
            "process-apply",
            "recipe.process_apply",
            "recipe-internal",
            "",
            f"Recipe SHA256 {'b4' * 32}; 192.0.2.43:9600; W16.1; 1 values; {private_name}",
            "success",
            "1 register values verified; no ladder-consumption confirmation",
        ),
        (
            "no-readback",
            "recipe.process_value",
            "recipe-internal",
            "",
            "PrivateParameterSentinel: D108–D109 = 420",
            "success",
            "Vendor completion",
        ),
        (
            "former",
            "account.role",
            "c2" * 16,
            "OPERATOR",
            "ENGINEER",
            "success",
            "Former Engineer",
        ),
        (
            "redacted",
            "account.reset_password",
            "c2" * 16,
            "",
            "",
            "success",
            private_name,
        ),
        (
            "stale",
            "account.role",
            "c2" * 16,
            "",
            "",
            "stale",
            "Former private candidate",
        ),
        (
            "activated",
            "production.start",
            "d3" * 16,
            "",
            "recorded-recipe-id",
            "success",
            "",
        ),
    )
    zh = language == "zh"
    pending = "无完成记录" if zh else "No completion recorded"
    uncertain = "执行结果不确定" if zh else "Execution uncertain"
    denied = "已拒绝" if zh else "Denied"
    protection = (
        "写入已被阻止，地址受保护" if zh else "Write blocked for a protected address"
    )
    acknowledgement = "PLC 已确认写入请求" if zh else "PLC acknowledged the write"
    unknown = "无法确认是否写入" if zh else "Write execution unknown"
    rejection = "PLC 拒绝写入" if zh else "PLC rejected the write"
    matched = "回读一致" if zh else "Readback matched"
    verified = (
        "所请求的寄存器值回读一致"
        if zh
        else "Requested register values matched on readback"
    )
    requested = "请求值" if zh else "Requested value"
    account = "账户" if zh else "Account"
    run = "生产运行" if zh else "Production Run"
    expected_results = {
        "pending": pending,
        "uncertain": uncertain,
        "blocked": denied,
        "denied": denied,
    }
    expected_details = {
        "pending": requested,
        "ack": acknowledgement,
        "uncertain": unknown,
        "blocked": protection,
        "rejection": rejection,
        "process-apply": verified,
    }
    query = AuditQuery()
    try:
        for row_id, action, target, before, after, outcome, reason in cases:
            op = AuditOperation(
                "synthetic-user", "Reviewer", action, target, before, after, id=row_id
            )
            identity.audit.intent(op).result(WAIT)
            if outcome != "unresolved":
                identity.audit.complete(op, outcome, reason).result(WAIT)
        stored = identity.audit_page(query).result(WAIT)
        page.set_language(language)
        page.show()
        page.refresh()
        until(lambda: {c[0] for c in cases} <= {r.operation.id for r in page.rows})
        for i, record in enumerate(page.rows):
            row_id = record.operation.id
            page.table.selectRow(i)
            target = page.table.item(i, 3)
            result = page.table.item(i, 4)
            assert target is not None and result is not None
            detail = page.details.toPlainText()
            if row_id in expected_results:
                assert result.text() == result.toolTip() == expected_results[row_id]
                assert result.data(Qt.ItemDataRole.AccessibleTextRole) == result.text()
            if row_id in expected_details:
                assert expected_details[row_id] in detail
            if row_id == "pending":
                assert "After" not in detail and "变更后" not in detail
            if row_id == "ack":
                assert requested in detail and matched not in detail
            if row_id in ("process-value", "no-readback"):
                expected = [f"{requested} {record.operation.after}"]
                if row_id == "process-value":
                    expected.append(matched)
                assert detail.splitlines() == expected
                assert (
                    result.text()
                    == result.toolTip()
                    == text("records_success", language)
                )
            if row_id == "denied":
                assert not detail
            if row_id == "legacy":
                assert "FinsTcpEnvelopeError" in detail
                assert "not sent" not in detail and "未发送" not in detail
            if row_id in ("former", "redacted", "stale", "activated"):
                caption = {"former": "Former Engineer", "activated": run}.get(
                    row_id, account
                )
                assert target.text() == target.toolTip() == caption
                assert private_name not in detail and "d3" * 16 not in detail
            if row_id == "stale":
                assert "Former private candidate" in detail
            if row_id == "activated":
                assert ("配方 ID" if zh else "Recipe ID") in detail
        for rendered in audit_report_texts(app, tmp_path, language, monkeypatch):
            assert f"{requested}: D108–D109 = 320\n{matched}" in rendered
            assert f"{requested}: D108–D109 = 420" in rendered.splitlines()
            assert f"{'变更后' if zh else 'After'}: D108–D109" not in rendered
            assert rendered.splitlines().count(matched) == 1
            packed = "".join(rendered.split())
            for value in (
                pending,
                uncertain,
                denied,
                protection,
                acknowledgement,
                unknown,
                rejection,
                matched,
                verified,
                requested,
                "Former Engineer",
                run,
                "保存通讯设置" if zh else "Save communication settings",
            ):
                assert "".join(value.split()) in packed
            for private in (
                "192.0.2.43",
                private_name,
                "PrivateParameterSentinel",
                "recipe-internal",
                "Former private candidate",
                "recorded-recipe-id",
                "d3" * 16,
                "Operation incomplete",
                "FinsTcpEnvelopeError",
                "W301",
                "W302",
                "未发送",
                "not sent",
                "Manual write overlaps",
                "no ladder-consumption confirmation",
            ):
                assert "".join(private.split()) not in packed
        assert identity.audit_page(query).result(WAIT) == stored
    finally:
        page.dispose()
        page.close()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_recipe_selection_reports_the_installed_historical_name(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    recipe_id = "selection-id-sentinel"
    path = data.recipes / f"{recipe_id}.json"
    recipe = replace(
        load_recipe(data.recipes / "local.json"),
        recipe_id=recipe_id,
        display_name="Outdated picker caption",
    )
    save_recipe(path, recipe)
    startup = authenticated_startup(data)
    app, identity = startup.application, startup.application.identity
    assert identity is not None
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    legacy_id = "legacy-selection-id"
    earlier = "Ivory capsule / 象牙色胶囊"
    renamed = "=Matte capsule / 哑光胶囊"
    literal = "<b>Gloss capsule</b>"
    private = "C:/PrivateSelectionSentinel/192.0.2.43"
    failed = "Failed choice sentinel"
    query = AuditQuery(action="production.recipe")
    try:
        window.show()
        picker = window.production_recipes
        index = picker.recipes.findData(recipe_id)
        assert picker.recipes.itemText(index) == recipe.display_name
        assert not identity.audit_page(query).result(WAIT)
        actor = identity.snapshot()
        assert actor.account is not None
        legacy = AuditOperation.by(actor, query.action, legacy_id, after=legacy_id)
        identity.audit.record(legacy, "success")

        expected = {legacy.id: ""}
        for name in (earlier, renamed, literal, private):
            save_recipe(path, replace(recipe, display_name=name))
            # The picker need not reload before selection reads the current saved file.
            picker.recipes.activated.emit(picker.recipes.findData(recipe_id))
            until(lambda: not window.editing.busy)
            state = app.snapshot()
            assert state.candidate_prepared is not None and state.active is None
            assert state.candidate_prepared.configuration.display_name == name
            record = identity.audit_page(query).result(WAIT)[0]
            op = record.operation
            assert op.id not in expected and op.target == recipe_id
            assert op.actor_id == actor.account.account_id
            assert record.outcome == "success" and record.selected_recipe_name == name
            assert op.before == op.after == ""
            expected[op.id] = "" if name == private else name

        save_recipe(
            path, replace(recipe, display_name=failed, model_id="missing-model")
        )
        picker.recipes.activated.emit(picker.recipes.findData(recipe_id))
        until(lambda: not window.editing.busy)
        observed = identity.audit_page(query).result(WAIT)
        failure = observed[0]
        assert failure.operation.target == recipe_id
        assert failure.outcome == "failed" and not failure.selected_recipe_name
        expected[failure.operation.id] = ""
        assert {r.operation.id for r in observed} == expected.keys()
        assert not identity.audit_page(AuditQuery(action="production.start")).result(
            WAIT
        )
        path.unlink()
        picker.reload()

        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        page = window.audit_page
        until(lambda: expected.keys() <= {r.operation.id for r in page.rows})
        for language in ("zh", "en"):
            window.set_language(language)
            fallback = text("page_recipe", language)
            for row, record in enumerate(page.rows):
                if record.operation.id not in expected:
                    continue
                caption = expected[record.operation.id] or fallback
                page.table.selectRow(row)
                item = page.table.item(row, 3)
                assert item is not None
                assert item.text() == item.data(Qt.ItemDataRole.AccessibleTextRole)
                assert item.text() == caption
                if caption == literal:
                    tooltip = QTextDocument()
                    tooltip.setHtml(item.toolTip())
                    assert tooltip.toPlainText() == literal
                else:
                    assert item.toolTip() == caption
                detail = page.details.toPlainText()
                if record.outcome == "success":
                    assert not detail and page.details.isHidden()
                else:
                    assert failed not in detail

            for rendered in audit_report_texts(app, tmp_path, language, monkeypatch):
                packed = "".join(rendered.split())
                for shown in (earlier, renamed, literal):
                    assert packed.count("".join(shown.split())) == 1
                for hidden in (
                    recipe_id,
                    legacy_id,
                    recipe.display_name,
                    private,
                    failed,
                ):
                    assert "".join(hidden.split()) not in packed
            with closing(load_workbook(tmp_path / f"audit-{language}.xlsx")) as book:
                action = (
                    "选择生产配方" if language == "zh" else "Select Production Recipe"
                )
                targets = [
                    row[3]
                    for row in book.worksheets[0].iter_rows(min_col=1, max_col=5)
                    if row[2].value == action
                ]
                assert [cell.value for cell in targets] == [
                    expected[record.operation.id] or fallback for record in observed
                ]
                assert all(cell.data_type == "s" for cell in targets)
        assert not path.exists()
        assert identity.audit_page(query).result(WAIT) == observed
    finally:
        close_window(window)
        delivery.detach()


def test_misplaced_login_credential_and_historical_payloads_are_private(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, identity = startup.application, startup.application.identity
    assert identity
    actor = identity.snapshot()
    assert actor.account
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        with pytest.raises(AuthenticationError):
            identity.login(TEST_PASSWORD, "different synthetic password").result(WAIT)
        assert identity.snapshot() == actor
        query = AuditQuery(action="session.login")
        failed, success = identity.audit_page(query).result(WAIT)
        assert failed.outcome == "failed" and failed.detail == "AuthenticationError"
        assert failed.operation.actor_id == actor.account.account_id
        assert failed.operation.username == actor.account.username
        assert (
            failed.operation.target,
            failed.operation.before,
            failed.operation.after,
        ) == ("", "", "")
        assert success.operation.target == actor.account.account_id
        assert success.outcome == "success"
        historical = "Historical credential input"
        for outcome in ("failed", "cancelled", "stale"):
            op = AuditOperation.by(
                actor, "session.login", historical, historical, historical
            )
            identity.audit.intent(op).result(WAIT)
            identity.audit.complete(
                op, outcome, "An earlier message: " + historical
            ).result(WAIT)
        evidence = identity.audit_page(query).result(WAIT)
        database = identity.audit.path.read_bytes()
        assert TEST_PASSWORD.encode() not in database
        assert historical.encode() in database

        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        page = window.audit_page
        expected = {row.operation.id for row in evidence}
        until(lambda: expected <= {row.operation.id for row in page.rows})
        for index, row in enumerate(page.rows):
            if row.operation.action != "session.login":
                continue
            page.table.selectRow(index)
            target = page.table.item(index, 3)
            assert target
            if row.outcome == "success":
                assert target.text() == "TestAdmin"
            else:
                assert target.text() == "Account"
            presented = [
                page.details.toPlainText(),
                page.details.toolTip(),
                page.details.accessibleDescription(),
            ]
            for column in range(page.table.columnCount()):
                cell = page.table.item(index, column)
                assert cell
                presented.extend(
                    (
                        cell.text(),
                        cell.toolTip(),
                        str(cell.data(Qt.ItemDataRole.AccessibleTextRole)),
                        str(cell.data(Qt.ItemDataRole.AccessibleDescriptionRole)),
                    )
                )
            assert all(
                secret not in "\n".join(presented)
                for secret in (historical, TEST_PASSWORD)
            )
        for output in audit_report_texts(app, tmp_path, "en", monkeypatch):
            assert "TestAdmin" in output and "Sign in" in output
            assert "Invalid credentials or account unavailable." in output
            assert historical not in output and TEST_PASSWORD not in output
        assert identity.audit_page(query).result(WAIT) == evidence
        assert identity.audit.path.read_bytes() == database
    finally:
        close_window(window)
        delivery.detach()


def test_customer_exports_of_actual_saves_omit_internal_configuration(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app, editor = startup.application, startup.editor
    identity = app.identity
    assert identity and editor and editor.entry
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    archive = "C:/EngineeringFixture/PrivateArchiveSentinel"
    host, port, route = "192.0.2.44", 31789, 113
    name = "Customer Capsule / 样品"
    try:
        window.show()
        window.set_language("en")
        window.navigate(1)
        until(lambda: not window.editing.busy and not window.recipe_page.labels_loading)
        editor.draft.display_name = name
        editor.draft.image_archive.root_directory = archive
        editor.draft.image_archive.save_ng = True
        window.recipe_page.save()
        until(lambda: not window.editing.busy)
        assert editor.entry.recipe.display_name == name
        assert editor.entry.recipe.image_archive.root_directory == archive

        communication = window.communication
        communication.protocol.setCurrentIndex(0)
        communication.host.setText(host)
        communication.port.setValue(port)
        communication.destination[0].setValue(route)
        communication.timeout.setValue(2.5)
        communication.persist_settings()
        until(lambda: not communication.busy)

        for kind, resource, path, private in (
            ("recipe", editor.entry.recipe.recipe_id, editor.entry.path, archive),
            ("communication", "communication.json", startup.data.communication, host),
        ):
            (revision,) = identity.audit.revisions(kind, resource).result(WAIT)
            assert revision.confirmed and private in revision.record.operation.after
            if kind == "communication":
                assert revision.record.operation.before == "Missing file"
                assert f"diagnostic.port = {port}" in revision.record.operation.after
                assert (
                    f"diagnostic.destination.network = {route}"
                    in revision.record.operation.after
                )
            content = identity.audit.revision_content(
                revision.record.operation.id
            ).result(WAIT)
            assert content == path.read_bytes() and private.encode() in content

        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        page = window.audit_page
        until(
            lambda: (
                {"recipe.save", "communication.save"}
                <= {r.operation.action for r in page.rows}
            )
        )
        for action, private in (("recipe.save", archive), ("communication.save", host)):
            row = next(
                i for i, r in enumerate(page.rows) if r.operation.action == action
            )
            page.table.selectRow(row)
            assert private in page.details.toPlainText()
            if action == "communication.save":
                (recorded,) = page.details.document().rootFrame().childFrames()
                assert isinstance(recorded, QTextTable) and recorded.columns() == 2
                assert (
                    text("audit_config_missing_file", "en")
                    in page.details.toPlainText()
                )

        for language in ("en", "zh"):
            for output in audit_report_texts(app, tmp_path, language, monkeypatch):
                # Long column labels can wrap onto another PDF line.
                compact = "".join(output.split())
                assert "".join(name.split()) in compact
                assert "".join(text("archive_ng", language).split()) in compact
                assert "".join(text("comm_timeout", language).split()) in compact
                assert (
                    "".join(text("audit_config_missing_file", language).split())
                    in compact
                )
                assert (
                    "".join(text("audit_recorded_after", language).split()) in compact
                )
                assert "2.5" in compact
                for private in (archive, host):
                    assert "".join(private.split()) not in compact
                assert {str(port), str(route)}.isdisjoint(output.splitlines())
            with closing(load_workbook(tmp_path / f"audit-{language}.xlsx")) as book:
                (sheet,) = book.worksheets
                rows = list(sheet.iter_rows(max_col=5))
                heading = (
                    text("audit_field", language),
                    None,
                    text("audit_recorded_after", language),
                    None,
                    None,
                )
                index = next(
                    i
                    for i, row in enumerate(rows)
                    if tuple(c.value for c in row) == heading
                )
                timeout = next(
                    row for row in rows[index + 1 :] if row[2].value == "2.5"
                )
                assert text("comm_timeout", language) in str(timeout[0].value)
                assert timeout[2].data_type == "s"
                assert timeout[2].alignment == Alignment(
                    horizontal="left", wrap_text=True, vertical="top"
                )
                assert timeout[1].value is timeout[3].value is timeout[4].value is None
    finally:
        close_window(window)
        delivery.detach()


def test_manual_write_audit_shows_complete_qt_values_and_bounded_customer_summaries(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    identity = app.identity
    assert identity
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    endpoint = FinsUdpEndpoint(
        host="192.0.2.78", port=31876, destination=FinsAddress(17, 184, 63)
    )
    values = tuple(range(65535, 65535 - MAX_WRITE_WORDS, -1))
    at_old_limit = (62345,) * 292 + (17,)
    new_writes = (values, at_old_limit)

    def acknowledge(req: CommunicationRequest) -> CommunicationResponse:
        return CommunicationResponse(req, None, 1, datetime.now(UTC))

    try:
        for words in new_writes:
            request = CommunicationRequest(
                RequestKind.WRITE, endpoint, 200, len(words), words
            )
            execute_manual(request, acknowledge, None, identity, None)

        # A legacy record ended in a partial word that still looked valid and
        # had the same token count as its recorded range.
        legacy_values = (60321,) * 292 + (12489,)
        legacy_target = manual_target(
            CommunicationRequest(
                RequestKind.WRITE, endpoint, 600, len(legacy_values), legacy_values
            )
        )
        legacy = AuditOperation.by(
            identity.snapshot(),
            "plc.write",
            legacy_target,
            after=", ".join(map(str, legacy_values))[:AUDIT_CHANGE_LIMIT],
        )
        assert legacy.after.endswith(", 1248")
        identity.audit.intent(legacy).result(WAIT)
        identity.audit.complete(legacy, "success").result(WAIT)

        evidence = identity.audit_page(AuditQuery(action="plc.write")).result(WAIT)
        assert len(evidence[1].operation.after) == AUDIT_CHANGE_LIMIT
        database = identity.audit.path.read_bytes()
        window.show()
        window.set_language("en")
        window.navigate(3)
        window.history_tabs.setCurrentIndex(2)
        page = window.audit_page
        record_ids = {row.operation.id for row in evidence}
        until(lambda: record_ids <= {row.operation.id for row in page.rows})
        expected_values = {
            row.operation.id: ", ".join(map(str, words))
            for row, words in zip(
                evidence, (legacy_values[:-1], *reversed(new_writes)), strict=True
            )
        }
        for row, entry in enumerate(page.rows):
            if entry.operation.id not in record_ids:
                continue
            page.table.selectRow(row)
            displayed = page.details.toPlainText()
            assert text("audit_requested", "en") in displayed
            assert expected_values[entry.operation.id] in displayed
            assert ("Recorded values are incomplete" in displayed) == (
                entry.operation.id == legacy.id
            )
            assert ", 1248" not in displayed
            target = page.table.item(row, 3)
            assert target and target.toolTip() == "PLC"
            assert endpoint.host not in displayed
            assert str(endpoint.destination) not in displayed

        for language in ("en", "zh"):
            for output in audit_report_texts(app, tmp_path, language, monkeypatch):
                visible = " ".join(output.split())
                assert f"PLC · D200 × {MAX_WRITE_WORDS}" in visible
                assert text("audit_requested", language) in visible
                assert ", ".join(map(str, values[:16])) in visible
                assert f", {values[16]}" not in visible
                assert ", 1248" not in visible
                assert audit_text("identity_audit_incomplete", language) in visible
                omitted = text("audit_plc_values_omitted", language)
                assert omitted.format(count=MAX_WRITE_WORDS - 16) in visible
                assert omitted.format(count=len(legacy_values) - 1 - 16) in visible
                for private in (
                    endpoint.host,
                    f":{endpoint.port}",
                    str(endpoint.destination),
                ):
                    assert private not in output
        assert identity.audit.path.read_bytes() == database
    finally:
        close_window(window)
        delivery.detach()


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
        for language in LANGUAGES:
            window.set_language(language)
            assert dialog.windowTitle() == text("customer_audit", language)
            assert "LOT-A · Yellow" in dialog.scope_label.text()
            assert "Former Operator" in dialog.scope_label.text()
            assert dialog.audit.account.username == "Former Operator"
            assert dialog.audit.filters.batch == batch
            assert dialog.format.currentData() == "pdf"
            assert dialog.output_language.currentData() == "zh"
            assert [dialog.output_language.itemData(i) for i in range(2)] == [
                "zh",
                "en",
            ]
        window.set_language("en")
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
