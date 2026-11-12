"""Fictitious commissioning observations for the explicit offline population command."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from capsule_vision.application import (
    alarm_records,
    batch_records,
    batch_review,
    defect_records,
)
from capsule_vision.application._security_lock import SecurityLease
from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.application.audit import AuditOperation, AuditQuery, AuditTrail
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.models import (
    ConfigurationIdentity,
    prepare_configuration,
)
from capsule_vision.application.startup import ApplicationData
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingMode,
    ReportingSettings,
)
from capsule_vision.configuration import (
    ResolvedInspectionConfiguration,
    load_machine_layout,
    resolve_configuration,
)
from capsule_vision.configuration.alarm_definitions import (
    TEMPLATE,
    AlarmDefinitions,
    load_alarm_definitions,
)
from capsule_vision.configuration.catalog import RecipeCatalog, discover_models
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
)
from capsule_vision.configuration.preferences import read_preferences
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleId, CycleStatus, FrameId
from capsule_vision.inspection.production import (
    Assessment,
    CameraInspectionResult,
    CameraTiming,
    ChannelInspectionResult,
    ChannelROI,
    CheckPolicy,
    CheckResult,
    FindingAssignment,
    FindingSource,
    OccupancyEvidence,
    OccupancyResult,
    OccupancyView,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    TraditionalSkipReason,
    ViMoFinding,
)
from capsule_vision.inspection.production.evidence import check_result
from capsule_vision.inspection.production.occupancy import occupancy_rect

# One shift per day. C is ended before the unbatched shift and reselected afterwards.
LOTS = ("A", "A", "B", "B", "C", None, "C", "D", "D", "E", "F", "F")
ALARM_CHECKS_PER_SHIFT = (0, 4, 5, 12, 6, 4, 8, 6, 3, 12, 4, 8)
SAMPLE = "Fictitious commissioning sample"


def example_id(name: str) -> str:
    return uuid5(NAMESPACE_URL, f"capsule-vision/development-review/v1/{name}").hex


@dataclass(frozen=True)
class Shift:
    index: int
    run: str
    label: str | None
    start: float

    @property
    def start_account(self) -> batch_review.StartAccount:
        username = (
            "DEV-Example-Operator" if self.index % 2 == 0 else "DEV-Example-Engineer"
        )
        return batch_review.StartAccount(example_id(username), username)

    @property
    def grouped(self) -> bool:
        return self.index not in (3, 9)

    @property
    def count(self) -> int:
        return 32 if self.grouped else 20

    @property
    def stop(self) -> float:
        return self.start + 105


def shifts(first: float) -> tuple[Shift, ...]:
    return tuple(
        Shift(
            i,
            example_id(f"run/{i}"),
            f"DEV-LOT-{lot}" if lot else None,
            first + i * 86400,
        )
        for i, lot in enumerate(LOTS)
    )


def active_configuration(
    data: ApplicationData,
) -> tuple[ResolvedInspectionConfiguration, ConfigurationIdentity]:
    recipe_id = read_preferences(data.preferences).preferences.last_applied_recipe_id
    entry = next(
        (
            e
            for e in RecipeCatalog(
                data.recipes, process_catalog_path=data.process_catalog
            )
            .discover()
            .entries
            if e.recipe.recipe_id == recipe_id
        ),
        None,
    )
    if entry is None:
        raise ValueError(f"A valid saved current Recipe is required: {recipe_id!r}")
    config = resolve_configuration(
        load_machine_layout(data.machine),
        entry.recipe,
        discover_models(data.models).registry,
        process_catalog=entry.process_catalog,
    )
    if len(config.layout.cameras) not in (5, 6) or not (
        config.capsule.enabled or config.checks.vimo.enabled
    ):
        raise ValueError(
            "Examples require 30 or 36 configured channels and an enabled inspection"
        )
    return config, ConfigurationIdentity.from_prepared(
        prepare_configuration(config, base_directory=data.models)
    )


def channel_result(
    config: ResolvedInspectionConfiguration,
    roi: ChannelROI,
    frame: FrameId,
    empty: bool,
    defect: bool,
    error: bool,
) -> ChannelInspectionResult:
    rect = occupancy_rect(roi.rect)
    upper = rect.width * (rect.height // 2)
    lower = rect.width * rect.height - upper
    occupancy = (
        OccupancyResult(
            error_code="occupancy.execution_error", exception_type="ValueError"
        )
        if error
        else OccupancyResult(
            OccupancyEvidence(
                rect,
                OccupancyView(0 if empty else upper // 3, upper),
                OccupancyView(0 if empty else lower // 3, lower),
            )
        )
    )
    policy = config.checks.vimo
    assignments: tuple[FindingAssignment, ...] = ()
    if defect and not config.capsule.enabled:
        label = "DEV-example-defect"
        if label in policy.disabled_labels:
            raise ValueError(
                "The commissioning example's defect label is excluded by this Recipe"
            )
        mark = BoundingRect(rect.x + rect.width // 2, rect.y + rect.height // 2, 1, 1)
        finding = ViMoFinding(frame, 0, label, 1, mark, "DEV-example")
        assignments = (FindingAssignment(finding, roi.channel_id, 1, 1.0),)
    vimo = check_result(
        FindingSource.VIMO,
        CheckPolicy(policy.enabled, policy.required),
        matched=assignments,
    )
    source = FindingSource.TRADITIONAL
    traditional = check_result(
        source,
        CheckPolicy(config.capsule.enabled and not empty, config.capsule.enabled),
    )
    if defect and config.capsule.enabled:
        code = (
            "capsule.color.upper.tolerance"
            if config.capsule.color.enabled
            else "capsule.size.upper.length"
        )
        traditional = CheckResult(
            source,
            Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG),
            (Reason(source, code),),
        )
    return ChannelInspectionResult(
        frame.camera_id,
        roi.channel_id,
        roi.slot,
        (vimo, traditional),
        assignments,
        occupancy,
        TraditionalSkipReason.CONFIRMED_EMPTY
        if empty and config.capsule.enabled
        else None,
    )


def cycle_result(
    config: ResolvedInspectionConfiguration, shift: Shift, cycle: int
) -> ProductionInspectionResult:
    cameras: list[CameraInspectionResult] = []
    group, slot = divmod(cycle - 1, 4)
    pattern = shift.index * 7 + group
    positions = tuple(
        r.channel_id for camera in config.layout.cameras for r in camera.slots
    )

    def position(offset: int) -> int:
        return positions[(pattern * 5 + offset) % len(positions)]

    # Empty pockets stay empty while the same carriers rotate. Some defects
    # appear in only one view of the rotation; the reporting owner still does the OR.
    empty_count = (0, 1, 0, 2, 0, 1, 3, 0)[pattern % 8]
    empty = {position(i) for i in range(empty_count)}
    defects = {position(5)} if pattern % 4 == 0 and slot == 2 else set[int]()
    if pattern % 9 == 3 and slot in (0, 3):
        defects.add(position(6 + slot))
    errors: set[int] = set()
    if shift.index == 3 and cycle == 9:
        errors.add(
            position(10)
        )  # Per-Cycle inspection without physical group identity.
    if shift.index == 11 and cycle == 27:
        errors.update((position(10), position(11)))
        defects.add(
            position(11)
        )  # Definite NG and technical ERROR on the same channel.
    for camera in config.layout.cameras:
        frame = FrameId(CycleId(cycle), camera.camera_id, cycle)
        channels = tuple(
            channel_result(
                config,
                r,
                frame,
                r.channel_id in empty,
                r.channel_id in defects,
                r.channel_id in errors,
            )
            for r in camera.slots
        )
        height = max(r.rect.y + r.rect.height for r in camera.slots)
        width = max(r.rect.x + r.rect.width for r in camera.slots)
        cameras.append(
            CameraInspectionResult(
                camera,
                frame,
                (height, width, 3),
                channels,
                (),
                CameraTiming(
                    1.1 if config.capsule.enabled else None,
                    4.2 if config.checks.vimo.enabled else None,
                    0.2,
                    0.4,
                ),
            )
        )
    admitted = 5 + group * 12 + slot * 0.14
    return ProductionInspectionResult(
        CycleId(cycle),
        CycleStatus.COMPLETE,
        config.layout,
        tuple(cameras),
        admitted,
        admitted + 0.06,
        0.3,
    )


def alarm_events(
    shift: Shift,
    batch: str | None,
    definitions: AlarmDefinitions,
    selection: StatusPreferences,
) -> Iterator[AlarmEvent]:
    checks = ALARM_CHECKS_PER_SHIFT[shift.index]
    for i in range(checks):
        signal = definitions.signals[(shift.index * 3 + i) % len(definitions.signals)]
        gap = (shift.index, i) in ((2, 3), (8, 1))
        at = shift.start - 120 + i * 9
        if shift.index in (5, 11) and i == checks - 1:
            # A startup fault and explicit reset precede a successful Run.
            # Neither observation has a PLC address or changes a physical group.
            event = AlarmEvent(
                at,
                "production_fault",
                "activated",
                "software",
                "生产应用故障",
                "Production application fault",
                "application",
                None,
                None,
                None,
                "application/1",
                batch,
                shift.label,
                "startup · TimeoutError",
            )
            yield event
            yield replace(event, observed=at + 6, state="recovered")
            continue
        event = AlarmEvent(
            at,
            signal.id,
            "observed_active" if i == 0 else "activated",
            signal.category,
            signal.zh,
            signal.en,
            f"fins_tcp://{selection.endpoint.host}:{selection.endpoint.port}",
            None,
            selection.word_address,
            signal.bit,
            f"{definitions.origin}/{definitions.version}  SHA-256 {definitions.digest}",
            batch,
            shift.label,
            SAMPLE if batch is None else "",
            signal.translations,
        )
        yield event
        yield replace(
            event,
            observed=at + 3 + i % 5,
            state="observed_recovered" if gap else "recovered",
            detail="First observation after a communication gap"
            if gap
            else event.detail,
        )


def make_history(
    path: Path,
    schedule: tuple[Shift, ...],
    config: ResolvedInspectionConfiguration,
    identity: ConfigurationIdentity,
    definitions: AlarmDefinitions,
    alarm: StatusPreferences,
    four: ReportingSettings,
    per_cycle: ReportingSettings,
) -> None:
    now = schedule[0].start - 180
    history = ProductionHistory(path, clock=lambda: now)
    try:
        selection = history.initial_selection.result()
        for shift in schedule:
            now = shift.start - 150
            if shift.label != (selection.label if selection else None):
                selection = history.select_batch(shift.label).result()
            batch = selection.uuid if selection else None
            for event in alarm_events(shift, batch, definitions, alarm):
                history.offer_alarms((event,))
            now = shift.start
            settings = (
                four if shift.grouped else per_cycle if shift.index == 3 else None
            )
            history.start_run(
                batch, shift.run, identity, settings, shift.start_account
            ).result()
            reports: list[FirstStationReport] = []
            for cycle in range(1, shift.count + 1):
                result = cycle_result(config, shift, cycle)
                now = shift.start + result.result_ready_at
                history.admitted(shift.run, cycle)
                context = HistoryContext(
                    now,
                    batch or shift.run,
                    shift.label,
                    shift.run,
                    identity,
                    config.display_name,
                )
                if not history.offer(context, result):
                    raise RuntimeError("History refused a commissioning Cycle")
                if shift.grouped:
                    reports.append(
                        FirstStationReport.from_result(shift.index + 1, result)
                    )
                    if len(reports) == 4:
                        merged = tuple(
                            a | b | c | d
                            for a, b, c, d in zip(
                                *(r.reject_words for r in reports), strict=True
                            )
                        )
                        words = (
                            (merged[0], merged[1])
                            if len(merged) == 2
                            else (merged[0], merged[1], merged[2])
                        )
                        started = reports[0].result_ready_at
                        assert started is not None
                        fact = CompletedReportingBatch(
                            reports[0].identity,
                            4000 + shift.index * 8 + cycle // 4,
                            tuple(reports),
                            words,
                            round((result.result_ready_at - started + 0.06) * 1000, 3),
                        )
                        history.offer_group(shift.run, fact)
                        reports.clear()
            now = shift.stop
            interrupted = shift.index == 9
            history.finish_run(
                shift.run, "interrupted" if interrupted else "stopped", interrupted
            ).result()
        stats = history.snapshot()
        if (
            stats.persisted != 360
            or stats.dropped
            or stats.failed
            or history.alarm_storage() != (0, "")
        ):
            raise RuntimeError("Incomplete commissioning History or Alarm records")
    finally:
        history.request_stop()
        history.join()


@dataclass(frozen=True)
class AuditExample:
    operation: AuditOperation
    completed: str
    outcome: str
    detail: str


def audit_examples(
    schedule: tuple[Shift, ...], recipe: str, recipe_name: str
) -> Iterator[AuditExample]:
    previous = ""
    for shift in schedule:
        admin, engineer, operator = (
            "DEV-Example-Admin",
            "DEV-Example-Engineer",
            shift.start_account.username,
        )
        trainee = f"DEV-Example-Trainee-{shift.index // 3 + 1}"
        account_action, old, new = (
            ("account.create", "", f"{trainee} · OPERATOR · enabled"),
            ("account.role", "OPERATOR", "ENGINEER"),
            ("account.enabled", "True", "False"),
        )[shift.index % 3]
        old_timeout, new_timeout = ((1.0, 2.0), (2.0, 1.0))[(shift.index // 3) % 2]
        engineering = (
            (
                "recipe.save",
                recipe,
                "",
                f"display_name = {recipe_name}"
                if shift.index == 0 and recipe_name
                else "",
            ),
            ("vision.sources", "CameraSources", "", ""),
            (
                "communication.save",
                "CommunicationPreferences",
                f"diagnostic.timeout = {old_timeout}",
                f"diagnostic.timeout = {new_timeout}",
            ),
        )[shift.index % 3]
        events = [
            (-300, admin, "session.login", example_id(admin), "", ""),
            (-280, admin, account_action, example_id(trainee), old, new),
            (-270, admin, "session.logout", example_id(admin), "", ""),
            (-260, engineer, "session.login", example_id(engineer), "", ""),
            (-240, engineer, *engineering),
            (-220, engineer, "session.logout", example_id(engineer), "", ""),
            (-190, operator, "session.login", example_id(operator), "", ""),
            (-170, operator, "production.recipe", recipe, "", recipe),
        ]
        if shift.label != (previous or None):
            events.append(
                (
                    -150,
                    operator,
                    "batch.select" if shift.label else "batch.end",
                    shift.label or previous,
                    previous,
                    shift.label or "",
                )
            )
        events.append((0, operator, "production.start", recipe, "", ""))
        if shift.index != 9:
            events.append((105, operator, "production.stop", shift.run, "", ""))
        events.append((115, operator, "session.logout", example_id(operator), "", ""))
        for seq, (offset, actor, action, target, before, after) in enumerate(events):
            stamp = datetime.fromtimestamp(shift.start + offset, UTC)
            # Keep the original example IDs, including when upgrading this script.
            # A failed save is a requested change only. The Run uses its own captured settings.
            failed = offset == -240 and shift.index in (4, 11)
            issue = (
                ("ConfigurationError" if shift.index == 4 else "ConfigurationIOError")
                if failed
                else ""
            )
            yield AuditExample(
                AuditOperation(
                    example_id(actor),
                    actor,
                    action,
                    target,
                    before,
                    after,
                    example_id(f"audit/{shift.index}/{seq}"),
                    stamp.isoformat(timespec="microseconds"),
                ),
                (stamp + timedelta(milliseconds=120)).isoformat(
                    timespec="microseconds"
                ),
                "failed" if failed else "success",
                issue,
            )
        previous = shift.label or ""


def initialize_empty(path: Path, template: Path, *, audit: bool) -> None:
    """Install the normal owner's empty schema only in a genuinely blank database."""
    with closing(sqlite3.connect(path, timeout=0)) as target:
        target.execute("PRAGMA locking_mode=EXCLUSIVE")
        target.execute("BEGIN EXCLUSIVE")
        version = target.execute("PRAGMA user_version").fetchone()[0]
        occupied = target.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        target.commit()
        if version == (2 if audit else 4):
            return
        if version or occupied:
            raise ValueError(
                f"Unsupported {path.name} schema {version}; existing database preserved"
            )
        if audit:
            owner = AuditTrail(template, lambda _: 0)
            try:
                owner.page(AuditQuery()).result()
            finally:
                owner.close()
        else:
            history = ProductionHistory(template)
            try:
                history.initial_selection.result()
            finally:
                history.request_stop()
                history.join()
        with closing(sqlite3.connect(template)) as blank:
            blank.backup(target)


