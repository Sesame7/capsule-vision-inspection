import argparse
import os
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from threading import enumerate as threads
from time import monotonic
from typing import BinaryIO

import pytest
from tests.support.application import FakeSession, prepared, production
from tests.support.diagnostics import observations
from tests.support.startup import close, saved_data

from capsule_vision.application import (
    ApplicationCapacityError,
    ApplicationError,
    ProductionApplication,
)
from capsule_vision.application.archive_links import PublishedImage
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.diagnostics import (
    DiagnosticLevel,
    RunDiagnostics,
    RuntimeDiagnostics,
)
from capsule_vision.inspection import CycleId, CycleStatus
from capsule_vision.ui import bootstrap


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline
        Event().wait(0.002)


@pytest.mark.parametrize("broken_storage", [False, True])
def test_actual_timeout_overload_and_fault_ignore_writer_health(
    tmp_path: Path, broken_storage: bool
) -> None:
    logs = tmp_path / "logs"
    if broken_storage:
        logs.write_bytes(b"preserve")
    session = FakeSession()
    gate = session.join_gate = Event()
    with closing(RuntimeDiagnostics(logs, "standard")) as writer:
        app = ProductionApplication(
            lambda _p, _g: session, diagnostics=writer, max_pending_cycles=1
        )
        try:
            app.configure(prepared(tmp_path))
            app.start().result(3)
            run = app.snapshot().run_id
            cycle = app.begin_cycle(CycleId(12))
            session.cycles[0].accepted.set_result(None)
            with pytest.raises(ApplicationCapacityError):
                app.begin_cycle(CycleId(13))
            result = replace(production(12), cycle_status=CycleStatus.TIMED_OUT)
            session.cycles[0].result.set_result(result)
            assert (
                cycle.result.result(3) is result
                and app.snapshot().counters.cycles_completed == 1
            )
            session.failure = RuntimeError("fixture-private-value")
            session.finished.set_result(None)
            assert session.join_entered.wait(3)
            stopped = app.stop()
            gate.set()
            with pytest.raises(ApplicationError):
                stopped.result(3)
        finally:
            gate.set()
            app.close().result(3)
            assert app.join(3)
    if broken_storage:
        assert writer.snapshot().error and logs.read_bytes() == b"preserve"
        return
    (terminal,) = observations(logs, "cycle.terminal")
    assert (terminal["status"], terminal["cycle_id"], terminal["run_id"]) == (
        "TIMED_OUT",
        12,
        run,
    )
    (rejected,) = observations(logs, "cycle.rejected")
    assert (rejected["error_type"], rejected["cycle_id"]) == (
        "ApplicationCapacityError",
        13,
    )
    (fault,) = observations(logs, "production.fault")
    assert (fault["stage"], fault["error_type"], fault["run_id"]) == (
        "runtime",
        "RuntimeError",
        run,
    )
    assert all(
        b"fixture-private-value" not in p.read_bytes() for p in logs.glob("*.jsonl")
    )


@pytest.mark.parametrize("level", [None, "standard", "detailed"])
def test_normal_entry_owns_optional_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: DiagnosticLevel | None
) -> None:
    entry = tmp_path / "entry"
    arguments = ["--window-size", "1080x1920"]
    if level:
        arguments.extend(["--diagnostics", level])

    def run(
        data: ApplicationData,
        args: argparse.Namespace,
        _parser: argparse.ArgumentParser,
        writer: RuntimeDiagnostics | None,
    ) -> int:
        assert data.root == entry / "data" and args.window_size == (1080, 1920)
        assert bool(writer) == bool(level)
        assert sum(t.name == "RuntimeDiagnostics" for t in threads()) == int(
            level is not None
        )
        if writer:
            assert writer.level == level and writer.detailed == (level == "detailed")
            writer.record("owner.settled")
        return 3

    monkeypatch.setattr(bootstrap, "_run", run)
    assert bootstrap.main(entry, arguments) == 3
    directory = entry / "data/logs"
    assert directory.exists() == bool(level)
    assert not any(t.name == "RuntimeDiagnostics" for t in threads())
    if level:
        rows = observations(directory)
        assert [r["event"] for r in rows] == [
            "application.startup",
            "owner.settled",
            "application.exit",
        ]
        assert rows[0]["level"] == level and rows[-1]["exit_code"] == 3


