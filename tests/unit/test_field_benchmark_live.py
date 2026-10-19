"""Live-run gates and detached hosting with no field, camera or GPU access."""

import json
import socket
import struct
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from time import monotonic

import pytest
from field_benchmark.analyze import load_rows
from field_benchmark.hosting import SocketChannel, join_detached, start_detached
from field_benchmark.ledger import Fields, Ledger
from field_benchmark.live import STAGES, claim_stage, drive_backend, require_admission
from field_benchmark.measurements import live_measurements
from field_benchmark.observations import Observations
from field_benchmark.resources import require_resources
from field_benchmark.schedule import BurstPlan
from field_benchmark.trigger import (
    PhotoClient,
    Readiness,
    SupervisorSettings,
    endpoint_gate,
    require_manual_idle,
)
from tests.application_support import production
from tests.benchmark_support import BitPeer

from vimo_demo.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.application.basler_connection import (
    ConnectedCamera,
    ConnectionState,
    SourceConnectionSnapshot,
)
from vimo_demo.application.basler_control import BaslerControlSnapshot
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.inspection import CameraId


def test_live_endpoint_is_explicit_fixed_and_precedes_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint_gate("127.0.0.1", 1234, False)
    endpoint_gate("192.168.251.1", 9600, True)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Rejected endpoint must not open a socket")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    for host, port, live in (
        ("192.168.251.1", 9600, False),
        ("192.168.251.2", 9600, True),
        ("192.168.251.1", 9601, True),
    ):
        with pytest.raises(ValueError, match="endpoint"):
            PhotoClient(host, port=port, timeout=0.2, live=live)


def test_readonly_plc_preflight_and_manual_stopped_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def words(
        _self: PhotoClient, start: int, *_args: object, **_kwargs: object
    ) -> tuple[int, ...]:
        calls.append(start)
        return ({10: 0x5521, 15: 1, 16: 0, 18: 32}[start],)

    monkeypatch.setattr(PhotoClient, "read_words", words)
    bit = [0]

    def read(_self: PhotoClient) -> int:
        return bit[0]

    monkeypatch.setattr(PhotoClient, "read_photo", read)
    client = object.__new__(PhotoClient)
    good = client.preflight()
    assert calls == [10, 15, 16, 18] and good["W18"] == 32
    require_manual_idle(good)
    for key, value in (("W15", 0), ("W16", 2), ("W16", 4), ("photo", 1)):
        with pytest.raises(RuntimeError, match="manual/stopped"):
            require_manual_idle({**good, key: value})
    bit[0] = 1
    with pytest.raises(RuntimeError, match="changed"):
        client.preflight()


def test_stage_budget_prior_pass_and_no_replacement(tmp_path: Path) -> None:
    assert [(s.name, s.pulses) for s in STAGES] == [
        ("single", 1),
        ("burst", 8),
        ("B", 80),
        ("R", 80),
        ("A", 80),
        ("RA", 80),
    ]
    assert sum(s.pulses for s in STAGES) == 329
    with pytest.raises(RuntimeError, match="no escalation"):
        claim_stage(tmp_path, "burst")
    stage, directory = claim_stage(tmp_path, "single")
    assert stage.pulses == 1
    with pytest.raises(FileExistsError):
        claim_stage(tmp_path, "single")
    (directory / "stage-result.json").write_text('{"passed":false}')
    with pytest.raises(RuntimeError):
        claim_stage(tmp_path, "burst")
    (directory / "stage-result.json").write_text('{"passed":true}')
    assert claim_stage(tmp_path, "burst")[0].pulses == 8


class Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = ProductionApplicationSnapshot(
            ApplicationState.READY,
            1,
            None,
            None,
            None,
            ProductionCounters(),
            None,
            False,
            None,
        )
        self.source = SourceConnectionSnapshot(
            ConnectionState.DISCONNECTED,
            tuple(
                ConnectedCamera(CameraId(f"CAM{i + 1}"), str(100 + i), 60, 20, "fake")
                for i in range(6)
            ),
            False,
            True,
            "",
            None,
            0,
            0,
            None,
            False,
        )
        self.control = BaslerControlSnapshot(
            1, ReplayState.AUTOMATIC, True, True, 0, 0, 0
        )
        self.fail_stop = False

    def connect_source(self) -> None:
        self.calls.append("Connect")
        self.source = replace(
            self.source,
            state=ConnectionState.CONNECTED,
            thread_alive=True,
            cleanup_safe=False,
        )

    def disconnect_source(self) -> None:
        self.calls.append("Disconnect")
        self.source = replace(
            self.source,
            state=ConnectionState.DISCONNECTED,
            thread_alive=False,
            cleanup_safe=True,
        )

    def start(self) -> Future[None]:
        self.calls.append("Start")
        self.state = replace(
            self.state, state=ApplicationState.RUNNING, runtime_owned=True
        )
        self.source = replace(self.source, inspection_generation=1)
        future: Future[None] = Future()
        future.set_result(None)
        return future

    def stop(self) -> Future[None]:
        self.calls.append("Stop")
        future: Future[None] = Future()
        if self.fail_stop:
            future.set_exception(RuntimeError("real cleanup failure"))
        else:
            self.state = replace(
                self.state, state=ApplicationState.READY, runtime_owned=False
            )
            future.set_result(None)
        return future

    def close(self) -> Future[None]:
        self.calls.append("Close")
        self.state = replace(
            self.state, state=ApplicationState.CLOSED, runtime_owned=False
        )
        future: Future[None] = Future()
        future.set_result(None)
        return future

    def join(self, timeout: float | None = None) -> bool:
        self.calls.append("Join")
        return True

    def snapshot(self) -> ProductionApplicationSnapshot:
        return self.state

    def source_snapshot(self) -> SourceConnectionSnapshot:
        return self.source

    def control_snapshot(self) -> BaslerControlSnapshot:
        return self.control


