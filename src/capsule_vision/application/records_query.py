"""Shared Records search scope and bounded, evidence-only identity lookups."""

import sqlite3
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, replace
from threading import Event
from typing import TYPE_CHECKING

from .audit import AuditQuery, AuditRecord
from .identity_models import Role

if TYPE_CHECKING:
    from .history import ProductionHistory
    from .identity import LocalIdentity


@dataclass(frozen=True, slots=True)
class RecordsScope:
    since: int | None = None
    until: int | None = None
    batch: str | None = None
    unbatched: bool = False

    def __post_init__(self) -> None:
        if self.batch is not None and self.unbatched:
            raise ValueError("A Records scope cannot select a Batch and no Batch")


@dataclass(frozen=True, slots=True)
class BatchChoice:
    uuid: str
    label: str
    activity: float

    @property
    def cursor(self) -> tuple[float, str]:
        return self.activity, self.uuid


@dataclass(frozen=True, slots=True)
class BatchChoices:
    rows: tuple[BatchChoice, ...]
    more: bool
    selected_matches: bool
    unbatched: bool


# An interval matches an individual Run, never the span between separate Runs.
RUN_OVERLAP = """(:until IS NULL OR r.started<:until)
    AND (:since IS NULL OR r.started>=:since OR r.terminated>:since
         OR (r.outcome='open' AND :now>=:since))"""


def batch_choices(
    db: sqlite3.Connection,
    scope: RecordsScope,
    search: str,
    cursor: tuple[float, str] | None,
    now: float,
    alarms: bool = False,
) -> BatchChoices:
    args: dict[str, object] = {
        "since": scope.since,
        "until": scope.until,
        "now": now,
        "search": search,
        "batch": scope.batch,
    }
    if (
        scope.since is not None
        and scope.until is not None
        and scope.since >= scope.until
    ):
        return BatchChoices((), False, False, False)
    # Closed Runs supply an end observation. Open Runs supply only recorded
    # activity, not a fabricated last-inspection timestamp or the query clock.
    sql = (
        """WITH matching AS (
        SELECT b.uuid,b.label,max(coalesce(r.terminated,
            max(r.started,
                coalesce((SELECT max(c.completed_at) FROM cycles c WHERE c.run_id=r.uuid),r.started),
                coalesce((SELECT max(g.observed) FROM plc_groups g WHERE g.run=r.uuid),r.started)))) AS activity
        FROM batches b JOIN runs r ON r.batch=b.uuid WHERE """
        + RUN_OVERLAP
        + " GROUP BY b.uuid) "
    )
    clauses = ["instr(lower(label),lower(:search))>0"]
    if cursor is not None:
        clauses.append("(activity,uuid)<(:activity,:uuid)")
        args["activity"], args["uuid"] = cursor
    rows = db.execute(
        sql
        + "SELECT uuid,label,activity FROM matching WHERE "
        + " AND ".join(clauses)
        + " ORDER BY activity DESC,uuid DESC LIMIT 51",
        args,
    ).fetchall()
    selected = (
        scope.batch is None
        or db.execute(
            "SELECT 1 FROM runs r WHERE r.batch=:batch AND " + RUN_OVERLAP + " LIMIT 1",
            args,
        ).fetchone()
        is not None
    )
    unbatched = (
        db.execute(
            "SELECT 1 FROM runs r WHERE r.batch IS NULL AND "
            + RUN_OVERLAP
            + " LIMIT 1",
            args,
        ).fetchone()
        is not None
        or db.execute(
            """SELECT 1 FROM cycles WHERE batch_label IS NULL
        AND (:since IS NULL OR completed_at>=:since)
        AND (:until IS NULL OR completed_at<:until) LIMIT 1""",
            args,
        ).fetchone()
        is not None
    )
    if alarms and not unbatched:
        unbatched = (
            db.execute(
                """SELECT 1 FROM alarm_events WHERE batch_id IS NULL
            AND observed>=:cutoff AND (:since IS NULL OR observed>=:since)
            AND (:until IS NULL OR observed<:until) LIMIT 1""",
                args | {"cutoff": now - 30 * 86400},
            ).fetchone()
            is not None
        )
    return BatchChoices(
        tuple(BatchChoice(*r) for r in rows[:50]),
        len(rows) > 50,
        selected and (not scope.unbatched or unbatched),
        unbatched,
    )