@pytest.mark.parametrize("arguments", [["--diagnostics"], ["--diagnostics", "debug"]])
def test_invalid_arguments_have_no_startup_effect(
    tmp_path: Path, arguments: list[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        bootstrap.main(tmp_path, arguments)
    assert raised.value.code == 2 and not list(tmp_path.iterdir())


def test_schema_clocks_and_generation_binding(tmp_path: Path) -> None:
    with closing(RuntimeDiagnostics(tmp_path, "detailed")) as writer:
        trace = RunDiagnostics(writer, 9, "胶囊")
        trace.record("inspection.warmup", outcome="completed")
        trace.run_id = "run-9"
        trace.record(
            "fixture",
            cycle_id=17,
            camera_id="CAM2",
            duration_ms=1.5,
            reject_words=(1, 2),
        )
    first, second = observations(tmp_path)
    assert first["run_id"] is None and second["run_id"] == "run-9"
    assert (
        second["recipe_id"] == "胶囊"
        and second["camera_id"] == "CAM2"
        and second["cycle_id"] == 17
    )
    assert (
        second["generation"] == 9
        and second["duration_ms"] == 1.5
        and second["reject_words"] == [1, 2]
    )
    assert second["schema"] == "capsule.runtime.v1"
    assert first["session_id"] == second["session_id"] == writer.session_id
    assert first["pid"] == os.getpid()
    for row in (first, second):
        assert (
            isinstance(row["utc"], str)
            and datetime.fromisoformat(row["utc"]).utcoffset() == timedelta()
        )
    a, b = first["monotonic_ns"], second["monotonic_ns"]
    assert isinstance(a, int) and isinstance(b, int) and 0 < a <= b
    assert writer.snapshot().written == 2 and writer.snapshot().dropped == 0


def test_retention_across_launches_preserves_unowned_files(tmp_path: Path) -> None:
    foreign = [
        tmp_path / "history.jsonl",
        tmp_path / ("capsule-runtime-v1-" + "0" * 32 + "-00000000.jsonl"),
    ]
    for path in foreign:
        path.write_bytes(b"customer evidence")
    previous = ""
    for launch in range(2):
        with closing(
            RuntimeDiagnostics(tmp_path, "standard", segment_bytes=600, segments=3)
        ) as writer:
            for i in range(25):
                writer.record("rotate", index=i)
        assert not writer.snapshot().error and writer.snapshot().written == 25
        segments = sorted(p for p in tmp_path.glob("*.jsonl") if p not in foreign)
        assert len(segments) == 3 and sum(p.stat().st_size for p in segments) <= 1800
        assert all(
            p.stat().st_size <= 600 and writer.session_id in p.name for p in segments
        )
        assert all(p.read_bytes() == b"customer evidence" for p in foreign)
        if launch:
            assert previous != writer.session_id
        previous = writer.session_id


def test_blocked_writer_bounded_loss_and_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()

    def blocked(writer: RuntimeDiagnostics, index: int) -> BinaryIO:
        entered.set()
        assert release.wait(5)
        return (
            writer.directory
            / f"capsule-runtime-v1-{writer.session_id}-{index:08d}.jsonl"
        ).open("xb")

    monkeypatch.setattr(RuntimeDiagnostics, "_segment", blocked)
    writer = RuntimeDiagnostics(tmp_path, "detailed", capacity=2)
    done = Thread(target=writer.close)
    try:
        writer.record("first")
        assert entered.wait(3)
        for i in range(10):
            writer.record("queued", cycle_id=i)
        assert writer.snapshot().dropped == 8
        done.start()
        done.join(0.03)
        assert done.is_alive()
    finally:
        release.set()
        writer.close()
        if done.ident is not None:
            done.join(3)
    assert [r["cycle_id"] for r in observations(tmp_path, "queued")] == [0, 1]
    assert [r["dropped"] for r in observations(tmp_path, "diagnostics.loss")] == [8]
    assert writer.snapshot().written == 3


def test_writer_failure_and_competing_launch_are_isolated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "unwritable"
    file.write_bytes(b"preserve")
    with closing(RuntimeDiagnostics(file, "standard")) as broken:
        broken.record("lost")
    assert broken.snapshot().error and broken.snapshot().dropped == 1
    assert (
        file.read_bytes() == b"preserve"
        and "Runtime diagnostics unavailable" in capsys.readouterr().err
    )
    # Establish ownership deterministically before launching the contender.
    with closing(RuntimeDiagnostics(tmp_path, "standard")) as owner:
        owner.record("first")
        wait_for(lambda: owner.snapshot().written == 1)
        with closing(RuntimeDiagnostics(tmp_path, "standard")) as contender:
            contender.record("contender")
        assert contender.snapshot().error and contender.snapshot().written == 0
        owner.record("last")
    assert [r["event"] for r in observations(tmp_path)] == ["first", "last"]


def test_service_storage_failures_reach_standard_without_audit_payloads(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    private = b"fixture-private-value"
    data.audit.write_bytes(private)
    data.history.write_bytes(private)
    logs = data.root / "logs"
    with closing(RuntimeDiagnostics(logs, "standard")) as writer:
        startup = load_startup(data, diagnostics=writer)
        try:
            history = startup.application.history
            assert history is not None
            with pytest.raises(RuntimeError):
                history.initial_selection.result(3)
        finally:
            close(startup)
    assert any(
        r["stage"] == "sqlite" and r["error_type"] == "DatabaseError"
        for r in observations(logs, "history.storage_issue")
    )
    assert any(
        r["stage"] == "open" and r["error_type"] == "DatabaseError"
        for r in observations(logs, "audit.storage_issue")
    )
    assert all(private not in p.read_bytes() for p in logs.glob("*.jsonl"))
    assert data.audit.read_bytes() == data.history.read_bytes() == private


def test_failed_history_link_keeps_its_own_identity(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    with closing(RuntimeDiagnostics(logs, "standard")) as writer:
        history = ProductionHistory(tmp_path / "history.sqlite3", diagnostics=writer)
        try:
            history.initial_selection.result(3)
            history.offer_image(
                PublishedImage("absent-run", 51, "CAM3", tmp_path / "private.png", 1)
            )
        finally:
            history.request_stop()
            assert history.join(3)
        assert history.snapshot().image_links_lost == 1
    (event,) = observations(logs, "history.storage_issue")
    assert (
        event["stage"],
        event["error_type"],
        event["run_id"],
        event["cycle_id"],
        event["camera_id"],
    ) == ("image_links", "IntegrityError", "absent-run", 51, "CAM3")
    assert "private.png" not in str(event)
