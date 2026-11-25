"""History's retained detections and durable attributed categories are different units."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.support.application import WAIT, FakeSession, close
from tests.support.business import mixed_result
from tests.support.production import inferred
from tests.support.reports import pdf_text
from tests.unit.test_configured_occupancy import configuration, cycle, scene
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE

from capsule_vision.application import (
    ConfiguredProductionPipeline,
    ProductionApplication,
)
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.production_report import write_report
from capsule_vision.application.report_scope import (
    HistoryQuery,
    ReportRequest,
    ReportType,
)
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
                ReportRequest("batch", batch=batch.uuid),
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
    finally:
        finish(owner)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db:
        facts = db.execute(
            "SELECT findings,complete FROM cycle_classification"
        ).fetchall()
    assert len(facts) == 2
    for findings, complete in facts:
        entries = json.loads(findings)
        hits = {(ch, name) for ch, source, name in entries if source == "vimo"}
        assert complete and len(hits) == 6 and {name for _, name in hits} == {"Surface"}
    assert len(result.unassigned_findings) == 6


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
        now += 31 * 86400
        report = tmp_path / "retained.pdf"
        write_report(
            owner,
            ReportRequest("batch", batch=batch.uuid),
            report,
            "en",
            Event(),
            lambda: None,
        )
        assert "Batch Production Report" in pdf_text(report)
        assert batch.label in pdf_text(report)
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


def test_export_admission_rejects_unsupported_formats_and_scopes(
    tmp_path: Path,
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    app = ProductionApplication(lambda _p, _g: FakeSession(), history=history)
    try:
        request = ReportRequest("defects", batch="explicit")
        for suffix in ("xlsx", "xls", "csv", "tmp"):
            dest = tmp_path / f"report.{suffix}"
            dest.write_bytes(b"original")
            with pytest.raises(ValueError, match="pdf"):
                app.export_report(request, dest, "en")
            assert dest.read_bytes() == b"original"
        for removed in ({"basis": "detections"}, {"filters": HistoryQuery(since=10)}):
            with pytest.raises(TypeError):
                replace(request, **removed)
        for kind in ("cycles", "detections"):
            with pytest.raises(ValueError, match="Unsupported"):
                ReportRequest(cast(ReportType, kind), batch="explicit")
        with pytest.raises(ValueError, match="Batch identity"):
            replace(request, batch="")
        batch = history.select_batch("Existing Batch").result(WAIT)
        assert batch
        for invalid in (object(), batch.uuid):
            with pytest.raises(ValueError, match="Unsupported"):
                app.export_report(
                    cast(ReportRequest, invalid), tmp_path / "bad.pdf", "en"
                )
            with pytest.raises(ValueError, match="Unsupported"):
                history.capture_report(
                    cast(ReportRequest, invalid), tmp_path / "bad.db", Event()
                )
        assert (
            not (tmp_path / "bad.pdf").exists() and not (tmp_path / "bad.db").exists()
        )
        with pytest.raises(ValueError, match="Batch unavailable"):
            app.export_report(request, tmp_path / "missing.pdf", "en").result(WAIT)
        assert not (tmp_path / "missing.pdf").exists()
    finally:
        close(app)
