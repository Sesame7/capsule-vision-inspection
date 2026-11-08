"""Actual report writers preserve matrix cells across both pagination axes."""

import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from openpyxl import load_workbook
from reportlab.pdfgen.canvas import Canvas
from tests.support.application import WAIT
from tests.unit.test_defect_reports import NOW
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE
from tests.unit.test_run_classification import multiview

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.production_report import write_workbook
from capsule_vision.application.report_content import ReportData
from capsule_vision.application.report_pdf import write_pdf
from capsule_vision.application.report_scope import ReportRequest
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
)
from capsule_vision.inspection.production import (
    FindingSource,
    ProductionInspectionResult,
    Reason,
)


def matrix_snapshot(path: Path, count: int, extra: int = 0) -> Path:
    """One eligible fact per Run; overlapping labels affect the same NG position."""
    now = NOW
    owner = ProductionHistory(path, clock=lambda: now)
    snapshot = path.with_name(path.stem + "-snapshot.db")
    try:
        batch = owner.select_batch("QA-Matrix").result(WAIT)
        assert batch
        for i in range(count):
            run = f"run-{i}"
            owner.start_run(batch.uuid, run, RECIPE, FOUR_CYCLE).result(WAIT)
            results: list[ProductionInspectionResult] = []
            for cycle in range(1, 5):
                result = multiview(cycle)
                camera = result.cameras[0]
                channel = camera.channels[0]
                vimo, traditional = channel.checks
                vimo = replace(
                    vimo,
                    reasons=vimo.reasons
                    + tuple(
                        Reason(
                            FindingSource.VIMO, "defect_region", f"Surface mark {n:02d}"
                        )
                        for n in range(extra)
                    ),
                )
                result = replace(
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
                results.append(result)
                assert owner.offer(
                    HistoryContext(now, batch.uuid, batch.label, run, RECIPE), result
                )
            reports = tuple(FirstStationReport.from_result(1, r) for r in results)
            owner.offer_group(
                run,
                CompletedReportingBatch(
                    reports[0].identity, i + 1, reports, reports[0].reject_words, 123.0
                ),
            )
            owner.finish_run(run, "stopped", False).result(WAIT)
            now += 60
        assert owner.snapshot().failed == 0
        owner.capture_report(
            ReportRequest("defects", "capsule", batch.uuid), snapshot, Event()
        ).result(WAIT)
    finally:
        finish(owner)
    return snapshot


@pytest.mark.parametrize(("runs", "extra"), [(1, 0), (25, 0), (100, 40)])
def test_matrix_pagination_and_numeric_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: int, extra: int
) -> None:
    snapshot = matrix_snapshot(tmp_path / "history.db", runs, extra)
    drawn: list[str] = []
    row_labels: list[tuple[tuple[int, float], str]] = []
    numeric_lines: dict[tuple[int, float], list[str]] = {}
    pages: list[int] = []
    draw, right, page = Canvas.drawString, Canvas.drawRightString, Canvas.showPage

    def record(canvas: Canvas, x: float, y: float, value: str) -> None:
        assert 0 <= y <= 596
        drawn.append(value)
        if value == "Batch Total" or re.fullmatch(r"Run \d+", value):
            row_labels.append(((canvas.getPageNumber(), y), value))
        draw(canvas, x, y, value)

    def record_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        page(canvas)

    def record_right(canvas: Canvas, x: float, y: float, value: str) -> None:
        assert 0 <= x <= 842 and 0 <= y <= 596
        drawn.append(value)
        numeric_lines.setdefault((canvas.getPageNumber(), y), []).append(value)
        right(canvas, x, y, value)

    monkeypatch.setattr(Canvas, "drawString", record)
    monkeypatch.setattr(Canvas, "drawRightString", record_right)
    monkeypatch.setattr(Canvas, "showPage", record_page)
    with closing(sqlite3.connect(snapshot)) as db:
        data = ReportData(db, "en", Event())
        keys = data.ordered_categories()
        assert data.positions.channel_ng == 4 * runs
        write_pdf(db, tmp_path / "matrix.pdf", "en", Event())
        counts = Counter(drawn)
        groups = counts["Batch Total"]
        assert groups >= 1 and (groups > 1) is bool(extra)
        for i in range(1, runs + 1):
            assert counts[f"Run {i}"] == groups
        expected_rows = list(data.batch_category_rows())
        for expected in expected_rows:
            label = str(expected[0]).splitlines()[0]
            fragments = [
                numeric_lines[key]
                for key, actual_label in row_labels
                if actual_label == label
            ]
            assert all(int(fragment[0]) == expected[1] for fragment in fragments)
            assert [int(v) for fragment in fragments for v in fragment[1:]] == list(
                expected[2:]
            )
        for name in data.category_names().values():
            assert "".join(name.split()) in "".join(
                "".join(line.split()) for line in drawn
            )
        sizes = re.findall(
            rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]",
            (tmp_path / "matrix.pdf").read_bytes(),
        )
        assert len(sizes) == len(pages) and all(float(w) > float(h) for w, h in sizes)
        assert len(pages) == 1 if runs == 1 else len(pages) > groups
        workbook = tmp_path / "matrix.xlsx"
        write_workbook(db, workbook, "en", Event())
        with closing(load_workbook(workbook)) as book:
            sheet = book.worksheets[0]
            rows = list(sheet.values)
            start = next(i for i, r in enumerate(rows) if r[0] == "Batch Total")
            assert rows[start - 1][: len(keys) + 2] == tuple(
                data.matrix_table().headers
            )
            actual = [r[: len(keys) + 2] for r in rows[start : start + runs + 1]]
            assert actual == expected_rows
            assert all(type(v) is int for row in actual for v in row[1:])
            assert sum(cast(int, r[1]) for r in actual[1:]) == actual[0][1]
            assert sheet.freeze_panes == f"C{start + 1}"


