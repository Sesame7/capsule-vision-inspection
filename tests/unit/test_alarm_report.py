"""Alarm reports retain independent events, exact scope and safe publication."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from openpyxl import load_workbook
from reportlab.pdfgen.canvas import Canvas
from tests.support.application import WAIT, FakeSession, close
from tests.support.startup import TEST_PASSWORD, provision_account

from capsule_vision.application import ProductionApplication, production_report
from capsule_vision.application.account_maintenance import initialize_administrator
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery
from capsule_vision.application.alarm_report import (
    AlarmReportRequest,
    NoAlarmRecords,
    capture,
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

    def authorize(revision: int | None) -> int:
        with identity.admission(Role.OPERATOR, revision) as actor:
            return actor.revision

    history = ProductionHistory(
        tmp_path / "history.sqlite3", clock=lambda: STAMP, alarm_access=authorize
    )
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), identity=identity, history=history
    )
    try:
        yield app, identity, history
    finally:
        close(app)


def event(n: int = 0) -> AlarmEvent:
    return AlarmEvent(
        STAMP + n,
        f"signal-{n}",
        "activated",
        "equipment",
        "气压低",
        "Low air pressure",
        "fins_tcp://127.0.0.1:9600",
        1,
        18,
        5,
        "definition/1 " + "a" * 64,
        None,
        None,
    )


def offer(history: ProductionHistory, events: list[AlarmEvent]) -> None:
    for i in range(0, len(events), 16):
        history.offer_alarms(tuple(events[i : i + 16]))
        history.alarm_page(AlarmQuery()).result(WAIT)


def fill(history: ProductionHistory) -> tuple[str, list[AlarmEvent]]:
    batch = history.select_batch("LOT-A · 黄色胶囊").result(WAIT)
    assert batch
    states = ("activated", "recovered", "observed_active", "observed_recovered")
    # Arrival order intentionally differs from timestamps, with timestamp ties.
    events = [
        replace(
            event(n // 2),
            state=states[n % 4],
            batch_id=batch.uuid,
            batch_label=batch.label,
            en=f"Low air pressure {n:03}",
        )
        for n in reversed(range(147))
    ]
    offer(
        history,
        events
        + [
            replace(event(10), en="Same time, absent association"),
            replace(
                event(10),
                batch_id="different",
                batch_label=batch.label,
                en="Same label, different identity",
            ),
            replace(
                event(10),
                category="software",
                source="application",
                en="Production fault",
            ),
            replace(event(0), observed=STAMP - 31 * 86400, en="Expired"),
        ],
    )
    return batch.uuid, events


def rows(
    path: Path, language: Language = "en"
) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as db:
        table = next(ReportData(db, language, Event()).tables())
        return table.headers, list(table.rows)


def test_scope_snapshot_all_pages_order_and_exact_batch(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    _, identity, history = owners
    batch, events = fill(history)
    request = AlarmReportRequest(RecordsScope(STAMP, STAMP + 100, batch), "equipment")
    path = tmp_path / "snapshot.db"
    history.capture_alarm_report(
        request, path, Event(), identity.snapshot().revision
    ).result(WAIT)
    headings, values = rows(path)
    assert headings == ("Local time", "Alarm", "State", "Source")
    assert len(values) == 147
    expected = sorted(
        enumerate(events), key=lambda v: (v[1].observed, v[0]), reverse=True
    )
    assert [r[1] for r in values] == [e.en for _, e in expected]
    assert len(set(r[1] for r in values)) == 147
    assert all(r[3] == "PLC" for r in values)
    assert {r[2] for r in values} == {
        "Activated",
        "Recovered",
        "Active on read",
        "Inactive on read",
    }
    assert "Same time" not in str(values) and batch not in str(values)
    # A later event cannot enter an existing snapshot.
    offer(
        history, [replace(event(99), batch_id=batch, batch_label="LOT-A", en="Later")]
    )
    assert rows(path)[1] == values
    narrow = tmp_path / "narrow.db"
    history.capture_alarm_report(
        AlarmReportRequest(RecordsScope(STAMP + 10, STAMP + 11, batch)), narrow, Event()
    ).result(WAIT)
    assert len(rows(narrow)[1]) == 2
    unbatched = tmp_path / "unbatched.db"
    history.capture_alarm_report(
        AlarmReportRequest(RecordsScope(unbatched=True), "software"), unbatched, Event()
    ).result(WAIT)
    assert [r[1] for r in rows(unbatched)[1]] == ["Production fault"]
    all_events = tmp_path / "all.db"
    history.capture_alarm_report(AlarmReportRequest(), all_events, Event()).result(WAIT)
    assert (
        len(rows(all_events)[1]) == 151
    )  # expired event excluded; later event included
    assert rows(all_events)[0][-1] == "Batch ID"


@pytest.mark.parametrize("role", list(Role))
def test_permissions(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    role: Role,
) -> None:
    app, identity, history = owners
    offer(history, [event()])
    if role == Role.GUEST:
        identity.logout()
    elif role != Role.ADMINISTRATOR:
        provision_account(identity, role.name, role, TEST_PASSWORD)
        identity.login(role.name, TEST_PASSWORD).result(WAIT)
    path = tmp_path / "alarm.xlsx"
    if role < Role.OPERATOR:
        with pytest.raises(AuthorizationError):
            app.export_report(AlarmReportRequest(), path, "en")
        with pytest.raises(AuthorizationError):
            history.capture_alarm_report(
                AlarmReportRequest(), tmp_path / "denied.db", Event()
            )
        assert not path.exists() and not list(tmp_path.glob("*.tmp"))
    else:
        assert app.export_report(AlarmReportRequest(), path, "en").result(WAIT) == path


@pytest.mark.parametrize("language", ["zh", "en"])
def test_pdf_excel_complete_and_paginated(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    app, _, history = owners
    batch, events = fill(history)
    request = AlarmReportRequest(RecordsScope(batch=batch))
    pages: list[int] = []
    captions: list[str] = []
    draw, page = Canvas.drawString, Canvas.showPage

    def draw_text(canvas: Canvas, x: float, y: float, msg: str) -> None:
        assert 0 <= x <= 842 and 0 <= y <= 596
        captions.append(msg)
        draw(canvas, x, y, msg)

    def next_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        page(canvas)

    monkeypatch.setattr(Canvas, "drawString", draw_text)
    monkeypatch.setattr(Canvas, "showPage", next_page)
    app.export_report(request, tmp_path / "alarm.pdf", language).result(WAIT)
    assert len(pages) > 1
    assert captions.count("State" if language == "en" else "状态") == len(pages)
    assert captions.count("PLC") == len(events)
    assert not any("fins_tcp" in v or "signal-" in v or "W18" in v for v in captions)
    path = tmp_path / "alarm.xlsx"
    app.export_report(request, path, language).result(WAIT)
    with closing(load_workbook(path)) as book:
        sheet = book.worksheets[0]
        records = [r for r in sheet.values if isinstance(r[0], datetime)]
        assert len(records) == 147 and sheet.max_column == 4
        assert [r[0] for r in records] == sorted(
            [datetime.fromtimestamp(e.observed) for e in events], reverse=True
        )
        assert (
            sheet.page_setup.orientation == "landscape" and sheet.freeze_panes == "A8"
        )
        assert not any(c.data_type == "f" for row in sheet for c in row)


def test_details_unknown_state_and_missing_fields(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    _, _, history = owners
    offer(
        history,
        [
            replace(
                event(1),
                state="observed_recovered",
                detail="First valid observation after a communication gap",
            ),
            replace(
                event(2),
                state="unavailable",
                source="application",
                detail="runtime · TimeoutError",
            ),
            replace(
                event(3),
                detail="Inspect supply · fins_tcp://127.0.0.1:9600 · W18.5 · bit 5",
            ),
            replace(
                event(4),
                en="=Long recorded alarm name " * 12,
                detail="Check material feed",
            ),
            replace(
                event(5),
                en="",
                source="opaque-source",
                detail="runtime · InternalSDKError",
            ),
        ],
    )
    snapshot = tmp_path / "details.db"
    history.capture_alarm_report(AlarmReportRequest(), snapshot, Event()).result(WAIT)
    _, values = rows(snapshot)
    assert "InternalSDKError" not in str(values)
    assert values[0][1] == "Alarm\nRuntime fault" and values[0][3] == "—"
    assert str(values[1][1]).endswith("Check material feed")
    assert "Inspect supply" in str(values[2][1])
    assert all(
        v not in str(values) for v in ("fins_tcp", "W18", "bit 5", "definition/")
    )
    assert values[3][2] == "Unknown" and "Timeout" in str(values[3][1])
    assert values[4][2] == "Inactive on read" and "communication gap" in str(
        values[4][1]
    )


@pytest.mark.parametrize(
    "ending", ["logout", "role", "disable", "cancel", "close", "write_error"]
)
def test_failed_or_cancelled_publication(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    app, identity, history = owners
    offer(history, [event()])
    account = provision_account(identity, "Operator", Role.OPERATOR, TEST_PASSWORD)
    identity.login("Operator", TEST_PASSWORD).result(WAIT)
    entered, release, cancel = Event(), Event(), Event()
    write = production_report.write_workbook

    def held(db: sqlite3.Connection, path: Path, lang: Language, stop: Event) -> None:
        entered.set()
        assert release.wait(WAIT)
        if ending == "write_error":
            path.write_bytes(b"partial")
            raise OSError("Controlled failure")
        write(db, path, lang, stop)

    monkeypatch.setattr(production_report, "write_workbook", held)
    path = tmp_path / "alarm.xlsx"
    path.write_bytes(b"preserved")
    future = app.export_report(AlarmReportRequest(), path, "en", cancel=cancel)
    try:
        assert entered.wait(WAIT)
        if ending == "logout":
            identity.logout()
        elif ending in ("role", "disable"):
            identity.login("Admin", TEST_PASSWORD).result(WAIT)
            if ending == "role":
                identity.set_role(account.account_id, Role.ENGINEER).result(WAIT)
            else:
                identity.set_enabled(account.account_id, False).result(WAIT)
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
        assert path.read_bytes() == b"preserved" and not list(tmp_path.glob("*.tmp"))
    finally:
        release.set()


def test_empty_and_legacy_alarm_store(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
) -> None:
    app, _, history = owners
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"existing")
    with pytest.raises(NoAlarmRecords):
        app.export_report(AlarmReportRequest(), path, "en").result(WAIT)
    assert path.read_bytes() == b"existing"
    offer(history, [event()])
    with pytest.raises(NoAlarmRecords):
        app.export_report(AlarmReportRequest(RecordsScope(until=1)), path, "en").result(
            WAIT
        )
    # No translation column is required by the two-language customer report.
    close(app)
    with closing(sqlite3.connect(history.path)) as db, db:
        db.execute("ALTER TABLE alarm_events DROP COLUMN translations")
    with closing(sqlite3.connect(history.path)) as db:
        capture(
            db,
            AlarmReportRequest(),
            tmp_path / "legacy.db",
            STAMP,
            Event(),
            available=True,
        )
    assert len(rows(tmp_path / "legacy.db")[1]) == 1


def test_pending_snapshot_rechecks_session_and_pre_cancel_has_no_file(
    tmp_path: Path,
    owners: tuple[ProductionApplication, LocalIdentity, ProductionHistory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, identity, history = owners
    offer(history, [event()])
    from capsule_vision.application import history as history_module

    entered, release = Event(), Event()

    def held(
        db: sqlite3.Connection,
        request: AlarmReportRequest,
        path: Path,
        now: float,
        cancel: Event,
        *,
        available: bool,
    ) -> None:
        entered.set()
        assert release.wait(WAIT)
        capture(db, request, path, now, cancel, available=available)

    monkeypatch.setattr(history_module, "capture_alarm", held)
    future = history.capture_alarm_report(
        AlarmReportRequest(), tmp_path / "private.db", Event()
    )
    try:
        assert entered.wait(WAIT)
        identity.logout()
        release.set()
        with pytest.raises(StaleSessionError):
            future.result(WAIT)
    finally:
        release.set()
    identity.login("Admin", TEST_PASSWORD).result(WAIT)
    cancel = Event()
    cancel.set()
    with pytest.raises(CancelledError):
        history.capture_alarm_report(
            AlarmReportRequest(), tmp_path / "cancelled.db", cancel
        ).result(WAIT)
    assert not (tmp_path / "cancelled.db").exists()
