"""Printed-page contracts without a PDF parser or additional runtime dependency."""

import re
import secrets
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas
from tests.unit.test_report_matrix import matrix_snapshot

from capsule_vision.application import report_identification
from capsule_vision.application.report_content import ReportData, Table
from capsule_vision.application.report_pdf import write_pdf
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language


@pytest.fixture(autouse=True)
def isolated_export_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        report_identification, "workstation_label", lambda: "WS-0123-4567-89AB"
    )

    def suffix(_size: int) -> str:
        return "a1b2c3"

    monkeypatch.setattr(secrets, "token_hex", suffix)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("scope", ["batch", "defects"])
@pytest.mark.parametrize("workstation", ["WS-0123-4567-89AB", None])
def test_landscape_print_bounds_and_finalized_localized_footers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
    scope: str,
    workstation: str | None,
) -> None:
    monkeypatch.setattr(report_identification, "workstation_label", lambda: workstation)
    snapshot = matrix_snapshot(tmp_path / "history.db", 25)
    marks: list[tuple[int, float, float, str, float]] = []
    pages: list[int] = []
    font, size = "", 0.0
    draw, right, show, set_font = (
        Canvas.drawString,
        Canvas.drawRightString,
        Canvas.showPage,
        Canvas.setFont,
    )
    width, height = landscape(A4)

    def font_changed(
        c: Canvas, name: str, value: float, leading: float | None = None
    ) -> None:
        nonlocal font, size
        font, size = name, value
        set_font(c, name, value, leading)

    def record(c: Canvas, x: float, y: float, value: str) -> None:
        assert x >= 14 * mm - 0.01 and y >= 14 * mm - 0.01
        assert x + pdfmetrics.stringWidth(value, font, size) <= width - 14 * mm + 0.01
        assert y + size <= height - 14 * mm + 0.01
        marks.append((c.getPageNumber(), x, y, value, size))
        draw(c, x, y, value)

    def record_right(c: Canvas, x: float, y: float, value: str) -> None:
        assert x <= width - 14 * mm + 0.01 and y >= 14 * mm - 0.01
        assert x - pdfmetrics.stringWidth(value, font, size) >= 14 * mm - 0.01
        marks.append((c.getPageNumber(), x, y, value, size))
        right(c, x, y, value)

    def page(c: Canvas) -> None:
        pages.append(c.getPageNumber())
        show(c)

    monkeypatch.setattr(Canvas, "setFont", font_changed)
    monkeypatch.setattr(Canvas, "drawString", record)
    monkeypatch.setattr(Canvas, "drawRightString", record_right)
    monkeypatch.setattr(Canvas, "showPage", page)
    path = tmp_path / "report.pdf"
    before = datetime.now().astimezone().replace(microsecond=0)
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("UPDATE meta SET scope=?, observed=1000000000", (scope,))
        write_pdf(db, path, language, Event())
    after = datetime.now().astimezone().replace(microsecond=0)
    assert len(pages) > 1 and pages == list(range(1, len(pages) + 1))
    captions = Counter(mark[3] for mark in marks)
    assert captions["Capsule Vision Inspection"] == len(pages)
    assert "SAMPLE" not in captions
    assert (
        captions[
            "Capsule Vision Inspection · " + text("report_system_generated", language)
        ]
        == 1
    )
    assert captions[text("customer_" + scope, language)] == len(pages)
    assert captions[text("report_as_of", language)] == 1
    observed = datetime.fromtimestamp(1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
    assert captions[observed] == 1
    prefix = text("report_exported", language) + ": "
    exported = [value[len(prefix) :] for value in captions if value.startswith(prefix)]
    assert len(exported) == 1 and exported[0] != observed
    assert before <= datetime.fromisoformat(exported[0]) <= after
    assert captions[prefix + exported[0]] == len(pages)
    assert datetime.fromisoformat(exported[0]).utcoffset() is not None
    assert captions[
        text("report_workstation", language)
        + ": "
        + (workstation or text("unavailable", language))
    ] == len(pages)
    reference_prefix = text("report_reference", language) + ": "
    references = [
        v[len(reference_prefix) :] for v in captions if v.startswith(reference_prefix)
    ]
    assert len(references) == 1
    assert re.fullmatch(r"EXP-\d{8}-\d{6}-A1B2C3", references[0])
    assert captions[reference_prefix + references[0]] == len(pages)
    for number in pages:
        assert (
            captions[
                text("report_page_total", language).format(
                    page=number, total=len(pages)
                )
            ]
            == 1
        )
    # Each physical page references its own footer form; there is no extra blank
    # page after the forms are resolved. Production is landscape as well as matrices.
    pdf = path.read_bytes()
    assert b"/Creator (Capsule Vision Inspection)" in pdf
    if language == "en":
        assert b"/Title (" + text("customer_" + scope, language).encode() + b")" in pdf
    assert set(re.findall(rb"/FormXob\.footer-(\d+)\b", pdf)) == {
        str(n).encode() for n in pages
    }
    sizes = re.findall(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", pdf)
    assert len(sizes) == len(pages)
    assert all((float(w), float(h)) == pytest.approx((width, height)) for w, h in sizes)
    body = [m for m in marks if 14 * mm + 26 < m[2] < height - 14 * mm - 59]
    assert body and all(m[4] >= 9.5 for m in body)
    if scope == "batch":
        assert captions[text("report_signature_date", language)] == 2
        assert sum(m[4] == 21 for m in marks) == 4
        # Counts and rates share the right-aligned numeric table columns.
        assert any(
            m[1] == pytest.approx(width - 14 * mm - 6) and m[3].endswith("%")
            for m in body
        )


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("scope", ["batch", "defects"])
def test_sample_is_explicit_background_only_and_preserves_report_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: Language, scope: str
) -> None:
    snapshot = matrix_snapshot(tmp_path / "history.db", 25)
    identity = report_identification.ExportIdentification.create()
    monkeypatch.setattr(
        report_identification.ExportIdentification, "create", lambda: identity
    )
    marks: list[tuple[int, float, float, str]] = []
    order: list[tuple[int, str]] = []
    pages: list[int] = []
    backgrounds: list[tuple[int, str]] = []
    fills: list[tuple[float, float, float, float | None]] = []
    draw, centered, show, fill = (
        Canvas.drawString,
        Canvas.drawCentredString,
        Canvas.showPage,
        Canvas.setFillColorRGB,
    )

    def record(c: Canvas, x: float, y: float, value: str) -> None:
        marks.append((c.getPageNumber(), x, y, value))
        order.append((c.getPageNumber(), value))
        draw(c, x, y, value)

    def background(c: Canvas, x: float, y: float, value: str) -> None:
        backgrounds.append((c.getPageNumber(), value))
        order.append((c.getPageNumber(), value))
        centered(c, x, y, value)

    def page(c: Canvas) -> None:
        pages.append(c.getPageNumber())
        show(c)

    def color(
        c: Canvas, r: float, g: float, b: float, alpha: float | None = None
    ) -> None:
        fills.append((r, g, b, alpha))
        fill(c, r, g, b, alpha)

    monkeypatch.setattr(Canvas, "drawString", record)
    monkeypatch.setattr(Canvas, "drawCentredString", background)
    monkeypatch.setattr(Canvas, "showPage", page)
    monkeypatch.setattr(Canvas, "setFillColorRGB", color)
    baseline: list[tuple[int, float, float, str]] = []
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("UPDATE meta SET scope=?", (scope,))
        # These ordinary stored values and filenames must never enable a watermark.
        db.execute("UPDATE batch SET label='SAMPLE'")
        for index, sample in enumerate((None, True, False)):
            marks.clear()
            order.clear()
            pages.clear()
            backgrounds.clear()
            fills.clear()
            path = tmp_path / f"SAMPLE-{index}.pdf"
            if sample is None:
                write_pdf(db, path, language, Event())
            else:
                write_pdf(db, path, language, Event(), sample=sample)
            assert len(pages) > 1
            assert any(value == "SAMPLE" for *_, value in marks)
            if index == 0:
                baseline = marks.copy()
            else:
                assert marks == baseline  # Values, positions, pages and identities.
            if sample:
                assert backgrounds == [(number, "SAMPLE") for number in pages]
                assert fills.count((0.5, 0.5, 0.5, 0.12)) == len(pages)
                for number in pages:
                    assert [value for p, value in order if p == number][0] == "SAMPLE"
                assert b"/ca .12" in path.read_bytes()
            else:
                assert not backgrounds and all(alpha is None for *_, alpha in fills)


@pytest.mark.parametrize(("count", "detail_count"), [(30, 2), (1, 25), (1, 105)])
def test_event_change_details_stay_together_or_split_without_losing_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, detail_count: int
) -> None:
    """Exercise just the table layout with controlled, unbounded recorded details."""
    marks: list[tuple[int, float, str]] = []
    pages: list[int] = []
    draw, show = Canvas.drawString, Canvas.showPage
    oversized = detail_count > 25

    def tables(_data: ReportData) -> Iterator[Table]:
        yield Table(
            "Events",
            ("Event",),
            (1,),
            (
                (
                    f"Event {i}",
                    "\n".join(f"Change {i}.{n}" for n in range(detail_count)),
                )
                for i in range(count)
            ),
            detail_rows=True,
        )

    def record(c: Canvas, x: float, y: float, value: str) -> None:
        marks.append((c.getPageNumber(), y, value))
        draw(c, x, y, value)

    def page(c: Canvas) -> None:
        pages.append(c.getPageNumber())
        show(c)

    monkeypatch.setattr(ReportData, "tables", tables)
    monkeypatch.setattr(Canvas, "drawString", record)
    monkeypatch.setattr(Canvas, "showPage", page)
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("""CREATE TABLE meta AS SELECT 'audit' AS scope,'' AS basis,
            1000000000 AS observed,NULL AS since,NULL AS until,NULL AS filter_label,
            0 AS unbatched,0 AS scoped_segment,NULL AS account""")
        write_pdf(db, tmp_path / "events.pdf", "en", Event())
    assert len(pages) > 1
    captions = Counter(value for _, _, value in marks)
    content_pages = {
        p for p, _, value in marks if value.startswith(("Event ", "Change "))
    }
    heading_pages = {p for p, _, value in marks if value == "Event"}
    assert heading_pages == content_pages  # No orphan heading above a page break.
    assert captions["Events"] == captions["Event"] == len(content_pages)
    for i in range(count):
        assert captions[f"Event {i}"] == 1
        assert all(captions[f"Change {i}.{n}"] == 1 for n in range(detail_count))
        if not oversized:
            event_pages = {
                p
                for p, _, v in marks
                if v == f"Event {i}" or v.startswith(f"Change {i}.")
            }
            assert len(event_pages) == 1
    if oversized:
        # A detail longer than a page fills the remaining space instead of an
        # avoidable half-empty page, then continues under the repeated heading.
        for number in pages[:-1]:
            bottom = min(
                y for p, y, value in marks if p == number and value.startswith("Change")
            )
            assert 14 * mm + 26 < bottom < 14 * mm + 26 + 22
