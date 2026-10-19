"""Local tooling only: synthetic arrays and loopback UDP/TCP, never field assets."""

import json
import multiprocessing as mp
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import cast

import numpy as np
import pytest
from field_benchmark.analyze import Row, load_rows, reconcile
from field_benchmark.configuration import Mode, digest, prepare_run
from field_benchmark.ledger import Ledger
from field_benchmark.observations import Observations
from field_benchmark.schedule import BurstPlan, Slot, drive
from field_benchmark.trigger import (
    Channel,
    Controller,
    EndpointLock,
    PhotoClient,
    Readiness,
    SupervisorSettings,
    join_supervisor,
    start_supervisor,
)
from tests.application_support import prepared, production
from tests.benchmark_support import BitPeer

from vimo_demo.application import ProductionCycle
from vimo_demo.communication.fins import FinsProtocolError
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ImageArchiveSettings,
    MachineLayoutDefinition,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    save_camera_sources,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from vimo_demo.inspection import CycleId
from vimo_demo.inspection.production import ProductionInspectionResult


def settings(port: int, *, lease: float = 2) -> SupervisorSettings:
    now = monotonic()
    plan = BurstPlan(now + 0.8, 1, 1, now + 1.3, max_lateness=0.05)
    gate = Readiness(
        "local-test", 1, "epoch-1", "a" * 64, now + 8, True, True, True, True
    )
    return SupervisorSettings(
        "local-test", "127.0.0.1", port, plan, gate, 0.020, lease, 0.15
    )


def wait_slot(config: SupervisorSettings) -> Slot:
    slot = next(config.plan.slots())
    Event().wait(max(0, slot.planned_at - monotonic()))
    return slot


def test_absolute_bursts_budgets_late_and_cancel() -> None:
    plan = BurstPlan(10, 2, 8, 11)
    slots = list(plan.slots())
    assert [s.planned_at for s in slots] == pytest.approx(
        [10, 10.06, 10.12, 10.18, 10.5, 10.56, 10.62, 10.68]
    )
    assert [(s.group, s.offset) for s in slots] == [
        (g, n) for g in range(2) for n in range(4)
    ]
    now = [10.0]
    sent: list[Slot] = []
    seen: list[str] = []

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return False

    def send(slot: Slot) -> bool:
        sent.append(slot)
        now[0] += 0.020
        return True

    assert (
        drive(
            plan,
            send,
            lambda kind, _: seen.append(kind),
            Event(),
            clock=lambda: now[0],
            wait=wait,
        )
        == "budget_complete"
    )
    assert sent == slots
    now[0] = 10.07
    sent.clear()
    assert (
        drive(
            plan,
            send,
            lambda kind, _: seen.append(kind),
            Event(),
            clock=lambda: now[0],
            wait=wait,
        )
        == "late"
    )
    assert not sent and seen[-1] == "trigger_late"
    stop = Event()
    stop.set()
    assert drive(plan, send, lambda _kind, _slot: None, stop) == "cancelled"
    assert len(list(replace(plan, pulse_budget=3).slots())) == 3
    assert len(list(replace(plan, end=10.1).slots())) == 2
    for changes in (
        {"groups": 0},
        {"pulse_budget": 9},
        {"end": float("inf")},
        {"max_lateness": 0.06},
    ):
        with pytest.raises(ValueError):
            replace(plan, **changes)  # pyright: ignore[reportArgumentType] -- explicit invalid constructor table.


def test_bit_vectors_and_wrong_payload_are_rejected() -> None:
    with BitPeer() as peer:
        with PhotoClient("127.0.0.1", port=peer.port, timeout=0.2) as client:
            assert client.read_photo() == 0
            client.write_photo(1)
            assert client.read_photo() == 1
            client.write_photo(0)
            assert client.read_photo() == 0
            for invalid in (-1, 2, True):
                with pytest.raises(ValueError):
                    client.write_photo(invalid)
            peer.bad_readback = True
            with pytest.raises(FinsProtocolError):
                client.read_photo()
        assert peer.writes == [1, 0]
        assert peer.w10_other_bits == 0x5521
        assert peer.packets[1][10:] == bytes.fromhex("01 02 31 00 0a 07 00 01 01")
        assert peer.packets[3][10:] == bytes.fromhex("01 02 31 00 0a 07 00 01 00")


