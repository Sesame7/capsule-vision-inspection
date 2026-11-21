"""Customer report content from one private snapshot, for PDF publication."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import datetime
from threading import Event

from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from . import alarm_report, audit_report
from .audit_presentation import AuditDetailTable
from .batch_records import run_totals
from .defect_records import Category
from .models import PilotCounters, ProductionCounters

type Value = str | int | float | datetime | None
type Row = tuple[Value, ...]


@dataclass(frozen=True, slots=True)
class AuditReportRow:
    values: Row
    detail: str = ""
    detail_tables: tuple[AuditDetailTable, ...] = ()


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    widths: tuple[int, ...]
    rows: Iterable[Row | AuditReportRow]
    numeric_columns: tuple[int, ...] = ()
    emphasize_first: bool = False


def _production_counts(totals: PilotCounters | None) -> Row:
    if totals is None:
        return (None, None, None, None)
    return (
        totals.inspected,
        totals.channel_ok,
        totals.channel_ng,
        totals.channel_ok / totals.inspected if totals.inspected else None,
    )


class ReportData:
    def __init__(self, db: sqlite3.Connection, language: Language, cancel: Event):
        self.db, self.cancel = db, cancel
        self.language: Language = language
        db.row_factory = sqlite3.Row
        self.meta: sqlite3.Row = db.execute("SELECT * FROM meta").fetchone()
        self.scope: str = self.meta["scope"]
        if self.scope not in ("batch", "defects", "audit", "alarm"):
            raise ValueError("Unsupported report scope")
        self.title = self.t("customer_" + self.scope)
        self.batch: sqlite3.Row | None = None
        self.positions = PilotCounters()
        self.occurrences = ProductionCounters()
        self.categories: Counter[Category] = Counter()
        self.models: dict[tuple[str, str], str] = {}
        self.run_categories: dict[str, Counter[Category]] = {}
        self.run_complete: dict[str, bool] = {}
        self.batch_complete = False
        if self.scope in ("audit", "alarm"):
            return
        has_batch = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='batch'"
        ).fetchone()
        if not has_batch:
            raise ValueError("A Batch report requires a persisted Batch")
        self.batch = db.execute("SELECT * FROM batch").fetchone()
        if self.batch is None:
            raise ValueError("Production Batch unavailable")
        self.positions = PilotCounters(**json.loads(self.batch["positions"]))
        self.occurrences = ProductionCounters(**json.loads(self.batch["occurrences"]))
        if self.scope == "defects":
            self._batch_classification()
        models = sorted(
            {(m, s) for source, m, s, _ in self.categories if source == "vimo"}
        )
        self.models = {
            key: f"ViMo {n}" if len(models) > 1 else "ViMo"
            for n, key in enumerate(models, 1)
        }

    def _batch_classification(self) -> None:
        sums = Counter[str]()
        for (
            row
        ) in self.db.execute("""SELECT r.uuid,r.positions,r.reporting_mode,c.groups,c.ng
            FROM runs r LEFT JOIN run_coverage c ON c.run=r.uuid ORDER BY r.number"""):
            self.check()
            run, saved, mode, groups, ng = row
            totals = run_totals(saved, mode)
            self.run_complete[run] = bool(
                totals is not None
                and groups == totals.batches_completed
                and ng == totals.channel_ng
            )
            self.run_categories[run] = Counter()
            if saved is not None:
                sums.update(json.loads(saved))
        for run, source, model, sha, category, count in self.db.execute(
            "SELECT * FROM run_categories"
        ):
            self.check()
            self.run_categories[run][source, model, sha, category] = count
            self.categories[source, model, sha, category] += count
        self.batch_complete = (
            bool(self.run_complete)
            and all(self.run_complete.values())
            and PilotCounters(**sums) == self.positions
        )

    def t(self, key: str) -> str:
        return text(key, self.language)

    def check(self) -> None:
        if self.cancel.is_set():
            raise CancelledError()

    def category(self, source: str, model: str, sha: str, name: str) -> tuple[str, str]:
        if source == "vimo":
            return self.models[model, sha], name
        return self.t("export_traditional"), self.t("customer_" + name)

    def ordered_categories(self) -> list[Category]:
        return sorted(
            self.categories,
            key=lambda c: (
                (0 if c[3] == "length" else 1) if c[0] == "traditional" else 2,
                c[3].casefold(),
                c,
            ),
        )

    def category_names(self) -> dict[Category, str]:
        names = {key: self.category(*key)[1] for key in self.ordered_categories()}
        frequencies = Counter(names.values())
        for key, name in names.items():
            if frequencies[name] > 1 and key[0] == "vimo":
                names[key] = f"{name} ({self.category(*key)[0]})"
        # A recorded label can itself contain the suffix. Keep every identity distinct.
        used: set[str] = set()
        for key, name in names.items():
            caption, n = name, 2
            while caption in used:
                caption = f"{name} ({n})"
                n += 1
            names[key] = caption
            used.add(caption)
        return names

    def field_groups(self) -> tuple[list[tuple[str, Value]], list[tuple[str, Value]]]:
        if self.scope == "alarm":
            return alarm_report.field_groups(self.db, self.language)
        if self.scope == "audit":
            return audit_report.field_groups(self.db, self.language)
        t = self.t
        left: list[tuple[str, Value]] = []
        right: list[tuple[str, Value]] = []
        assert self.batch is not None
        left.append((t("batch_input"), self.batch["label"]))
        for name in ("product", "specification", "notes"):
            if self.batch[name]:
                left.append((t("batch_" + name), self.batch[name]))
        for key, column in (("customer_first", "first"), ("customer_last", "last")):
            if self.batch[column] is not None:
                right.append((t(key), datetime.fromtimestamp(self.batch[column])))
        accounts = [
            r[0]
            for r in self.db.execute(
                "SELECT username FROM runs WHERE username IS NOT NULL GROUP BY username ORDER BY min(number)"
            )
        ]
        if accounts:
            right.append((t("batch_start_account"), " / ".join(accounts)))
        return left, right

    def notes(self) -> Iterator[str]:
        if self.scope in ("audit", "alarm"):
            return
        t = self.t
        if self.scope == "defects":
            if not self.batch_complete and not self.categories:
                yield t("customer_classification_unavailable")
            return
        if not self.positions.batches_completed:
            yield t("customer_statistics_unavailable")
        flags = self.db.execute("""SELECT max(outcome='open'),max(uncertain),
            sum(outcome='interrupted'),sum(outcome='fault'),
            sum(username IS NULL) FROM runs""").fetchone()
        for flag, key in zip(
            flags[:2], ("report_in_progress", "run_uncertain_totals"), strict=True
        ):
            if flag:
                yield t(key)
        for n, key in zip(
            flags[2:4], ("report_interrupted", "state_FAULT"), strict=True
        ):
            if n:
                yield f"{t(key)}: {n} {t('report_runs')}"
        if flags[4]:
            yield t("customer_accounts_missing")
        if any(
            run_totals(saved, mode) is None
            for saved, mode in self.db.execute(
                "SELECT positions,reporting_mode FROM runs"
            )
        ):
            yield t("run_statistics_unavailable")
        if self.occurrences.technical_error_cycles:
            yield t("customer_errors").format(
                count=self.occurrences.technical_error_cycles
            )

    def production_rows(self) -> Iterator[Row]:
        batch = self.positions if self.positions.batches_completed else None
        # Start, end and account describe individual Runs.
        yield (
            self.t("customer_batch_total"),
            "",
            "",
            "",
            *_production_counts(batch),
        )
        for r in self.db.execute("SELECT * FROM runs ORDER BY number"):
            self.check()
            outcome = {
                "open": "report_in_progress",
                "stopped": None,
                "interrupted": "report_interrupted",
                "fault": "state_FAULT",
            }[r["outcome"]]
            label = f"{self.t('export_run')} {r['number']}"
            if outcome:
                label += " · " + self.t(outcome)
            if r["uncertain"]:
                label += "\n" + self.t("run_uncertain_totals")
            yield (
                label,
                datetime.fromtimestamp(r["started"]),
                datetime.fromtimestamp(r["terminated"])
                if r["terminated"] is not None
                else None,
                r["username"],
                *_production_counts(run_totals(r["positions"], r["reporting_mode"])),
            )

    def tables(self) -> Iterator[Table]:
        if self.scope == "alarm":
            yield alarm_report.table(self.db, self.language)
            return
        if self.scope == "audit":
            yield audit_report.table(self.db, self.language)
        elif self.scope == "defects":
            yield self.matrix_table()
        else:
            yield Table(
                self.t("report_summary"),
                tuple(
                    self.t(k)
                    for k in (
                        "batch_run",
                        "report_started",
                        "customer_end",
                        "batch_start_account",
                        "report_inspected",
                        "ok",
                        "ng",
                        "report_pass_rate",
                    )
                ),
                (140, 118, 118, 110, 74, 64, 64, 74),
                self.production_rows(),
                numeric_columns=(4, 5, 6, 7),
                emphasize_first=True,
            )

    def matrix_table(self, categories: tuple[Category, ...] | None = None) -> Table:
        names = self.category_names()
        keys = tuple(names) if categories is None else categories
        return Table(
            self.t("export_category_totals"),
            (
                self.t("export_run"),
                self.t("report_inspected"),
                "NG",
                *(names[key] for key in keys),
            ),
            (28, 14, 12, *(18 for _ in keys)),
            self.batch_category_rows(keys),
            numeric_columns=tuple(range(1, len(keys) + 3)),
            emphasize_first=True,
        )

    def batch_category_rows(
        self, categories: tuple[Category, ...] | None = None
    ) -> Iterator[Row]:
        keys = tuple(self.ordered_categories()) if categories is None else categories

        yield (
            self.t("customer_batch_total"),
            self.positions.inspected if self.positions.batches_completed else None,
            self.positions.channel_ng
            if self.positions.batches_completed or self.batch_complete
            else None,
            *(self.categories[key] if self.batch_complete else None for key in keys),
        )
        for row in self.db.execute("SELECT * FROM runs ORDER BY number"):
            self.check()
            label = f"{self.t('export_run')} {row['number']}\n{datetime.fromtimestamp(row['started']):%Y-%m-%d %H:%M:%S}"
            outcome = {
                "open": "report_in_progress",
                "interrupted": "report_interrupted",
                "fault": "state_FAULT",
            }.get(row["outcome"])
            if outcome:
                label += "\n" + self.t(outcome)
            if row["uncertain"]:
                label += "\n" + self.t("run_uncertain_totals")
            totals = run_totals(row["positions"], row["reporting_mode"])
            yield (
                label,
                totals.inspected if totals is not None else None,
                totals.channel_ng if totals is not None else None,
                *(
                    self.run_categories[row["uuid"]][key]
                    if self.run_complete[row["uuid"]]
                    else None
                    for key in keys
                ),
            )