@pytest.mark.parametrize("stop_failure", [False, True])
def test_backend_warmup_ready_terminal_and_cleanup(
    tmp_path: Path, stop_failure: bool
) -> None:
    ledger = Ledger(tmp_path / "backend.jsonl", "r", "backend")
    obs = Observations(ledger, "epoch", strict_run=True)
    app = Backend()
    app.fail_stop = stop_failure

    def trigger(snapshot: ProductionApplicationSnapshot) -> str:
        assert (
            app.calls == ["Connect", "Start"]
            and snapshot.counters == ProductionCounters()
        )
        obs.result(1, production())
        return "budget_complete"

    outcome = drive_backend(app, obs, trigger, 1)
    assert app.calls == ["Connect", "Start", "Stop", "Disconnect", "Close", "Join"]
    assert outcome["cleanup"] is True
    assert obs.stop.is_set() is stop_failure
    assert ("stop_error" in outcome) is stop_failure
    assert ledger.close()
    rows = load_rows([ledger.path])
    assert sum(r["kind"] == "cycle_terminal" for r in rows) == 1
    assert any(
        r["kind"] == "production_ready" and r["cycles_completed"] == 0 for r in rows
    )


def test_admission_requires_live_six_camera_generation_and_zero_inputs() -> None:
    app = Backend()
    with pytest.raises(RuntimeError, match="RUNNING"):
        require_admission(app)
    app.connect_source()
    app.start().result()
    require_admission(app)
    original = app.source
    for altered in (
        replace(original, inspection_generation=2),
        replace(original, thread_alive=False),
        replace(original, cameras=original.cameras[:-1]),
        replace(original, received_frames=1),
        replace(original, discarded_frames=1),
    ):
        app.source = altered
        with pytest.raises(RuntimeError):
            require_admission(app)
    app.source = original
    app.control = replace(app.control, admission_armed=False)
    with pytest.raises(RuntimeError, match="ARMED"):
        require_admission(app)


def test_resource_and_missing_evidence_fail_closed() -> None:
    good: Fields = {
        "available_memory_bytes": 3 * 1024**3,
        "disk_free_bytes": 6 * 1024**3,
        "gpu_temperature_c": 45,
    }
    require_resources(good)
    for key, value in (
        ("available_memory_bytes", None),
        ("disk_free_bytes", 1),
        ("gpu_temperature_c", 60),
    ):
        with pytest.raises(RuntimeError):
            require_resources({**good, key: value})
    issues, summary = live_measurements(
        [],
        {
            "serials": [str(i) for i in range(6)],
            "camera_ids": [f"CAM{i + 1}" for i in range(6)],
            "pulse_budget": 1,
            "groups": 1,
        },
    )
    assert "Final independent W10.7 low readback missing" in issues
    assert summary["interval_rate_hz"] is None


def test_socket_channel_bounds_and_eof() -> None:
    left, right = socket.socketpair()
    channel = SocketChannel(left)
    try:
        right.sendall(struct.pack("!I", 4097))
        with pytest.raises(ValueError, match="length"):
            channel.recv()
        right.close()
        with pytest.raises(EOFError):
            channel.recv()
    finally:
        channel.close()
        right.close()


def test_detached_supervisor_hosting_and_controller_eof(tmp_path: Path) -> None:
    with BitPeer() as peer:
        now = monotonic()
        gate = Readiness(
            "detached", 1, "epoch", "a" * 64, now + 12, True, True, True, True
        )
        settings = SupervisorSettings(
            "detached",
            "127.0.0.1",
            peer.port,
            BurstPlan(now + 3, 1, 1, now + 3.5),
            gate,
            0.020,
            1,
            0.2,
        )
        ledger = Ledger(tmp_path / "controller.jsonl", settings.run, "controller")
        process, controller = start_detached(settings, tmp_path, tmp_path / "locks")
        try:
            controller.acknowledge_ready()
            controller.connection.close()
            assert join_detached(process, ledger, 5)
        finally:
            controller.connection.close()
            assert join_detached(process, ledger, 5)
            assert ledger.close()
        final = json.loads((tmp_path / "supervisor-final.json").read_text())
        assert final["release_verified"] is True and final["activations"] == 0
        assert peer.writes == [0] and peer.bit == 0
