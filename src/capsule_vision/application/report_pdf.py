"""Paginated A4 production reports. The export worker owns this canvas."""

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from functools import cache
from itertools import zip_longest
from pathlib import Path
from threading import Event
from typing import cast

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from capsule_vision.localization.languages import Language

from .defect_records import Category
from .report_content import ReportData, Table, Value
from .report_identification import ExportIdentification

_FONT = "CapsuleReportCJK"
_MARGIN = 14 * mm
_SIZE = 10.0
_LINE = 14.0
_PAD = 6.0
_ROW_PAD = 6.0
_BOTTOM = _MARGIN + 26


def _matrix_columns(
    data: ReportData, body: float
) -> list[tuple[tuple[Category, ...], list[float]]]:
    """Keep readable category columns; repeat Run/NG for each horizontal group."""
    names = data.category_names()
    groups: list[tuple[tuple[Category, ...], list[float]]] = []
    keys: list[Category] = []
    widths: list[float] = []
    available = body - 144 - 48

    def finish() -> None:
        if widths:
            extra = (available - sum(widths)) / len(widths)
            groups.append((tuple(keys), [144, 48, *(w + extra for w in widths)]))

    for key, name in names.items():
        width = max(68, min(118, pdfmetrics.stringWidth(name, _FONT, _SIZE) + 2 * _PAD))
        if widths and sum(widths) + width > available:
            finish()
            keys, widths = [], []
        keys.append(key)
        widths.append(width)
    finish()
    return groups or [((), [body - 48, 48])]


@cache
def _glyphs() -> Mapping[int, int]:
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for path in (fonts / "Deng.ttf", fonts / "msyh.ttc", fonts / "simhei.ttf"):
        if path.is_file():
            # types-reportlab leaves the registry parameter untyped. Only pass a TTFont.
            font = TTFont(_FONT, str(path))
            pdfmetrics.registerFont(font)  # pyright: ignore[reportUnknownMemberType]
            return cast(Mapping[int, int], font.face.charToGlyph)
    raise ValueError(
        "A Chinese TrueType font is required for PDF export (DengXian, Microsoft YaHei or SimHei)"
    )


def _wrap(value: Value, width: float, size: float) -> list[str]:
    if value is None:
        return ["—"]
    msg = (
        value.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, datetime)
        else f"{value:.2%}"
        if isinstance(value, float)
        else str(value)
    )
    lines: list[str] = []
    glyphs = _glyphs()
    for part in msg.splitlines() or [""]:
        line = ""
        for char in part:
            if ord(char) not in glyphs:
                raise ValueError(f"PDF font cannot display U+{ord(char):04X}")
            if line and pdfmetrics.stringWidth(line + char, _FONT, size) > width:
                cut = line.rfind(" ")
                if cut > 0 and cut >= len(line) // 2:
                    lines.append(line[:cut])
                    line = line[cut + 1 :]
                else:
                    lines.append(line)
                    line = ""
            line += char
        lines.append(line)
    return lines


