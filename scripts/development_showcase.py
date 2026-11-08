"""Deterministic synthetic observations for the existing offline review workflow.

No SDK, image generation, camera or transport is used. The expected manifest is
computed from the scenario, separately from History's stored projections.
"""

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import cast
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid5
from zipfile import ZipFile

from development_records import AuditExample, active_configuration, channel_result

from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.application.audit import AuditOperation, AuditTrail
from capsule_vision.application.batch_review import BatchInformation, StartAccount
from capsule_vision.application.history import HistoryContext, ProductionHistory
from capsule_vision.application.startup import ApplicationData
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    FirstStationReport,
    ReportingMode,
    ReportingSettings,
)
from capsule_vision.configuration import ResolvedInspectionConfiguration
from capsule_vision.configuration.alarm_definitions import (
    TEMPLATE,
    AlarmDefinitions,
    load_alarm_definitions,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleId, CycleStatus, FrameId
from capsule_vision.inspection.production import (
    Assessment,
    CameraInspectionResult,
    CameraTiming,
    ChannelInspectionResult,
    CheckResult,
    FindingAssignment,
    FindingSource,
    ProductionInspectionResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)
from capsule_vision.inspection.production.evidence import check_result

# SQLite header provenance, not a production schema or a hardware observation.
APPLICATION_ID = 0x43565348  # CVSH: Capsule Vision synthetic showcase history.
VERSION = "capsule-vision/showcase/v1"
ACTORS = ("chen.min", "li.wei", "zhou.ning")
ENGINEER = "wu.jie"
GROUP_SECONDS = 6


def identity(name: str) -> str:
    return uuid5(NAMESPACE_URL, f"{VERSION}/{name}").hex


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def object_map(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) for k in cast(dict[object, object], value)
    ):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)


def model_labels(
    config: ResolvedInspectionConfiguration, models: Path
) -> tuple[str, ...]:
    """Read only this known package metadata form; never execute a vendor library.

    active_configuration already verifies the artifact digest. Unsupported or
    missing metadata is a limitation, not permission to invent defect names.
    """
    path = models / config.model.model_path
    with ZipFile(path) as archive:
        if archive.getinfo("manifest").file_size > 262_144:
            raise ValueError("Model manifest is too large for offline label inspection")
        manifest = object_map(json.loads(archive.read("manifest")))
    product = object_map(object_map(manifest["product"])["vimo_cloud"])
    modules = product.get("module_infos")
    if not isinstance(modules, list):
        raise ValueError("Model has no supported offline label catalog")
    for item in cast(list[object], modules):
        module = object_map(item)
        if module.get("id") != config.model.module_id:
            continue
        labels = module.get("features")
        if not isinstance(labels, list) or not all(
            isinstance(v, str) and v.strip() and v.isprintable()
            for v in cast(list[object], labels)
        ):
            raise ValueError("Model label metadata is incomplete")
        enabled = tuple(
            v for v in cast(list[str], labels) if config.checks.vimo.label_enabled(v)
        )
        if len(set(enabled)) != len(enabled) or len(enabled) < 2:
            raise ValueError(
                "Showcase needs at least two enabled, verified ViMo labels"
            )
        return enabled
    raise ValueError("Configured module has no supported offline label catalog")


@dataclass(frozen=True)
class Run:
    index: int
    uuid: str
    batch: str | None
    label: str | None
    started: float
    ended: float
    groups: int
    username: str
    outcome: str = "stopped"
    elevated: bool = False

    @property
    def actor(self) -> StartAccount:
        return StartAccount(identity("account/" + self.username), self.username)


@dataclass(frozen=True)
class Batch:
    uuid: str
    label: str
    info: BatchInformation


@dataclass(frozen=True)
class Plan:
    anchor: str
    profile: str
    batches: tuple[Batch, ...]
    runs: tuple[Run, ...]


