"""Equipment signals and bounded alarm observations on disposable local owners."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from tests.support.application import FakeSession, prepared
from tests.support.fins_tcp import TcpPeer
from tests.unit.test_batch_reporting import admit, owner, submit, wait

from capsule_vision.application import (
    ApplicationError,
    ProductionApplication,
    alarm_records,
)
from capsule_vision.application.alarm_monitor import AlarmMonitor
from capsule_vision.application.alarm_records import AlarmEvent, AlarmQuery
from capsule_vision.application.history import ProductionHistory
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.reporting import ReportOutcome
from capsule_vision.configuration.alarm_definitions import (
    TEMPLATE,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import (
    AlarmPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.configuration.errors import ConfigurationError


def signal(**fields: object) -> dict[str, object]:
    return {
        "id": "air",
        "bit": 3,
        "category": "equipment",
        "zh": "气压低",
        "en": "Low air pressure",
        **fields,
    }


def definitions(path: Path, *rows: dict[str, object], version: str = "site-1") -> None:
    path.write_text(json.dumps({"version": version, "signals": rows}), encoding="utf-8")


def selection(peer: TcpPeer, station: int = 1) -> AlarmPreferences:
    return AlarmPreferences(True, station, FinsTcpEndpoint(port=peer.port, timeout=0.5))


def test_bundled_workbook_mapping_override_and_persisted_selection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alarm_definitions.json"
    asset = load_alarm_definitions(path)
    assert asset.origin == "bundled" and asset.version == "2026-09-05"
    assert tuple(s.bit for s in asset.signals) == tuple(range(11))
    assert tuple(s.zh for s in asset.signals) == (
        "往复轴报警",
        "转囊轴报警",
        "急停",
        "气压低",
        "缺料停机",
        "往复轴行程未设置",
        "输送阀动作相位未设置",
        "未寻零，禁止启动",
        "未开启检测，禁止启动",
        "软件掉线",
        "软件报警",
    )
    assert all(s.en and s.active_high for s in asset.signals)
    definitions(path, signal(active_high=False))
    local = load_alarm_definitions(path)
    assert local.origin == "equipment" and local.digest != asset.digest
    assert not local.signals[0].active_high
    comm = tmp_path / "communication.json"
    comm.write_text("{}", encoding="utf-8")
    old = load_communication_preferences(comm)
    assert not old.alarm.enabled and old.alarm.address == 18
    value = replace(old, alarm=AlarmPreferences(True, 2, FinsTcpEndpoint(port=19600)))
    save_communication_preferences(comm, value)
    assert value.alarm.address == 118 and load_communication_preferences(comm) == value


@pytest.mark.parametrize(
    "rows",
    [
        (),
        (signal(), signal(bit=4)),
        (signal(), signal(id="second")),
        (signal(bit=True),),
        (signal(bit=16),),
        (signal(active_high=1),),
        (signal(category="fault"),),
        (signal(en=""),),
        (signal(id="unbounded name"),),
        (signal(foo=1),),
    ],
)
def test_invalid_equipment_asset_is_never_replaced(
    tmp_path: Path, rows: tuple[dict[str, object], ...]
) -> None:
    path = tmp_path / "alarm_definitions.json"
    definitions(path, *rows)
    original = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_alarm_definitions(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "alarm",
    [
        {"enabled": 1},
        {"station": True},
        {"station": 3},
        {"protocol": "fins_udp"},
        {"host": "invalid"},
        {"timeout": 10},
        {"port": 0},
    ],
)
def test_invalid_alarm_preferences_are_explicit_and_isolated_at_startup(
    tmp_path: Path, alarm: dict[str, object]
) -> None:
    path = tmp_path / "communication.json"
    path.write_text(
        json.dumps(
            {"alarm": alarm, "reporting": {"enabled": True, "protocol": "fins_tcp"}}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    errors: list[str] = []
    result = load_communication_preferences(path, alarm_error=errors.append)
    assert errors and result.reporting.enabled and not result.alarm.enabled


def test_transitions_gaps_polarity_and_frozen_definition(tmp_path: Path) -> None:
    path = tmp_path / "alarm_definitions.json"
    definitions(
        path, signal(active_high=False), signal(id="stop", bit=2, category="interlock")
    )
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        path, events.extend, lambda: (100.0, "batch-1", "Line A"), interval=0.025
    )
    with TcpPeer() as peer:
        try:
            monitor.apply_saved(selection(peer, 2))
            wait(lambda: len(events) == 1)
            assert events[0].signal == "air" and events[0].state == "observed_active"
            assert (events[0].station, events[0].address, events[0].bit) == (2, 118, 3)
            assert events[0].batch_id == "batch-1" and events[0].batch_label == "Line A"
            assert "equipment/site-1" in events[0].definition
            definitions(path, signal(id="new", bit=15), version="site-2")
            peer.set_words(118, (12,))
            wait(lambda: len(events) == 3)
            assert [(e.signal, e.state) for e in events[1:]] == [
                ("air", "recovered"),
                ("stop", "activated"),
            ]
            wait(lambda: len(peer.packets) >= 5)
            assert len(events) == 3
            peer.fail_next("read", "disconnect")
            wait(lambda: monitor.snapshot().state == "unavailable")
            assert [s.id for s in monitor.snapshot().active] == ["stop"]
            peer.set_words(118, (0,))
            wait(lambda: len(events) == 5)
            assert [e.state for e in events[-2:]] == [
                "observed_active",
                "observed_recovered",
            ]
            assert all(e.definition == events[0].definition for e in events)
            assert all(
                p[10:18] == bytes.fromhex("0101 b1 0076 00 0001") for p in peer.packets
            )
            monitor.apply_saved(AlarmPreferences())
            peer.set_words(18, (1 << 15,))
            monitor.apply_saved(selection(peer))
            wait(lambda: len(events) == 6)
            assert events[-1].signal == "new" and events[-1].address == 18
            assert "site-2" in events[-1].definition
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_invalid_asset_never_contacts_plc_and_requires_reactivation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alarm_definitions.json"
    path.write_bytes(b"{bad")
    monitor = AlarmMonitor(
        path, lambda _: None, lambda: (100.0, None, None), interval=0.02
    )
    with TcpPeer() as peer:
        try:
            monitor.apply_saved(selection(peer))
            wait(lambda: monitor.snapshot().state == "unavailable")
            assert (
                monitor.snapshot().issue
                and peer.connections == 0
                and path.read_bytes() == b"{bad"
            )
            path.write_bytes(TEMPLATE.read_bytes())
            monitor.apply_saved(
                selection(peer)
            )  # Unrelated settings save is not a restart.
            assert monitor.snapshot().state == "unavailable" and peer.connections == 0
            monitor.apply_saved(AlarmPreferences())
            monitor.apply_saved(selection(peer))
            wait(lambda: monitor.snapshot().state == "online")
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_blocked_alarm_read_never_serializes_four_cycle_sequence(
    tmp_path: Path,
) -> None:
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        tmp_path / "local.json",
        events.extend,
        lambda: (100.0, None, None),
        interval=0.03,
    )
    with TcpPeer(dual_session=True) as peer, owner(peer) as reporting:
        peer.set_words(18, (4,))
        peer.hold_next("read")
        try:
            monitor.apply_saved(
                AlarmPreferences(True, 1, FinsTcpEndpoint(port=peer.port, timeout=1.0))
            )
            assert peer.entered.wait(2)
            for group in range(8):
                first = group * 4 + 1
                peer.set_words(48, (group + 3,))
                admit(reporting, first)
                futures = submit(reporting, first)
                assert futures[-1].result(2).outcome is ReportOutcome.ACKNOWLEDGED
            assert not events and not peer.release.is_set()
            monitor.apply_saved(
                AlarmPreferences()
            )  # The held reply belongs to the previous selection.
            peer.release.set()
            writes = [
                int.from_bytes(p[13:15])
                for p in peer.packets
                if p[10:12] == b"\x01\x02"
            ]
            assert writes == [50, 49] * 8
            assert reporting.snapshot().phase_issue is None
        finally:
            peer.release.set()
            monitor.request_stop()
            assert monitor.join(2)
        assert not events


def event(observed: float = 100, signal_id: str = "on") -> AlarmEvent:
    return AlarmEvent(
        observed,
        signal_id,
        "activated",
        "equipment",
        "设备",
        "Equipment",
        "fins_tcp://127.0.0.1:9600",
        1,
        18,
        0,
        "equipment/v1",
        "uuid",
        "Lot",
    )


def test_history_handoff_bounds_retention_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    path = tmp_path / "history.sqlite3"
    entered, release = Event(), Event()
    append = alarm_records.append

    def held(db: sqlite3.Connection, events: tuple[AlarmEvent, ...]) -> None:
        entered.set()
        assert release.wait(3)
        append(db, events)

    monkeypatch.setattr(alarm_records, "append", held)
    history = ProductionHistory(
        path, max_pending=1, clock=lambda: now[0], alarm_access=lambda _: 0
    )
    try:
        history.initial_selection.result(2)
        history.offer_alarms((event(),))
        assert entered.wait(2)
        history.offer_alarms((event(signal_id="next"),))
        history.offer_alarms((event(signal_id="lost"),))
        assert history.alarm_storage()[0] == 1
        release.set()
    finally:
        release.set()
        history.request_stop()
        assert history.join(3)
    now[0] += 30 * 86400
    restored = ProductionHistory(path, clock=lambda: now[0], alarm_access=lambda _: 0)
    try:
        restored.initial_selection.result(2)
        rows = restored.alarm_page(AlarmQuery(batch="Lot")).result(2)
        assert [r.event.signal for r in rows] == ["next", "on"]
        assert not restored.alarm_page(AlarmQuery(category="startup")).result(2)
        now[0] += 61
        restored.offer_alarms((event(now[0], "new"),))
        assert [
            r.event.signal for r in restored.alarm_page(AlarmQuery()).result(2)
        ] == ["new"]
    finally:
        restored.request_stop()
        assert restored.join(3)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT signal FROM alarm_events").fetchall() == [("new",)]


def test_runtime_fault_and_reset_are_separate_application_events(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    gate = session.join_gate = Event()
    history = ProductionHistory(tmp_path / "history.sqlite3", alarm_access=lambda _: 0)
    app = ProductionApplication(lambda _p, _g: session, history=history)
    try:
        history.initial_selection.result(3)
        app.configure(prepared(tmp_path))
        app.start().result(3)
        session.failure = RuntimeError("Synthetic failure")
        session.finished.set_result(None)
        assert session.join_entered.wait(3)
        stopped = app.stop()
        gate.set()
        with pytest.raises(ApplicationError):
            stopped.result(3)
        app.reset_fault()
        events = [
            r.event for r in app.alarm_page(AlarmQuery(category="software")).result(3)
        ]
        assert [e.state for e in events] == ["recovered", "activated"]
        assert all(
            e.signal == "production_fault"
            and e.source == "application"
            and e.bit is None
            and e.station is None
            for e in events
        )
        assert all(e.batch_id is None and e.detail for e in events)
    finally:
        gate.set()
        app.close().result(3)
        assert app.join(3)


def test_damaged_alarm_table_cannot_block_named_batch_restoration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path)
    try:
        batch = history.select_batch("Line B").result(3)
        assert batch
    finally:
        history.request_stop()
        assert history.join(3)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE alarm_events(invalid INTEGER)")
    history = ProductionHistory(path, alarm_access=lambda _: 0)
    try:
        assert history.initial_selection.result(3) == batch
        assert history.alarm_storage()[1]
        with pytest.raises(sqlite3.Error):
            history.alarm_page(AlarmQuery()).result(3)
        assert history.select_batch("Line C").result(3)
    finally:
        history.request_stop()
        assert history.join(3)
