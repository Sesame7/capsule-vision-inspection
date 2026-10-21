"""Persistent scalar evidence, bounded handoff, identity and actual owner cleanup."""

import json
import sqlite3
import zlib
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from time import time
from uuid import uuid4

import pytest
from tests.application_support import (
    WAIT,
    FakeSession,
    ReplayFuture,
    close,
    prepared,
    production,
)
from tests.business_support import mixed_result

from vimo_demo.application import (
    ApplicationState,
    ConfigurationIdentity,
    ProductionApplication,
)
from vimo_demo.application.history import (
    HistoryContext,
    HistoryQuery,
    HistorySubmission,
    ProductionHistory,
    ResultFilter,
)
from vimo_demo.inspection import CycleId, CycleStatus
from vimo_demo.inspection.production import (
    FindingSource,
    OccupancyResult,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)


def context(*, stamp: float | None = None, run: str | None = None) -> HistoryContext:
    return HistoryContext(
        time() if stamp is None else stamp,
        uuid4().hex,
        "same label",
        run or uuid4().hex,
        ConfigurationIdentity("active", "model", "a" * 64),
    )


def finish(owner: ProductionHistory) -> None:
    owner.request_stop()
    assert owner.join(WAIT)
    assert not owner.snapshot().thread_alive


@pytest.mark.parametrize("status", [CycleStatus.COMPLETE, CycleStatus.TIMED_OUT])
@pytest.mark.parametrize("camera_count", [5, 6])
def test_persisted_evidence_is_exact_compact_and_survives_restart(
    tmp_path: Path,
    status: CycleStatus,
    camera_count: int,
) -> None:
    path = tmp_path / "history.sqlite3"
    owner = ProductionHistory(path)
    original = mixed_result()
    original = replace(
        original,
        layout=replace(original.layout, cameras=original.layout.cameras[:camera_count]),
        cameras=original.cameras[:camera_count],
    )
    camera = original.cameras[0]
    forced = replace(
        camera.channels[0],
        forced_reject=Reason(FindingSource.COLLECTION, "missing_frame"),
    )
    result = replace(
        original,
        cycle_status=status,
        cameras=(
            replace(camera, channels=(forced, *camera.channels[1:])),
            *original.cameras[1:],
        ),
    )
    stamp = context()
    try:
        assert owner.offer(stamp, result)
        records = owner.query().result(WAIT).records
        assert owner.snapshot().persisted == 1
        assert len(records) == 1
        record = records[0]
        assert record.context == stamp and record.cycle_id == result.cycle_id
        assert record.status == status.name
        assert (record.technical, record.judgment) == (
            result.assessment.technical_status.name,
            result.assessment.judgment.name,
        )
        assert (
            len(record.channels) == camera_count * 6
            and len(record.cameras) == camera_count
        )
        assert {c.business for c in record.channels} == {"OK", "NG", "EMPTY", "UNKNOWN"}
        assert any(
            c.technical == "ERROR" and c.judgment == "UNKNOWN" for c in record.channels
        )
        assert any(
            c.technical == "ERROR" and c.judgment == "NG" for c in record.channels
        )
        for stored, source in zip(
            record.channels,
            (c for camera in result.cameras for c in camera.channels),
            strict=True,
        ):
            assert (stored.camera, stored.slot, stored.channel) == (
                source.camera_id,
                source.slot,
                source.channel_id,
            )
            assert (stored.business, stored.technical, stored.judgment) == (
                source.business_status.name,
                source.assessment.technical_status.name,
                source.assessment.judgment.name,
            )
            assert stored.reasons == tuple(
                dict.fromkeys(r.code for r in source.reasons)
            )
        assert "missing_frame" in record.channels[0].reasons
        assert "execution_error" in record.channels[3].reasons
    finally:
        finish(owner)
    reopened = ProductionHistory(path)
    try:
        assert reopened.query().result(WAIT).records == records
        assert (
            reopened.snapshot().persisted == 0
        )  # Current owner commits, not lifetime count.
    finally:
        finish(reopened)


