"""Auxiliary writes/read observations, independent of production and real PLCs."""

import json
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from tests.support.fins_tcp import Fault, Stage, TcpPeer
from tests.unit.test_alarm_monitor import definitions, selection, signal
from tests.unit.test_batch_reporting import wait

from capsule_vision.application import ApplicationState
from capsule_vision.application.alarm_monitor import AlarmMonitor
from capsule_vision.application.alarm_records import AlarmEvent
from capsule_vision.communication import FinsTcpEndpoint, FinsUdpEndpoint
from capsule_vision.communication.modbus_tcp import ModbusSettings
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.configuration.errors import ConfigurationError


def words(packet: bytes) -> tuple[int, ...]:
    return tuple(int.from_bytes(packet[i : i + 2]) for i in range(18, len(packet), 2))


class WrappingMonitor(AlarmMonitor):
    def seed_before_wrap(self) -> None:
        self._heartbeat = 65534


def test_heartbeat_waits_for_fault_ack_on_the_same_connection(tmp_path: Path) -> None:
    monitor = AlarmMonitor(
        tmp_path / "alarms.json",
        lambda _: None,
        lambda: (1.0, None, None),
        state=lambda: ApplicationState.FAULT,
        interval=60,
    )
    with TcpPeer() as peer:
        peer.hold_next("sequence", start=211)
        try:
            monitor.apply_saved(
                replace(selection(peer, 118), heartbeat_address=146, fault_address=211)
            )
            assert peer.entered.wait(2)
            assert len(peer.packets) == 1
            assert peer.packets[0][10:18] == bytes.fromhex("0102 b1 00d3 00 0001")
            assert words(peer.packets[0]) == (1,)
            assert monitor.snapshot().state == "waiting" and peer.words(146) == (0,)
            peer.release.set()
            wait(lambda: monitor.snapshot().state == "online")
            assert [int.from_bytes(p[13:15]) for p in peer.packets] == [211, 146, 118]
            assert peer.words(211) == (1,) and peer.words(146) == (1,)
            assert peer.connections == 1
        finally:
            peer.release.set()
            monitor.request_stop()
            assert monitor.join(2)


def test_serial_sequence_wrap_fresh_fault_and_no_close_clear(tmp_path: Path) -> None:
    observed: list[int] = []
    states = iter(
        (ApplicationState.READY, ApplicationState.FAULT, ApplicationState.UNCONFIGURED)
    )

    def state() -> ApplicationState:
        observed.append(get_ident())
        return next(states, ApplicationState.UNCONFIGURED)

    monitor = WrappingMonitor(
        tmp_path / "alarms.json",
        lambda _: None,
        lambda: (42.0, None, None),
        state=state,
        interval=0.05,
    )
    monitor.seed_before_wrap()
    with TcpPeer() as peer:
        try:
            monitor.apply_saved(
                replace(selection(peer, 118), heartbeat_address=146, fault_address=211)
            )
            wait(lambda: monitor.snapshot().heartbeat == 1)
            monitor.request_stop()
            assert monitor.join(2)
            packets = peer.packets
            assert [words(p) for p in packets[:9:3]] == [(0,), (1,), (0,)]
            assert [words(p) for p in packets[1:9:3]] == [(65535,), (0,), (1,)]
            assert all(
                p[10:18] == bytes.fromhex("0102 b1 00d3 00 0001") for p in packets[::3]
            )
            assert all(
                p[10:18] == bytes.fromhex("0102 b1 0092 00 0001") for p in packets[1::3]
            )
            assert all(
                p[10:18] == bytes.fromhex("0101 b1 0076 00 0001") for p in packets[2::3]
            )
            assert (
                peer.connections == 1
                and len(set(observed)) == 1
                and observed[0] != get_ident()
            )
            assert len(packets) == 9  # Close sent no clearing request.
        finally:
            monitor.request_stop()
            assert monitor.join(2)