def schedule(anchor: date, offset: timezone, *, stress: bool = False) -> Plan:
    day = datetime.combine(anchor - timedelta(days=13), datetime.min.time(), offset)
    # Day, hour, minute, Batch, number of observed physical groups, operator.
    entries = (
        (0, 8, 10, 0, 180, 0),
        (0, 13, 20, 0, 360, 1),
        (1, 8, 5, 1, 240, 0),
        (1, 10, 40, 2, 420, 2),
        (2, 8, 0, 2, 300, 1),
        (2, 11, 0, 3, 120, 2),
        (2, 11, 34, 3, 260, 2),
        (3, 8, 10, 3, 320, 0),
        (4, 9, 20, 4, 480, 1),
        (5, 8, 0, 4, 260, 2),
        (6, 8, 30, 5, 140, 0),
        (6, 13, 10, 5, 400, 1),
        (7, 8, 10, 6, 220, 0),
        (8, 8, 20, 6, 360, 2),
        (8, 13, 10, 6, 180, 1),
        (9, 8, 0, 7, 520, 2),
        (9, 14, 30, 7, 160, 0),
        (10, 8, 15, 8, 320, 1),
        (10, 13, 20, 8, 460, 0),
        (11, 8, 10, 8, 280, 2),
        (11, 14, 0, -1, 12, 1),
        (12, 8, 10, 9, 420, 2),
        (12, 13, 30, 9, 300, 0),
        (13, 8, 0, 9, 240, 1),
        (13, 13, 20, 9, 380, 2),
    )
    if stress:
        entries = tuple(
            (13, 8 + i // 20, 3 * (i % 20), 0, 2, i % 3) for i in range(100)
        )
    batches: dict[int, Batch] = {}
    runs: list[Run] = []
    profile = "stress" if stress else "production"
    for i, (d, hour, minute, b, groups, actor) in enumerate(entries):
        start = day + timedelta(days=d, hours=hour, minutes=minute)
        if b >= 0 and b not in batches:
            label = f"GC{start:%y%m%d}-{b + 1:02}"
            batches[b] = Batch(
                identity(f"{profile}/{anchor}/batch/{b}"),
                label,
                BatchInformation(
                    "Gelatin capsules",
                    "Size 0 · Yellow",
                    "Continued across shifts" if b in (0, 2, 3, 6, 9) else "",
                ),
            )
        batch = batches.get(b)
        runs.append(
            Run(
                i,
                identity(f"{profile}/{anchor}/run/{i}"),
                batch.uuid if batch else None,
                batch.label if batch else None,
                start.timestamp(),
                start.timestamp() + groups * GROUP_SECONDS + 20,
                groups,
                ACTORS[actor],
                "interrupted" if i == 12 and not stress else "stopped",
                not stress and i == 7,
            )
        )
    if not stress:
        # The interrupted Run's end is the later recovery observation, not a last
        # capsule timestamp. No synthetic Stop/logout is claimed for the crash.
        runs[12] = replace(runs[12], ended=runs[13].started - 180)
    return Plan(anchor.isoformat(), profile, tuple(batches.values()), tuple(runs))


@dataclass(frozen=True)
class Hit:
    channel: int
    source: str
    category: str
    slots: tuple[int, ...]


@dataclass(frozen=True)
class Group:
    empty: frozenset[int]
    hits: tuple[Hit, ...]
    errors: frozenset[int]


def group_plan(
    run: Run,
    index: int,
    channels: int,
    labels: tuple[str, ...],
    *,
    size: bool,
    color: bool,
) -> Group:
    seed = int.from_bytes(hashlib.sha256(f"{run.uuid}/{index}".encode()).digest()[:8])
    rng = Random(seed)
    empty = frozenset(
        ch
        for ch in range(1, channels + 1)
        if rng.random() < (0.055 if index < 5 else 0.012)
    )
    categories = [("vimo", label) for label in labels]
    if size:
        categories += [
            ("traditional", f"capsule.size.{view}.length")
            for view in ("upper", "lower")
        ]
    if color:
        categories += [
            ("traditional", f"capsule.color.{view}.tolerance")
            for view in ("upper", "lower")
        ]
    hits: list[Hit] = []
    for channel in range(1, channels + 1):
        if channel in empty or rng.random() >= (
            0.065 if run.elevated else 0.008 + (run.index % 4) * 0.002
        ):
            continue
        source, category = rng.choice(categories)
        slots = tuple(sorted(rng.sample(range(4), 2 if rng.random() < 0.28 else 1)))
        hits.append(Hit(channel, source, category, slots))
        if rng.random() < 0.07:
            other = rng.choice([c for c in categories if c != (source, category)])
            hits.append(Hit(channel, *other, (rng.randrange(4),)))
    errors = frozenset(
        [next(ch for ch in range(1, channels + 1) if ch not in empty)]
        if run.index in (7, 12) and index == 17
        else []
    )
    return Group(empty, tuple(hits), errors)


@dataclass
class Expected:
    groups: int = 0
    ok: int = 0
    ng: int = 0
    empty: int = 0
    unknown: int = 0
    technical_error_cycles: int = 0
    multi_category_positions: int = 0
    multi_vimo_label_positions: int = 0
    explained_ng: int = 0
    positions: Counter[str] = field(default_factory=Counter[str])
    detections: Counter[str] = field(default_factory=Counter[str])

    def add(self, group: Group, channels: int) -> None:
        # Only scenario facts are inputs: no reports, final_channels, stored
        # counters or classification projections are consulted by this oracle.
        defects = {hit.channel for hit in group.hits}
        rejected = defects | group.errors
        self.groups += 1
        self.ng += len(rejected)
        self.empty += len(group.empty)
        self.ok += channels - len(rejected) - len(group.empty)
        self.technical_error_cycles += bool(group.errors)
        self.explained_ng += len(defects)
        for hit in group.hits:
            key = hit.source + "/" + hit.category
            self.positions[key] += 1
            self.detections[key] += len(hit.slots)
        for channel in defects:
            own = [h for h in group.hits if h.channel == channel]
            self.multi_category_positions += len(own) > 1
            self.multi_vimo_label_positions += sum(h.source == "vimo" for h in own) > 1

    def values(self) -> dict[str, object]:
        return {
            **asdict(self),
            "inspected": self.ok + self.ng,
            "positions": dict(sorted(self.positions.items())),
            "detections": dict(sorted(self.detections.items())),
        }


def cycle(
    config: ResolvedInspectionConfiguration,
    run: Run,
    group: Group,
    index: int,
    slot: int,
) -> ProductionInspectionResult:
    number = index * 4 + slot + 1
    cameras: list[CameraInspectionResult] = []
    for camera in config.layout.cameras:
        frame = FrameId(CycleId(number), camera.camera_id, number)
        rows: list[ChannelInspectionResult] = []
        for roi in camera.slots:
            row = channel_result(
                config,
                roi,
                frame,
                roi.channel_id in group.empty,
                False,
                roi.channel_id in group.errors and slot == 1,
            )
            hits = [
                h for h in group.hits if h.channel == roi.channel_id and slot in h.slots
            ]
            width, height = min(80, roi.rect.width), min(50, roi.rect.height)
            marks = tuple(
                FindingAssignment(
                    ViMoFinding(
                        frame,
                        roi.slot * 16 + i,
                        hit.category,
                        width * height,
                        BoundingRect(roi.rect.x, roi.rect.y, width, height),
                        config.model.module_id,
                    ),
                    roi.channel_id,
                    width * height,
                    1.0,
                )
                for i, hit in enumerate(hits)
                if hit.source == "vimo"
            )
            vimo = check_result(
                FindingSource.VIMO, config.checks.vimo.policy, matched=marks
            )
            traditional = tuple(
                Reason(FindingSource.TRADITIONAL, h.category)
                for h in hits
                if h.source == "traditional"
            )
            checks = (
                vimo,
                CheckResult(
                    FindingSource.TRADITIONAL,
                    Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG),
                    traditional,
                )
                if traditional
                else row.checks[1],
            )
            rows.append(replace(row, checks=checks, vimo_findings=marks))
        cameras.append(
            CameraInspectionResult(
                camera,
                frame,
                (
                    max(r.rect.y + r.rect.height for r in camera.slots),
                    max(r.rect.x + r.rect.width for r in camera.slots),
                    3,
                ),
                tuple(rows),
                (),
                CameraTiming(None, None, 0, None),
            )
        )
    admitted = 5 + index * GROUP_SECONDS + slot * 0.18
    return ProductionInspectionResult(
        CycleId(number),
        CycleStatus.COMPLETE,
        config.layout,
        tuple(cameras),
        admitted,
        admitted + 0.08,
        0,
    )


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="microseconds")