def test_matrix_missing_coverage_does_not_turn_into_zero(tmp_path: Path) -> None:
    snapshot = matrix_snapshot(tmp_path / "missing.db", 1)
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("DELETE FROM run_coverage")
        data = ReportData(db, "en", Event())
        assert not data.batch_complete
        assert all(
            row[1] == 4 and all(v is None for v in row[2:])
            for row in data.batch_category_rows()
        )
        output = tmp_path / "unavailable.xlsx"
        write_workbook(db, output, "en", Event())
        with closing(load_workbook(output)) as book:
            rows = list(book.worksheets[0].values)
            first = next(i for i, row in enumerate(rows) if row[0] == "Batch Total")
            assert all(v == "—" for row in rows[first : first + 2] for v in row[2:])


def test_matrix_literal_suffix_collisions_keep_distinct_columns(tmp_path: Path) -> None:
    snapshot = matrix_snapshot(tmp_path / "collision.db", 1)
    with closing(sqlite3.connect(snapshot)) as db:
        # A possible recorded label is identical to a generated disambiguation suffix.
        db.execute(
            "INSERT INTO run_categories SELECT run,source,model,sha,'Length (ViMo)',affected FROM run_categories WHERE category='Length'"
        )
        data = ReportData(db, "en", Event())
        names = data.category_names()
        assert len(set(names.values())) == len(names)
        assert "Length (ViMo) (2)" in names.values()
        assert len(data.matrix_table().headers) == len(names) + 2


def test_oversized_recorded_label_fails_pdf_without_pagination_loop(
    tmp_path: Path,
) -> None:
    snapshot = matrix_snapshot(tmp_path / "long.db", 1)
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute(
            "UPDATE run_categories SET category=? WHERE category='Dent'",
            ("Long " * 1000,),
        )
        with pytest.raises(ValueError, match="heading is too tall"):
            write_pdf(db, tmp_path / "long.pdf", "en", Event())
