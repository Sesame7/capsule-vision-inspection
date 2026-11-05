"""Inspect real workbooks written from isolated live History owners."""

import sqlite3
from concurrent.futures import CancelledError
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet._writer import ALL_TEMP_FILES
from tests.support.application import WAIT, FakeSession, close, prepared
from tests.support.business import mixed_result
from tests.support.startup import TEST_PASSWORD, authenticated_startup, saved_data
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish

from capsule_vision.application import (
    ApplicationStateError,
    ProductionApplication,
    production_report,
)
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.identity_models import StaleSessionError
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
        assert history.offer(replace(context, run_id="run-b"), mixed_result(1))
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
            assert book.sheetnames == [
                text("report_summary", language),
                text("report_groups", language),
            ]
            summary, groups = book.worksheets
            assert summary["B2"].value == batch.label and summary["B2"].data_type == "s"
            assert summary["B3"].value == batch.uuid
            assert summary["B6"].value == text(
                "batch_uncertain" if cameras == 6 else "report_recorded", language
            )
            assert summary["B5"].value == datetime.fromtimestamp(now)
            assert isinstance(summary["D5"].value, datetime)
            assert [c.value for c in summary[9][:8]] == [
                1,
                5 * cameras,
                cameras,
                4 * cameras,
                cameras,
                0.2,
                0.8,
                1,
            ]
            assert summary["F9"].number_format == summary["G9"].number_format == "0.00%"
            assert summary["B13"].value == 5
            assert [c.value for c in summary["A"][16:]] == ["run-a", "run-b", "run-c"]
            assert [summary[f"G{r}"].value for r in (17, 18, 19)] == [4, 1, 0]
            assert summary["E18"].value == "second" and summary["D18"].value == text(
                "report_stopped", language
            )
            assert groups.max_row == 2 and groups.max_column == 8 + cameras * 6
            assert [cell.value for cell in groups[1][8:]] == [
                f"CH{i:02}" for i in range(1, cameras * 6 + 1)
            ]
            assert [cell.value for cell in groups[2]] == [
                datetime.fromtimestamp(context.completed_at),
                context.run_id,
                65535,
                *(str(first + i) for i in range(4)),
                127.5,
                *(["OK", "EMPTY", "NG", "NG", "NG", "NG"] * cameras),
            ]
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
            summary, groups = book.worksheets
            assert summary.title == "Production Summary"
            assert summary["I16"].value == "Model ID" and summary.max_row == 16
            assert [c.value for c in summary[9][:8]] == [0, 0, 0, 0, 0, "—", "—", 0]
            assert groups.max_row == 1 and groups.max_column == 8
        with closing(
            load_workbook(
                app.export_report(batch.uuid, tmp_path / "expired.xlsx", "en").result(
                    WAIT
                )
            )
        ) as book:
            summary, groups = book.worksheets
            assert [c.value for c in summary[9][:8]] == [1, 25, 5, 20, 5, 0.2, 0.8, 0]
            assert "incomplete" in str(summary["B6"].value) and "expired" in str(
                summary["B6"].value
            )
            assert summary["D17"].value == "Interrupted" and summary[
                "C17"
            ].value == datetime.fromtimestamp(now)
            assert groups.max_row == 2
            assert groups["A1"].value == "Group detail incomplete"
            assert "0 / 1" in str(groups["B1"].value)
            assert groups["A2"].value == text("history_time", "en")
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
                assert summary["B13"].value == 0 and summary["C17"].value is None
                assert summary["B6"].value == summary["D17"].value == "In progress"
        else:
            with pytest.raises(OSError if ending == "error" else CancelledError):
                export.result(WAIT)
            assert target.read_bytes() == b"previous report"
        assert not list(tmp_path.glob("*.tmp")) and not history.snapshot().error
    finally:
        release.set()
        close(app)


def test_session_replacement_prevents_unpublished_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = authenticated_startup(saved_data(tmp_path)).application
    identity = app.identity
    assert identity is not None
    entered, release = Event(), Event()
    write = production_report.write_workbook

    def hold_after_write(
        db: sqlite3.Connection, staged: Path, language: Language, stop: Event
    ) -> None:
        write(db, staged, language, stop)
        entered.set()
        assert release.wait(WAIT)

    monkeypatch.setattr(production_report, "write_workbook", hold_after_write)
    target = tmp_path / "existing.xlsx"
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


def test_exposure_slots_and_layouts_come_from_retained_rows(
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
            detail = book.worksheets[1]
            assert detail.max_column == 44 and detail.max_row == 3
            assert detail["B2"].value == "run-5" and detail["E2"].value == "116"
            assert detail["AM2"].value is None and detail["AR3"].value == "NG"
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