def audit_facts(plan: Plan, recipe: str) -> tuple[AuditExample, ...]:
    events: list[AuditExample] = []

    def add(
        at: float,
        actor: str,
        action: str,
        target: str,
        before: str = "",
        after: str = "",
    ) -> None:
        operation = AuditOperation(
            identity("account/" + actor),
            actor,
            action,
            target,
            before,
            after,
            identity(f"{plan.profile}/{plan.anchor}/audit/{len(events)}"),
            utc(at),
        )
        events.append(AuditExample(operation, utc(at + 0.12), "success", ""))

    seen: set[str] = set()
    previous: Run | None = None
    for i, run in enumerate(plan.runs):
        new_session = (
            previous is None
            or previous.username != run.username
            or run.started - previous.started >= 8 * 3600
            or previous.outcome == "interrupted"
        )
        if new_session:
            add(run.started - 160, run.username, "session.login", run.actor.account_id)
            add(
                run.started - 145,
                run.username,
                "production.recipe",
                recipe,
                after=recipe,
            )
        if previous is None or previous.batch != run.batch:
            if run.batch is not None:
                add(
                    run.started - 130,
                    run.username,
                    "batch.select",
                    run.batch,
                    previous.label or "" if previous else "",
                    run.label or "",
                )
            elif previous is not None and previous.batch is not None:
                add(
                    run.started - 130,
                    run.username,
                    "batch.end",
                    previous.batch,
                    previous.label or "",
                )
        if run.batch is not None and run.batch not in seen:
            seen.add(run.batch)
            batch = next(b for b in plan.batches if b.uuid == run.batch)
            before, after = BatchInformation().changes(batch.info)
            add(
                run.started - 110,
                run.username,
                "batch.information",
                batch.label,
                before,
                after,
            )
        add(run.started, run.username, "production.start", run.uuid, after=recipe)
        if run.outcome != "interrupted":
            add(run.ended, run.username, "production.stop", run.uuid)
            following = plan.runs[i + 1] if i + 1 < len(plan.runs) else None
            if (
                following is None
                or following.username != run.username
                or following.started - run.started >= 8 * 3600
            ):
                add(
                    run.ended + 20, run.username, "session.logout", run.actor.account_id
                )
        previous = run
    if plan.profile == "production":
        for index, before, after in ((8, "1.0", "2.0"), (15, "2.0", "1.0")):
            at = plan.runs[index].started - 1800
            add(at, ENGINEER, "session.login", identity("account/" + ENGINEER))
            add(
                at + 120,
                ENGINEER,
                "communication.save",
                "CommunicationPreferences",
                "diagnostic.timeout = " + before,
                "diagnostic.timeout = " + after,
            )
            add(at + 180, ENGINEER, "session.logout", identity("account/" + ENGINEER))
    return tuple(sorted(events, key=lambda e: e.operation.utc))