@pytest.mark.parametrize(
    "fault", ("normal", "uncertain_ack", "release_failure", "delayed_release")
)
def test_independent_supervisor_pulse_and_release(tmp_path: Path, fault: str) -> None:
    with BitPeer() as peer:
        peer.drop_activation_ack = fault == "uncertain_ack"
        peer.reject_release = fault == "release_failure"
        if fault == "delayed_release":
            peer.allow_release_ack.clear()
        config = settings(peer.port)
        process, controller = start_supervisor(config, tmp_path, tmp_path / "locks")
        try:
            with pytest.raises(RuntimeError):
                controller.pulse(
                    next(config.plan.slots())
                )  # No supervisor-ready ACK yet.
            controller.acknowledge_ready()
            slot = wait_slot(config)
            if fault == "delayed_release":
                done = Event()
                outcome: list[bool] = []

                def invoke() -> None:
                    outcome.append(controller.pulse(slot))
                    done.set()

                call = Thread(target=invoke)
                call.start()
                assert peer.release_seen.wait(2)
                assert (
                    not done.is_set()
                )  # ACK/readback, not requested zero, settles it.
                peer.allow_release_ack.set()
                call.join(2)
                assert not call.is_alive() and outcome == [True]
            else:
                assert controller.pulse(slot) is (fault == "normal")
            if fault != "normal":
                if controller.failed:
                    with pytest.raises(RuntimeError):
                        controller.pulse(slot)
            else:
                controller.stop()
        finally:
            peer.allow_release_ack.set()
            controller.connection.close()
            process.join(5)
            assert not process.is_alive() and process.exitcode == 0
        final = json.loads((tmp_path / "supervisor-final.json").read_text())
        assert final["release_verified"] is (fault != "release_failure")
        assert peer.writes.count(1) == 1
        assert peer.bit == (1 if fault == "release_failure" else 0)
        assert peer.w10_other_bits == 0x5521


def controller_child(bootstrap: Channel) -> None:
    # Finish Windows spawn/import before the parent freezes a finite pulse plan.
    bootstrap.send("loaded")
    config, channel = cast(tuple[SupervisorSettings, Channel], bootstrap.recv())
    bootstrap.close()
    controller = Controller(config, channel)
    controller.acknowledge_ready()
    controller.pulse(wait_slot(config))
    channel.close()


def test_controller_process_termination_takeover(tmp_path: Path) -> None:
    with BitPeer() as peer:
        peer.allow_activation_ack.clear()
        parent_bootstrap, child_bootstrap = mp.get_context("spawn").Pipe()
        child = mp.get_context("spawn").Process(
            target=controller_child, args=(child_bootstrap,)
        )
        child.start()
        child_bootstrap.close()
        assert parent_bootstrap.poll(5) and parent_bootstrap.recv() == "loaded"
        config = settings(peer.port)
        supervisor, controller = start_supervisor(config, tmp_path, tmp_path / "locks")
        parent_bootstrap.send((config, controller.connection))
        parent_bootstrap.close()
        controller.connection.close()  # EOF is controlled solely by child lifetime.
        try:
            assert peer.high.wait(3)
            child.terminate()  # ONLY a synthetic test controller, never native work.
            child.join(3)
            assert not child.is_alive()
        finally:
            peer.allow_activation_ack.set()
            supervisor.join(5)
            if child.is_alive():
                child.terminate()
                child.join(3)
        assert not supervisor.is_alive() and supervisor.exitcode == 0
        assert peer.bit == 0 and peer.writes.count(1) == 1
        final = json.loads((tmp_path / "supervisor-final.json").read_text())
        assert final["release_verified"] is True
        rows = load_rows([tmp_path / "supervisor.jsonl"])
        assert any(row["kind"] == "lease_revoked" for row in rows)


def test_missing_heartbeat_exclusion_and_expired_gate(tmp_path: Path) -> None:
    with BitPeer() as peer:
        config = settings(peer.port, lease=0.12)
        process, controller = start_supervisor(config, tmp_path, tmp_path / "locks")
        controller.acknowledge_ready()
        with pytest.raises(OSError):
            EndpointLock(tmp_path / "locks", config.host, config.port)
        process.join(3)
        assert not process.is_alive() and process.exitcode == 0
        controller.connection.close()
        assert peer.writes == [0] and peer.bit == 0
        final = json.loads((tmp_path / "supervisor-final.json").read_text())
        assert final["reason"] == "lease_expired"
        lock = EndpointLock(tmp_path / "locks", config.host, config.port)
        lock.close()  # Actual process exit releases exclusive endpoint ownership.
    with pytest.raises(ValueError, match="loopback"):
        replace(config, host="192.168.251.1").validate()
    with pytest.raises(ValueError, match="readiness"):
        replace(config, readiness=replace(config.readiness, expires_at=0)).validate()


