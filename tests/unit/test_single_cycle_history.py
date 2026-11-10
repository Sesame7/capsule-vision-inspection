"""Run mode, not retained row count, determines valid physical group coverage."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.unit.test_defect_reports import LENGTH, MARK, NOW, classified
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.models import PilotCounters
from capsule_vision.application.report_content import ReportData
from capsule_vision.application.report_scope import HistoryQuery, ReportRequest
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingMode,
)


@pytest.mark.parametrize("cameras", [5, 6])
def test_mixed_run_memberships_classification_reports_and_retention(
    tmp_path: Path, cameras: int
) -> None:
    path = tmp_path / "history.sqlite3"
    now = NOW
    history = ProductionHistory(path, clock=lambda: now)
    expected = PilotCounters()
    try:
        batch = history.select_batch("Mixed modes").result(WAIT)
        assert batch
        for mode in (ReportingMode.FOUR_CYCLE, ReportingMode.SINGLE_CYCLE):
            size = mode.group_size
            assert size is not None
            run = mode.value
            history.start_run(
                batch.uuid, run, RECIPE, replace(FOUR_CYCLE, mode=mode)
            ).result(WAIT)
            results = [classified(11 + slot, cameras) for slot in range(size)]
            reports = tuple(FirstStationReport.from_result(1, r) for r in results)
            fact = CompletedReportingBatch(
                reports[0].identity, 92, reports, reports[0].reject_words, 120
            )
            expected = expected.completed(fact)
            history.offer_group(run, fact)
            for result in results:
                ctx = HistoryContext(
                    now + result.cycle_id, batch.uuid, batch.label, run, RECIPE
                )
                history.admitted(run, result.cycle_id)
                assert history.offer(ctx, result)
            history.offer_group(run, fact)
            history.finish_run(run, "stopped", False).result(WAIT)
            now += 100
        records = history.query().result(WAIT).records
        assert len(records) == 5 and all(r.group_first_cycle == 11 for r in records)
        assert sorted(r.group_slot for r in records if r.group_slot is not None) == [
            1,
            1,
            2,
            3,
            4,
        ]
        scope = history.defect_report_scope(HistoryQuery()).result(WAIT)
        assert scope.attributable and (
            scope.cycles,
            scope.groups,
            scope.covered_cycles,
        ) == (5, 2, 5)
        # One entire single-member group is valid; a single retained member of
        # a four-member Run remains partial even with identical CycleId/status.
        single = HistoryQuery(since=NOW + 100)
        assert history.defect_report_scope(single).result(WAIT).attributable
        partial = HistoryQuery(until=NOW + 12)
        assert not history.defect_report_scope(partial).result(WAIT).attributable
        with pytest.raises(ValueError, match="Detection Count"):
            history.capture_report(
                ReportRequest("defects", "capsule", filters=partial),
                tmp_path / "partial.db",
                Event(),
            ).result(WAIT)
        filtered = tmp_path / "scope.db"
        history.capture_report(
            ReportRequest("defects", "capsule"), filtered, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(filtered)) as db:
            report = ReportData(db, "en", Event())
            assert report.group_count == report.covered == 2
            assert report.ng == 8 and report.explained == 4
            assert report.categories.total() == 8
        assert history.snapshot().failed == 0
    finally:
        finish(history)
    with closing(sqlite3.connect(path)) as db:
        assert dict(db.execute("SELECT category,affected FROM batch_categories")) == {
            MARK: 4,
            "Dent": 2,
            LENGTH: 2,
        }
        assert db.execute(
            "SELECT groups,ng,explained FROM run_coverage ORDER BY run"
        ).fetchall() == [(1, 4, 2), (1, 4, 2)]
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    history = ProductionHistory(path, clock=lambda: now + 31 * 86400)
    try:
        assert not history.query().result(WAIT).records
        review = history.batch_detail(batch.uuid).result(WAIT)
        assert review.batch.totals == expected and len(review.runs) == 2
        assert all(
            run.totals and run.totals.batches_completed == 1 for run in review.runs
        )
        restored = history.initial_selection.result(WAIT)
        assert restored and restored.reporting_mode is ReportingMode.SINGLE_CYCLE
        for scope_name in ("batch", "defects"):
            target = tmp_path / f"{scope_name}.db"
            history.capture_report(
                ReportRequest(scope_name, "capsule", batch.uuid), target, Event()
            ).result(WAIT)
            with closing(sqlite3.connect(target)) as db:
                for language in ("zh", "en"):
                    report = ReportData(db, language, Event())
                    assert report.positions == expected
                    if scope_name == "defects":
                        assert report.batch_complete and report.categories.total() == 8
                    assert len(list(report.runs())) == 2
    finally:
        finish(history)


def test_one_member_fact_cannot_reinterpret_a_four_member_run(tmp_path: Path) -> None:
    from tests.unit.test_run_positions import fact

    history = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: NOW)
    try:
        history.start_run(None, "four", RECIPE, FOUR_CYCLE).result(WAIT)
        history.offer_group("four", fact(11, 5, 1))
        history.query().result(WAIT)
        assert history.snapshot().failed == 1
    finally:
        finish(history)
    with closing(sqlite3.connect(history.path)) as db:
        assert db.execute("SELECT count(*) FROM plc_groups").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM open_run_facts").fetchone() == (0,)