@dataclass(frozen=True)
class AlarmRead:
    observed: float
    word: int | None
    batch: str | None
    label: str | None


def alarm_facts(
    plan: Plan,
    definitions: AlarmDefinitions,
) -> tuple[tuple[AlarmEvent, ...], tuple[AlarmRead, ...]]:
    if plan.profile == "stress":
        return (), ()
    events: list[AlarmEvent] = []
    reads: list[AlarmRead] = []
    inactive = sum(1 << s.bit for s in definitions.signals if not s.active_high)
    episodes = (
        (
            5,
            "material_shortage_stop",
            plan.runs[5].ended - 1,
            plan.runs[6].started - 120,
            False,
            False,
        ),
        (
            11,
            "low_air_pressure",
            plan.runs[11].ended + 30,
            plan.runs[11].ended + 270,
            False,
            True,
        ),
        (
            15,
            "homing_required",
            plan.runs[15].started - 90,
            plan.runs[15].started - 15,
            True,
            False,
        ),
        (
            20,
            "inspection_not_enabled",
            plan.runs[20].started - 90,
            plan.runs[20].started - 15,
            True,
            False,
        ),
    )
    for index, key, start, end, first, gap in episodes:
        signal = next((s for s in definitions.signals if s.id == key), None)
        if signal is None:
            raise ValueError(
                f"Showcase alarm scenario requires a configured definition: {key}"
            )
        run = plan.runs[index]
        if not first:
            reads.append(AlarmRead(start - 1, inactive, run.batch, run.label))
        reads.append(
            AlarmRead(start, inactive ^ (1 << signal.bit), run.batch, run.label)
        )
        if gap:
            reads.append(AlarmRead((start + end) / 2, None, run.batch, run.label))
        reads.append(AlarmRead(end, inactive, run.batch, run.label))
        event = AlarmEvent(
            start,
            key,
            "observed_active" if first else "activated",
            signal.category,
            signal.zh,
            signal.en,
            "fins_tcp://127.0.0.1:9600",
            1,
            18,
            signal.bit,
            f"Synthetic showcase; {definitions.origin}/{definitions.version} SHA-256 {definitions.digest}",
            run.batch,
            run.label,
            translations=signal.translations,
        )
        events.extend(
            (
                event,
                replace(
                    event,
                    observed=end,
                    state="observed_recovered" if gap else "recovered",
                    detail="First valid observation after a communication gap"
                    if gap
                    else "",
                ),
            )
        )
    return tuple(events), tuple(reads)