def test_controller_schedule_ledger_and_wrong_run_takeover(tmp_path: Path) -> None:
    with BitPeer() as peer:
        config = settings(peer.port)
        ledger = Ledger(tmp_path / "controller.jsonl", config.run, "controller")
        process, controller = start_supervisor(config, tmp_path, tmp_path / "locks")
        try:
            assert controller.run(ledger, Event()) == "budget_complete"
        finally:
            controller.connection.close()
            assert join_supervisor(process, ledger, 3)
            assert ledger.close()
        rows = load_rows([ledger.path, tmp_path / "supervisor.jsonl"])
        assert len([r for r in rows if r["kind"] == "trigger_plan"]) == 1
        assert len([r for r in rows if r["kind"] == "activation_send"]) == 1
        assert any(r["kind"] == "heartbeat" for r in rows)
        assert peer.bit == 0 and peer.writes.count(1) == 1
    # IPC identities cannot borrow a different run's ready acknowledgment/lease.
    from field_benchmark.trigger import Request

    with BitPeer() as peer:
        other = tmp_path / "wrong-run"
        other.mkdir()
        config = settings(peer.port)
        process, controller = start_supervisor(config, other, tmp_path / "locks")
        controller.acknowledge_ready()
        controller.connection.send(Request("foreign", "heartbeat"))
        process.join(3)
        controller.connection.close()
        assert process.exitcode == 0 and not process.is_alive()
        assert peer.bit == 0 and 1 not in peer.writes
        assert "supervisor_failure" in (other / "supervisor.jsonl").read_text()


def test_ledger_queue_pressure_and_unfinished_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = cast(Callable[[Ledger], None], vars(Ledger)["_write"])

    def blocked(ledger: Ledger) -> None:
        entered.set()
        assert release.wait(3)
        original(ledger)

    monkeypatch.setattr(Ledger, "_write", blocked)
    ledger = Ledger(tmp_path / "queue.jsonl", "r", "blocked", capacity=1)
    try:
        assert entered.wait(1)
        assert not ledger.emit("overflow")  # Clock already occupies the one slot.
        assert ledger.abort.is_set() and ledger.error == "ledger queue overflow"
        assert not ledger.close(0.01)
        assert not ledger.path.with_suffix(".closed.json").exists()
    finally:
        release.set()
        assert not ledger.close()  # Actual join occurred; overflow stays a failure.
    assert ledger.path.with_suffix(".closed.json").exists()


@pytest.mark.parametrize("mode", list(Mode))
def test_configuration_freezes_identity_not_saved_endpoint(
    tmp_path: Path, mode: Mode
) -> None:
    source = tmp_path / "saved"
    source.mkdir()
    original = prepared(source).configuration
    paths = [
        source / name
        for name in ("model.json", "machine.json", "recipe.json", "sources.json")
    ]
    save_model_definition(paths[0], original.model)
    save_machine_layout(paths[1], MachineLayoutDefinition(original.layout))
    fixture = Path(__file__).parents[1] / "fixtures/configuration/defaults.json"
    recipe = replace(
        load_recipe(fixture),
        image_archive=ImageArchiveSettings(
            str(tmp_path / "PROTECTED-SavedImages"), True, True, True, True
        ),
    )
    save_recipe(paths[2], recipe)
    sources = CameraSources(
        tuple(
            CameraFolder(c, "references", basler_serial=str(100 + i))
            for i, c in enumerate(original.layout.camera_ids)
        ),
        mode="basler",
    )
    save_camera_sources(paths[3], sources)
    before = [digest(p) for p in paths]
    files = prepare_run(
        tmp_path / "runs",
        mode,
        model_file=paths[0],
        machine_file=paths[1],
        recipe_file=paths[2],
        sources_file=paths[3],
        simulator_port=32123,
    )
    assert [digest(p) for p in paths] == before
    assert load_machine_layout(files.machine).layout == original.layout
    assert (
        load_model_definition(files.model).expected_sha256
        == original.model.expected_sha256
    )
    effective = load_recipe(files.recipe)
    assert replace(effective, image_archive=recipe.image_archive) == recipe
    assert effective.image_archive.enabled is (mode in (Mode.A, Mode.RA))
    assert Path(effective.image_archive.root_directory).is_relative_to(files.root)
    assert load_camera_sources(files.sources).basler_serials(
        original.layout.camera_ids
    ) == sources.basler_serials(original.layout.camera_ids)
    assert (files.reporting is not None) is (mode in (Mode.R, Mode.RA))
    if files.reporting:
        assert (
            files.reporting.host == "127.0.0.1"
            and files.reporting.transport.value == "udp"
        )
    another = prepare_run(
        tmp_path / "runs",
        mode,
        model_file=paths[0],
        machine_file=paths[1],
        recipe_file=paths[2],
        sources_file=paths[3],
        simulator_port=32123,
    )
    assert another.root != files.root