def installed(
    db: sqlite3.Connection, table: str, key: str, ids: tuple[str, ...]
) -> bool:
    placeholders = ",".join("?" for _ in ids)
    count = db.execute(
        f"SELECT count(*) FROM {table} WHERE {key} IN ({placeholders})", ids
    ).fetchone()[0]
    if count not in (0, len(ids)):
        raise ValueError(
            f"Partial commissioning dataset in {table}; inspect it before rerunning"
        )
    return bool(count)


def append_history(db: sqlite3.Connection) -> None:
    # Transfer only the new owner's records. Never run recovery or retention on pre-existing data.
    if db.execute("SELECT 1 FROM batches JOIN sample.batches USING(label)").fetchone():
        raise ValueError(
            "A development Batch label already exists without the matching example Runs"
        )
    columns = {row[1] for row in db.execute("PRAGMA table_info(alarm_events)")}
    alarm_records.create(db, legacy=bool(columns and "translations" not in columns))
    defect_records.create(db)
    batch_review.initialize(db)
    db.execute(batch_records.RUN_POSITIONS_SCHEMA)
    db.execute("UPDATE batches SET selected=0 WHERE selected=1")
    for table in (
        "batches",
        "batch_ends",
        "runs",
        "run_positions",
        "run_start_accounts",
        "batch_inspection_times",
        "batch_information",
        "plc_groups",
        "group_members",
        "cycles",
        "cycle_classification",
        "group_classification",
        "batch_coverage",
        "batch_categories",
        "alarm_events",
    ):
        # Local auto-increment identifiers belong to the destination. Run/Cycle, Batch and group keys travel together.
        columns_sql = ",".join(
            row[1]
            for row in db.execute(f"PRAGMA sample.table_info({table})")
            if row[1] != "id"
        )
        db.execute(
            f"INSERT INTO main.{table} ({columns_sql}) SELECT {columns_sql} FROM sample.{table} ORDER BY rowid"
        )