def build(
    source: ApplicationData,
    directory: Path,
    plan: Plan,
) -> Path:
    """Create an empty, isolated pair through existing owners and validate it."""
    if directory.exists():
        raise ValueError("Showcase staging directory must not already exist")
    config, captured = active_configuration(source)
    labels = model_labels(config, source.models)
    definitions = load_alarm_definitions(source.root / TEMPLATE.name)
    alarms, reads = alarm_facts(plan, definitions)
    audits = audit_facts(plan, config.recipe_id)
    channels = len(config.layout.cameras) * 6
    settings = ReportingSettings(FinsTcpEndpoint(), mode=ReportingMode.FOUR_CYCLE)
    directory.mkdir(parents=True)
    data = ApplicationData.at_entry(directory)
    data.root.mkdir()
    now = plan.runs[0].started - 200
    selected: str | None = None
    batch_id = plan.batches[0].uuid
    by_batch = {b.uuid: Expected() for b in plan.batches}
    expected_runs: list[dict[str, object]] = []
    # Only this disposable fixture owner exists in this process. Scope the UUID
    # provider to generation, preserving the real selection/admission path.
    with patch(
        "capsule_vision.application.batch_records.uuid4", lambda: UUID(batch_id)
    ):
        history = ProductionHistory(data.history, clock=lambda: now)
        try:
            history.initial_selection.result()
            seen: set[str] = set()
            for run in plan.runs:
                now = run.started - 130
                if selected != run.batch:
                    batch_id = run.batch or batch_id
                    history.select_batch(run.label).result()
                    selected = run.batch
                if run.batch is not None and run.batch not in seen:
                    seen.add(run.batch)
                    info = next(b.info for b in plan.batches if b.uuid == run.batch)
                    history.update_batch_information(
                        run.batch,
                        BatchInformation(),
                        info,
                        lambda _l, _i: nullcontext(),
                    ).result()
                now = run.started
                history.start_run(
                    run.batch, run.uuid, captured, settings, run.actor
                ).result()
                expected = Expected()
                for index in range(run.groups):
                    group = group_plan(
                        run,
                        index,
                        channels,
                        labels,
                        size=config.capsule.size.enabled,
                        color=config.capsule.color.enabled,
                    )
                    expected.add(group, channels)
                    if run.batch is not None:
                        by_batch[run.batch].add(group, channels)
                    reports: list[FirstStationReport] = []
                    for slot in range(4):
                        result = cycle(config, run, group, index, slot)
                        now = run.started + result.result_ready_at
                        history.admitted(run.uuid, result.cycle_id)
                        if not history.offer(
                            HistoryContext(
                                now,
                                run.batch or run.uuid,
                                run.label,
                                run.uuid,
                                captured,
                                config.display_name,
                            ),
                            result,
                        ):
                            raise RuntimeError("Showcase History handoff was refused")
                        reports.append(
                            FirstStationReport.from_result(run.index + 1, result)
                        )
                    merged = tuple(
                        a | b | c | d
                        for a, b, c, d in zip(
                            *(r.reject_words for r in reports), strict=True
                        )
                    )
                    history.offer_group(
                        run.uuid,
                        CompletedReportingBatch(
                            reports[0].identity,
                            (1000 + run.index * 1000 + index) % 65536,
                            tuple(reports),
                            merged,
                            620.0,
                        ),
                    )
                    # Drain a bounded slice, never grow the inbox or fabricate a
                    # persisted result after an overflow. This is offline only.
                    if index % 8 == 7:
                        history.batch_page("").result()
                now = run.ended
                history.finish_run(
                    run.uuid, run.outcome, run.outcome == "interrupted"
                ).result()
                expected_runs.append(
                    {
                        **asdict(run),
                        "actor_id": run.actor.account_id,
                        "totals": expected.values(),
                    }
                )
            now = plan.runs[-1].ended + 30
            if alarms:
                history.offer_alarms(alarms)
            history.batch_page("").result()
            state = history.snapshot()
            if (
                state.persisted != sum(r.groups * 4 for r in plan.runs)
                or state.dropped
                or state.failed
                or history.alarm_storage() != (0, "")
            ):
                raise RuntimeError("Showcase facts were not completely persisted")
        finally:
            history.request_stop()
            history.join()
    completed = ""
    with patch("capsule_vision.application.audit._utc", lambda: completed):
        audit = AuditTrail(data.audit, lambda _: 0)
        try:
            for event in audits:
                completed = event.completed
                audit.intent(event.operation).result()
                audit.complete(event.operation, event.outcome, event.detail).result()
        finally:
            audit.close()
    for path in (data.history, data.audit):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
    manifest: dict[str, object] = {
        "version": VERSION,
        "synthetic": True,
        "profile": plan.profile,
        "anchor": plan.anchor,
        "provenance": "Planned synthetic evidence; no acquisition, inference SDK, PLC I/O, ACK or ejection confirmation. No camera archives.",
        "configuration": {
            **asdict(captured),
            "recipe_name": config.display_name,
            "channels": channels,
            "vimo_labels": labels,
            "size_enabled": config.capsule.size.enabled,
            "color_enabled": config.capsule.color.enabled,
        },
        "limitations": (
            []
            if config.capsule.size.enabled
            else ["Size is disabled in the saved Recipe; no Size finding is generated."]
        )
        + (
            []
            if config.capsule.color.enabled
            else [
                "Color is disabled in the saved Recipe; no Color finding is generated."
            ]
        ),
        "expected": {
            "batches": [
                {**asdict(b), "totals": by_batch[b.uuid].values()} for b in plan.batches
            ],
            "runs": expected_runs,
            "audit": [asdict(e) for e in audits],
            "alarms": [asdict(e) for e in alarms],
            "alarm_reads": [asdict(r) for r in reads],
        },
    }
    target = directory / "expected.json"
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    verify(directory)
    return target