def test_application_batch_run_and_exactly_once_identities(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = prepared(tmp_path)
    sessions: list[FakeSession] = []

    def factory(_config: object, _generation: int) -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    def completed(app: ProductionApplication) -> None:
        cycle = app.begin_cycle(CycleId(1))
        source = sessions[-1].cycles[-1]
        source.accepted.set_result(None)
        source.result.set_result(mixed_result())
        assert cycle.result.result(WAIT).cycle_id == 1
        # Existing repeated callback regression: history must share accounting's gate.
        assert isinstance(source.result, ReplayFuture)
        source.result.replay()

    history = ProductionHistory(path)
    app = ProductionApplication(factory, history=history)
    try:
        app.configure(config)
        app.start_batch("same")
        assert not history.query().result(WAIT).records
        for _ in range(2):
            app.start().result(WAIT)
            assert (
                len(history.query().result(WAIT).records) == len(sessions) - 1
            )  # No warm-up records.
            completed(app)
            app.stop().result(WAIT)
        assert app.snapshot().counters.cycles_completed == 2
        records = history.query().result(WAIT).records
        assert records[0].context.batch_segment == records[1].context.batch_segment
        assert records[0].context.run_id != records[1].context.run_id
        app.start_batch("same")
        assert app.snapshot().counters.cycles_completed == 0
        pending = replace(
            config, configuration=replace(config.configuration, recipe_id="next")
        )
        app.configure(pending)
        assert len(history.query().result(WAIT).records) == 2
        app.start().result(WAIT)
        completed(app)
        app.stop().result(WAIT)
        records = history.query().result(WAIT).records
        assert (
            len(records)
            == history.snapshot().accepted
            == history.snapshot().persisted
            == 3
        )
        assert records[0].context.batch_segment != records[1].context.batch_segment
        assert records[0].context.active.recipe_id == "next"
        assert records[1].context.active == ConfigurationIdentity.from_prepared(config)
    finally:
        close(app)
    assert not history.snapshot().thread_alive
    history = ProductionHistory(path)
    restarted = ProductionApplication(factory, history=history)
    try:
        assert restarted.snapshot().batch_id is None
        assert restarted.snapshot().counters.cycles_completed == 0
        restarted.configure(config)
        restarted.start().result(WAIT)
        completed(restarted)
        rows = history.query().result(WAIT).records
        assert (
            len({r.record_id for r in rows})
            == len({r.context.run_id for r in rows})
            == 4
        )
        assert rows[0].context.batch_label is None
        assert rows[0].context.batch_segment not in {
            r.context.batch_segment for r in rows[1:]
        }
    finally:
        close(restarted)


def test_active_recipe_name_is_frozen_in_history_across_rename_and_failed_start(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    original = prepared(tmp_path)
    first = replace(
        original,
        configuration=replace(original.configuration, display_name="阿莫西林胶囊"),
    )
    renamed = replace(
        first,
        configuration=replace(
            first.configuration, display_name="黄色胶囊-规格B-灯检方案"
        ),
    )
    unapplied = replace(
        renamed, configuration=replace(renamed.configuration, display_name="未激活名称")
    )
    sessions = [FakeSession(), FakeSession(), FakeSession(automatic_start=False)]
    sessions[2].ready.set_exception(ValueError("controlled warm-up failure"))
    created: list[FakeSession] = []

    def factory(_config: object, _generation: int) -> FakeSession:
        session = sessions[len(created)]
        created.append(session)
        return session

    history = ProductionHistory(path)
    app = ProductionApplication(factory, history=history)
    try:
        for config in (first, renamed):
            app.configure(config)
            before = app.snapshot()
            assert before.candidate_recipe_name == config.configuration.display_name
            if config is renamed:
                assert before.active_recipe_name == first.configuration.display_name
                assert before.active == before.candidate  # Same ID, different names.
            app.start().result(WAIT)
            assert (
                app.snapshot().active_recipe_name == config.configuration.display_name
            )
            cycle = app.begin_cycle(CycleId(1))
            created[-1].cycles[-1].accepted.set_result(None)
            created[-1].cycles[-1].result.set_result(mixed_result())
            cycle.result.result(WAIT)
            app.stop().result(WAIT)
        records = history.query().result(WAIT).records
        assert [r.context.recipe_name for r in records] == [
            renamed.configuration.display_name,
            first.configuration.display_name,
        ]
        assert records[0].context.active == records[1].context.active
        app.configure(unapplied)
        assert history.query().result(WAIT).records == records
        with pytest.raises(Exception, match="activate"):
            app.start().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert app.snapshot().active_recipe_name == renamed.configuration.display_name
        assert (
            app.snapshot().candidate_recipe_name == unapplied.configuration.display_name
        )
        assert app.snapshot().counters.cycles_completed == 2
        assert history.query().result(WAIT).records == records
    finally:
        close(app)
    reopened = ProductionHistory(path)
    try:
        assert reopened.query().result(WAIT).records == records
    finally:
        finish(reopened)


def test_earlier_h1_payload_without_recipe_name_remains_readable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    stamp = replace(context(), recipe_name="Original name")
    owner = ProductionHistory(path)
    try:
        assert owner.offer(stamp, mixed_result())
        assert (
            owner.query().result(WAIT).records[0].context.recipe_name == "Original name"
        )
    finally:
        finish(owner)
    with sqlite3.connect(path) as connection:
        blob = connection.execute("SELECT detail FROM cycles").fetchone()[0]
        legacy = zlib.compress(
            json.dumps(json.loads(zlib.decompress(blob))[:2]).encode()
        )
        connection.execute("UPDATE cycles SET detail = ?", (legacy,))
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    owner = ProductionHistory(path)
    try:
        record = owner.query().result(WAIT).records[0]
        assert record.context.recipe_name is None
        assert record.context.active == stamp.active and record.cycle_id == 1
        assert len(record.channels) == 36 and len(record.cameras) == 6
        assert owner.offer(replace(stamp, recipe_name="Renamed later"), mixed_result(2))
        rows = owner.query().result(WAIT).records
        assert [r.context.recipe_name for r in rows] == ["Renamed later", None]
    finally:
        finish(owner)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT detail FROM cycles WHERE cycle_id='1'"
            ).fetchone()[0]
            == legacy
        )


