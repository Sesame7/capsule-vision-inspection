"""A manual Excel report from one immutable History snapshot. No live DB or Qt access."""

import json
import sqlite3
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.cell import Cell, WriteOnlyCell
from openpyxl.styles import Font, NamedStyle, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet._write_only import WriteOnlyWorksheet
from openpyxl.worksheet._writer import WorksheetWriter
from openpyxl.writer.excel import ExcelWriter

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .history import ProductionHistory
from .models import ConfigurationIdentity, PilotCounters, ProductionCounters

_EXCEL_ROWS = 1_048_576
_RUN_HEADER = 16
type _Value = str | int | float | datetime | None


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise CancelledError()


def write_report(
    history: ProductionHistory,
    batch: str,
    destination: Path,
    language: Language,
    cancel: Event,
    admit_publication: Callable[[], None],
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
            history.capture_report(
                batch, snapshot, cancel
            ).result()  # Export thread only.
            _check_cancel(cancel)
            with closing(
                sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.set_progress_handler(lambda: int(cancel.is_set()), 4096)
                try:
                    write_workbook(
                        db, staged, "zh" if language == "zh" else "en", cancel
                    )
                except sqlite3.OperationalError:
                    _check_cancel(cancel)
                    raise
        _check_cancel(cancel)
        admit_publication()
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


def _cell(sheet: WriteOnlyWorksheet, value: _Value, style: str = "Normal") -> Cell:
    if isinstance(value, str) and len(value.encode("utf-16-le")) > 2 * 32767:
        raise ValueError("Text exceeds the Excel cell length limit")
    cell = WriteOnlyCell(sheet, value)
    if isinstance(value, str):
        cell.data_type = "s"  # Batch labels, IDs and status codes are literal text.
    cell.style = style
    if isinstance(value, datetime):
        cell.number_format = "yyyy-mm-dd hh:mm:ss.000"
    return cell


def _row(
    sheet: WriteOnlyWorksheet, values: Iterable[_Value | Cell], *, heading: bool = False
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
    def t(key: str) -> str:
        return text(key, language)

    db.row_factory = sqlite3.Row
    batch = db.execute("SELECT * FROM batch").fetchone()
    positions = PilotCounters(**json.loads(batch["positions"]))
    occurrences = ProductionCounters(**json.loads(batch["occurrences"]))
    retained, channels = db.execute(
        "SELECT count(*),coalesce(max(json_array_length(final_channels)),0) FROM plc_groups"
    ).fetchone()
    runs, active, uncertain, expired = db.execute(
        "SELECT count(*),max(outcome='open'),max(uncertain),max(detail_expired) FROM runs"
    ).fetchone()
    partial = retained != positions.batches_completed
    if retained > _EXCEL_ROWS - 1 - int(partial) or runs > _EXCEL_ROWS - _RUN_HEADER:
        raise ValueError("Batch exceeds the Excel worksheet row limit")
    if channels not in (0, 30, 36):
        raise ValueError("Invalid stored group layout")
    status = [
        t(key)
        for flag, key in (
            (active, "report_in_progress"),
            (uncertain, "batch_uncertain"),
            (expired, "batch_expired"),
            (partial, "report_partial"),
        )
        if flag
    ]
    book = Workbook(write_only=True)
    book.add_named_style(
        NamedStyle(
            name="Heading",
            font=Font(bold=True, color="FFFFFF"),
            fill=PatternFill("solid", fgColor="295269"),
        )
    )
    book.add_named_style(NamedStyle(name="Rate", number_format="0.00%"))
    summary = _sheet(
        book, t("report_summary"), (38, 40, 26, 26, 26, 25, 24, 52, 30, 68), "A2"
    )
    detail = _sheet(
        book,
        t("report_groups"),
        (26, 38, 16, 20, 20, 20, 20, 21, *(10 for _ in range(channels))),
        "C3" if partial else "C2",
    )
    try:
        _row(summary, [t("report_summary")], heading=True)
        _row(summary, [t("batch_input"), batch["label"]])
        _row(summary, ["Batch UUID", batch["uuid"]])
        _row(summary, [t("report_created"), datetime.fromtimestamp(batch["created"])])
        _row(
            summary,
            [
                t("report_as_of"),
                datetime.fromtimestamp(batch["observed"]),
                t("report_generated"),
                datetime.now(),
            ],
        )
        _row(summary, [t("report_data"), " · ".join(status) or t("report_recorded")])
        _row(summary, [])
        _row(
            summary,
            (
                t(k)
                for k in (
                    "report_completed_groups",
                    "report_inspected",
                    "report_passed",
                    "report_rejected",
                    "report_empty",
                    "report_pass_rate",
                    "report_reject_rate",
                    "report_retained_groups",
                )
            ),
            heading=True,
        )
        passed, rejected = positions.channel_ok, positions.channel_ng
        inspected = passed + rejected
        _row(
            summary,
            [
                positions.batches_completed,
                inspected,
                passed,
                rejected,
                positions.channel_empty,
                *(
                    _cell(summary, n / inspected, "Rate") if inspected else "—"
                    for n in (passed, rejected)
                ),
                retained,
            ],
        )
        _row(summary, [])
        _row(summary, [t("report_occurrences")], heading=True)
        _row(
            summary,
            [
                t("report_started_cycles"),
                t("cycles"),
                t("report_ng_cycles"),
                t("report_err_cycles"),
            ],
        )
        _row(
            summary,
            [
                occurrences.cycles_started,
                occurrences.cycles_completed,
                occurrences.production_ng_cycles,
                occurrences.technical_error_cycles,
            ],
        )
        _row(summary, [])
        _row(summary, [t("report_runs")], heading=True)
        _row(
            summary,
            [
                "Run ID",
                t("report_started"),
                t("report_end_observed"),
                t("identity_status"),
                t("recipe_id"),
                t("plc_mode"),
                t("report_retained_cycles"),
                t("report_data"),
                "Model ID",
                "Model SHA-256",
            ],
            heading=True,
        )
        for run in db.execute("SELECT * FROM runs ORDER BY started,uuid"):
            _check_cancel(cancel)
            recipe = ConfigurationIdentity(**json.loads(run["recipe"]))
            mode = {
                "four_cycle": "plc_mode_four_cycle",
                "per_cycle": "plc_mode_per_cycle",
                "disabled": "service_off",
            }[run["reporting_mode"]]
            outcome = {
                "open": "report_in_progress",
                "stopped": "report_stopped",
                "interrupted": "report_interrupted",
                "fault": "state_FAULT",
            }[run["outcome"]]
            notes = [
                t(k)
                for flag, k in (
                    (run["uncertain"], "run_uncertain_totals"),
                    (run["detail_expired"], "batch_expired"),
                )
                if flag
            ]
            _row(
                summary,
                [
                    run["uuid"],
                    datetime.fromtimestamp(run["started"]),
                    datetime.fromtimestamp(run["terminated"])
                    if run["terminated"] is not None
                    else None,
                    t(outcome),
                    recipe.recipe_id,
                    t(mode),
                    run["retained_cycles"],
                    " · ".join(notes) or "—",
                    recipe.model_id,
                    recipe.model_sha256,
                ],
            )
        if partial:
            _row(
                detail,
                [
                    t("report_partial"),
                    f"{t('report_retained_groups')}  {retained} / {positions.batches_completed}",
                ],
            )
        _row(
            detail,
            [
                t("history_time"),
                "Run ID",
                t("report_counter"),
                *(f"{t('history_cycle')} {n}" for n in range(1, 5)),
                t("report_duration"),
                *(f"CH{n:02d}" for n in range(1, channels + 1)),
            ],
            heading=True,
        )
        _group_rows(db, detail, cancel)
        _check_cancel(cancel)
        with ZipFile(staged, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            ExcelWriter(book, archive).save()
    finally:
        # openpyxl removes its write-only XML files as each sheet is packed.
        # On cancellation or disk failure, release the remaining sheets as well.
        for sheet in (summary, detail):
            writer = getattr(sheet, "_writer", None)
            if isinstance(writer, WorksheetWriter):
                try:
                    if not sheet.closed:
                        sheet.close()
                finally:
                    if isinstance(writer.out, str) and Path(writer.out).exists():
                        writer.cleanup()


def _group_rows(
    db: sqlite3.Connection, sheet: WriteOnlyWorksheet, cancel: Event
) -> None:
    rows = db.execute("""
        WITH slots AS (
            SELECT run, first_cycle, count(*) AS members,
                max(CASE WHEN slot=1 THEN cycle END) AS c1, max(CASE WHEN slot=2 THEN cycle END) AS c2,
                max(CASE WHEN slot=3 THEN cycle END) AS c3, max(CASE WHEN slot=4 THEN cycle END) AS c4
            FROM group_members GROUP BY run,first_cycle)
        SELECT g.observed,g.run,g.first_cycle,g.plc_counter,g.duration_ms,g.final_channels,
               m.members,m.c1,m.c2,m.c3,m.c4
        FROM plc_groups g LEFT JOIN slots m USING(run,first_cycle)
        ORDER BY g.observed,g.run,g.rowid
    """)
    for observed, run, first, counter, duration, encoded, count, *cycles in rows:
        _check_cancel(cancel)
        if count != 4 or None in cycles or len(set(cycles)) != 4 or cycles[0] != first:
            raise ValueError("Incomplete stored PLC group membership")
        pairs: list[tuple[int, str]] = json.loads(encoded)
        finals = dict(pairs)
        if (
            len(pairs) not in (30, 36)
            or set(finals) != set(range(1, len(pairs) + 1))
            or not set(finals.values()) <= {"OK", "NG", "EMPTY", "UNKNOWN"}
        ):
            raise ValueError("Invalid stored group channel outcomes")
        _row(
            sheet,
            [
                datetime.fromtimestamp(observed),
                run,
                counter,
                *cycles,
                duration,
                *(finals[n] for n in range(1, len(pairs) + 1)),
            ],
        )