def verify(directory: Path, data: ApplicationData | None = None) -> dict[str, int]:
    """Compare persisted projections with the independently planned manifest."""
    manifest = object_map(
        json.loads((directory / "expected.json").read_text(encoding="utf-8"))
    )
    if manifest.get("version") != VERSION or manifest.get("synthetic") is not True:
        raise ValueError("Not a synthetic showcase manifest")
    expected = object_map(manifest["expected"])
    batches = [object_map(v) for v in cast(list[object], expected["batches"])]
    runs = [object_map(v) for v in cast(list[object], expected["runs"])]
    data = data or ApplicationData.at_entry(directory)
    with closing(sqlite3.connect(data.history.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Missing synthetic History provenance")
        if (
            db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("Invalid showcase History")
        if db.execute("SELECT count(*) FROM runs").fetchone()[0] != len(
            runs
        ) or db.execute("SELECT count(*) FROM batches").fetchone()[0] != len(batches):
            raise ValueError("Unexpected showcase Run/Batch scope")
        for kind, items in (("batch", batches), ("run", runs)):
            for item in items:
                uid = str(item["uuid"])
                wanted = object_map(item["totals"])
                row = db.execute(
                    "SELECT positions FROM batches WHERE uuid=?"
                    if kind == "batch"
                    else "SELECT positions FROM run_positions WHERE run=?",
                    (uid,),
                ).fetchone()
                if row is None:
                    raise ValueError("Missing durable showcase summary")
                totals = object_map(json.loads(row[0]))
                if totals != {
                    "batches_completed": wanted["groups"],
                    **{
                        "channel_" + key: wanted[key]
                        for key in ("ok", "ng", "empty", "unknown")
                    },
                } or wanted["inspected"] != cast(int, totals["channel_ok"]) + cast(
                    int, totals["channel_ng"]
                ):
                    raise ValueError(
                        f"{kind} totals disagree with independent manifest: {uid}"
                    )
                categories: Counter[str] = Counter()
                detections: Counter[str] = Counter()
                multi = vimo_multi = explained = 0
                condition = "r.batch=?" if kind == "batch" else "r.uuid=?"
                for (encoded,) in db.execute(
                    f"SELECT g.findings FROM group_classification g JOIN runs r ON r.uuid=g.run WHERE {condition}",
                    (uid,),
                ):
                    entries = cast(
                        list[tuple[int, str, str, str, str]], json.loads(encoded)
                    )
                    for _, source, _, _, name in entries:
                        categories[source + "/" + name] += 1
                    for ch in {e[0] for e in entries}:
                        own = [e for e in entries if e[0] == ch]
                        multi += len(own) > 1
                        vimo_multi += sum(e[1] == "vimo" for e in own) > 1
                        explained += 1
                for (encoded,) in db.execute(
                    f"SELECT c.findings FROM cycle_classification c JOIN runs r ON r.uuid=c.run WHERE {condition}",
                    (uid,),
                ):
                    for _, source, name in cast(
                        list[tuple[int, str, str]], json.loads(encoded)
                    ):
                        detections[source + "/" + name] += 1
                if (
                    dict(categories) != wanted["positions"]
                    or dict(detections) != wanted["detections"]
                    or (multi, vimo_multi, explained)
                    != (
                        wanted["multi_category_positions"],
                        wanted["multi_vimo_label_positions"],
                        wanted["explained_ng"],
                    )
                ):
                    raise ValueError(
                        "Classification or overlap disagrees with independent manifest"
                    )
                if kind == "batch":
                    durable = dict(
                        db.execute(
                            "SELECT source||'/'||category,affected FROM batch_categories WHERE batch=?",
                            (uid,),
                        )
                    )
                    coverage = db.execute(
                        "SELECT groups,ng,explained FROM batch_coverage WHERE batch=?",
                        (uid,),
                    ).fetchone()
                    if durable != wanted["positions"] or coverage != (
                        wanted["groups"],
                        wanted["ng"],
                        wanted["explained_ng"],
                    ):
                        raise ValueError(
                            "Durable classification differs from independent manifest"
                        )
                    info = object_map(item["info"])
                    stored = db.execute(
                        "SELECT b.label,i.product,i.specification,i.notes FROM batches b JOIN batch_information i ON i.batch=b.uuid WHERE b.uuid=?",
                        (uid,),
                    ).fetchone()
                    if stored != (
                        item["label"],
                        info["product"],
                        info["specification"],
                        info["notes"],
                    ):
                        raise ValueError("Batch product metadata differs")
        for run in runs:
            saved = db.execute(
                "SELECT r.batch,r.started,r.terminated,r.outcome,r.uncertain,a.account_id,a.username FROM runs r JOIN run_start_accounts a ON a.run=r.uuid WHERE r.uuid=?",
                (run["uuid"],),
            ).fetchone()
            if saved != (
                run["batch"],
                run["started"],
                run["ended"],
                run["outcome"],
                run["outcome"] == "interrupted",
                run["actor_id"],
                run["username"],
            ):
                raise ValueError("Run lifecycle/account evidence differs")
            actual = db.execute(
                "SELECT count(*),sum(technical='ERROR'),min(completed_at),max(completed_at) FROM cycles WHERE run_id=?",
                (run["uuid"],),
            ).fetchone()
            if actual[:2] != (
                4 * cast(int, run["groups"]),
                object_map(run["totals"])["technical_error_cycles"],
            ) or not cast(float, run["started"]) <= actual[2] <= actual[3] <= cast(
                float, run["ended"]
            ):
                raise ValueError("Cycle time/count/technical evidence differs")
        cycles = db.execute("SELECT count(*) FROM cycles").fetchone()[0]
        groups = sum(cast(int, r["groups"]) for r in runs)
        if (
            cycles != 4 * groups
            or db.execute("SELECT count(*) FROM group_members").fetchone()[0] != cycles
            or db.execute("SELECT count(*) FROM group_classification").fetchone()[0]
            != groups
        ):
            raise ValueError("Incomplete four-cycle coverage")
        alarm_count = (
            db.execute("SELECT count(*) FROM alarm_events").fetchone()[0]
            if expected["alarms"]
            else 0
        )
        if alarm_count != len(cast(list[object], expected["alarms"])):
            raise ValueError("Incomplete alarm observations")
        if alarm_count:
            columns = "observed,signal,state,category,zh,en,source,station,address,bit,definition,batch_id,batch_label,detail"
            saved_alarms = db.execute(
                f"SELECT {columns} FROM alarm_events ORDER BY id"
            ).fetchall()
            if saved_alarms != [
                tuple(object_map(e)[k] for k in columns.split(","))
                for e in cast(list[object], expected["alarms"])
            ]:
                raise ValueError(
                    "Alarm transitions/context differ from simulated observations"
                )
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='archive_images'"
            ).fetchone()
            and db.execute("SELECT 1 FROM archive_images LIMIT 1").fetchone()
        ):
            raise ValueError("Synthetic showcase must not contain fabricated archives")
        if db.execute("""SELECT 1 FROM group_members m JOIN cycles c ON (c.run_id,c.cycle_id)=(m.run,m.cycle)
            JOIN plc_groups g USING(run,first_cycle) WHERE CAST(m.cycle AS INTEGER)!=CAST(m.first_cycle AS INTEGER)+m.slot-1 OR (m.slot=4 AND c.completed_at!=g.observed) LIMIT 1""").fetchone():
            raise ValueError(
                "Invalid physical group membership or completion timestamp"
            )
    with closing(sqlite3.connect(data.audit.as_uri() + "?mode=ro", uri=True)) as db:
        if (
            db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("Invalid showcase Audit/provenance")
        rows = db.execute(
            "SELECT o.id,o.utc,o.actor_id,o.username,o.action,o.target,o.old_value,o.new_value,c.utc,c.outcome,c.detail FROM operations o JOIN completions c USING(id) ORDER BY o.utc"
        ).fetchall()
        planned: list[tuple[object, ...]] = []
        for value in cast(list[object], expected["audit"]):
            record = object_map(value)
            op = object_map(record["operation"])
            planned.append(
                tuple(
                    op[k]
                    for k in (
                        "id",
                        "utc",
                        "actor_id",
                        "username",
                        "action",
                        "target",
                        "before",
                        "after",
                    )
                )
                + (record["completed"], record["outcome"], record["detail"])
            )
        if rows != planned:
            raise ValueError("Audit timeline differs from independent manifest")
    return {
        "batches": len(batches),
        "runs": len(runs),
        "groups": groups,
        "cycles": cycles,
        "audit": len(planned),
        "alarms": alarm_count,
    }