@pytest.mark.parametrize(
    "stage,fault,address,health",
    [
        ("sequence", "plc", 47, "partial"),
        ("sequence", "drop_after", 47, "unavailable"),
        ("sequence", "sid", 47, "unavailable"),
        ("sequence", "plc", 46, "partial"),
        ("sequence", "drop_after", 46, "unavailable"),
        ("sequence", "sid", 46, "unavailable"),
        ("read", "disconnect", 18, "partial"),
        ("read", "sid", 18, "partial"),
        ("handshake", "reject", None, "unavailable"),
    ],
)
def test_independent_success_failure_and_later_recovery(
    tmp_path: Path, stage: Stage, fault: Fault, address: int | None, health: str
) -> None:
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        tmp_path / "alarms.json",
        events.extend,
        lambda: (10.0, None, None),
        state=lambda: ApplicationState.FAULT,
        interval=0.15,
    )
    with TcpPeer() as peer:
        peer.set_words(18, (1,))
        peer.fail_next(stage, fault, start=address)
        try:
            monitor.apply_saved(
                replace(
                    selection(peer),
                    endpoint=FinsTcpEndpoint(port=peer.port, timeout=0.05),
                )
            )
            wait(lambda: monitor.snapshot().state == health)
            failed = monitor.snapshot()
            assert failed.issue and (
                failed.read_ok is (stage == "sequence" and fault == "plc")
            )
            assert failed.write_ok is (stage == "read")
            if stage == "sequence":
                assert f"W{address}" in failed.write_issue
            if failed.read_ok:
                assert (
                    failed.active
                    and failed.observed_at == 10
                    and failed.written_at is None
                )
            else:
                assert failed.observed_at is None and not events
            count = len(peer.packets)
            assert not Event().wait(0.04)  # No immediate retry/reconnect.
            assert len(peer.packets) == count
            wait(lambda: monitor.snapshot().state == "online")
            good = monitor.snapshot()
            assert (
                good.read_ok
                and good.write_ok
                and good.fault == 1
                and good.heartbeat == 2
            )
            assert good.active and good.observed_at == good.written_at == 10
            writes = [
                (int.from_bytes(p[13:15]), words(p))
                for p in peer.packets
                if p[10:12] == b"\x01\x02"
            ]
            first = (
                []
                if stage == "handshake"
                else [(47, (1,))]
                if address == 47
                else [(47, (1,)), (46, (1,))]
            )
            assert writes == first + [(47, (1,)), (46, (2,))]
            assert peer.connections == (1 if fault == "plc" else 2)
            assert len(events) == 1 and events[0].state == "observed_active"
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_transport_gap_never_clears_alarm_and_recovers_as_observed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alarms.json"
    definitions(path, signal())
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        path,
        events.extend,
        lambda: (20.0, "batch", "Line"),
        state=lambda: ApplicationState.READY,
        interval=0.1,
    )
    with TcpPeer() as peer:
        peer.set_words(18, (8,))
        try:
            monitor.apply_saved(
                replace(
                    selection(peer),
                    endpoint=FinsTcpEndpoint(port=peer.port, timeout=0.05),
                )
            )
            wait(lambda: monitor.snapshot().read_ok)
            peer.fail_next("sequence", "drop_after")
            wait(lambda: monitor.snapshot().state == "unavailable")
            stale = monitor.snapshot()
            assert stale.raw_word == 8 and len(stale.active) == 1 and len(events) == 1
            peer.set_words(18, (0,))
            wait(lambda: len(events) == 2)
            assert (
                events[-1].state == "observed_recovered"
                and events[-1].batch_id == "batch"
            )
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_saved_disable_keeps_old_write_reservation_until_retirement(
    tmp_path: Path,
) -> None:
    monitor = AlarmMonitor(
        tmp_path / "alarms.json",
        lambda _: None,
        lambda: (1.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=0.02,
    )
    with TcpPeer() as peer:
        peer.hold_next("sequence")
        selected = selection(peer)
        try:
            monitor.apply_saved(selected)
            assert peer.entered.wait(2)
            monitor.apply_saved(replace(selected, enabled=False))
            assert monitor.snapshot().state == "off"
            assert monitor.protected_settings() == (selected,)
            monitor.request_stop()
            assert not monitor.join(0)
            assert monitor.protected_settings() == (selected,)
            peer.release.set()
            assert monitor.join(2) and monitor.protected_settings() == ()
            assert len(peer.packets) == 1 and words(peer.packets[0]) == (0,)
            assert int.from_bytes(peer.packets[0][13:15]) == 47
            assert (
                monitor.snapshot().state == "off"
                and monitor.snapshot().written_at is None
            )
        finally:
            peer.release.set()
            monitor.request_stop()
            assert monitor.join(2)


@pytest.mark.parametrize(
    "payload",
    [
        {"alarm": {}},
        {"alarm": {"word_address": 18}},
        {"status": {}},
        {"status": {"word_address": 18, "heartbeat_address": 46}},
        *(
            {"status": {"fault_address": 47, **invalid}}
            for invalid in (
                {"station": 1},
                {"protocol": "fins_udp"},
                {"heartbeat_address": 512},
                {"heartbeat_address": -1},
                {"heartbeat_address": True},
                {"heartbeat_address": 46.0},
                {"heartbeat_address": None},
                {"heartbeat_address": 18},
                {"word_address": 47},
                {"fault_address": 46},
                {"fault_address": 18},
                {"fault_address": -1},
                {"fault_address": 512},
                {"fault_address": True},
                {"fault_address": 47.0},
                {"fault_address": None},
                {"fault_address": "W47"},
                {"running_address": -1},
                {"running_address": 512},
                {"running_address": True},
                {"running_address": 16.0},
                {"running_address": None},
                {"running_address": "W16.1"},
                {"running_address": 46},
                {"running_address": 47},
                {"running_bit": 1},
            )
        ),
    ],
)
def test_strict_schema_never_migrates_or_repairs(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "communication.json"
    saved = json.dumps(payload).encode()
    path.write_bytes(saved)
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    assert path.read_bytes() == saved


def test_defaults_explicit_roundtrip_and_register_bounds(tmp_path: Path) -> None:
    path = tmp_path / "communication.json"
    path.write_text("{}", encoding="utf-8")
    assert load_communication_preferences(path).status == StatusPreferences()
    assert load_communication_preferences(path).status.running_address == 16
    path.write_text('{"status":{"fault_address":47}}', encoding="utf-8")
    assert load_communication_preferences(path).status == StatusPreferences()
    for word, heartbeat, fault, running in (
        (0, 511, 510, 16),
        (511, 1, 47, 0),
        (18, 46, 47, 511),
        (118, 146, 211, 216),
        (18, 46, 47, 18),  # Two read targets may share a word.
    ):
        value = CommunicationPreferences(
            status=StatusPreferences(
                True,
                word_address=word,
                heartbeat_address=heartbeat,
                fault_address=fault,
                running_address=running,
            )
        )
        save_communication_preferences(path, value)
        assert load_communication_preferences(path) == value
        encoded = json.loads(path.read_text(encoding="utf-8"))["status"]
        assert (
            encoded["protocol"] == "fins_tcp"
            and encoded["heartbeat_address"] == heartbeat
            and encoded["fault_address"] == fault
            and encoded["running_address"] == running
        )
        assert encoded["word_address"] == word and "alarm" not in json.loads(
            path.read_text(encoding="utf-8")
        )


@pytest.mark.parametrize("mode", list(ReportingMode))
@pytest.mark.parametrize("base", [49, 149])
def test_configured_status_reporting_ranges_cannot_conflict(
    mode: ReportingMode, base: int
) -> None:
    settings = ReportingSettings(FinsTcpEndpoint(), base, mode=mode)
    report = ReportingSelection(True, settings)
    for value in (
        StatusPreferences(True, heartbeat_address=base),
        StatusPreferences(True, fault_address=base),
        StatusPreferences(True, word_address=base),
    ):
        with pytest.raises(ValueError, match="overlap"):
            CommunicationPreferences(reporting=report, status=value)
    if mode.group_size:
        with pytest.raises(ValueError, match="overlap"):
            CommunicationPreferences(
                reporting=report,
                status=StatusPreferences(True, heartbeat_address=base - 1),
            )
    else:
        CommunicationPreferences(
            reporting=report, status=StatusPreferences(True, heartbeat_address=base - 1)
        )
    # Reporting may lie between the independent status-write words.
    CommunicationPreferences(
        reporting=report,
        status=StatusPreferences(
            True, heartbeat_address=base - 3, fault_address=base + 4
        ),
    )
    # Matching FINS UDP/TCP endpoints share WR ownership; distinct targets do not.
    if mode is ReportingMode.PER_CYCLE:
        with pytest.raises(ValueError, match="overlap"):
            CommunicationPreferences(
                reporting=replace(
                    report, settings=replace(settings, endpoint=FinsUdpEndpoint())
                ),
                status=StatusPreferences(True, heartbeat_address=base),
            )
    CommunicationPreferences(
        reporting=report,
        status=StatusPreferences(
            True, FinsTcpEndpoint(port=9601), heartbeat_address=base
        ),
    )
    CommunicationPreferences(
        reporting=ReportingSelection(True, ReportingSettings(ModbusSettings(), base)),
        status=StatusPreferences(True, heartbeat_address=base),
    )


@pytest.mark.parametrize("mode", list(ReportingMode))
@pytest.mark.parametrize("base", [49, 149])
@pytest.mark.parametrize("enabled", [False, True])
def test_running_target_cannot_alias_configured_automatic_reporting(
    tmp_path: Path, mode: ReportingMode, base: int, enabled: bool
) -> None:
    report = ReportingSelection(
        enabled, ReportingSettings(FinsTcpEndpoint(), base, mode=mode)
    )
    start = base - 1 if mode.group_size else base
    for address in range(start, base + 4):
        # Status Off does not make a conflicting future read target safe.
        with pytest.raises(ValueError, match="overlap"):
            CommunicationPreferences(
                reporting=report, status=StatusPreferences(running_address=address)
            )
    for address in (start - 1, base + 4):
        CommunicationPreferences(
            reporting=report,
            status=StatusPreferences(
                heartbeat_address=400, fault_address=401, running_address=address
            ),
        )
    CommunicationPreferences(
        reporting=report,
        status=StatusPreferences(
            False, FinsTcpEndpoint(port=9601), running_address=base
        ),
    )
    if mode is ReportingMode.PER_CYCLE:
        with pytest.raises(ValueError, match="overlap"):
            CommunicationPreferences(
                reporting=replace(
                    report,
                    settings=replace(report.settings, endpoint=FinsUdpEndpoint()),
                ),
                status=StatusPreferences(running_address=base),
            )
    CommunicationPreferences(
        reporting=ReportingSelection(
            enabled, ReportingSettings(ModbusSettings(), base)
        ),
        status=StatusPreferences(running_address=base),
    )
    path = tmp_path / "communication.json"
    content = json.dumps(
        {
            "reporting": {
                "protocol": "fins_tcp",
                "base_address": base,
                "mode": mode.value,
                "enabled": enabled,
            },
            "status": {"fault_address": 47, "running_address": base},
        }
    ).encode()
    path.write_bytes(content)
    with pytest.raises(ConfigurationError, match="overlap"):
        load_communication_preferences(path)
    issues: list[str] = []
    recovered = load_communication_preferences(path, status_error=issues.append)
    assert recovered.reporting == report and not recovered.status.enabled
    assert recovered.status.running_address == 16 and len(issues) == 1
    assert path.read_bytes() == content


def test_running_only_save_preserves_connection_health_and_alarm_baseline(
    tmp_path: Path,
) -> None:
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        tmp_path / "alarms.json",
        events.extend,
        lambda: (42.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=60,
    )
    with TcpPeer() as peer:
        selected = replace(selection(peer), enabled=False)
        try:
            monitor.apply_saved(selected)
            monitor.apply_saved(replace(selected, running_address=216))
            assert monitor.snapshot().state == "off" and peer.connections == 0
            peer.set_words(18, (1,))
            monitor.apply_saved(replace(selected, enabled=True))
            wait(lambda: monitor.snapshot().state == "online")
            before = monitor.snapshot()
            recorded = tuple(events)
            monitor.apply_saved(replace(before.selection, running_address=316))
            assert monitor.snapshot() == replace(
                before, selection=replace(before.selection, running_address=316)
            )
            assert not Event().wait(0.05)
            assert peer.connections == 1 and len(peer.packets) == 3
            assert [int.from_bytes(p[13:15]) for p in peer.packets] == [47, 46, 18]
            assert tuple(events) == recorded
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_running_and_output_address_swap_applies_without_intermediate_collision(
    tmp_path: Path,
) -> None:
    monitor = AlarmMonitor(
        tmp_path / "alarms.json",
        lambda _: None,
        lambda: (1.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=60,
    )
    with TcpPeer() as peer:
        try:
            selected = selection(peer)
            monitor.apply_saved(selected)
            wait(lambda: monitor.snapshot().state == "online")
            swapped = replace(selected, heartbeat_address=16, running_address=46)
            monitor.apply_saved(swapped)
            wait(lambda: monitor.snapshot().state == "online")
            assert monitor.snapshot().selection == swapped
            assert peer.connections == 2
            assert [int.from_bytes(p[13:15]) for p in peer.packets] == [
                47,
                46,
                18,
                47,
                16,
                18,
            ]
        finally:
            monitor.request_stop()
            assert monitor.join(2)