def append_audit(
    db: sqlite3.Connection, staged: Path, rows: tuple[AuditExample, ...]
) -> None:
    owner = AuditTrail(staged, lambda _: 0)
    try:
        for row in rows:
            owner.intent(row.operation).result()
            owner.complete(row.operation, row.outcome, row.detail).result()
    finally:
        owner.close()
    db.execute("ATTACH DATABASE ? AS sample_audit", (str(staged),))
    # Review uses rowid keysets. Leave existing rows and their cursors intact.
    minimum = db.execute("SELECT min(rowid) FROM audit.operations").fetchone()[0]
    offset = 0 if minimum is None else min(0, minimum) - len(rows) - 1
    db.execute(
        "INSERT INTO audit.operations(rowid,id,utc,actor_id,username,action,target,old_value,new_value) "
        "SELECT rowid+?,* FROM sample_audit.operations ORDER BY rowid",
        (offset,),
    )
    # The live writer timestamps completion on arrival. Import the historical example's
    # completion time alongside its validated outcome without updating any audit record.
    db.executemany(
        "INSERT INTO audit.completions(id,utc,outcome,detail) "
        "SELECT id,?,outcome,detail FROM sample_audit.completions WHERE id=?",
        ((row.completed, row.operation.id) for row in rows),
    )


