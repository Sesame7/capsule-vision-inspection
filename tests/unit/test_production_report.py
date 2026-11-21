"""Inspect published PDFs and their durable content from isolated History owners."""

import sqlite3
from concurrent.futures import CancelledError
from contextlib import closing, nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from reportlab.pdfgen.canvas import Canvas
from scripts.development_records import Shift, cycle_result
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.business import mixed_result
from tests.support.reports import pdf_text
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
from capsule_vision.application.report_content import ReportData
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
def test_pdf_uses_only_selected_batch_and_persisted_physical_outcomes(
    tmp_path: Path, cameras: int, language: Language
) -> None:
    now = 1_800_000_000.0
    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: now)
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    try:
        # Preserve the literal business label and omit internal inspection identities.
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
        path = app.export_report(batch.uuid, tmp_path / "report.pdf", language).result(
            WAIT
        )
        body = pdf_text(path)
        assert text("customer_batch", language) in body and batch.label in body
        assert text("identity_status", language) not in body
        assert text("report_stopped", language) not in body
        assert "Other" not in body and str(first) not in body and "CH01" not in body
        assert context.run_id not in body and RECIPE.model_sha256 not in body
        snapshot = tmp_path / "summary.db"
        history.capture_report(batch.uuid, snapshot, Event()).result(WAIT)
        with closing(sqlite3.connect(snapshot)) as db:
            data = ReportData(db, language, Event())
            (table,) = data.tables()
            assert table.headers == tuple(
                text(key, language)
                for key in (
                    "batch_run",
                    "report_started",
                    "customer_end",
                    "batch_start_account",
                    "report_inspected",
                    "ok",
                    "ng",
                    "report_pass_rate",
                )
            )
            assert table.emphasize_first and table.numeric_columns == (4, 5, 6, 7)
            total, *runs = data.production_rows()
            assert list(table.rows) == [total, *runs]
            assert total[:4] == (text("customer_batch_total", language), "", "", "")
            assert total[4:] == (
                5 * cameras,
                cameras,
                4 * cameras,
                0.2,
            )
            labels = [f"{text('export_run', language)} {i}" for i in (1, 2, 3)]
            if cameras == 6:
                labels[-1] += "\n" + text("run_uncertain_totals", language)
            assert [row[0] for row in runs] == labels
            assert runs[0][4:] == (5 * cameras, cameras, 4 * cameras, 0.2)
            assert all(row[4:] == (None,) * 4 for row in runs[1:])
            assert text("run_statistics_unavailable", language) in tuple(data.notes())
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
        empty_text = pdf_text(
            app.export_report(empty.uuid, tmp_path / "empty.pdf", "ja").result(WAIT)
        )
        assert (
            "Batch Production Report" in empty_text
            and "Production statistics unavailable" in empty_text
        )
        assert "Batch Total" in empty_text and "Run 1" not in empty_text
        assert empty_text.count("—") == 4
        empty_matrix = pdf_text(
            app.export_report(
                ReportRequest("defects", batch=empty.uuid),
                tmp_path / "no-runs.pdf",
                "en",
            ).result(WAIT)
        )
        assert "Batch Total" in empty_matrix and "Run 1" not in empty_matrix
        assert "Inspected" in empty_matrix and empty_matrix.count("—") == 2
        expired = pdf_text(
            app.export_report(batch.uuid, tmp_path / "expired.pdf", "en").result(WAIT)
        )
        assert "20.00%" in expired and "First inspection" in expired
        assert (
            "Interrupted: 1 Runs" in expired and "Records may be incomplete" in expired
        )
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
    write = report_pdf.write_pdf

    def held(
        db: sqlite3.Connection, staged: Path, language: Language, stop: Event
    ) -> None:
        entered.set()
        assert release.wait(WAIT)
        if ending == "error":
            staged.write_bytes(b"incomplete")
            raise OSError("Controlled file failure")
        write(db, staged, language, stop)

    monkeypatch.setattr(report_pdf, "write_pdf", held)
    target = tmp_path / "report.pdf"
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
            body = pdf_text(export.result(WAIT))
            assert "In progress" in body and "First inspection" not in body
        else:
            with pytest.raises(OSError if ending == "error" else CancelledError):
                export.result(WAIT)
            assert target.read_bytes() == b"previous report"
        assert not list(tmp_path.glob("*.tmp")) and not history.snapshot().error
    finally:
        release.set()
        close(app)


