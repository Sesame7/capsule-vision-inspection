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
from openpyxl.styles import Alignment
from reportlab.pdfgen.canvas import Canvas
from tests.support.application import WAIT, FakeSession, close
from tests.support.startup import TEST_PASSWORD, provision_account
from tests.unit.test_durable_batches import RECIPE

from capsule_vision.application import ProductionApplication, production_report
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.audit import (
    AUDIT_CHANGE_LIMIT,
    AuditAccount,
    AuditOperation,
    AuditQuery,
    AuditRecord,
)
from capsule_vision.application.audit_presentation import local_change_comparison
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
from capsule_vision.application.report_content import AuditReportRow, ReportData
from capsule_vision.localization.catalog import text
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
        last = rows[-1]
        assert isinstance(last, AuditReportRow)
        assert last.values[0] == datetime.fromtimestamp(STAMP)
        assert last.comparison is not None
        assert last.comparison.headers == ("Field", "Before", "Requested value")
        assert last.comparison.rows == (("Product name", "Previous", "Product 000"),)
        assert last.detail == "Reasons: Could not read or write the configuration file"
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
    assert text("audit_field", language) in drawn
    assert (
        text("audit_after", language) in drawn
        and text("audit_requested", language) in drawn
    )
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
        last_event = next(
            r
            for r in range(8, sheet.max_row + 1)
            if sheet.cell(r, 1).value == datetime.fromtimestamp(STAMP)
        )
        heading, change, failure = last_event + 1, last_event + 2, last_event + 3
        assert tuple(sheet.cell(heading, c).value for c in (1, 2, 4)) == (
            text("audit_field", language),
            text("audit_before", language),
            text("audit_requested", language),
        )
        assert tuple(sheet.cell(change, c).value for c in (1, 2, 4)) == (
            text("batch_product", language),
            "Previous",
            "Product 000",
        )
        assert all(sheet.cell(change, c).data_type == "s" for c in (1, 2, 4))
        top = Alignment(wrap_text=True, vertical="top")
        assert all(sheet.cell(change, c).alignment == top for c in (1, 2, 4))
        assert {
            f"B{change}:C{change}",
            f"D{change}:E{change}",
            f"A{failure}:E{failure}",
        } <= {str(region) for region in sheet.merged_cells.ranges}
        assert sheet.cell(failure, 1).value == (
            f"{text('reasons', language)}: {text('records_config_io', language)}"
        )


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
    comparison, detail = details(entry, "en")
    assert comparison is not None
    assert comparison.headers[-1] == "Requested value" and "Timeout" in detail
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
    comparison, detail = details(secret, "en")
    assert comparison is None
    assert "secret" not in detail and "hidden" not in detail and "Denied" in detail


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
        comparison, detail = details(entry, language)
        assert comparison is not None
        assert [change[1:] for change in comparison.rows] == [
            ("Salt coating", "=Unchanged formula-like name"),
            ("120", "130"),
            ("压痕", "破损"),
        ]
        value = str(comparison) + detail
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
    assert details(unknown, "en") == (None, "Reasons: Operation incomplete")


@pytest.mark.parametrize(
    ("action", "recorded", "retained"),
    [
        (
            "vision.sources",
            "cameras[0].camera_id = CAM1\ncameras[0].basler_serial = SerialSentinel\n"
            "cameras[0].folder = C:/PathSentinel\nmode = basler",
            ("CAM1", "Industrial cameras"),
        ),
        (
            "recipe.delete",
            "display_name = Customer capsule / blue\n"
            "recipe_id = IdentifierSentinel\nimage_archive.root_directory = C:/PathSentinel",
            ("Customer capsule / blue",),
        ),
        (
            "communication.save",
            "diagnostic.timeout = 2.5\ndiagnostic.timeout = CredentialSentinel\n"
            "reporting.settings.mode = four_cycle\nweb.host = AddressSentinel\n"
            "web.display_name = FieldSentinel\ndiagnostic.destination.node = RouteSentinel",
            ("2.5", "Four-cycle"),
        ),
        (
            "batch.information",
            "product = Customer capsule\nnotes = password: CredentialSentinel\n"
            "specification = Uses C:/PathSentinel\nfuture = FieldSentinel",
            ("Customer capsule",),
        ),
        ("future.action", "display_name = UnknownActionSentinel", ()),
        ("model.publish", "Customer capsule · ModuleSentinel · " + "a" * 64, ()),
        (
            "recipe.process_value",
            "IdentifierSentinel: D101–D102 = -42",
            ("D101–D102 = -42",),
        ),
    ],
)
def test_customer_details_require_approved_actions_fields_and_values(
    action: str, recorded: str, retained: tuple[str, ...]
) -> None:
    entry = AuditRecord(
        1,
        operation(action, "different-target", 0, after=recorded),
        "failed",
        "",
        "Transport error at HostSentinel:9600",
    )
    comparison, shown = details(entry, "en")
    assert comparison is None
    assert "Reasons: Operation incomplete" in shown and "Sentinel" not in shown
    assert all(value in shown for value in retained)