class _Pages:
    def __init__(self, data: ReportData, path: Path, *, sample: bool = False):
        self.data = data
        self.sample = sample
        self.width, self.height = landscape(A4)
        self.body = self.width - 2 * _MARGIN
        self.top = self.height - _MARGIN - 59
        self.identification = ExportIdentification.create()
        self.canvas = Canvas(
            str(path), pagesize=(self.width, self.height), pageCompression=1
        )
        self.title = data.title
        self.canvas.setTitle(self.title)
        self.canvas.setCreator("Capsule Vision Inspection")
        self.y = 0.0
        self.page()

    def page(self) -> None:
        self.data.check()
        c = self.canvas
        if self.y:
            c.showPage()
        if self.sample:
            # Paint before the document, with graphics state isolated from all
            # text/table content. Only an explicit engineering renderer opts in.
            c.saveState()
            c.translate(self.width / 2, self.height / 2)
            c.rotate(30)
            c.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.12)
            c.setFont(_FONT, 64)
            c.drawCentredString(0, -22, "SAMPLE")
            c.restoreState()
        c.setFillColorRGB(0.38, 0.40, 0.43)
        c.setFont(_FONT, 8.5)
        c.drawString(_MARGIN, self.height - _MARGIN - 8.5, "Capsule Vision Inspection")
        for offset, key, value in (
            (
                8.5,
                "report_workstation",
                self.identification.workstation or self.data.t("unavailable"),
            ),
            (22, "report_reference", self.identification.reference),
        ):
            c.drawRightString(
                self.width - _MARGIN,
                self.height - _MARGIN - offset,
                f"{self.data.t(key)}: {value}",
            )
        c.setFillColorRGB(0.16, 0.25, 0.34)
        c.setFont(_FONT, 17)
        c.drawString(_MARGIN, self.height - _MARGIN - 33, self.title)
        c.setStrokeColorRGB(0.78, 0.80, 0.82)
        c.setLineWidth(0.5)
        c.line(_MARGIN, self.top + 14, self.width - _MARGIN, self.top + 14)
        # Forward-referenced forms give an exact total without retaining/replaying
        # page canvases or consuming the snapshot's row iterators a second time.
        # types-reportlab omits the form argument types; these calls use only a
        # string name and default bounds, and no extra endForm attributes.
        c.doForm(f"footer-{c.getPageNumber()}")  # pyright: ignore[reportUnknownMemberType]
        self.y = self.top

    def issuance_mark(self) -> None:
        """An underlined software name and explicit process label, never a signer."""
        c = self.canvas
        brand = "Capsule Vision Inspection"
        caption = brand + " · " + self.data.t("report_system_generated")
        size = 8.5
        x = (self.width - pdfmetrics.stringWidth(caption, _FONT, size)) / 2
        c.saveState()
        c.setFont(_FONT, size)
        c.setFillColorRGB(0.38, 0.40, 0.43)
        c.drawString(x, _MARGIN, caption)
        c.setStrokeColorRGB(0.66, 0.68, 0.70)
        c.setLineWidth(0.5)
        c.line(
            x,
            _MARGIN - 2.5,
            x + pdfmetrics.stringWidth(brand, _FONT, size),
            _MARGIN - 2.5,
        )
        c.line(x - 6, _MARGIN - 2.5, x - 6, _MARGIN + 8.5)
        c.line(x - 6, _MARGIN + 8.5, x - 2, _MARGIN + 8.5)
        c.restoreState()

    def save(self) -> None:
        c = self.canvas
        total = c.getPageNumber()
        c.showPage()
        for number in range(1, total + 1):
            self.data.check()
            c.beginForm(f"footer-{number}")  # pyright: ignore[reportUnknownMemberType]
            c.setStrokeColorRGB(0.78, 0.80, 0.82)
            c.setLineWidth(0.5)
            c.line(_MARGIN, _MARGIN + 13, self.width - _MARGIN, _MARGIN + 13)
            c.setFillColorRGB(0.38, 0.40, 0.43)
            c.setFont(_FONT, 9)
            c.drawString(
                _MARGIN,
                _MARGIN,
                f"{self.data.t('report_exported')}: {self.identification.exported.isoformat(' ', timespec='seconds')}",
            )
            if number == 1:
                self.issuance_mark()
            c.drawRightString(
                self.width - _MARGIN,
                _MARGIN,
                self.data.t("report_page_total").format(page=number, total=total),
            )
            c.endForm()  # pyright: ignore[reportUnknownMemberType]
        self.data.check()
        c.save()

    def paragraph(self, value: Value, *, size: float = 10, gap: float = 6) -> None:
        for line in _wrap(value, self.body, size):
            if self.y - size - 5 < _BOTTOM:
                self.page()
            self.canvas.setFillColorRGB(0.12, 0.19, 0.23)
            self.canvas.setFont(_FONT, size)
            self.canvas.drawString(_MARGIN, self.y - size, line)
            self.y -= size + 5
        self.y -= gap

    def summary(self) -> None:
        groups = self.data.field_groups()
        groups[1].append(
            (
                self.data.t("report_as_of"),
                datetime.fromtimestamp(self.data.meta["observed"]),
            )
        )
        group_width = (self.body - 22) / 2
        label_widths = [
            min(
                group_width * 0.42,
                max(
                    (pdfmetrics.stringWidth(label, _FONT, _SIZE) for label, _ in group),
                    default=0,
                )
                + 9,  # One point of tolerance avoids splitting an exact-width label.
            )
            for group in groups
        ]
        for pair in zip_longest(*groups):
            cells: list[tuple[float, list[str]]] = []
            for i, field in enumerate(pair):
                if field is None:
                    continue
                x = _MARGIN + i * (group_width + 22)
                label, value = field
                cells.extend(
                    (
                        (x, _wrap(label, label_widths[i] - 8, _SIZE)),
                        (
                            x + label_widths[i],
                            _wrap(value, group_width - label_widths[i], _SIZE),
                        ),
                    )
                )
            for line in range(max(len(lines) for _, lines in cells)):
                if self.y - _LINE < _BOTTOM:
                    self.page()
                self.canvas.setFillColorRGB(0.18, 0.20, 0.23)
                self.canvas.setFont(_FONT, _SIZE)
                for x, lines in cells:
                    if line < len(lines):
                        self.canvas.drawString(x, self.y - _SIZE, lines[line])
                self.y -= _LINE
            self.y -= 4
        self.y -= 8
        metrics = self.data.metrics()
        if not metrics:
            return
        if self.y - 80 < _BOTTOM:
            self.page()
        width = self.body / len(metrics)
        c = self.canvas
        c.setStrokeColorRGB(0.78, 0.80, 0.82)
        c.line(_MARGIN, self.y, self.width - _MARGIN, self.y)
        c.line(_MARGIN, self.y - 62, self.width - _MARGIN, self.y - 62)
        for i, (label, value) in enumerate(metrics):
            x = _MARGIN + i * width + 10
            c.setFillColorRGB(0.38, 0.40, 0.43)
            c.setFont(_FONT, 10)
            c.drawString(x, self.y - 18, label)
            c.setFillColorRGB(0.16, 0.25, 0.34)
            c.setFont(_FONT, 21)
            caption = (
                f"{value:.2%}"
                if isinstance(value, float)
                else f"{value:,}"
                if isinstance(value, int)
                else "—"
            )
            c.drawString(x, self.y - 48, caption)
        self.y -= 80

    def band(
        self,
        cells: list[list[str]],
        widths: list[float],
        *,
        head: bool = False,
        numeric: tuple[int, ...] = (),
        emphasize: bool = False,
        separator: bool = True,
    ) -> None:
        n = max(map(len, cells))
        h = n * _LINE + _ROW_PAD
        c = self.canvas
        if head:
            c.setFillColorRGB(0.94, 0.95, 0.96)
            c.rect(_MARGIN, self.y - h, self.body, h, fill=1, stroke=0)
            c.setFillColorRGB(0.16, 0.25, 0.34)
        else:
            if emphasize:
                c.setFillColorRGB(0.97, 0.97, 0.98)
                c.rect(_MARGIN, self.y - h, self.body, h, fill=1, stroke=0)
            c.setFillColorRGB(0.18, 0.20, 0.23)
        c.setFont(_FONT, _SIZE)
        x = _MARGIN
        for column, (width, lines) in enumerate(zip(widths, cells, strict=True)):
            for i, line in enumerate(lines):
                if column in numeric:
                    c.drawRightString(x + width - _PAD, self.y - 13 - i * _LINE, line)
                else:
                    c.drawString(x + _PAD, self.y - 13 - i * _LINE, line)
            x += width
        self.y -= h
        if separator:
            c.setStrokeColorRGB(0.84, 0.85, 0.87)
            c.line(_MARGIN, self.y, self.width - _MARGIN, self.y)

    def table(self, table: Table, widths: list[float] | None = None) -> None:
        if widths is None:
            weights = sum(table.widths)
            widths = [self.body * w / weights for w in table.widths]

        def cells(row: tuple[Value, ...]) -> list[list[str]]:
            return [
                _wrap(v, w - 2 * _PAD, _SIZE) for v, w in zip(row, widths, strict=True)
            ]

        headers = cells(table.headers)
        # Recorded labels are unbounded text. A repeated heading must leave room
        # for at least one data line; otherwise pagination cannot make progress.
        header_height = 21 + max(map(len, headers)) * _LINE + _ROW_PAD
        row_space = self.top - _BOTTOM - header_height
        if row_space < _LINE + _ROW_PAD:
            raise ValueError("Report table heading is too tall for an A4 page")

        def heading() -> None:
            self.canvas.setFillColorRGB(0.15, 0.31, 0.39)
            self.canvas.setFont(_FONT, 12)
            self.canvas.drawString(_MARGIN, self.y - 12, table.title)
            self.y -= 21
            self.band(headers, widths, head=True, numeric=table.numeric_columns)

        count = 0
        for row in table.rows:
            self.data.check()
            count += 1
            blocks = [(cells(row[:-1] if table.detail_rows else row), widths)]
            if table.detail_rows and row[-1]:
                blocks.append(
                    ([_wrap(row[-1], self.body - 2 * _PAD, _SIZE)], [self.body])
                )
            height = sum(
                max(map(len, values)) * _LINE + _ROW_PAD for values, _ in blocks
            )
            if count == 1:
                # Keep the first event with its heading. An event larger than a
                # whole page can instead start here and continue on the next.
                required = height if height <= row_space else _LINE + _ROW_PAD
                if self.y - header_height - required < _BOTTOM:
                    self.page()
                heading()
            if self.y - height < _BOTTOM and height <= row_space:
                self.page()
                heading()
            for block, (values, block_widths) in enumerate(blocks):
                while any(values):
                    fit = int((self.y - _BOTTOM - _ROW_PAD) // _LINE)
                    if fit < 1:
                        self.page()
                        heading()
                        fit = int((self.y - _BOTTOM - _ROW_PAD) // _LINE)
                    self.band(
                        [v[:fit] for v in values],
                        block_widths,
                        numeric=table.numeric_columns if len(block_widths) > 1 else (),
                        emphasize=table.emphasize_first and count == 1,
                        separator=block == len(blocks) - 1
                        and all(len(v) <= fit for v in values),
                    )
                    values = [v[fit:] for v in values]
        if not count:
            if self.y - header_height - _LINE - _ROW_PAD < _BOTTOM:
                self.page()
            heading()
            self.band([[self.data.t("export_none")]], [self.body])
        self.y -= 14

    def signatures(self) -> None:
        if self.y - 84 < _BOTTOM:
            self.page()
        c = self.canvas
        width = (self.body - 36) / 2
        c.setFont(_FONT, _SIZE)
        c.setFillColorRGB(0.18, 0.20, 0.23)
        c.setStrokeColorRGB(0.65, 0.67, 0.70)
        for i, key in enumerate(("customer_prepared", "customer_reviewed")):
            x = _MARGIN + i * (width + 36)
            caption = self.data.t(key).rstrip("_ ")
            for offset, label in (
                (26, caption),
                (62, self.data.t("report_signature_date")),
            ):
                c.drawString(x, self.y - offset, label)
                start = x + pdfmetrics.stringWidth(label, _FONT, _SIZE) + 10
                c.line(start, self.y - offset - 2, x + width, self.y - offset - 2)
        self.y -= 84


def write_pdf(
    db: sqlite3.Connection,
    path: Path,
    language: Language,
    cancel: Event,
    *,
    sample: bool = False,
) -> None:
    """Render a report; sample marking is an explicit engineering-only opt-in."""
    _glyphs()
    data = ReportData(db, language, cancel)
    pages = _Pages(data, path, sample=sample)
    pages.summary()
    if data.batch is not None and data.scope == "defects":
        for i, (keys, widths) in enumerate(_matrix_columns(data, pages.body)):
            if i:
                pages.page()
            pages.table(data.matrix_table(keys), widths)
    else:
        for table in data.tables():
            if data.scope == "batch":
                table = replace(table, numeric_columns=(5, 6, 7, 8))
            pages.table(table)
    for note in data.notes():
        pages.paragraph(note, gap=4)
    if data.scope == "batch":
        pages.signatures()
    data.check()
    pages.save()
