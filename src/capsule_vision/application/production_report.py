"""Manual report publication from one immutable History snapshot."""

import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
from contextlib import AbstractContextManager, closing, nullcontext
from datetime import datetime
from math import ceil
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.cell import Cell, WriteOnlyCell
from openpyxl.styles import Alignment, Font, NamedStyle, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
from openpyxl.worksheet._writer import WorksheetWriter
from openpyxl.worksheet.cell_range import CellRange
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.writer.excel import ExcelWriter

from capsule_vision.localization.languages import Language

from . import alarm_report
from .alarm_report import AlarmReportRequest
from .audit_report import AuditReportRequest
from .audit_report import capture as capture_audit
from .history import ProductionHistory
from .identity import LocalIdentity
from .report_content import ReportData, Value
from .report_scope import ReportRequest

_EXCEL_ROWS = 1_048_576
_WRAPPED = Alignment(wrap_text=True, vertical="center")


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def write_report(
    history: ProductionHistory | None,
    batch: str | ReportRequest | AuditReportRequest | AlarmReportRequest,
    destination: Path,
    language: Language,
    cancel: Event,
    admit_publication: Callable[[], AbstractContextManager[None] | None],
    *,
    identity: LocalIdentity | None = None,
    revision: int | None = None,
) -> None:
    _check_cancel(cancel)
    # Same-directory replacement also preserves an existing report on failure.
    with NamedTemporaryFile(
        dir=destination.parent, suffix=".tmp", delete=False
    ) as handle:
        staged = Path(handle.name)
    try:
        with TemporaryDirectory(prefix="capsule-report-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            if isinstance(batch, AuditReportRequest):
                if identity is None or revision is None:
                    raise ValueError("Audit export requires an authorized session")
                capture_audit(identity, history, batch, snapshot, cancel, revision)
            elif isinstance(batch, AlarmReportRequest):
                if history is None:
                    raise ValueError("History unavailable")
                history.capture_alarm_report(batch, snapshot, cancel, revision).result()
            else:
                if history is None:
                    raise ValueError("History unavailable")
                history.capture_report(
                    batch, snapshot, cancel
                ).result()  # Export thread only.
            _check_cancel(cancel)
            with closing(
                sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
                try:
                    report_language = "zh" if language == "zh" else "en"
                    if destination.suffix.lower() == ".pdf":
                        from .report_pdf import write_pdf

                        write_pdf(db, staged, report_language, cancel)
                    else:
                        write_workbook(db, staged, report_language, cancel)
                except sqlite3.OperationalError:
                    _check_cancel(cancel)
                    raise
        _check_cancel(cancel)
        with admit_publication() or nullcontext():
            _check_cancel(cancel)
            staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def _sheet(
    book: Workbook, title: str, widths: Iterable[int], freeze: str
) -> WriteOnlyWorksheet:
    sheet = book.create_sheet(title)
    sheet.freeze_panes = freeze
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    return sheet


def _cell(sheet: WriteOnlyWorksheet, value: Value, style: str = "Normal") -> Cell:
    if isinstance(value, str) and len(value.encode("utf-16-le")) > 2 * 32767:
        raise ValueError("Text exceeds the Excel cell length limit")
    cell = WriteOnlyCell(sheet, value)
    if isinstance(value, str):
        cell.data_type = "s"  # Batch labels, IDs and status codes are literal text.
    cell.style = style
    if style == "Normal":
        cell.font = Font(name="Microsoft YaHei", size=11, color="243B47")
    if isinstance(value, str):
        cell.alignment = _WRAPPED
    if isinstance(value, datetime):
        cell.number_format = "yyyy-mm-dd hh:mm:ss"
        cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    if isinstance(value, float):
        cell.number_format = "0.00%"  # The report's only fractional values are rates.
    if style == "Normal" and isinstance(value, (int, float)):
        cell.alignment = Alignment(horizontal="right", vertical="center")
    return cell


def _row(
    sheet: WriteOnlyWorksheet, values: Iterable[Value | Cell], *, heading: bool = False
) -> None:
    cells = [
        v
        if isinstance(v, Cell)
        else _cell(sheet, v, "Heading" if heading else "Normal")
        for v in values
    ]
    # openpyxl's write-only append stub omits its row type. Supply only typed cells.
    sheet.append(cells)  # pyright: ignore[reportUnknownMemberType]


def write_workbook(
    db: sqlite3.Connection, staged: Path, language: Language, cancel: Event
) -> None:
    data = ReportData(db, language, cancel)
    book = Workbook(write_only=True)
    for name, size, bold, fill, color in (
        ("Title", 20, True, "FFFFFF", "193B4E"),
        ("Heading", 11, True, "E8F0F4", "193B4E"),
        ("Metric", 20, True, "E8F0F4", "193B4E"),
        ("Rate", 20, True, "E8F0F4", "193B4E"),
    ):
        book.add_named_style(
            NamedStyle(
                name=name,
                font=Font(name="Microsoft YaHei", size=size, bold=bold, color=color),
                fill=PatternFill("solid", fgColor=fill),
                alignment=Alignment(vertical="center", wrap_text=True),
                number_format="0.00%" if name == "Rate" else "General",
            )
        )
    has_runs = (
        data.scope == "batch"
        and db.execute("SELECT count(*) FROM runs").fetchone()[0] > 1
    )
    matrix = data.batch is not None and data.scope == "defects"
    audit = data.scope == "audit"
    alarm = data.scope == "alarm"
    widths = (
        (20, 24, 24, 24, 16, 12, 10, 10, 14)
        if has_runs
        else (28, 12, *(18 for _ in range(max(5, len(data.categories)))))
        if matrix
        else (24, 20, 25, 30, 24)
        if audit
        else alarm_report.widths(db)
        if alarm
        else (22, 36, 22, 24, 20)
    )
    last = get_column_letter(len(widths))
    metadata_columns = min(8, len(widths))
    header_last = get_column_letter(metadata_columns)
    groups = data.field_groups()
    header_rows = max(map(len, groups))
    sheet = _sheet(
        book,
        data.title,
        widths,
        f"C{header_rows + 6}"
        if matrix
        else f"A{header_rows + 6}"
        if audit or alarm
        else "A3",
    )
    # WriteOnlyWorksheet uses Worksheet._setup for these shared layout attributes;
    # openpyxl stubs omit them from its write-only interface.
    layout = cast(Worksheet, sheet)
    sheet.sheet_view.showGridLines = False
    layout.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    layout.page_setup.orientation = (
        "landscape" if matrix or audit or alarm else "portrait"
    )
    layout.page_setup.paperSize = Worksheet.PAPERSIZE_A4
    layout.page_setup.fitToWidth = 0 if matrix and len(data.categories) > 8 else 1
    layout.page_setup.fitToHeight = 0
    sheet.print_title_rows = (
        f"{header_rows + 5}:{header_rows + 5}" if matrix or audit or alarm else "1:2"
    )
    if matrix:
        sheet.print_title_cols = "A:B"
    number = 0

    def row(
        values: Iterable[Value],
        style: str = "Normal",
        height: float = 24,
        *,
        run_number: bool = False,
        numeric: tuple[int, ...] = (),
    ) -> None:
        nonlocal number
        data.check()
        number += 1
        if number > _EXCEL_ROWS:
            raise ValueError("Report exceeds the Excel worksheet row limit")
        layout.row_dimensions[number].height = height
        cells = [_cell(sheet, v, style) for v in values]
        if run_number:
            cells[0].alignment = Alignment(horizontal="center", vertical="center")
        for column in numeric:
            cells[column].alignment = Alignment(
                horizontal="right", vertical="center", wrap_text=True
            )
        _row(sheet, cells)

    def merged(start: str, end: str) -> None:
        layout.merged_cells.ranges.add(CellRange(f"{start}{number}:{end}{number}"))

    def lines(value: Value, width: int) -> int:
        msg = (
            f"{value:.2%}"
            if isinstance(value, float)
            else str(value)
            if value is not None
            else "—"
        )
        return sum(
            max(1, ceil(sum(1 if ord(c) < 256 else 2 for c in part) / width))
            for part in msg.split("\n")
        )

    try:
        row([data.title], "Title", 38)
        merged("A", header_last)
        row([data.t("report_as_of"), datetime.fromtimestamp(data.meta["observed"])])
        merged("B", header_last)
        # Business metadata occupies two balanced groups independently of matrix width.
        split = min(
            range(2, metadata_columns - 1),
            key=lambda n: abs(sum(widths[:n]) - sum(widths[n:metadata_columns])),
        )
        for i in range(header_rows):
            fields: list[Value] = [None] * len(widths)
            height = 20
            for group, start, end in (
                (groups[0], 0, split),
                (groups[1], split, metadata_columns),
            ):
                if i < len(group):
                    label, value = group[i]
                    fields[start], fields[start + 1] = label, value
                    height = max(
                        height,
                        20
                        * max(
                            lines(label, widths[start] - 2),
                            lines(value, sum(widths[start + 1 : end]) - 2),
                        ),
                    )
            row(fields, height=height)
            merged("B", get_column_letter(split))
            merged(get_column_letter(split + 2), get_column_letter(metadata_columns))
        row([], height=12)
        metrics = data.metrics()
        if metrics:
            spans = (
                ((0, 1), (2, 3), (4, 5), (6, 8))
                if has_runs
                else tuple((i, 4 if i == 3 else i) for i in range(len(metrics)))
            )
            headings: list[Value] = [None] * len(widths)
            metric_cells: list[Value | Cell] = [None] * len(widths)
            for (label, value), (start, _) in zip(metrics, spans, strict=True):
                headings[start] = label
                metric_cells[start] = _cell(
                    sheet,
                    value if value is not None else "—",
                    "Rate" if label == data.t("report_pass_rate") else "Metric",
                )
            row(headings, "Heading", 26)
            for start, end in spans:
                if start != end:
                    merged(get_column_letter(start + 1), get_column_letter(end + 1))
            number += 1
            layout.row_dimensions[number].height = 38
            _row(sheet, metric_cells)
            for start, end in spans:
                if start != end:
                    merged(get_column_letter(start + 1), get_column_letter(end + 1))
            row([], height=12)
        for table in data.tables():
            row([table.title], "Heading")
            merged("A", header_last)
            if matrix:
                row(
                    table.headers,
                    "Heading",
                    20
                    * max(lines(v, widths[i] - 2) for i, v in enumerate(table.headers)),
                    numeric=table.numeric_columns,
                )
            elif len(table.headers) == 2:
                row(
                    [table.headers[0], None, None, None, table.headers[1]],
                    "Heading",
                    32,
                )
                merged("A", "D")
            else:
                row(table.headers, "Heading", 32)
            count = 0
            for values in table.rows:
                count += 1
                if table.detail_rows:
                    row(
                        values[:-1],
                        height=20
                        * max(
                            lines(v, w - 2)
                            for v, w in zip(values[:-1], widths, strict=True)
                        ),
                    )
                    if values[-1]:
                        row(
                            [values[-1]], height=18 * lines(values[-1], sum(widths) - 2)
                        )
                        merged("A", last)
                elif matrix:
                    row(
                        [v if v is not None else "—" for v in values],
                        "Heading" if count == 1 else "Normal",
                        height=20
                        * max(lines(v, widths[i] - 2) for i, v in enumerate(values)),
                        numeric=table.numeric_columns,
                    )
                elif len(values) == 2:
                    row(
                        [values[0], None, None, None, values[1]],
                        height=20 * lines(values[0], 104),
                    )
                    merged("A", "D")
                else:
                    row(
                        values,
                        run_number=not alarm,
                        height=20
                        * max(
                            lines(v, w)
                            for v, w in zip(
                                values, (w - 2 for w in widths), strict=True
                            )
                        ),
                    )
            if not count:
                row([data.t("export_none")])
                merged("A", last)
            row([], height=12)
        for note in data.notes():
            row([note], height=18 * lines(note, 114))
            merged("A", last)
        if data.scope == "batch":
            row([], height=24)
            row(
                [data.t("customer_prepared"), None, data.t("customer_reviewed")],
                height=30,
            )
            merged("A", "B")
            merged("C", last)
        data.check()
        with ZipFile(staged, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            ExcelWriter(book, archive).save()
    finally:
        _clean_sheets((sheet,))


def _clean_sheets(sheets: Iterable[WriteOnlyWorksheet]) -> None:
    for sheet in sheets:
        writer = getattr(sheet, "_writer", None)
        if isinstance(writer, WorksheetWriter):
            try:
                if not sheet.closed:
                    sheet.close()
            finally:
                if isinstance(writer.out, str) and Path(writer.out).exists():
                    writer.cleanup()
