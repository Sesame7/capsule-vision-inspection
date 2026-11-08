"""Customer Audit exports use immutable evidence, exact scope and fenced publication."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from openpyxl import load_workbook
from reportlab.pdfgen.canvas import Canvas
from tests.support.application import WAIT, FakeSession, close
from tests.support.startup import TEST_PASSWORD, provision_account
from tests.unit.test_durable_batches import RECIPE

from capsule_vision.application import ProductionApplication, production_report
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.audit import (
    AuditAccount,
    AuditOperation,
    AuditQuery,
    AuditRecord,
)
from capsule_vision.application.audit_report import (
    AuditReportRequest,
    NoAuditRecords,
    capture,
    details,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.records_query import RecordsScope
from capsule_vision.application.report_content import ReportData
from capsule_vision.localization.languages import Language

STAMP = 1_800_000_000


@pytest.fixture
def owners(
    tmp_path: Path,
) -> Iterator[tuple[ProductionApplication, LocalIdentity, ProductionHistory]]:
    initialize_administrator(tmp_path / "security.sqlite3", "Admin", TEST_PASSWORD)
    identity = LocalIdentity(tmp_path / "security.sqlite3")
    identity.login("Admin", TEST_PASSWORD).result(WAIT)
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: STAMP)
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), identity=identity, history=history
    )
    try:
        yield app, identity, history
    finally:
        close(app)


def operation(
    action: str, target: str, n: int, before: str = "", after: str = ""
) -> AuditOperation:
    return AuditOperation(
        "retired",
        "Former Operator",
        action,
        target,
        before,
        after,
        id=f"observed-{n}",
        utc=datetime.fromtimestamp(STAMP + n, UTC).isoformat(timespec="microseconds"),
    )


def fill(identity: LocalIdentity, history: ProductionHistory) -> tuple[str, list[str]]:
    batch = history.select_batch("LOT-A · Yellow").result(WAIT)
    assert batch
    history.start_run(batch.uuid, "actual-run", RECIPE, None).result(WAIT)
    history.finish_run("actual-run", "stopped", False).result(WAIT)
    expected: list[str] = []
    for n in range(145):
        op = operation(
            "batch.information",
            batch.label,
            n,
            "product = Previous",
            f"product = Product {n:03}",
        )
        identity.audit.record(
            op,
            "failed" if n % 7 == 0 else "success",
            "ConfigurationIOError" if n % 7 == 0 else "",
        )
        expected.append(op.id)
        if n % 20 == 0:
            identity.audit_page(AuditQuery()).result(WAIT)
    extra = (
        ("production.start", "actual-run", "success"),
        ("production.stop", "actual-run", "uncertain"),
        ("session.login", "actual-run", "success"),
        ("production.start", "actual-run", "failed"),
        ("production.stop", "missing-run", "success"),
        ("batch.select", batch.uuid, "denied"),
        ("batch.information", "another Batch", "success"),
    )
    for n, (action, target, outcome) in enumerate(extra, 145):
        op = operation(action, target, n)
        identity.audit.record(op, outcome)
        if n < 147:
            expected.append(op.id)
    identity.audit.record(
        AuditOperation(
            "different",
            "Former Operator",
            "batch.select",
            batch.uuid,
            after=batch.label,
        ),
        "success",
    )
    identity.audit_page(AuditQuery()).result(WAIT)
    return batch.uuid, expected


def evidence(path: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as db:
        return (
            db.execute("SELECT * FROM operations ORDER BY id").fetchall(),
            db.execute("SELECT * FROM completions ORDER BY id").fetchall(),
        )


def test_complete_filtered_snapshot_and_no_temporal_batch_attribution(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    _, identity, history = owners
    batch, expected = fill(identity, history)
    original = evidence(identity.audit.path)
    request = AuditReportRequest(
        RecordsScope(STAMP, STAMP + 200, batch),
        AuditAccount("retired", "Former Operator"),
    )
    snapshot = tmp_path / "snapshot.sqlite3"
    capture(identity, history, request, snapshot, Event(), identity.snapshot().revision)
    with closing(sqlite3.connect(snapshot)) as db:
        assert [
            row[0]
            for row in db.execute("SELECT id FROM audit_records ORDER BY cursor DESC")
        ] == expected[::-1]
        data = ReportData(db, "en", Event())
        rows = list(next(data.tables()).rows)
        assert len(rows) == 147
        assert rows[-1][0] == datetime.fromtimestamp(STAMP)
        assert "Before: Product name · Previous" in str(rows)
        assert "Requested value: Product name · Product 000" in str(rows)
        assert "Could not read or write the configuration file" in str(rows)
        assert batch not in str(rows) and "actual-run" not in str(rows)
    assert evidence(identity.audit.path) == original
    narrow = tmp_path / "narrow.db"
    capture(
        identity,
        history,
        AuditReportRequest(
            RecordsScope(STAMP + 10, STAMP + 12, batch), request.account
        ),
        narrow,
        Event(),
        identity.snapshot().revision,
    )
    with closing(sqlite3.connect(narrow)) as db:
        assert db.execute(
            "SELECT id FROM audit_records ORDER BY cursor"
        ).fetchall() == [("observed-10",), ("observed-11",)]


@pytest.mark.parametrize("role", list(Role))
def test_export_permission_at_application_boundary(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    role: Role,
) -> None:
    app, identity, _ = owners
    if role == Role.GUEST:
        identity.logout()
    elif role != Role.ADMINISTRATOR:
        provision_account(identity, role.name, role, TEST_PASSWORD)
        identity.login(role.name, TEST_PASSWORD).result(WAIT)
    destination = tmp_path / "report.xlsx"
    if role < Role.ENGINEER:
        with pytest.raises(AuthorizationError):
            app.export_report(AuditReportRequest(), destination, "en")
        assert not destination.exists() and not list(tmp_path.glob("*.tmp"))
    else:
        assert (
            app.export_report(AuditReportRequest(), destination, "en").result(WAIT)
            == destination
        )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_actual_reports_include_all_rows_and_paginate(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    language: Language,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, identity, history = owners
    batch, expected = fill(identity, history)
    request = AuditReportRequest(
        RecordsScope(STAMP, STAMP + 200, batch),
        AuditAccount("retired", "Former Operator"),
    )
    pages: list[int] = []
    drawn: list[str] = []
    draw, page = Canvas.drawString, Canvas.showPage

    def capture_text(canvas: Canvas, x: float, y: float, msg: str) -> None:
        assert 0 <= x <= 842 and 0 <= y <= 596
        drawn.append(msg)
        draw(canvas, x, y, msg)

    def capture_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        page(canvas)

    monkeypatch.setattr(Canvas, "drawString", capture_text)
    monkeypatch.setattr(Canvas, "showPage", capture_page)
    app.export_report(request, tmp_path / "audit.pdf", language).result(WAIT)
    # The selected account also appears once in the business-information header.
    assert len(pages) > 1 and drawn.count("Former Operator") == len(expected) + 1
    if language == "en":
        assert "From" in drawn and "Before" in drawn
    assert not any(
        "batch.information" in s or "observed-" in s or "actual-run" in s for s in drawn
    )
    output = tmp_path / "audit.xlsx"
    app.export_report(request, output, language).result(WAIT)
    with closing(load_workbook(output)) as book:
        sheet = book.worksheets[0]
        rows = list(sheet.values)
        observations = [r for r in rows if isinstance(r[0], datetime)]
        assert len(observations) == len(expected)
        assert [r[0] for r in observations] == [
            datetime.fromtimestamp(STAMP + i) for i in range(146, -1, -1)
        ]
        assert sheet.page_setup.orientation == "landscape"
        assert sheet.freeze_panes == "A8"
        assert len(book.worksheets) == 1 and sheet.max_column == 5


@pytest.mark.parametrize(
    "ending", ["logout", "role", "disable", "reset", "cancel", "close", "write_error"]
)
def test_pending_export_rechecks_publication_and_cleans_up(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    app, identity, _ = owners
    provision_account(identity, "Reviewer", Role.ENGINEER, TEST_PASSWORD)
    identity.login("Reviewer", TEST_PASSWORD).result(WAIT)
    actor = identity.snapshot().account
    assert actor
    entered, release, cancel = Event(), Event(), Event()
    write = production_report.write_workbook

    def held(db: sqlite3.Connection, path: Path, lang: Language, stop: Event) -> None:
        entered.set()
        assert release.wait(WAIT)
        if ending == "write_error":
            path.write_bytes(b"incomplete")
            raise OSError("Controlled export failure")
        write(db, path, lang, stop)

    monkeypatch.setattr(production_report, "write_workbook", held)
    destination = tmp_path / "audit.xlsx"
    destination.write_bytes(b"previous")
    future = app.export_report(AuditReportRequest(), destination, "en", cancel=cancel)
    try:
        assert entered.wait(WAIT)
        if ending == "logout":
            identity.logout()
        elif ending == "role":
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            identity.set_role(actor.account_id, Role.OPERATOR).result(WAIT)
        elif ending == "disable":
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            identity.set_enabled(actor.account_id, False).result(WAIT)
        elif ending == "reset":
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            identity.reset_password(actor.account_id, TEST_PASSWORD + " reset").result(
                WAIT
            )
        elif ending == "cancel":
            cancel.set()
        elif ending == "close":
            assert not app.close().done()
        release.set()
        with pytest.raises(
            (CancelledError, StaleSessionError)
            if ending in ("cancel", "close")
            else OSError
            if ending == "write_error"
            else StaleSessionError
        ):
            future.result(WAIT)
        assert destination.read_bytes() == b"previous"
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        release.set()


def test_empty_scope_and_disabled_historical_actor(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    app, identity, _ = owners
    account = provision_account(
        identity, "Former account", Role.OPERATOR, TEST_PASSWORD
    )
    identity.audit.record(
        AuditOperation(
            account.account_id,
            account.username,
            "batch.select",
            "opaque",
            after="Former Batch",
        ),
        "success",
    )
    identity.set_enabled(account.account_id, False).result(WAIT)
    output = tmp_path / "former.xlsx"
    app.export_report(
        AuditReportRequest(account=AuditAccount(account.account_id, account.username)),
        output,
        "en",
    ).result(WAIT)
    with closing(load_workbook(output)) as book:
        assert any(
            r[1] == account.username and r[3] == "Former Batch"
            for r in book.worksheets[0].values
        )
    output.write_bytes(b"preserved")
    with pytest.raises(NoAuditRecords):
        app.export_report(
            AuditReportRequest(RecordsScope(until=1)), output, "en"
        ).result(WAIT)
    assert output.read_bytes() == b"preserved"


def test_private_snapshot_does_not_adopt_later_events_or_completions(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, identity, _ = owners
    op = operation(
        "batch.information", "Existing Batch", 0, "product = Old", "product = New"
    )
    identity.audit.intent(op).result(WAIT)
    entered, release = Event(), Event()
    write = production_report.write_workbook

    def held(db: sqlite3.Connection, path: Path, lang: Language, stop: Event) -> None:
        entered.set()
        assert release.wait(WAIT)
        write(db, path, lang, stop)

    monkeypatch.setattr(production_report, "write_workbook", held)
    output = tmp_path / "snapshot.xlsx"
    future = app.export_report(
        AuditReportRequest(account=AuditAccount("retired", "Former Operator")),
        output,
        "en",
    )
    try:
        assert entered.wait(WAIT)
        identity.audit.complete(op, "success").result(WAIT)
        identity.audit.record(
            operation("batch.select", "new", 1, after="Later Batch"), "success"
        )
        release.set()
        future.result(WAIT)
        with closing(load_workbook(output)) as book:
            rows = list(book.worksheets[0].values)
            assert "Later Batch" not in str(rows)
            assert any(r[4] == "Unresolved" for r in rows)
    finally:
        release.set()


def test_customer_changes_keep_failures_but_never_credentials_or_opaque_ids() -> None:
    entry = AuditRecord(
        1,
        operation("batch.information", "LOT", 0, "notes = old", "notes = new"),
        "failed",
        "",
        "TimeoutError",
    )
    assert "Requested value" in details(entry, "en") and "Timeout" in details(
        entry, "en"
    )
    secret = AuditRecord(
        2,
        operation(
            "account.reset_password",
            "a" * 32,
            1,
            "password = secret",
            "verifier = hidden",
        ),
        "failed",
        "",
        "AuthorizationError",
    )
    value = details(secret, "en")
    assert "secret" not in value and "hidden" not in value and "Denied" in value


def test_report_localizes_configuration_changes_and_retains_recorded_names() -> None:
    entry = AuditRecord(
        1,
        operation(
            "recipe.save",
            "recipe-id",
            0,
            "display_name = Salt coating\ncapsule.size.target_length = 120\ndefaults.vimo.disabled_labels[0] = 压痕\nmodel_id = internal-token",
            "display_name = =Unchanged formula-like name\ncapsule.size.target_length = 130\ndefaults.vimo.disabled_labels[0] = 破损\nmodel_id = other-token",
        ),
        "failed",
        "",
        "ConfigurationIOError",
    )
    for language in ("zh", "en"):
        value = details(entry, language)
        assert "Salt coating" in value and "=Unchanged formula-like name" in value
        assert "120" in value and "130" in value and "压痕" in value and "破损" in value
        assert not any(
            raw in value
            for raw in (
                "internal-token",
                "other-token",
                "capsule.",
                "disabled_labels",
                "ConfigurationIOError",
            )
        )
    unknown = AuditRecord(
        2, operation("model.cache", "a" * 64, 1), "failed", "", "VendorNativeException"
    )
    assert details(unknown, "en") == "Reasons: Operation incomplete"


def test_unbatched_scope_and_missing_batch_never_broaden_to_all_records(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    app, identity, history = owners
    history.start_run(None, "unbatched-run", RECIPE, None).result(WAIT)
    history.finish_run("unbatched-run", "stopped", False).result(WAIT)
    for n, (action, target, outcome) in enumerate(
        (
            ("production.start", "unbatched-run", "success"),
            ("production.stop", "unbatched-run", "uncertain"),
            ("production.clear_statistics", "unbatched", "success"),
            ("production.start", "unbatched-run", "failed"),
            ("session.login", "unbatched-run", "success"),
        )
    ):
        identity.audit.record(operation(action, target, n), outcome)
    output = tmp_path / "unbatched.xlsx"
    app.export_report(
        AuditReportRequest(RecordsScope(unbatched=True)), output, "en"
    ).result(WAIT)
    with closing(load_workbook(output)) as book:
        observations = [
            r for r in book.worksheets[0].values if isinstance(r[0], datetime)
        ]
        assert len(observations) == 3
        assert observations[-1][3] == "Production Run"
    with pytest.raises(ValueError, match="Batch is unavailable"):
        app.export_report(
            AuditReportRequest(RecordsScope(batch="missing")),
            tmp_path / "missing.xlsx",
            "en",
        ).result(WAIT)


def test_export_keeps_literal_names_and_manual_write_evidence(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    app, identity, _ = owners
    for n, op in enumerate(
        (
            operation(
                "plc.write", "FINS/TCP 127.0.0.1:9600 177 100 × 2", 700, after="5, 6"
            ),
            operation("account.role", "opaque-account", 701, "OPERATOR", "ENGINEER"),
            operation("communication.web_power", "web", 702, "False", "True"),
        )
    ):
        identity.audit.intent(op).result(WAIT)
        identity.audit.complete(
            op, "success", "=Original username" if n == 1 else ""
        ).result(WAIT)
    path = tmp_path / "details.xlsx"
    app.export_report(
        AuditReportRequest(RecordsScope(STAMP + 700, STAMP + 703)), path, "en"
    ).result(WAIT)
    with closing(load_workbook(path)) as book:
        values = list(book.worksheets[0].values)
        assert "PLC · W100 × 2" in str(values) and "5, 6" in str(values)
        assert "After: Enabled" in str(values)
        assert "127.0.0.1" not in str(values) and "opaque-account" not in str(values)
        named = [
            c
            for row in book.worksheets[0]
            for c in row
            if c.value == "=Original username"
        ]
        assert len(named) == 1 and named[0].data_type == "s"
