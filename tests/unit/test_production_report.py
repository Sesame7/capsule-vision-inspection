"""Inspect real workbooks written from isolated live History owners."""

import sqlite3
from concurrent.futures import CancelledError
from contextlib import closing, nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet._writer import ALL_TEMP_FILES
from reportlab.pdfgen.canvas import Canvas
from scripts.development_records import Shift, cycle_result
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.business import mixed_result
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data
from tests.unit.test_configured_occupancy import configuration
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish

from capsule_vision.application import (
    ApplicationStateError,
    ProductionApplication,
    production_report,
    report_pdf,
)
from capsule_vision.application.batch_review import BatchInformation, StartAccount
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import StaleSessionError
from capsule_vision.application.report_scope import HistoryQuery, ReportRequest
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingMode,
    ReportingSettings,
)
from capsule_vision.inspection import CycleId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

FOUR_CYCLE = ReportingSettings(FinsTcpEndpoint(), mode=ReportingMode.FOUR_CYCLE)


def record_group(
    history: ProductionHistory,
    context: HistoryContext,
    first: int,
    cameras: int,
    counter: int,
) -> None:
    results = [mixed_result(first + i, camera_count=cameras) for i in range(4)]
    reports = tuple(FirstStationReport.from_result(1, r) for r in results)
    history.offer_group(
        context.run_id,
        CompletedReportingBatch(
            reports[0].identity, counter, reports, reports[0].reject_words, 127.5
        ),
    )
    for result in reversed(results):
        assert history.offer(context, result)