@pytest.mark.parametrize("outcome", ["success", "failed", "stale", "unresolved"])
def test_role_compares_recorded_values_under_the_correct_heading(outcome: str) -> None:
    entry = AuditRecord(
        1,
        operation("account.role", "different-target", 0, "OPERATOR", "ENGINEER"),
        outcome,
        "",
        "StaleSessionError" if outcome == "stale" else "",
    )
    for language in ("zh", "en"):
        comparison, reason = details(entry, language)
        assert comparison is not None and comparison == local_change_comparison(
            entry, language
        )
        assert comparison.headers == (
            text("audit_field", language),
            text("audit_before", language),
            text(
                "audit_after" if outcome == "success" else "audit_requested", language
            ),
        )
        assert comparison.rows == (
            (
                text("identity_role", language),
                text("identity_operator", language),
                text("identity_engineer", language),
            ),
        )
        assert bool(reason) == (outcome == "stale")


def test_disclosed_pairs_use_recorded_indices_and_preserve_empty_values() -> None:
    entry = AuditRecord(
        1,
        operation(
            "recipe.save",
            "recipe-id",
            0,
            "product_code = OLD\ndefaults.vimo.disabled_labels[0] = Scar\n"
            "defaults.vimo.disabled_labels[1] = —\ndisplay_name = Secret name",
            "defaults.vimo.disabled_labels[1] = Bubble\nproduct_code = \n"
            "display_name = C:/PathSentinel\ndefaults.vimo.disabled_labels[0] = —",
        ),
        "unresolved",
        "",
        "",
    )
    comparison, detail = details(entry, "en")
    assert comparison is not None
    assert comparison.headers[-1] == "Requested value"
    assert comparison.rows == (
        (text("product_code", "en"), "OLD", "(empty value)"),
        (f"ViMo · {text('customer_audit_excluded', 'en')} [0]", "Scar", "—"),
        (f"ViMo · {text('customer_audit_excluded', 'en')} [1]", "—", "Bubble"),
    )
    assert "Before: Recipe name = Secret name" in detail
    assert text("audit_approved_values", "en") in detail
    assert "PathSentinel" not in str(comparison) + detail
    local = local_change_comparison(entry, "en")
    assert local is not None and local.rows[-1] == (
        "Recipe name",
        "Secret name",
        "C:/PathSentinel",
    )


@pytest.mark.parametrize(
    "before,after,incomplete",
    [
        ("diagnostic.timeout = 1.0", "web.enabled = True", False),
        (
            "diagnostic.timeout = 1.0\ndiagnostic.timeout = 1.1",
            "diagnostic.timeout = 2.0",
            False,
        ),
        (
            "diagnostic.timeout = 1.0\nmissing delimiter",
            "diagnostic.timeout = 2.0",
            False,
        ),
        ("", "diagnostic.timeout = 2.0", False),
        ("diagnostic.timeout = 1.0\n…", "diagnostic.timeout = 2.0\n…", True),
        (
            ("diagnostic.timeout = 1.0\nweb.enabled = " + "x" * AUDIT_CHANGE_LIMIT)[
                :AUDIT_CHANGE_LIMIT
            ],
            "diagnostic.timeout = 2.0",
            True,
        ),
    ],
    ids=[
        "unrelated",
        "duplicate",
        "malformed",
        "missing-side",
        "ellipsis",
        "size-limit",
    ],
)
def test_unsafe_change_pairs_keep_disclosed_text(
    before: str, after: str, incomplete: bool
) -> None:
    entry = AuditRecord(
        1,
        operation("communication.save", "communication.json", 0, before, after),
        "failed",
        "",
        "OSError",
    )
    comparison, detail = details(entry, "en")
    assert comparison is None and local_change_comparison(entry, "en") is None
    assert "Requested value: " in detail
    assert ("Before: " in detail) == bool(before)
    assert (text("identity_audit_incomplete", "en") in detail) == incomplete
    assert "missing delimiter" not in detail


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
        assert ("Field", "Before", None, "After", None) in values
        assert ("Status", "Disabled", None, "Enabled", None) in values
        assert "127.0.0.1" not in str(values) and "opaque-account" not in str(values)
        named = [
            c
            for row in book.worksheets[0]
            for c in row
            if c.value == "=Original username"
        ]
        assert len(named) == 1 and named[0].data_type == "s"
