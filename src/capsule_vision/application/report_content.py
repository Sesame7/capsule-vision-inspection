"""Customer report content from one private snapshot, shared by PDF and Excel."""

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
from .batch_records import run_totals
from .defect_records import Category, Hit, customer_hits
from .models import PilotCounters, ProductionCounters

type Value = str | int | float | datetime | None
type Row = tuple[Value, ...]


@dataclass(frozen=True)
class Table:
    title: str
    headers: tuple[str, ...]
    widths: tuple[int, ...]
    rows: Iterable[Row]
    numeric_columns: tuple[int, ...] = ()
    emphasize_first: bool = False
    detail_rows: bool = False


class ReportData:
    def __init__(self, db: sqlite3.Connection, language: Language, cancel: Event):
        self.db, self.cancel = db, cancel
        self.language: Language = language
        db.row_factory = sqlite3.Row
        self.meta: sqlite3.Row = db.execute("SELECT * FROM meta").fetchone()
        self.scope: str = self.meta["scope"]
        self.basis: str = self.meta["basis"]
        self.title = self.t("customer_" + self.scope)
        self.batch: sqlite3.Row | None = None
        self.positions = PilotCounters()
        self.occurrences = ProductionCounters()
        self.group_count = self.covered = self.ng = self.covered_ng = self.explained = 0
        self.cycles = self.complete = 0
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
        if has_batch:
            self.batch = db.execute("SELECT * FROM batch").fetchone()
            assert self.batch is not None
            self.positions = PilotCounters(**json.loads(self.batch["positions"]))
            self.occurrences = ProductionCounters(
                **json.loads(self.batch["occurrences"])
            )
            if self.scope == "defects":
                self._batch_classification()
        else:
            self.cycles, self.complete = db.execute(
                "SELECT count(*),count(*) FILTER (WHERE complete=1) FROM cycles"
            ).fetchone()
            if self.basis == "detections":
                for row in db.execute("SELECT model_id,model_sha,findings FROM cycles"):
                    self.check()
                    entries: list[tuple[int, str, str]] = json.loads(row[2] or "[]")
                    self.categories.update(
                        (s, m, sha, name)
                        for _, s, m, sha, name in customer_hits(
                            (
                                ch,
                                s,
                                row[0] if s == "vimo" else "",
                                row[1] if s == "vimo" else "",
                                name,
                            )
                            for ch, s, name in entries
                        )
                    )
            else:
                for encoded, hits in db.execute("""SELECT g.final_channels,e.findings
                    FROM plc_groups g LEFT JOIN group_classification e USING(run,first_cycle)"""):
                    self.check()
                    self.group_count += 1
                    finals: list[tuple[int, str]] = json.loads(encoded)
                    ng = sum(s == "NG" for _, s in finals)
                    self.ng += ng
                    if hits is not None:
                        self.covered += 1
                        self.covered_ng += ng
                        group_entries: list[Hit] = json.loads(hits)
                        normalized = customer_hits(group_entries)
                        self.explained += len({hit[0] for hit in normalized})
                        self.categories.update(
                            (s, m, sha, name) for _, s, m, sha, name in normalized
                        )
        models = sorted(
            {(m, s) for source, m, s, _ in self.categories if source == "vimo"}
        )
        self.models = {
            key: f"ViMo {n}" if len(models) > 1 else "ViMo"
            for n, key in enumerate(models, 1)
        }

    def _batch_classification(self) -> None:
        sums = Counter[str]()
        for row in self.db.execute("""SELECT r.uuid,r.positions,r.reporting_mode,c.groups,c.ng,c.explained
            FROM runs r LEFT JOIN run_coverage c ON c.run=r.uuid ORDER BY r.number"""):
            self.check()
            run, saved, mode, groups, ng, explained = row
            totals = run_totals(saved, mode)
            self.run_complete[run] = bool(
                totals is not None
                and groups == totals.batches_completed
                and ng == totals.channel_ng
            )
            self.run_categories[run] = Counter()
            if saved is not None:
                sums.update(json.loads(saved))
            self.covered += groups or 0
            self.covered_ng += ng or 0
            self.explained += explained or 0
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
        if self.batch is not None:
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
        else:
            left.append(
                (
                    t("batch_input"),
                    (
                        t("records_no_batch")
                        if self.meta["unbatched"]
                        else self.meta["filter_label"]
                        if self.meta["scoped_segment"]
                        else t("export_all")
                    ),
                )
            )
            if self.meta["since"] is not None:
                right.append(
                    (t("export_from"), datetime.fromtimestamp(self.meta["since"]))
                )
            if self.meta["until"] is not None:
                right.append(
                    (t("export_before"), datetime.fromtimestamp(self.meta["until"]))
                )
            if self.meta["since"] is None and self.meta["until"] is None:
                right.append((t("export_period"), t("export_all")))
            left.append(
                (
                    t("history_result"),
                    t("export_all")
                    if self.meta["result"] == "all"
                    else self.meta["result"],
                )
            )
            left.append(
                (
                    t("customer_unit"),
                    t(
                        "customer_positions"
                        if self.basis == "capsule"
                        else "export_detections"
                    ),
                )
            )
        return left, right

    def metrics(self) -> tuple[tuple[str, Value], ...]:
        if self.scope in ("audit", "alarm"):
            return ()
        if self.batch is not None:
            if self.scope == "defects":
                return ()
            p = self.positions
            return (
                (
                    self.t("report_inspected"),
                    p.inspected if p.batches_completed else None,
                ),
                ("OK", p.channel_ok if p.batches_completed else None),
                ("NG", p.channel_ng if p.batches_completed else None),
                (
                    self.t("report_pass_rate"),
                    p.channel_ok / p.inspected if p.inspected else None,
                ),
            )
        if self.basis == "capsule":
            return (
                (self.t("customer_total_ng"), self.ng),
                (self.t("customer_classified_ng"), self.explained),
            )
        return ()

    def notes(self) -> Iterator[str]:
        if self.scope in ("audit", "alarm"):
            return
        t = self.t
        if self.batch is not None:
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
        else:
            if self.basis == "capsule":
                if self.covered != self.group_count:
                    yield t("customer_group_coverage")
                if self.covered_ng > self.explained:
                    yield t("customer_unclassified").format(
                        count=self.covered_ng - self.explained
                    )
                if self.ng > self.covered_ng:
                    yield t("customer_missing_ng").format(
                        count=self.ng - self.covered_ng
                    )
            else:
                if self.complete != self.cycles:
                    yield t("customer_cycle_coverage").format(
                        covered=self.complete, total=self.cycles
                    )
            if not self.cycles:
                yield t("export_none")

    def runs(self) -> Iterator[Row]:
        for r in self.db.execute("SELECT * FROM runs ORDER BY number"):
            self.check()
            outcome = {
                "open": "report_in_progress",
                "stopped": "report_stopped",
                "interrupted": "report_interrupted",
                "fault": "state_FAULT",
            }[r["outcome"]]
            totals = run_totals(r["positions"], r["reporting_mode"])
            status = self.t(outcome)
            if r["uncertain"]:
                status += " · " + self.t("run_uncertain_totals")
            yield (
                r["number"],
                datetime.fromtimestamp(r["started"]),
                datetime.fromtimestamp(r["terminated"])
                if r["terminated"] is not None
                else None,
                r["username"],
                status,
                totals.inspected if totals is not None else None,
                totals.channel_ok if totals is not None else None,
                totals.channel_ng if totals is not None else None,
                totals.channel_ok / totals.inspected
                if totals is not None and totals.inspected
                else None,
            )

    def tables(self) -> Iterator[Table]:
        if self.scope == "alarm":
            yield alarm_report.table(self.db, self.language)
            return
        if self.scope == "audit":
            yield audit_report.table(self.db, self.language)
        elif self.batch is not None:
            if self.scope == "defects":
                yield self.matrix_table()
            elif self.db.execute("SELECT count(*) FROM runs").fetchone()[0] > 1:
                yield Table(
                    self.t("report_runs"),
                    tuple(
                        self.t(k)
                        for k in (
                            "export_run",
                            "report_started",
                            "customer_end",
                            "batch_start_account",
                            "identity_status",
                            "report_inspected",
                            "ok",
                            "ng",
                            "report_pass_rate",
                        )
                    ),
                    (5, 16, 16, 20, 10, 9, 7, 7, 10),
                    self.runs(),
                )
        else:
            yield Table(
                self.t("export_category_totals"),
                (
                    self.t("export_category"),
                    self.t("export_count"),
                ),
                (84, 16),
                self.category_rows(),
                numeric_columns=(1,),
            )

    def matrix_table(self, categories: tuple[Category, ...] | None = None) -> Table:
        names = self.category_names()
        keys = tuple(names) if categories is None else categories
        return Table(
            self.t("export_category_totals"),
            (self.t("export_run"), "NG", *(names[key] for key in keys)),
            (28, 12, *(18 for _ in keys)),
            self.batch_category_rows(keys),
            numeric_columns=tuple(range(1, len(keys) + 2)),
            emphasize_first=True,
        )

    def category_rows(self) -> Iterator[Row]:
        for category, name in self.category_names().items():
            self.check()
            yield (name, self.categories[category])

    def batch_category_rows(
        self, categories: tuple[Category, ...] | None = None
    ) -> Iterator[Row]:
        keys = tuple(self.ordered_categories()) if categories is None else categories

        yield (
            self.t("customer_batch_total"),
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
                totals.channel_ng if totals is not None else None,
                *(
                    self.run_categories[row["uuid"]][key]
                    if self.run_complete[row["uuid"]]
                    else None
                    for key in keys
                ),
            )