def test_session_replacement_prevents_unpublished_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = authenticated_startup(saved_data(tmp_path)).application
    identity = app.identity
    assert identity is not None
    entered, release = Event(), Event()
    write = report_pdf.write_pdf

    def hold_after_write(
        db: sqlite3.Connection, staged: Path, language: Language, stop: Event
    ) -> None:
        write(db, staged, language, stop)
        entered.set()
        assert release.wait(WAIT)

    monkeypatch.setattr(report_pdf, "write_pdf", hold_after_write)
    target = tmp_path / "existing.pdf"
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
    target = tmp_path / "combined.pdf"
    try:
        body = pdf_text(app.export_report(batch.uuid, target, "en").result(WAIT))
        assert "20.00%" in body and "CH01" not in body and "116" not in body
        before = target.read_bytes()

        def disk_full(*args: object, **kwargs: object) -> None:
            raise OSError("Archive full")

        with monkeypatch.context() as patch:
            patch.setattr(Canvas, "save", disk_full)
            with pytest.raises(OSError, match="Archive full"):
                app.export_report(batch.uuid, target, "en").result(WAIT)
        assert target.read_bytes() == before
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
                request = ReportRequest("batch", batch=batch.uuid)
                output = tmp_path / f"{group}.pdf"
                production_report.write_report(
                    owner, request, output, "en", Event(), lambda: None
                )
                body = pdf_text(output)
                assert (
                    "Lin" in body
                    and "First inspection" in body
                    and "Latest inspection" in body
                )
                assert info.product in body and "Exposure cycles" not in body
                snap = tmp_path / f"{group}.db"
                owner.capture_report(request, snap, Event()).result(WAIT)
                with closing(sqlite3.connect(snap)) as db:
                    summary = ReportData(db, "en", Event())
                    (table,) = summary.tables()
                    total, run = summary.production_rows()
                    assert list(table.rows) == [total, run]
                    assert total[4:] == run[4:] and run[2] is None
                    assert run[0] == "Run 1 · In progress"
                    assert "Batch Total" in body and "Run 1 · In progress" in body
                    assert dict(summary.field_groups()[1])[
                        "Latest inspection"
                    ] == datetime.fromtimestamp(now + group * 4 + 4)
                    if group == 199:
                        assert summary.positions.inspected > 5000
        assert pages == [1, 1]
    finally:
        finish(owner)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_hundred_run_customer_report_keeps_every_row_and_factual_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: Language
) -> None:
    owner = ProductionHistory(tmp_path / "hundred.sqlite3")
    run_labels: list[str] = []
    run_prefix = text("export_run", language) + " "
    drawn: list[str] = []
    pages: list[int] = []
    draw, page = Canvas.drawString, Canvas.showPage

    def record_text(canvas: Canvas, x: float, y: float, value: str) -> None:
        drawn.append(value)
        # The approved 14 mm margin and 6 pt cell inset locate the Run column.
        if x == pytest.approx(14 * 72 / 25.4 + 6) and value.startswith(run_prefix):
            run_labels.append(value)
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
            outcome = {0: "interrupted", 1: "fault"}.get(n, "stopped")
            owner.finish_run(f"run-{n}", outcome, n == 0).result(WAIT)
        request = ReportRequest("batch", batch=batch.uuid)
        production_report.write_report(
            owner, request, tmp_path / "hundred.pdf", language, Event(), lambda: None
        )
        assert run_labels == [
            f"{run_prefix}1 · {text('report_interrupted', language)}",
            f"{run_prefix}2 · {text('state_FAULT', language)}",
            *(f"{run_prefix}{n}" for n in range(3, 101)),
        ]
        assert drawn.count(text("customer_batch_total", language)) == 1
        assert len(pages) > 1 and pages == list(range(1, len(pages) + 1))
        assert text("report_interrupted", language) in " ".join(drawn)
        assert text("run_uncertain_totals", language) in " ".join(drawn)
        assert not any(
            word in " ".join(drawn).lower()
            for word in ("cycle", "plc", "attribut", "四次", "周期")
        )
    finally:
        finish(owner)