@pytest.mark.parametrize("cameras,language", [(5, "zh"), (6, "en")])
def test_workbook_uses_only_selected_batch_and_persisted_physical_outcomes(
    tmp_path: Path, cameras: int, language: Language
) -> None:
    now = 1_800_000_000.0
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    try:
        # A label that looks like a formula and a long numeric ID must survive Excel exactly.
        batch = history.select_batch("=LOT-005").result(WAIT)
        assert batch
        first = 9_007_199_254_740_993
        context = HistoryContext(now, batch.uuid, batch.label, "run-a", RECIPE)
        history.start_run(batch.uuid, context.run_id, RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(history, context, first, cameras, 65535)
        history.finish_run(context.run_id, "stopped", False).result(WAIT)
        now += 5
        history.start_run(
            batch.uuid,
            "run-b",
            replace(RECIPE, recipe_id="second"),
            replace(FOUR_CYCLE, mode=ReportingMode.PER_CYCLE),
        ).result(WAIT)
        assert history.offer(
            replace(context, run_id="run-b", recipe_name="Gold A"), mixed_result(1)
        )
        history.finish_run("run-b", "stopped", False).result(WAIT)
        now += 5
        history.start_run(batch.uuid, "run-c", RECIPE, None).result(WAIT)
        # A stopped Run can also have lost facts, independently of detail expiry.
        history.finish_run("run-c", "stopped", cameras == 6).result(WAIT)
        history.select_batch(None).result(WAIT)
        other = history.select_batch("Other").result(WAIT)
        assert other
        history.start_run(other.uuid, "other", RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(
            history,
            replace(
                context,
                run_id="other",
                batch_segment=other.uuid,
                batch_label=other.label,
            ),
            first,
            6,
            4,
        )
        path = app.export_report(batch.uuid, tmp_path / "report.xlsx", language).result(
            WAIT
        )
        with closing(load_workbook(path)) as book:
            assert book.sheetnames == [text("customer_batch", language)]
            summary = book.worksheets[0]
            rows = list(summary.values)
            assert (text("batch_input", language), batch.label) == rows[2][:2]
            assert summary["B3"].data_type == "s"
            metrics = next(
                i
                for i, r in enumerate(rows)
                if r[0] == text("report_inspected", language)
            )
            assert tuple(rows[metrics + 1][col] for col in (0, 2, 4, 6)) == (
                5 * cameras,
                cameras,
                4 * cameras,
                0.2,
            )
            assert summary.cell(metrics + 2, 7).number_format == "0.00%"
            assert isinstance(summary["B2"].value, datetime)
            runs = [r for r in rows if type(r[0]) is int and isinstance(r[1], datetime)]
            assert [r[0] for r in runs] == [1, 2, 3]
            assert runs[0][5:] == (5 * cameras, cameras, 4 * cameras, 0.2)
            assert all(r[5:] == (None,) * 4 for r in runs[1:])
            assert runs[1][4] == text("report_stopped", language)
            flat = str(rows)
            assert "Other" not in flat and str(first) not in flat and "CH01" not in flat
            assert context.run_id not in flat and RECIPE.model_sha256 not in flat
            assert text("run_statistics_unavailable", language) in flat
            assert text("customer_batch_basis", language) not in flat
            if cameras == 6:
                assert text("run_uncertain_totals", language) in flat
    finally:
        close(app)


def test_expiry_interruption_and_empty_batch_do_not_invent_detail(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000.0
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path, clock=lambda: now)
    try:
        batch = history.select_batch("Expired").result(WAIT)
        assert batch
        history.start_run(batch.uuid, "lost-owner", RECIPE, FOUR_CYCLE).result(WAIT)
        record_group(
            history,
            HistoryContext(now, batch.uuid, batch.label, "lost-owner", RECIPE),
            1,
            5,
            17,
        )
    finally:
        finish(history)
    now += 31 * 86400
    history = ProductionHistory(path, clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    try:
        empty = history.select_batch("No Runs").result(WAIT)
        assert empty
        with closing(
            load_workbook(
                app.export_report(empty.uuid, tmp_path / "empty.xlsx", "ja").result(
                    WAIT
                )
            )
        ) as book:
            summary = book.worksheets[0]
            assert (
                summary.title == "Batch Production Report" and len(book.worksheets) == 1
            )
            rows = list(summary.values)
            metrics = next(i for i, r in enumerate(rows) if r[0] == "Inspected")
            assert rows[metrics + 1][:4] == ("—", "—", "—", "—")
            assert "Production statistics unavailable" in str(rows)
            assert not any(type(r[0]) is int for r in rows)
        with closing(
            load_workbook(
                app.export_report(batch.uuid, tmp_path / "expired.xlsx", "en").result(
                    WAIT
                )
            )
        ) as book:
            rows = list(book.worksheets[0].values)
            metrics = next(i for i, r in enumerate(rows) if r[0] == "Inspected")
            assert rows[metrics + 1][:4] == (25, 5, 20, 0.2)
            assert (
                "First inspection",
                datetime.fromtimestamp(1_800_000_000.0),
            ) in [r[i : i + 2] for r in rows for i in range(len(r) - 1)]
            assert "Interrupted: 1 Runs" in str(rows)
            assert "Records may be incomplete" in str(rows)
            assert len(book.worksheets) == 1
    finally:
        close(app)


@pytest.mark.parametrize("ending", ["complete", "cancel", "error", "close"])
def test_active_export_is_a_consistent_snapshot_and_releases_actual_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    session = FakeSession()
    app = ProductionApplication(
        lambda _p, _g: session, history=history, initial_candidate=prepared(tmp_path)
    )
    entered, release, cancel = Event(), Event(), Event()
    write = production_report.write_workbook

    def held(
        db: sqlite3.Connection, staged: Path, language: Language, stop: Event
    ) -> None:
        entered.set()
        assert release.wait(WAIT)
        if ending == "error":
            staged.write_bytes(b"incomplete")
            raise OSError("Controlled file failure")
        write(db, staged, language, stop)

    monkeypatch.setattr(production_report, "write_workbook", held)
    target = tmp_path / "report.xlsx"
    target.write_bytes(b"previous report")
    try:
        app.start_batch("Running").result(WAIT)
        app.start().result(WAIT)
        batch = app.snapshot().batch_uuid
        assert batch
        export = app.export_report(batch, target, "en", cancel=cancel)
        assert entered.wait(WAIT)
        with pytest.raises(ApplicationStateError, match="already"):
            app.export_report(batch, target, "en")
        cycle = app.begin_cycle(CycleId(1))
        session.cycles[0].accepted.set_result(None)
        session.cycles[0].result.set_result(mixed_result(1))
        assert cycle.result.result(WAIT).cycle_id == 1
        assert len(history.query().result(WAIT).records) == 1
        app.stop().result(WAIT)
        assert app.snapshot().counters.cycles_completed == 1 and not export.done()
        if ending == "close":
            assert not app.close().done()
        elif ending == "cancel":
            cancel.set()
        release.set()
        if ending == "complete":
            with closing(load_workbook(export.result(WAIT))) as book:
                summary = book.worksheets[0]
                rows = list(summary.values)
                assert "In progress" in str(rows)
                assert "Inspection span" not in str(rows)
                assert "First inspection" not in str(rows)
        else:
            with pytest.raises(OSError if ending == "error" else CancelledError):
                export.result(WAIT)
            assert target.read_bytes() == b"previous report"
        assert not list(tmp_path.glob("*.tmp")) and not history.snapshot().error
    finally:
        release.set()
        close(app)


@pytest.mark.parametrize("extension", ["xlsx", "pdf"])
def test_session_replacement_prevents_unpublished_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    app = authenticated_startup(saved_data(tmp_path)).application
    identity = app.identity
    assert identity is not None
    entered, release = Event(), Event()
    write = (
        production_report.write_workbook
        if extension == "xlsx"
        else report_pdf.write_pdf
    )

    def hold_after_write(
        db: sqlite3.Connection, staged: Path, language: Language, stop: Event
    ) -> None:
        write(db, staged, language, stop)
        entered.set()
        assert release.wait(WAIT)

    if extension == "xlsx":
        monkeypatch.setattr(production_report, "write_workbook", hold_after_write)
    else:
        monkeypatch.setattr(report_pdf, "write_pdf", hold_after_write)
    target = tmp_path / f"existing.{extension}"
    target.write_bytes(b"original")
    try:
        app.start_batch("Selected").result(WAIT)
        batch = app.snapshot().batch_uuid
        assert batch
        result = app.export_report(batch, target, "en")
        assert entered.wait(WAIT)
        identity.login("TestAdmin", TEST_PASSWORD).result(WAIT)
        release.set()
        with pytest.raises(StaleSessionError):
            result.result(WAIT)
        assert target.read_bytes() == b"original"
    finally:
        release.set()
        close(app)


def test_compact_export_omits_exposure_rows_and_releases_spools_on_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path)
    try:
        batch = history.select_batch("Both layouts").result(WAIT)
        assert batch
        for cameras in (5, 6):
            run = f"run-{cameras}"
            history.start_run(batch.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            record_group(
                history,
                HistoryContext(1_800_000_000.0, batch.uuid, batch.label, run, RECIPE),
                100,
                cameras,
                21,
            )
            history.finish_run(run, "stopped", False).result(WAIT)
    finally:
        finish(history)
    # This isolated fixture distinguishes the saved slot from first_cycle + 1.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE group_members SET cycle='116' WHERE run='run-5' AND slot=2")
    history = ProductionHistory(path)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    target = tmp_path / "combined.xlsx"
    try:
        with closing(
            load_workbook(app.export_report(batch.uuid, target, "en").result(WAIT))
        ) as book:
            assert len(book.worksheets) == 1
            rows = list(book.worksheets[0].values)
            metrics = next(i for i, r in enumerate(rows) if r[0] == "Inspected")
            assert tuple(rows[metrics + 1][col] for col in (0, 2, 4, 6)) == (
                55,
                11,
                44,
                0.2,
            )
            assert "116" not in str(rows) and "CH01" not in str(rows)
        before = target.read_bytes()
        spools = set(ALL_TEMP_FILES)

        def disk_full(*args: object, **kwargs: object) -> None:
            raise OSError("Archive full")

        with monkeypatch.context() as patch:
            patch.setattr(ZipFile, "write", disk_full)
            with pytest.raises(OSError, match="Archive full"):
                app.export_report(batch.uuid, target, "en").result(WAIT)
        assert target.read_bytes() == before and set(ALL_TEMP_FILES) == spools
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        close(app)


def test_high_volume_report_length_business_fields_and_entire_batch_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    now = 1_800_000_000.0
    observed = now
    owner = ProductionHistory(tmp_path / "volume.sqlite3", clock=lambda: observed)
    pages: list[int] = []
    show = Canvas.showPage

    def count_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        show(canvas)

    monkeypatch.setattr(Canvas, "showPage", count_page)
    try:
        batch = owner.select_batch("QA-High-volume").result(WAIT)
        assert batch
        info = BatchInformation(
            "Capsules / 胶囊", "Size 0", "Development acceptance sample"
        )
        owner.update_batch_information(
            batch.uuid, BatchInformation(), info, lambda _l, _i: nullcontext()
        ).result(WAIT)
        owner.start_run(
            batch.uuid, "volume", RECIPE, FOUR_CYCLE, StartAccount("operator-id", "Lin")
        ).result(WAIT)
        ctx = HistoryContext(now, batch.uuid, batch.label, "volume", RECIPE)
        lengths: list[int] = []
        for group in range(200):
            observed = now + group * 4 + 4
            results = [
                cycle_result(
                    config, Shift(0, "volume", batch.label, now), group * 4 + slot
                )
                for slot in range(1, 5)
            ]
            reports = tuple(FirstStationReport.from_result(1, r) for r in results)
            words = tuple(
                a | b | c | d
                for a, b, c, d in zip(*(r.reject_words for r in reports), strict=True)
            )
            owner.offer_group(
                "volume",
                CompletedReportingBatch(
                    reports[0].identity, group, reports, words, 120.0
                ),
            )
            for result in results:
                assert owner.offer(
                    replace(ctx, completed_at=now + result.cycle_id), result
                )
            owner.query(HistoryQuery(limit=1)).result(
                WAIT
            )  # Drain bounded test handoffs.
            if group in (0, 199):
                request = ReportRequest(
                    "batch",
                    "capsule",
                    batch.uuid,
                    HistoryQuery(since=now + 999999, result=frozenset(("ERROR",))),
                )
                xlsx = tmp_path / f"{group}.xlsx"
                production_report.write_report(
                    owner, request, xlsx, "en", Event(), lambda: None
                )
                production_report.write_report(
                    owner,
                    request,
                    tmp_path / f"{group}.pdf",
                    "en",
                    Event(),
                    lambda: None,
                )
                with closing(load_workbook(xlsx)) as book:
                    sheet = book.worksheets[0]
                    lengths.append(sheet.max_row)
                    rows = list(sheet.values)
                    assert (
                        next(r[1] for r in rows if r[0] == text("batch_product", "en"))
                        == info.product
                    )
                    pairs = [r[i : i + 2] for r in rows for i in range(len(r) - 1)]
                    assert ("Start Account", "Lin") in pairs
                    assert (
                        "First inspection",
                        datetime.fromtimestamp(now + 1),
                    ) in pairs
                    assert (
                        "Latest inspection",
                        datetime.fromtimestamp(now + group * 4 + 4),
                    ) in pairs
                    assert not any(r[0] == "Runs" for r in rows)
                    if group == 199:
                        metrics = next(
                            i for i, r in enumerate(rows) if r[0] == "Inspected"
                        )
                        inspected = rows[metrics + 1][0]
                        assert isinstance(inspected, int) and inspected > 5000
        assert lengths[0] == lengths[1] < 30 and pages == [1, 1]
    finally:
        finish(owner)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_hundred_run_customer_report_keeps_every_row_and_factual_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: Language
) -> None:
    owner = ProductionHistory(tmp_path / "hundred.sqlite3")
    numbers: list[int] = []
    drawn: list[str] = []
    pages: list[int] = []
    draw, page = Canvas.drawString, Canvas.showPage

    def record_text(canvas: Canvas, x: float, y: float, value: str) -> None:
        drawn.append(value)
        # The approved 14 mm margin and 6 pt cell inset locate the Run column.
        if x == pytest.approx(14 * 72 / 25.4 + 6) and value.isdecimal():
            numbers.append(int(value))
        draw(canvas, x, y, value)

    def record_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        page(canvas)

    monkeypatch.setattr(Canvas, "drawString", record_text)
    monkeypatch.setattr(Canvas, "showPage", record_page)
    try:
        batch = owner.select_batch("Many shifts").result(WAIT)
        assert batch
        for n in range(100):
            owner.start_run(
                batch.uuid, f"run-{n}", RECIPE, FOUR_CYCLE, StartAccount("actor", "Lin")
            ).result(WAIT)
            owner.finish_run(
                f"run-{n}", "interrupted" if n == 0 else "stopped", n == 0
            ).result(WAIT)
        request = ReportRequest("batch", "capsule", batch.uuid)
        for suffix in ("xlsx", "pdf"):
            production_report.write_report(
                owner,
                request,
                tmp_path / f"hundred.{suffix}",
                language,
                Event(),
                lambda: None,
            )
        with closing(load_workbook(tmp_path / "hundred.xlsx")) as book:
            rows = list(book.worksheets[0].values)
            runs = [r for r in rows if type(r[0]) is int and isinstance(r[1], datetime)]
            assert [r[0] for r in runs] == list(range(1, 101))
            assert all(r[5:8] == (0, 0, 0) and r[8] is None for r in runs)
            assert text("run_uncertain_totals", language) in str(rows)
            assert text("customer_batch_basis", language) not in str(rows)
        assert numbers == list(range(1, 101))
        assert len(pages) > 1 and pages == list(range(1, len(pages) + 1))
        assert text("report_interrupted", language) in " ".join(drawn)
        assert text("run_uncertain_totals", language) in " ".join(drawn)
        assert not any(
            word in " ".join(drawn).lower()
            for word in ("cycle", "plc", "attribut", "四次", "周期")
        )
    finally:
        finish(owner)