def populate(
    data: ApplicationData, *, configuration: ApplicationData | None = None
) -> None:
    print(f"Offline commissioning examples for {data.root}")
    source = configuration or data
    with (
        SecurityLease(data.security),
        TemporaryDirectory(prefix="capsule-review-") as scratch,
    ):
        temp = Path(scratch)
        initialize_empty(data.history, temp / "empty-history.db", audit=False)
        initialize_empty(data.audit, temp / "empty-audit.db", audit=True)
        with closing(sqlite3.connect(data.history, timeout=0)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("ATTACH DATABASE ? AS audit", (str(data.audit),))
            # Take both writer reservations before checking identities. app.py shares the OS maintenance lease.
            db.execute("BEGIN EXCLUSIVE")
            if db.execute("SELECT 1 FROM runs WHERE outcome='open'").fetchone():
                raise ValueError(
                    "History has an unfinished Run; review it in app.py and close before populating"
                )
            today = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
            schedule = shifts((today - timedelta(days=12)).timestamp())
            has_history = installed(
                db, "main.runs", "uuid", tuple(s.run for s in schedule)
            )
            prepared = None
            first_op = example_id("audit/0/0")
            if has_history:
                first, recipe = db.execute(
                    "SELECT started,json_extract(recipe,'$.recipe_id') FROM runs WHERE uuid=?",
                    (schedule[0].run,),
                ).fetchone()
                schedule = shifts(first)
            else:
                prepared = active_configuration(source)
                recipe = prepared[0].recipe_id
                if row := db.execute(
                    "SELECT utc FROM audit.operations WHERE id=?", (first_op,)
                ).fetchone():
                    schedule = shifts(datetime.fromisoformat(row[0]).timestamp() + 300)
            rows = tuple(
                audit_examples(
                    schedule, recipe, prepared[0].display_name if prepared else ""
                )
            )
            has_audit = installed(
                db, "audit.operations", "id", tuple(row.operation.id for row in rows)
            )
            if has_history and has_audit:
                print(
                    "Commissioning examples already installed; existing selection, records and accounts kept."
                )
                return
            if prepared is not None:
                prefs = (
                    load_communication_preferences(source.communication)
                    if source.communication.exists()
                    else CommunicationPreferences()
                )
                definitions = load_alarm_definitions(source.root / TEMPLATE.name)
                # These are historical example settings, never persisted CommunicationPreferences.
                four = prefs.reporting.settings
                if not isinstance(
                    four.endpoint, FinsTcpEndpoint
                ) or four.base_address not in (49, 149):
                    four = ReportingSettings(FinsTcpEndpoint())
                four = replace(four, mode=ReportingMode.FOUR_CYCLE)
                built = temp / "built.db"
                make_history(
                    built,
                    schedule,
                    *prepared,
                    definitions,
                    prefs.status,
                    four,
                    replace(four, mode=ReportingMode.PER_CYCLE),
                )
                db.execute("ATTACH DATABASE ? AS sample", (str(built),))
                append_history(db)
            if not has_audit:
                append_audit(db, temp / "example-audit.db", rows)
            for schema in ("main", "audit"):
                if db.execute(
                    f"PRAGMA {schema}.foreign_key_check"
                ).fetchone() or db.execute(
                    f"PRAGMA {schema}.quick_check"
                ).fetchone() != ("ok",):
                    raise RuntimeError(
                        f"Integrity check failed for {schema}; rolling back"
                    )
        print(
            f"Installed 6 named Batches, 12 Runs, 360 Cycles, 80 four-cycle groups, 144 alarm observations and {len(rows)} example audit entries."
        )