def test_bounded_slow_persistence_never_blocks_accounting_or_terminal_delivery(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    owner_thread: list[int] = []

    class SlowHistory(ProductionHistory):
        def _write(
            self, connection: sqlite3.Connection, item: HistorySubmission
        ) -> None:
            owner_thread.append(get_ident())
            entered.set()
            assert release.wait(WAIT)
            super()._write(connection, item)

    history = SlowHistory(tmp_path / "history.sqlite3", max_pending=1)
    session = FakeSession()
    app = ProductionApplication(lambda _c, _g: session, history=history)
    app.configure(prepared(tmp_path))
    try:
        app.start().result(WAIT)
        for i in range(1, 4):
            cycle = app.begin_cycle(CycleId(i))
            source = session.cycles[-1]
            source.accepted.set_result(None)
            source.result.set_result(mixed_result(i))
            cycle.result.result(WAIT)
            if i == 1:
                assert entered.wait(WAIT)
        assert app.snapshot().state is ApplicationState.RUNNING
        assert app.snapshot().counters.cycles_completed == 3
        snapshot = history.snapshot()
        assert (
            snapshot.accepted == 2 and snapshot.dropped == 1 and snapshot.persisted == 0
        )
        assert snapshot.pending <= 1
        query = history.query()
        assert query.done()
        with pytest.raises(RuntimeError, match="capacity"):
            query.result()
        closing = app.close()
        assert not closing.done() and not history.join(0)
        release.set()
        closing.result(WAIT)
        assert app.join(WAIT)
        assert history.snapshot().persisted == 2 and not history.snapshot().thread_alive
        assert set(owner_thread) and get_ident() not in owner_thread
    finally:
        release.set()
        if app.snapshot().state is not ApplicationState.CLOSED:
            close(app)


def test_write_failure_and_initial_unavailability_are_observable_without_fault(
    tmp_path: Path,
) -> None:
    class DiskFull(ProductionHistory):
        def _write(
            self, connection: sqlite3.Connection, item: HistorySubmission
        ) -> None:
            raise sqlite3.OperationalError("controlled disk full")

    history = DiskFull(tmp_path / "history.sqlite3")
    session = FakeSession()
    app = ProductionApplication(lambda _c, _g: session, history=history)
    try:
        app.configure(prepared(tmp_path))
        app.start().result(WAIT)
        receipt = app.begin_cycle(CycleId(1))
        session.cycles[0].accepted.set_result(None)
        session.cycles[0].result.set_result(mixed_result())
        receipt.result.result(WAIT)
        assert not history.query().result(WAIT).records
        assert history.snapshot().failed == 1 and history.snapshot().persisted == 0
        assert "controlled disk full" in history.snapshot().error
        assert (
            app.snapshot().state is ApplicationState.RUNNING
            and app.snapshot().counters.cycles_completed == 1
        )
    finally:
        close(app)
    bad = tmp_path / "not-a-directory"
    bad.write_bytes(b"preserve")
    broken = ProductionHistory(bad / "history.sqlite3")
    try:
        with pytest.raises(Exception, match="not-a-directory"):
            broken.query().result(WAIT)
        assert broken.join(WAIT)
        session = FakeSession()
        running = ProductionApplication(lambda _c, _g: session, history=broken)
        try:
            running.configure(prepared(tmp_path))
            running.start().result(WAIT)
            receipt = running.begin_cycle(CycleId(1))
            session.cycles[0].accepted.set_result(None)
            session.cycles[0].result.set_result(mixed_result())
            receipt.result.result(WAIT)
            assert running.snapshot().state is ApplicationState.RUNNING
            assert running.snapshot().counters.cycles_completed == 1
            assert broken.snapshot().dropped == 1 and broken.snapshot().error
            assert bad.read_bytes() == b"preserve"
        finally:
            close(running)
    finally:
        finish(broken)


@pytest.mark.parametrize("disabled", [False, True])
def test_disabled_states_and_occupancy_error_codes_survive(
    tmp_path: Path, disabled: bool
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    result = (
        production(status=TechnicalStatus.DISABLED, judgment=ProductJudgment.UNKNOWN)
        if disabled
        else production()
    )
    if not disabled:
        camera = result.cameras[0]
        failed = replace(
            camera.channels[0],
            occupancy=OccupancyResult(
                error_code="occupancy_failure", exception_type="ValueError"
            ),
        )
        result = replace(
            result,
            cameras=(
                replace(camera, channels=(failed, *camera.channels[1:])),
                *result.cameras[1:],
            ),
        )
    try:
        assert history.offer(context(), result)
        record = history.query().result(WAIT).records[0]
        if disabled:
            assert record.technical == "DISABLED" and record.judgment == "UNKNOWN"
            assert all(
                c.technical == "DISABLED" and c.judgment == "UNKNOWN" and not c.reasons
                for c in record.channels
            )
        else:
            assert (
                record.channels[0].technical == "ERROR"
                and record.channels[0].judgment == "UNKNOWN"
            )
            assert "occupancy_failure" in record.channels[0].reasons
    finally:
        finish(history)


def test_duplicate_identity_large_cycle_and_failed_start_are_not_fabricated(
    tmp_path: Path,
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    blocked_start = FakeSession(automatic_start=False)
    blocked_start.ready.set_exception(ValueError("startup failed"))
    app = ProductionApplication(lambda _c, _g: blocked_start, history=history)
    try:
        app.configure(prepared(tmp_path))
        with pytest.raises(Exception, match="activate"):
            app.start().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        assert not history.query().result(WAIT).records
        stamp = context()
        assert history.offer(stamp, mixed_result())
        assert history.offer(stamp, mixed_result())
        large = 2**80 + 1
        assert history.offer(stamp, mixed_result(large))
        records = history.query().result(WAIT).records
        assert {r.cycle_id for r in records} == {1, large}
        assert len(records) == 2 and history.snapshot().persisted == 2
        assert history.snapshot().failed == 1 and "UNIQUE" in history.snapshot().error
        assert app.snapshot().counters.cycles_completed == 0
    finally:
        close(app)


def test_fatal_history_owner_exits_and_accounts_for_accepted_work(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()

    class FailedOwner(ProductionHistory):
        def _write(
            self, connection: sqlite3.Connection, item: HistorySubmission
        ) -> None:
            entered.set()
            assert release.wait(WAIT)
            raise SystemExit("controlled history owner exit")

    history = FailedOwner(tmp_path / "history.sqlite3")
    try:
        assert history.offer(context(), mixed_result())
        assert entered.wait(WAIT)
        assert history.offer(context(), mixed_result(2))
        query = history.query()
        assert not query.cancel()
        release.set()
        with pytest.raises(RuntimeError, match="SystemExit"):
            query.result(WAIT)
        assert history.join(WAIT)
        observed = history.snapshot()
        assert observed.accepted == observed.failed == 2 and observed.persisted == 0
        assert not observed.thread_alive and not observed.accepting
    finally:
        release.set()
        finish(history)


def test_bounded_filters_keyset_pages_and_incremental_wall_clock_retention(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    owner = ProductionHistory(
        tmp_path / "history.sqlite3", clock=lambda: now[0], retention_batch=2
    )
    shared = context(stamp=1000)
    try:
        for i in range(1, 8):
            assert owner.offer(shared, mixed_result(i))
        first = owner.query(HistoryQuery(limit=3)).result(WAIT)
        assert [r.cycle_id for r in first.records] == [7, 6, 5] and first.more
        cursor = (first.records[-1].context.completed_at, first.records[-1].record_id)
        second = owner.query(HistoryQuery(limit=3, cursor=cursor)).result(WAIT)
        assert [r.cycle_id for r in second.records] == [4, 3, 2] and second.more
        cursor = (second.records[0].context.completed_at, second.records[0].record_id)
        newer = owner.query(HistoryQuery(limit=3, cursor=cursor, newer=True)).result(
            WAIT
        )
        assert newer.records == first.records and not newer.more
        filters: tuple[ResultFilter, ...] = ("NG", "ERROR", "EMPTY", "UNKNOWN")
        for result_filter in filters:
            rows = owner.query(
                HistoryQuery(result=result_filter, batch_segment=shared.batch_segment)
            ).result(WAIT)
            assert len(rows.records) == 7
        assert not owner.query(HistoryQuery(result="OK")).result(WAIT).records
        assert not owner.query(HistoryQuery(since=1001)).result(WAIT).records
        assert not owner.query(HistoryQuery(until=1000)).result(WAIT).records
        assert len(first.batches) == 1
        now[0] = 1000 + 30 * 86400  # Exact boundary retained.
        assert len(owner.query().result(WAIT).records) == 7
        now[0] += 1
        assert len(owner.query().result(WAIT).records) == 5
        assert len(owner.query().result(WAIT).records) == 3
        assert len(owner.query().result(WAIT).records) == 1
        assert not owner.query().result(WAIT).records
        assert owner.snapshot().persisted == 7  # Retention is not record loss.
        with pytest.raises(ValueError):
            HistoryQuery(limit=51)
    finally:
        finish(owner)


def test_segment_time_is_earliest_retained_observation_not_date_scope_or_batch_start(
    tmp_path: Path,
) -> None:
    owner = ProductionHistory(tmp_path / "history.sqlite3", clock=lambda: 2000)
    first = context(stamp=1000)
    second = replace(first, batch_segment="distinct-segment", run_id="other-run")
    try:
        for cycle, stamp in ((1, 1000), (2, 1100), (3, 1300)):
            assert owner.offer(replace(first, completed_at=stamp), mixed_result(cycle))
        assert owner.offer(replace(second, completed_at=1200), mixed_result(1))
        page = owner.query(HistoryQuery(since=1050)).result(WAIT)
        assert page.batches == (
            (first.batch_segment, first.batch_label, 1000),
            (second.batch_segment, second.batch_label, 1200),
        )
        assert {r.context.completed_at for r in page.records} == {1100, 1200, 1300}
        selected = owner.query(HistoryQuery(batch_segment=second.batch_segment)).result(
            WAIT
        )
        assert (
            len(selected.records) == 1
            and selected.records[0].context.run_id == "other-run"
        )
    finally:
        finish(owner)
