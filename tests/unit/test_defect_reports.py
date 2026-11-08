"""History's retained detections and durable attributed categories are different units."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from numpy.typing import NDArray
from openpyxl import load_workbook
from tests.support.application import WAIT
from tests.support.business import mixed_result
from tests.support.production import inferred
from tests.unit.test_configured_occupancy import configuration, cycle, scene
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.production_report import write_report
from capsule_vision.application.report_content import ReportData
from capsule_vision.application.report_scope import HistoryQuery, ReportRequest
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
)
from capsule_vision.inference import BoundingRect, ImageResult
from capsule_vision.inspection.production import (
    Assessment,
    CameraInspectionResult,
    ChannelInspectionResult,
    CheckResult,
    FindingSource,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)

MARK = "=表面色斑 / Surface mark"
LENGTH = "capsule.size.upper.length"
NOW = 1_800_000_000.0


def test_pdf_unrenderable_business_name_preserves_existing_destination(
    tmp_path: Path,
) -> None:
    owner = ProductionHistory(tmp_path / "history.sqlite3")
    target = tmp_path / "batch.pdf"
    target.write_bytes(b"previous report")
    try:
        batch = owner.select_batch("QA \U0010fffe").result(WAIT)
        assert batch
        with pytest.raises(ValueError, match="PDF font cannot display"):
            write_report(
                owner,
                ReportRequest("batch", "capsule", batch.uuid),
                target,
                "en",
                Event(),
                lambda: None,
            )
        assert target.read_bytes() == b"previous report"
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        finish(owner)


def test_persisted_categories_follow_effective_configured_pipeline(
    tmp_path: Path,
) -> None:
    config = configuration(tmp_path)
    config = replace(
        config,
        checks=replace(
            config.checks,
            vimo=replace(config.checks.vimo, disabled_labels=("excluded",)),
        ),
    )
    pixels = np.pad(scene(), ((0, 0), (0, 1), (0, 0)))
    raw = inferred(*(BoundingRect(x, 1, 1, 1) for x in (1, 81, 82, 83, 480)))
    raw = replace(
        raw,
        image_shape=pixels.shape,
        regions=tuple(
            replace(r, label=label)
            for r, label in zip(
                raw.regions,
                ("empty-only", "excluded", "Surface", "Surface", "background-only"),
                strict=True,
            )
        ),
    )

    def infer(_: NDArray[np.uint8]) -> ImageResult:
        return raw

    result = (
        ConfiguredProductionPipeline(config)
        .inspect(cycle(config, pixels), infer)
        .production
    )
    owner = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: NOW)
    try:
        for run, model in (
            ("first", RECIPE),
            ("second", replace(RECIPE, model_sha256="b" * 64)),
        ):
            owner.start_run(None, run, model, None).result(WAIT)
            assert owner.offer(HistoryContext(NOW, run, None, run, model), result)
            owner.finish_run(run, "stopped", False).result(WAIT)
        target = tmp_path / "snapshot"
        owner.capture_report(
            ReportRequest("defects", "detections"), target, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(target)) as db:
            data = ReportData(db, "en", Event())
            assert {
                (data.category(*key), n)
                for key, n in data.categories.items()
                if key[0] == "vimo"
            } == {(("ViMo 1", "Surface"), 6), (("ViMo 2", "Surface"), 6)}
            assert len(result.unassigned_findings) == 6
            assert data.cycles == data.complete == 2
    finally:
        finish(owner)


def classified(cycle: int, cameras: int) -> ProductionInspectionResult:
    result = mixed_result(cycle, camera_count=cameras)
    processed: list[CameraInspectionResult] = []
    for index, camera in enumerate(result.cameras):
        present, empty = (c.occupancy for c in camera.channels[:2])
        rows: list[ChannelInspectionResult] = []
        for slot, row in enumerate(camera.channels):
            vimo, traditional = FindingSource.VIMO, FindingSource.TRADITIONAL
            checks = tuple(
                CheckResult(s, Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK))
                for s in (vimo, traditional)
            )
            ng = Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG)
            row = replace(row, occupancy=present, traditional_skip=None, checks=checks)
            if index == 0 and slot in (0, 1):
                # Two regions with this label on CH1 and a second category on the same channel.
                reasons = (Reason(vimo, "defect_region", MARK),) * 2
                if slot == 0:
                    reasons += (Reason(vimo, "defect_region", "Dent"),)
                row = replace(
                    row,
                    checks=(
                        CheckResult(vimo, ng, reasons),
                        CheckResult(traditional, ng, (Reason(traditional, LENGTH),) * 2)
                        if slot == 0
                        else checks[1],
                    ),
                )
            elif index == 0 and slot == 2:
                row = replace(
                    row, forced_reject=Reason(FindingSource.COLLECTION, "cycle_failed")
                )
            elif index == 0 and slot == 3:
                row = replace(row, occupancy=empty)
            elif index == 0 and slot == 4:
                row = replace(
                    row,
                    checks=(
                        CheckResult(
                            vimo,
                            Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
                            (
                                Reason(
                                    vimo, "execution_error", exception_type="ValueError"
                                ),
                            ),
                        ),
                        checks[1],
                    ),
                )
            rows.append(row)
        assert camera.frame_id is not None
        background = ViMoFinding(
            camera.frame_id,
            7,
            "Background only",
            1,
            BoundingRect(0, 0, 1, 1),
            "example",
        )
        processed.append(
            replace(camera, channels=tuple(rows), unassigned_findings=(background,))
        )
    return replace(result, cameras=tuple(processed))


@pytest.mark.parametrize("cameras,first_arrival", [(5, True), (6, False)])
def test_group_categories_survive_retention_once_and_preserve_missing_coverage(
    tmp_path: Path, cameras: int, first_arrival: bool
) -> None:
    now = NOW
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path, clock=lambda: now)
    try:
        batch = owner.select_batch("Lot F").result(WAIT)
        assert batch
        ctx = HistoryContext(
            now, batch.uuid, batch.label, "run-one", RECIPE, "Capsule A"
        )
        owner.start_run(batch.uuid, ctx.run_id, RECIPE, FOUR_CYCLE).result(WAIT)
        for first in (1, 5):
            results = [classified(first + i, cameras) for i in range(4)]
            reports = tuple(FirstStationReport.from_result(1, r) for r in results)
            group = CompletedReportingBatch(
                reports[0].identity, 7, reports, reports[0].reject_words, 100.0
            )
            if first_arrival:
                owner.offer_group(ctx.run_id, group)
            for r in reversed(results):
                if first == 5 and r.cycle_id == 6:
                    continue  # An attributable group with a lost History detail.
                assert owner.offer(ctx, r)
            owner.offer_group(ctx.run_id, group)
            assert owner.offer(ctx, results[0])
        owner.finish_run(ctx.run_id, "stopped", False).result(WAIT)
        # Delivered again after finish and after restart below.
        owner.offer_group(ctx.run_id, group)
        assert owner.snapshot().failed == 0
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        counts = dict(db.execute("SELECT category,affected FROM batch_categories"))
        assert counts == {MARK: 2, "Dent": 1, LENGTH: 1}
        assert db.execute(
            "SELECT groups,ng,explained FROM batch_coverage"
        ).fetchone() == (1, 4, 2)
        assert db.execute("SELECT count(*) FROM group_classification").fetchone() == (
            1,
        )
        assert db.execute("SELECT count(*) FROM cycle_classification").fetchone() == (
            7,
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    owner = ProductionHistory(path, clock=lambda: now)
    try:
        owner.offer_group(ctx.run_id, group)
        assert not owner.defect_report_scope(HistoryQuery()).result(WAIT).attributable
        snap = tmp_path / "detections.db"
        owner.capture_report(
            ReportRequest("defects", "detections"), snap, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(snap)) as db:
            data = ReportData(db, "en", Event())
            assert data.cycles == 7 and data.categories.total() == 7 * 4
        now += 31 * 86400
        report = tmp_path / "retained.xlsx"
        write_report(
            owner,
            ReportRequest("batch", "capsule", batch.uuid),
            report,
            "en",
            Event(),
            lambda: None,
        )
        with closing(load_workbook(report)) as book:
            assert book.sheetnames == ["Batch Production Report"]
            rows = list(book.worksheets[0].values)
            metrics = next(i for i, r in enumerate(rows) if r[0] == "Inspected")
            assert rows[metrics + 1][2] == 8
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM cycle_classification").fetchone() == (
            0,
        )
        assert db.execute("SELECT count(*) FROM group_classification").fetchone() == (
            0,
        )
        assert (
            dict(db.execute("SELECT category,affected FROM batch_categories")) == counts
        )


def test_filtered_detection_export_includes_every_retained_cycle_and_only_observed_hits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path, clock=lambda: NOW)
    ctx = HistoryContext(NOW, "shift", None, "unbatched", RECIPE, "白色 A")
    try:
        owner.start_run(None, ctx.run_id, RECIPE, None).result(WAIT)
        for i in range(56):
            assert owner.offer(replace(ctx, completed_at=NOW + i), classified(i + 1, 5))
        owner.finish_run(ctx.run_id, "stopped", False).result(WAIT)
        assert owner.snapshot().persisted == 56
    finally:
        finish(owner)
    # Supported pre-classification records coexist with new ones (no inferred labels).
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM cycle_classification WHERE cycle='2'")
    owner = ProductionHistory(path, clock=lambda: NOW + 56)
    try:
        query = HistoryQuery(
            since=NOW + 1,
            until=NOW + 55,
            batch_segment="shift",
            result=frozenset(("ERROR",)),
            limit=1,
            cursor=(NOW + 2, 1),
        )
        path_out = tmp_path / "cycles.xlsx"
        write_report(
            owner,
            ReportRequest("defects", "detections", filters=query),
            path_out,
            "en",
            Event(),
            lambda: None,
        )
        with closing(load_workbook(path_out)) as book:
            assert book.sheetnames == ["Defect Classification Report"]
            rows = list(book.worksheets[0].values)
            assert (MARK, None, None, None, 106) in rows
            assert "53 / 54" in str(rows)
            assert "Background" not in str(rows)
            assert len(rows) < 30  # No individual Cycle/channel rows.
            assert any(
                book.worksheets[0].cell(i, 1).data_type == "s"
                for i, r in enumerate(rows, 1)
                if r[0] == MARK
            )
        # Even a database with no optional classification tables remains exportable.
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        for table in (
            "cycle_classification",
            "group_classification",
            "batch_coverage",
            "batch_categories",
        ):
            db.execute(f"DROP TABLE {table}")
    owner = ProductionHistory(path, clock=lambda: NOW + 56)
    try:
        snap = tmp_path / "legacy.sqlite3"
        owner.capture_report(
            ReportRequest("defects", "detections", filters=query), snap, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(snap)) as db:
            data = ReportData(db, "en", Event())
            assert not data.categories and data.cycles == 54
            assert "0 / 54" in " ".join(data.notes())
    finally:
        finish(owner)


@pytest.mark.parametrize(
    "case",
    [
        "complete",
        "partial_date",
        "partial_result",
        "unattributed",
        "bad_slots",
        "historical",
        "empty",
    ],
)
def test_capsule_basis_requires_whole_filtered_group_scope(
    tmp_path: Path, case: str
) -> None:
    path = tmp_path / "scope.sqlite3"
    owner = ProductionHistory(path, clock=lambda: NOW + 20)
    batch = owner.select_batch("Scoped lot").result(WAIT)
    assert batch
    ctx = HistoryContext(NOW, batch.uuid, batch.label, "grouped", RECIPE)
    try:
        owner.start_run(batch.uuid, ctx.run_id, RECIPE, FOUR_CYCLE).result(WAIT)
        for first in (1, 5):
            results = [classified(first + n, 5) for n in range(4)]
            reports = tuple(FirstStationReport.from_result(1, r) for r in results)
            owner.offer_group(
                ctx.run_id,
                CompletedReportingBatch(
                    reports[0].identity, first, reports, reports[0].reject_words, 1.0
                ),
            )
            for r in results:
                assert owner.offer(replace(ctx, completed_at=NOW + r.cycle_id), r)
        owner.finish_run(ctx.run_id, "stopped", False).result(WAIT)
        if case == "unattributed":
            owner.select_batch(None).result(WAIT)
            owner.start_run(None, "solo", RECIPE, None).result(WAIT)
            assert owner.offer(
                replace(ctx, run_id="solo", batch_label=None, batch_segment="solo"),
                classified(1, 5),
            )
            owner.finish_run("solo", "stopped", False).result(WAIT)
    finally:
        finish(owner)
    with closing(sqlite3.connect(path)) as db, db:
        if case == "bad_slots":
            db.execute("UPDATE group_members SET slot=1 WHERE cycle='2'")
        elif case == "historical":
            db.execute("DELETE FROM group_classification WHERE first_cycle='5'")
            db.execute("DELETE FROM cycle_classification WHERE cycle='5'")
        elif case == "partial_result":
            db.execute("UPDATE cycles SET technical='SUCCESS' WHERE cycle_id='2'")
    owner = ProductionHistory(path, clock=lambda: NOW + 20)
    query = HistoryQuery(
        since=NOW + 2
        if case == "partial_date"
        else NOW + 100
        if case == "empty"
        else None,
        result=frozenset(("ERROR",))
        if case == "partial_result"
        else frozenset(("OK", "NG", "EMPTY", "UNKNOWN", "ERROR")),
    )
    try:
        eligibility = owner.defect_report_scope(query).result(WAIT)
        expected = case in ("complete", "historical")
        assert eligibility.attributable is expected
        request = ReportRequest("defects", "capsule", filters=query)
        snapshot = tmp_path / "snapshot.db"
        if expected:
            owner.capture_report(request, snapshot, Event()).result(WAIT)
            with closing(sqlite3.connect(snapshot)) as db:
                data = ReportData(db, "en", Event())
                assert data.ng == 8 and data.group_count == 2
                factor = 1 if case == "historical" else 2
                assert (
                    data.categories[
                        ("vimo", RECIPE.model_id, RECIPE.model_sha256, MARK)
                    ]
                    == 2 * factor
                )
                assert data.categories.total() == 4 * factor
                assert (
                    data.explained == 2 * factor
                )  # overlapping categories do not inflate total NG
                assert data.covered == factor
                if case == "historical":
                    assert "Incomplete classification records" in " ".join(data.notes())
                    assert "NG with incomplete classification records: 4" in " ".join(
                        data.notes()
                    )
        else:
            with pytest.raises(ValueError, match="Detection Count"):
                owner.capture_report(request, snapshot, Event()).result(WAIT)
        detection = tmp_path / "detections.db"
        owner.capture_report(
            ReportRequest("defects", "detections", filters=query), detection, Event()
        ).result(WAIT)
        with closing(sqlite3.connect(detection)) as db:
            data = ReportData(db, "en", Event())
            assert data.cycles == eligibility.cycles
            if case == "complete":
                assert (
                    data.categories[
                        ("vimo", RECIPE.model_id, RECIPE.model_sha256, MARK)
                    ]
                    == 16
                )
                assert data.categories.total() == 32
    finally:
        finish(owner)


def test_export_rechecks_scope_after_preview_without_silent_basis_change(
    tmp_path: Path,
) -> None:
    owner = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: NOW)
    try:
        batch = owner.select_batch("Lot").result(WAIT)
        assert batch
        owner.start_run(batch.uuid, "run", RECIPE, FOUR_CYCLE).result(WAIT)
        results = [classified(n, 5) for n in range(1, 5)]
        reports = tuple(FirstStationReport.from_result(1, r) for r in results)
        owner.offer_group(
            "run",
            CompletedReportingBatch(
                reports[0].identity, 7, reports, reports[0].reject_words, 1.0
            ),
        )
        ctx = HistoryContext(NOW, batch.uuid, batch.label, "run", RECIPE)
        for result in results:
            assert owner.offer(ctx, result)
        assert owner.defect_report_scope(HistoryQuery()).result(WAIT).attributable
        assert owner.offer(ctx, classified(5, 5))
        target = tmp_path / "existing.xlsx"
        target.write_bytes(b"previous")
        with pytest.raises(ValueError, match="Detection Count"):
            write_report(
                owner,
                ReportRequest("defects", "capsule"),
                target,
                "en",
                Event(),
                lambda: None,
            )
        assert target.read_bytes() == b"previous" and not list(tmp_path.glob("*.tmp"))
    finally:
        finish(owner)
