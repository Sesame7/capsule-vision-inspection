"""Durable customer categories use eligible final positions, never exposure totals."""

import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import WAIT
from tests.support.reports import pdf_text
from tests.unit.test_defect_reports import MARK, NOW, classified
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.production_report import write_report
from capsule_vision.application.report_content import ReportData
from capsule_vision.application.report_scope import (
    ReportRequest,
)
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
)
from capsule_vision.inspection.production import (
    FindingSource,
    ProductionInspectionResult,
    Reason,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language


def multiview(cycle: int) -> ProductionInspectionResult:
    result = classified(cycle, 5)
    camera = result.cameras[0]
    channel = camera.channels[0]
    vimo, traditional = channel.checks
    traditional = replace(
        traditional,
        reasons=tuple(
            Reason(FindingSource.TRADITIONAL, code)
            for code in (
                "capsule.size.upper.length",
                "capsule.size.lower.length",
                "capsule.size.upper.body_missing",
                "capsule.color.upper.tolerance",
                "capsule.color.lower.tolerance",
            )
        ),
    )
    vimo = replace(
        vimo,
        reasons=vimo.reasons
        + (
            Reason(FindingSource.VIMO, "defect_region", "Length"),
            Reason(FindingSource.VIMO, "defect_region", "Color"),
        ),
    )
    return replace(
        result,
        cameras=(
            replace(
                camera,
                channels=(
                    replace(channel, checks=(vimo, traditional)),
                    *camera.channels[1:],
                ),
            ),
            *result.cameras[1:],
        ),
    )


def submit(
    owner: ProductionHistory,
    ctx: HistoryContext,
    first: int,
    *,
    group_first: bool = False,
) -> CompletedReportingBatch:
    results = [multiview(first + slot) for slot in range(4)]
    reports = tuple(FirstStationReport.from_result(1, result) for result in results)
    group = CompletedReportingBatch(
        reports[0].identity, 7, reports, reports[0].reject_words, 123.0
    )
    if group_first:
        owner.offer_group(ctx.run_id, group)
    for result in reversed(results):
        assert owner.offer(ctx, result)
    owner.offer_group(ctx.run_id, group)
    owner.offer_group(ctx.run_id, group)
    assert owner.offer(ctx, results[0])
    return group


def populate(path: Path) -> str:
    owner = ProductionHistory(path, clock=lambda: NOW)
    try:
        batch = owner.select_batch("QA-White-06").result(WAIT)
        assert batch
        for index, run in enumerate(("first", "second")):
            recipe = replace(RECIPE, model_sha256=str(index) * 64)
            owner.start_run(batch.uuid, run, recipe, FOUR_CYCLE).result(WAIT)
            ctx = HistoryContext(NOW, batch.uuid, batch.label, run, recipe)
            submit(owner, ctx, 11, group_first=bool(index))
            if index == 0:
                submit(owner, ctx, 15)
            owner.finish_run(run, "stopped", False).result(WAIT)
        assert owner.snapshot().failed == 0
        return batch.uuid
    finally:
        finish(owner)


def test_run_categories_reconcile_survive_restart_expiry_and_duplicate_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    batch = populate(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT run,groups,ng,explained FROM run_coverage ORDER BY run"
        ).fetchall() == [
            ("first", 2, 8, 4),
            ("second", 1, 4, 2),
        ]
        original = db.execute(
            "SELECT * FROM run_categories ORDER BY run,source,category"
        ).fetchall()
        for run, multiplier in (("first", 2), ("second", 1)):
            counts = {
                (s, name): n
                for s, name, n in db.execute(
                    "SELECT source,category,affected FROM run_categories WHERE run=?",
                    (run,),
                )
            }
            assert counts == {
                ("traditional", "length"): multiplier,
                ("traditional", "color"): multiplier,
                ("vimo", MARK): 2 * multiplier,
                ("vimo", "Dent"): multiplier,
                ("vimo", "Length"): multiplier,
                ("vimo", "Color"): multiplier,
            }
    owner = ProductionHistory(path, clock=lambda: NOW + 31 * 86400)
    try:
        snap = tmp_path / "batch.db"
        owner.capture_report(
            ReportRequest("defects", batch=batch),
            snap,
            Event(),
        ).result(WAIT)
        with closing(sqlite3.connect(snap)) as db:
            data = ReportData(db, "en", Event())
            assert data.batch_complete and all(data.run_complete.values())
            assert data.positions.channel_ng == 12
            assert data.categories == sum(
                data.run_categories.values(), Counter[tuple[str, str, str, str]]()
            )
            names = data.category_names()
            assert len(set(names.values())) == len(names)
            assert list(names.values())[:2] == ["Length", "Color"]
            assert (
                "Length (ViMo 1)" in names.values()
                and "Length (ViMo 2)" in names.values()
            )
            rows = list(data.batch_category_rows())
            assert [r[1:3] for r in rows] == [(87, 12), (58, 8), (29, 4)]
            assert sum(data.categories.values()) > data.positions.channel_ng
            assert rows[0][0] == "Batch Total" and str(rows[1][0]).startswith("Run 1\n")
            assert rows[0][3:5] == (3, 3)
            for column, key in enumerate(names, 3):
                assert rows[0][column] == data.categories[key]
                assert rows[1][column] == data.run_categories["first"][key]
                assert rows[2][column] == data.run_categories["second"][key]
            assert (
                0 in rows[1][3:]
            )  # The other Run's Model categories are confirmed zero.
            assert "capsule." not in str(rows) and RECIPE.model_id not in str(rows)
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT * FROM run_categories ORDER BY run,source,category"
            ).fetchall()
            == original
        )
        assert db.execute("SELECT count(*) FROM plc_groups").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM cycle_classification").fetchone() == (
            0,
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "case",
    [
        "complete",
        "missing",
        "expired",
        "unknown_totals",
        "bad_members",
        "bad_hit",
        "uncertain",
    ],
)
def test_legacy_run_backfill_never_allocates_residual_batch_categories(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "history.sqlite3"
    batch = populate(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DROP TABLE run_categories")
        db.execute("DROP TABLE run_coverage")
        before = db.execute("SELECT * FROM batch_categories").fetchall()
        if case == "missing":
            db.execute(
                "DELETE FROM group_classification WHERE run='first' AND first_cycle='11'"
            )
        elif case in ("expired", "uncertain"):
            column = "detail_expired" if case == "expired" else "uncertain"
            db.execute(f"UPDATE runs SET {column}=1 WHERE uuid='first'")
            if case == "expired":
                db.execute("DELETE FROM plc_groups WHERE run='first'")
        elif case == "unknown_totals":
            db.execute("UPDATE run_positions SET positions=NULL WHERE run='first'")
        elif case == "bad_members":
            db.execute(
                "UPDATE group_members SET slot=1 WHERE run='first' AND cycle='12'"
            )
        elif case == "bad_hit":
            db.execute("""UPDATE group_classification SET findings='[[31,"vimo","model","sha","Wrong channel"]]'
                WHERE run='first' AND first_cycle='11'""")
    complete = case in ("complete", "uncertain")
    for index in range(2):
        owner = ProductionHistory(path, clock=lambda: NOW)
        try:
            snap = tmp_path / f"legacy-{index}.db"
            owner.capture_report(
                ReportRequest("defects", batch=batch), snap, Event()
            ).result(WAIT)
            with closing(sqlite3.connect(snap)) as db:
                data = ReportData(db, "en", Event())
                assert data.batch_complete is complete
                assert data.run_complete == {
                    "first": complete,
                    "second": True,
                }
                rows = list(data.batch_category_rows())
                if not complete:
                    assert all(v is None for v in rows[0][3:] + rows[1][3:])
                    assert rows[2][3] == 1
                assert data.positions.channel_ng == 12
        finally:
            finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM batch_categories").fetchall() == before


def test_group_category_failure_rolls_back_eligible_fact_and_all_aggregates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path, clock=lambda: NOW)
    owner.initial_selection.result(WAIT)
    finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TRIGGER fail_category BEFORE UPDATE ON run_coverage
            BEGIN SELECT RAISE(ABORT,'Controlled category failure'); END""")
    owner = ProductionHistory(path, clock=lambda: NOW)
    try:
        batch = owner.select_batch("Atomic").result(WAIT)
        assert batch
        owner.start_run(batch.uuid, "first", RECIPE, FOUR_CYCLE).result(WAIT)
        submit(owner, HistoryContext(NOW, batch.uuid, batch.label, "first", RECIPE), 11)
        review = owner.batch_detail(batch.uuid).result(WAIT)
        assert review.batch.totals.batches_completed == 0
        assert (
            review.runs[0].totals is not None
            and review.runs[0].totals.batches_completed == 0
        )
        assert owner.snapshot().failed > 0
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        for table in (
            "plc_groups",
            "group_classification",
            "run_categories",
            "batch_categories",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert db.execute("SELECT groups FROM run_coverage").fetchone() == (0,)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_actual_batch_classification_pdf(tmp_path: Path, language: Language) -> None:
    path = tmp_path / "history.sqlite3"
    batch = populate(path)
    owner = ProductionHistory(path, clock=lambda: NOW)
    try:
        output = tmp_path / "defects.pdf"
        write_report(
            owner,
            ReportRequest("defects", batch=batch),
            output,
            language,
            Event(),
            lambda: None,
        )
        content = pdf_text(output)
        assert text("customer_batch_total", language) in content
        assert "capsule." not in content and "Source" not in content
        assert "Dent" in content and "12" in content
    finally:
        finish(owner)


def test_unbatched_groups_still_persist_classification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path, clock=lambda: NOW)
    try:
        owner.start_run(None, "cancelled", RECIPE, FOUR_CYCLE).result(WAIT)
        owner.finish_run("cancelled", "unactivated", False).result(WAIT)
        owner.start_run(None, "unbatched", RECIPE, FOUR_CYCLE).result(WAIT)
        group = submit(
            owner, HistoryContext(NOW, "unbatched", None, "unbatched", RECIPE), 11
        )
    finally:
        finish(owner)
    owner = ProductionHistory(path, clock=lambda: NOW + 31 * 86400)
    try:
        owner.offer_group("unbatched", group)
        owner.query().result(WAIT)
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT run,groups,ng FROM run_coverage").fetchall() == [
            ("unbatched", 1, 4)
        ]
        assert db.execute("SELECT sum(affected) FROM run_categories").fetchone() == (7,)
        assert db.execute("SELECT count(*) FROM batch_categories").fetchone() == (0,)