def associated_audit(
    db: sqlite3.Connection, scope: RecordsScope, records: tuple[AuditRecord, ...]
) -> tuple[AuditRecord, ...]:
    """Resolve at most one audit page on the History owner. No temporal joins."""
    if len(records) > 51:
        raise ValueError("Audit association accepts one bounded page")
    selected = db.execute(
        "SELECT label FROM batches WHERE uuid=?", (scope.batch,)
    ).fetchone()
    result: list[AuditRecord] = []
    for record in records:
        op = record.operation
        matches = False
        if (
            op.action == "production.stop"
            or op.action == "production.start"
            and record.outcome == "success"
        ):
            # Start failures target a Recipe, not a Run. Stop may target a pending
            # Start; only an actual persisted Run or Cycle proves its association.
            run = db.execute(
                "SELECT batch FROM runs WHERE uuid=?", (op.target,)
            ).fetchone()
            if run is not None:
                matches = (
                    run[0] == scope.batch
                    if scope.batch
                    else scope.unbatched and run[0] is None
                )
            elif scope.unbatched:
                matches = (
                    db.execute(
                        "SELECT 1 FROM cycles WHERE run_id=? AND batch_label IS NULL LIMIT 1",
                        (op.target,),
                    ).fetchone()
                    is not None
                )
        elif op.action in ("batch.select", "batch.end") and record.outcome == "success":
            matches = scope.batch is not None and op.target == scope.batch
        elif (
            op.action == "batch.information"
            and selected
            and record.outcome not in ("denied", "stale")
            and len(op.target) < 512
        ):
            # The worker records the immutable, unique business label. Admission
            # denials target an ID; neither is guessed from an opaque string.
            # A full-capacity target may be a truncated longer label, so cannot
            # prove this Batch's identity even when its prefix matches exactly.
            matches = op.target == selected[0]
        elif op.action == "production.clear_statistics" and record.outcome == "success":
            matches = scope.unbatched and op.target == "unbatched"
        if matches:
            result.append(record)
    return tuple(result)


def scoped_audit_page(
    history: ProductionHistory,
    identity: LocalIdentity,
    query: AuditQuery,
    scope: RecordsScope,
    revision: int,
    cancel: Event,
) -> Future[tuple[AuditRecord, ...]]:
    """Alternate bounded reads on the existing owners; never block either worker.

    In particular History can await an audit intent during a metadata edit, so
    an audit-worker wait for History would deadlock. Only completed callbacks
    advance this read-only join. At most 51 matches and one input page are held.
    """
    done: Future[tuple[AuditRecord, ...]] = Future()
    done.set_running_or_notify_cancel()
    matches: list[AuditRecord] = []

    def check() -> None:
        if cancel.is_set():
            raise CancelledError()
        with identity.admission(Role.ENGINEER, revision):
            pass

    def read(current: AuditQuery) -> None:
        try:
            check()
            identity.audit_page(current, revision).add_done_callback(received)
        except Exception as exc:
            done.set_exception(exc)

    def received(future: Future[tuple[AuditRecord, ...]]) -> None:
        try:
            check()
            records = future.result()  # Callback: the future is terminal.
            if not records:
                done.set_result(tuple(matches))
                return

            def resolved(f: Future[tuple[AuditRecord, ...]]) -> None:
                try:
                    check()
                    matches.extend(f.result()[: 51 - len(matches)])
                    if len(matches) == 51 or len(records) < 51:
                        done.set_result(tuple(matches))
                    else:
                        read(
                            replace(
                                query, before=records[-1].cursor, associated_only=True
                            )
                        )
                except Exception as exc:
                    done.set_exception(exc)

            history.associated_audit(scope, records).add_done_callback(resolved)
        except Exception as exc:
            done.set_exception(exc)

    read(replace(query, associated_only=True))
    return done