def test_scalar_ledger_bounded_and_done_callbacks(tmp_path: Path) -> None:
    from concurrent.futures import Future

    ledger = Ledger(tmp_path / "events.jsonl", "test", "backend", event_budget=50)
    obs = Observations(ledger, "source-1")
    accepted: Future[None] = Future()
    result = production()
    terminal: Future[ProductionInspectionResult] = Future()
    obs.cycle(1, CycleId(1), ProductionCycle(accepted, terminal))
    accepted.set_result(None)
    terminal.set_result(result)
    with pytest.raises(TypeError):
        ledger.emit("invalid", pixels=cast(str, np.zeros((2, 2))))
    assert ledger.close()
    rows = load_rows([ledger.path])
    assert [r["kind"] for r in rows[:4]] == [
        "clock",
        "cycle_requested",
        "cycle_admitted",
        "cycle_terminal",
    ]
    assert len([r for r in rows if r["kind"] == "camera_result"]) == 6
    small = Ledger(tmp_path / "small.jsonl", "test", "small", event_budget=1)
    assert not small.emit("overflow") and small.abort.is_set()
    assert not small.close()


def test_reconciliation_rejects_missing_duplicate_and_mismatch() -> None:
    rows: list[Row] = []

    def add(kind: str, **fields: object) -> None:
        rows.append(
            {
                "run": "r",
                "producer": "p",
                "index": len(rows),
                "at": float(len(rows)),
                "kind": kind,
                **fields,
            }
        )

    add("clock")
    add("trigger_plan", pulse=0)
    add("supervisor_ready")
    add("activation_send", pulse=0)
    add("activation_ack", pulse=0)
    add("release_send", pulse=0)
    add("release_ack", pulse=0)
    add("release_readback", pulse=0, low=True)
    add("cycle_admitted", generation=1, cycle=7)
    add(
        "cycle_terminal",
        generation=1,
        cycle=7,
        status="COMPLETE",
        channel_ids=list(range(1, 37)),
        business=["OK"] * 36,
        technical=["SUCCESS"] * 36,
        cameras=[f"CAM{i}" for i in range(1, 7)],
    )
    for i in range(1, 7):
        fields = {"generation": 1, "cycle": 7, "camera": f"CAM{i}", "sequence": 7}
        add("sdk_metadata", source_epoch="e", camera=f"CAM{i}", image_number=7)
        add("frame_submitted", **fields, source_epoch="e", shape=[20, 60, 3])
        add("frame_admitted", **fields)
        add("camera_result", **fields, shape=[20, 60, 3])
    add("report_eligible", generation=1, cycle=7, words=[0, 0, 0])
    add("report_attempt", generation=1, cycle=7)
    add("report_initial", generation=1, cycle=7, sequence=65534)
    add("report_write_send", generation=1, cycle=7, start=50, words=[0, 0, 0])
    add("report_write_send", generation=1, cycle=7, start=49, words=[65535])
    add(
        "report_outcome",
        generation=1,
        cycle=7,
        outcome="acknowledged",
        sequence=65535,
        words=[0, 0, 0],
        completion_to_ack_ms=2.5,
    )
    add("target_commit", sequence=65535, words=[0, 0, 0])
    add("stop_requested")
    add("owner_stopped", owner="session", terminated=True, cleanup_safe=True)
    assert (
        reconcile(
            rows, "r", reports=True, archives=False, required_owners=("session",)
        )["evidence_complete"]
        is True
    )
    mismatched = [dict(r) for r in rows]
    target = next(r for r in mismatched if r["kind"] == "target_commit")
    target["words"] = [1, 0, 0]
    mismatch = reconcile(
        mismatched, "r", reports=True, archives=False, required_owners=("session",)
    )
    assert "target sequence/bitmap mismatch" in str(mismatch["issues"])
    uncertain = [dict(r) for r in rows]
    outcome = next(r for r in uncertain if r["kind"] == "report_outcome")
    outcome.update(outcome="failed", stage="sequence", end_code=None)
    failure = reconcile(
        uncertain, "r", reports=True, archives=False, required_owners=("session",)
    )
    assert cast(dict[str, object], failure["reporting"])["uncertain"] == 1
    assert failure["evidence_complete"] is False
    add("cycle_terminal", generation=1, cycle=7)
    add("target_commit", sequence=0, words=[1, 0, 0])
    add("archive_eligible", generation=1, cycle=7, camera="CAM1", sequence=7)
    add("archive_snapshot", dropped=1)
    add("unresolved_release")
    add("missing", observation="SDK callback")
    broken = reconcile(
        rows,
        "r",
        reports=True,
        archives=True,
        required_owners=("session", "camera_owner"),
    )
    assert broken["evidence_complete"] is False
    issues = str(broken["issues"])
    for expected in (
        "exactly-once",
        "commit count",
        "missing/dropped",
        "drops/failures",
        "unresolved",
        "SDK callback",
        "camera_owner",
    ):
        assert expected in issues
