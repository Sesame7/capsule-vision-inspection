"""Actual report writers preserve matrix cells across both pagination axes."""

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest
from reportlab.pdfgen.canvas import Canvas
from tests.support.application import WAIT
from tests.support.reports import pdf_text
from tests.unit.test_defect_reports import NOW
from tests.unit.test_durable_batches import RECIPE
from tests.unit.test_history import finish
from tests.unit.test_production_report import FOUR_CYCLE
from tests.unit.test_run_classification import multiview

from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.models import PilotCounters
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
            ReportRequest("defects", batch=batch.uuid), snapshot, Event()
        ).result(WAIT)
    finally:
        finish(owner)
    return snapshot


@pytest.mark.parametrize(("runs", "extra"), [(1, 0), (25, 0), (100, 40)])
def test_matrix_pagination_and_numeric_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runs: int, extra: int
) -> None:
    snapshot = matrix_snapshot(tmp_path / "history.db", runs, extra)
    drawn: list[str] = []
    row_labels: list[tuple[tuple[int, float], str]] = []
    header_lines: list[tuple[int, float]] = []
    numeric_lines: dict[tuple[int, float], list[tuple[float, str]]] = {}
    pages: list[int] = []
    draw, right, page = Canvas.drawString, Canvas.drawRightString, Canvas.showPage

    def record(canvas: Canvas, x: float, y: float, value: str) -> None:
        assert 0 <= y <= 596
        drawn.append(value)
        if value == "Batch Total" or re.fullmatch(r"Run \d+", value):
            row_labels.append(((canvas.getPageNumber(), y), value))
        if value == "Run":
            header_lines.append((canvas.getPageNumber(), y))
        draw(canvas, x, y, value)

    def record_page(canvas: Canvas) -> None:
        pages.append(canvas.getPageNumber())
        page(canvas)

    def record_right(canvas: Canvas, x: float, y: float, value: str) -> None:
        assert 0 <= x <= 842 and 0 <= y <= 596
        drawn.append(value)
        numeric_lines.setdefault((canvas.getPageNumber(), y), []).append((x, value))
        right(canvas, x, y, value)

    monkeypatch.setattr(Canvas, "drawString", record)
    monkeypatch.setattr(Canvas, "drawRightString", record_right)
    monkeypatch.setattr(Canvas, "showPage", record_page)
    with closing(sqlite3.connect(snapshot)) as db:
        if extra:
            db.execute(
                "UPDATE run_categories SET category=? WHERE category='Surface mark 39'",
                ("Long recorded label " * 3 + "visible end",),
            )
        data = ReportData(db, "en", Event())
        assert len(data.ordered_categories()) >= extra
        assert (data.positions.inspected, data.positions.channel_ng) == (
            29 * runs,
            4 * runs,
        )
        write_pdf(db, tmp_path / "matrix.pdf", "en", Event())
        counts = Counter(drawn)
        groups = counts["Batch Total"]
        assert groups >= 1 and (groups > 1) is bool(extra)
        assert [p for p, _ in header_lines] == pages
        anchors = [x for x, _ in numeric_lines[header_lines[0]][:2]]
        for key in header_lines:
            assert numeric_lines[key][:2] == list(
                zip(anchors, ("Inspected", "NG"), strict=True)
            )
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
            for fragment in fragments:
                assert [x for x, _ in fragment[:2]] == anchors
                assert tuple(int(v) for _, v in fragment[:2]) == expected[1:3]
            assert [int(v) for fragment in fragments for _, v in fragment[2:]] == list(
                expected[3:]
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


def test_matrix_missing_coverage_does_not_turn_into_zero(tmp_path: Path) -> None:
    snapshot = matrix_snapshot(tmp_path / "missing.db", 1)
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("DELETE FROM run_coverage")
        data = ReportData(db, "en", Event())
        assert not data.batch_complete
        assert all(
            row[1:3] == (29, 4) and all(v is None for v in row[3:])
            for row in data.batch_category_rows()
        )
        output = tmp_path / "unavailable.pdf"
        write_pdf(db, output, "en", Event())
        assert "—" in pdf_text(output)


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
        assert data.matrix_table().headers == (
            "Run",
            "Inspected",
            "NG",
            *names.values(),
        )


def test_batch_totals_survive_a_missing_run_summary(tmp_path: Path) -> None:
    snapshot = matrix_snapshot(tmp_path / "historical.db", 2)
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("UPDATE runs SET positions=NULL, uncertain=1 WHERE number=1")
        defects = ReportData(db, "en", Event())
        total, missing, known = defects.batch_category_rows()
        assert total[1:3] == (58, 8) and known[1:3] == (29, 4)
        assert missing[1:3] == (None, None)
        assert all(x is None for x in total[3:] + missing[3:])
        assert known[3:5] == (1, 1)
        assert defects.t("run_uncertain_totals") in str(missing[0])

        db.execute("UPDATE meta SET scope='batch'")
        production = ReportData(db, "en", Event())
        total, missing, known = production.production_rows()
        assert total[-4:] == (58, 50, 8, 50 / 58)
        assert known[-4:] == (29, 25, 4, 25 / 29)
        assert missing[-4:] == (None,) * 4
        assert production.t("run_uncertain_totals") in str(missing[0])


@pytest.mark.parametrize("groups", [0, 1])
def test_zero_denominator_with_and_without_an_attributed_group(
    tmp_path: Path,
    groups: int,
) -> None:
    snapshot = matrix_snapshot(tmp_path / "all-empty.db", 1)
    positions = json.dumps(
        asdict(PilotCounters(batches_completed=groups, channel_empty=30 * groups))
    )
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("UPDATE batch SET positions=?", (positions,))
        db.execute("UPDATE runs SET positions=?", (positions,))
        db.execute("UPDATE run_coverage SET groups=?, ng=0, explained=0", (groups,))
        db.execute("DELETE FROM run_categories")
        defects = ReportData(db, "en", Event())
        assert defects.batch_complete
        assert [row[1:] for row in defects.batch_category_rows()] == [
            (0 if groups else None, 0),
            (0, 0),
        ]
        assert defects.matrix_table().headers == ("Run", "Inspected", "NG")
        output = tmp_path / "empty-group.pdf"
        write_pdf(db, output, "en", Event())
        assert "Inspected" in pdf_text(output)

        db.execute("UPDATE meta SET scope='batch'")
        production = ReportData(db, "en", Event())
        total, run = production.production_rows()
        assert total[-4:] == ((0, 0, 0, None) if groups else (None,) * 4)
        assert run[-4:] == (0, 0, 0, None)
        write_pdf(db, output, "en", Event())
        assert "%" not in pdf_text(output)


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
