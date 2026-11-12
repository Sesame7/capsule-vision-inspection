"""Equipment signals and bounded alarm observations on disposable local owners."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from tests.support.application import FakeSession, prepared
from tests.support.diagnostics import observations
from tests.support.fins_tcp import TcpPeer
from tests.unit.test_batch_reporting import admit, owner, submit, wait

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
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
    create_local_alarm_definitions,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.configuration.errors import ConfigurationError
from capsule_vision.diagnostics import RuntimeDiagnostics


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


def selection(peer: TcpPeer, address: int = 18) -> StatusPreferences:
    return StatusPreferences(
        True, FinsTcpEndpoint(port=peer.port, timeout=0.5), address
    )


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
    assert not old.status.enabled and old.status.word_address == 18
    value = replace(
        old, status=StatusPreferences(True, FinsTcpEndpoint(port=19600), 118)
    )
    save_communication_preferences(comm, value)
    assert (
        value.status.word_address == 118
        and load_communication_preferences(comm) == value
    )


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
        (signal(translations=None),),
        (signal(translations=["de"]),),
        (signal(translations={"de": 7}),),
        (signal(translations={"de": "   "}),),
        (signal(translations={"de": "a" * 129}),),
        (signal(translations={"de": "two\nlines"}),),
        (signal(translations={"pt-br": "wrong code"}),),
        (signal(translations={"zh-CN": "unsupported alias"}),),
        (signal(translations={"en": "Conflicting name"}),),
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


def test_template_creation_is_exclusive_and_keeps_invalid_or_custom_local_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "equipment" / "alarm_definitions.json"
    with ThreadPoolExecutor(max_workers=2) as pool:
        copies = [pool.submit(create_local_alarm_definitions, path) for _ in range(2)]
        failures = [f.exception(3) for f in copies]
    assert failures.count(None) == 1
    assert sum(isinstance(e, FileExistsError) for e in failures) == 1
    assert path.read_bytes() == TEMPLATE.read_bytes()
    definitions(path, signal(zh="本机报警"))
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        create_local_alarm_definitions(path)
    assert path.read_bytes() == before
    path.write_bytes(b"{invalid")
    with pytest.raises(ConfigurationError):
        create_local_alarm_definitions(path)
    assert path.read_bytes() == b"{invalid"
    assert list(path.parent.iterdir()) == [path]


def test_exact_alarm_labels_and_bounded_optional_translations(tmp_path: Path) -> None:
    path = tmp_path / "alarm_definitions.json"
    definitions(
        path,
        signal(
            translations={
                "zh": "气压低",
                "zh-TW": "氣壓低",
                "en": "Low air pressure",
                "pt-BR": "Pressão baixa",
            }
        ),
    )
    (alarm,) = load_alarm_definitions(path).signals
    assert alarm.label("zh") == "气压低" and alarm.label("zh-TW") == "氣壓低"
    assert alarm.label("pt-BR") == "Pressão baixa"
    assert (
        alarm.label("pt-PT")
        == alarm.label("fr")
        == alarm.label("en")
        == "Low air pressure"
    )
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace('"pt-BR":', '"pt-BR":"duplicado","pt-BR":'), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="Duplicate"):
        load_alarm_definitions(path)
    path.write_bytes(content.encode() + b" " * 32768)
    with pytest.raises(ConfigurationError, match="32 KiB"):
        load_alarm_definitions(path)


def test_unreadable_local_definitions_never_use_or_copy_the_bundle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alarm_definitions.json"
    path.write_bytes(b"local equipment data")
    with patch.object(
        Path, "open", autospec=True, side_effect=PermissionError("access denied")
    ) as opened:
        for command in (load_alarm_definitions, create_local_alarm_definitions):
            with pytest.raises(ConfigurationError) as failure:
                command(path)
            assert isinstance(failure.value.__cause__, PermissionError)
        assert [call.args[0] for call in opened.call_args_list] == [path, path]
    assert path.read_bytes() == b"local equipment data"


@pytest.mark.parametrize(
    "alarm",
    [
        {"enabled": 1},
        {"station": 1, "word_address": 18},
        {"protocol": "fins_udp"},
        {"host": "invalid"},
        {"timeout": 10},
        {"port": 0},
        {"word_address": True},
        {"word_address": -1},
        {"word_address": 512},
        {"word_address": 18.0},
        {"word_address": "W18"},
        {"word_address": None},
    ],
)
def test_invalid_alarm_preferences_are_explicit_and_isolated_at_startup(
    tmp_path: Path, alarm: dict[str, object]
) -> None:
    path = tmp_path / "communication.json"
    path.write_text(
        json.dumps(
            {
                "status": {"word_address": 18, "fault_address": 47, **alarm},
                "reporting": {"enabled": True, "protocol": "fins_tcp"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    errors: list[str] = []
    result = load_communication_preferences(path, status_error=errors.append)
    assert errors and result.reporting.enabled and not result.status.enabled


def test_transitions_gaps_polarity_and_frozen_definition(tmp_path: Path) -> None:
    path = tmp_path / "alarm_definitions.json"
    definitions(
        path,
        signal(active_high=False, translations={"de": "Luftdruck niedrig"}),
        signal(id="stop", bit=2, category="interlock"),
    )
    events: list[AlarmEvent] = []
    writer = RuntimeDiagnostics(tmp_path / "logs", "standard")
    monitor = AlarmMonitor(
        path,
        events.extend,
        lambda: (100.0, "batch-1", "Line A"),
        state=lambda: ApplicationState.READY,
        interval=0.025,
        diagnostics=writer,
    )
    with TcpPeer() as peer:
        try:
            monitor.apply_saved(selection(peer, 118))
            wait(lambda: len(events) == 1)
            assert events[0].signal == "air" and events[0].state == "observed_active"
            assert (events[0].station, events[0].address, events[0].bit) == (
                None,
                118,
                3,
            )
            assert events[0].batch_id == "batch-1" and events[0].batch_label == "Line A"
            assert "equipment/site-1" in events[0].definition
            assert monitor.snapshot().active[0].label("de") == "Luftdruck niedrig"
            initial = monitor.snapshot()
            assert initial.raw_word == 0 and initial.observed_at == 100.0
            assert initial.definitions == load_alarm_definitions(path)
            assert initial.selection == selection(peer, 118)
            definitions(
                path,
                signal(id="new", bit=15, translations={"de": "Neuer Alarm"}),
                version="site-2",
            )
            peer.set_words(118, (12,))
            wait(lambda: len(events) == 3)
            assert [(e.signal, e.state) for e in events[1:]] == [
                ("air", "recovered"),
                ("stop", "activated"),
            ]
            wait(lambda: len(peer.packets) >= 5)
            assert len(events) == 3
            peer.fail_next("read", "disconnect")
            wait(lambda: monitor.snapshot().state == "partial")
            assert [s.id for s in monitor.snapshot().active] == ["stop"]
            stale = monitor.snapshot()
            assert stale.raw_word == 12 and stale.observed_at == 100.0
            assert stale.definitions == initial.definitions and stale.issue
            peer.set_words(118, (0,))
            wait(lambda: len(events) == 5)
            assert [e.state for e in events[-2:]] == [
                "observed_active",
                "observed_recovered",
            ]
            assert all(e.definition == events[0].definition for e in events)
            assert [e.label("de") for e in events if e.signal == "air"] == [
                "Luftdruck niedrig"
            ] * 3
            assert all(
                p[10:18] == bytes.fromhex("0101 b1 0076 00 0001")
                if p[10:12] == b"\x01\x01"
                else p[10:18]
                in (
                    bytes.fromhex("0102 b1 002f 00 0001"),
                    bytes.fromhex("0102 b1 002e 00 0001"),
                )
                for p in peer.packets
            )
            monitor.apply_saved(StatusPreferences())
            assert monitor.snapshot().raw_word is None
            assert monitor.snapshot().observed_at is None
            assert monitor.snapshot().definitions is None
            peer.set_words(18, (1 << 15,))
            monitor.apply_saved(selection(peer))
            wait(lambda: len(events) == 6)
            assert events[-1].signal == "new" and events[-1].address == 18
            assert "site-2" in events[-1].definition
            assert events[-1].label("de") == "Neuer Alarm"
        finally:
            monitor.request_stop()
            assert monitor.join(2)
            writer.close()
    assert [r["event"] for r in observations(tmp_path / "logs")] == [
        "alarm_monitor.unavailable",
        "alarm_monitor.recovered",
    ]


@pytest.mark.parametrize("address", (0, 18, 77, 118, 511))
def test_explicit_alarm_word_roundtrip(tmp_path: Path, address: int) -> None:
    path = tmp_path / "communication.json"
    alarm = StatusPreferences(enabled=True, word_address=address)
    preferences = CommunicationPreferences(status=alarm)
    save_communication_preferences(path, preferences)
    assert load_communication_preferences(path) == preferences
    encoded = json.loads(path.read_text(encoding="utf-8"))["status"]
    assert encoded["word_address"] == address and "station" not in encoded


def test_example_definitions_and_explicit_word_activation(tmp_path: Path) -> None:
    examples = Path(__file__).parents[1] / "fixtures" / "alarms"
    standard = load_alarm_definitions(examples / "standard-11.json")
    alternate = load_alarm_definitions(examples / "alternate-6.json")
    assert (examples / "standard-11.json").read_bytes() == TEMPLATE.read_bytes()
    assert len(standard.signals) == 11 and len(alternate.signals) == 6
    assert [s.bit for s in alternate.signals] == [0, 2, 5, 8, 12, 15]
    assert alternate.signals[0].id == standard.signals[0].id
    assert alternate.signals[0].en != standard.signals[0].en
    assert "emergency_stop" not in {s.id for s in alternate.signals}
    assert alternate.signals[1].label("de") == "Schutztür offen"
    assert alternate.signals[1].label("it") == "Guard door open"
    path = tmp_path / "alarm_definitions.json"
    path.write_bytes((examples / "standard-11.json").read_bytes())
    events: list[AlarmEvent] = []
    monitor = AlarmMonitor(
        path,
        events.extend,
        lambda: (123.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=0.025,
    )
    with TcpPeer() as peer:
        selected = replace(selection(peer, 118), word_address=211)
        peer.set_words(211, (1,))
        try:
            monitor.apply_saved(selected)
            wait(lambda: monitor.snapshot().state == "online")
            assert monitor.snapshot().definitions == standard
            path.write_bytes((examples / "alternate-6.json").read_bytes())
            monitor.apply_saved(selected)
            assert monitor.snapshot().definitions == standard
            monitor.apply_saved(replace(selected, enabled=False))
            assert monitor.snapshot().selection.word_address == 211
            monitor.apply_saved(selected)
            wait(
                lambda: (
                    monitor.snapshot().definitions == alternate
                    and monitor.snapshot().state == "online"
                )
            )
            assert [s.id for s in monitor.snapshot().active] == [
                "reciprocating_axis_alarm",
                "guard_open",
                "low_air_pressure",
            ]
            assert all(e.address == 211 and e.station is None for e in events)
            assert all(
                p[10:18] == bytes.fromhex("0101 b1 00d3 00 0001")
                if p[10:12] == b"\x01\x01"
                else p[10:18]
                in (
                    bytes.fromhex("0102 b1 002f 00 0001"),
                    bytes.fromhex("0102 b1 002e 00 0001"),
                )
                for p in peer.packets
            )
        finally:
            monitor.request_stop()
            assert monitor.join(2)


def test_invalid_asset_never_contacts_plc_and_requires_reactivation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alarm_definitions.json"
    path.write_bytes(b"{bad")
    monitor = AlarmMonitor(
        path,
        lambda _: None,
        lambda: (100.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=0.02,
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
            monitor.apply_saved(StatusPreferences())
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
        state=lambda: ApplicationState.READY,
        interval=0.03,
    )
    with TcpPeer(dual_session=True) as peer, owner(peer) as reporting:
        peer.set_words(18, (4,))
        peer.hold_next("read")
        try:
            monitor.apply_saved(
                StatusPreferences(True, FinsTcpEndpoint(port=peer.port, timeout=1.0))
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
                StatusPreferences()
            )  # The held reply belongs to the previous selection.
            peer.release.set()
            writes = [
                int.from_bytes(p[13:15])
                for p in peer.packets
                if p[10:12] == b"\x01\x02"
            ]
            assert writes == [47, 46] + [50, 49] * 8
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


def test_legacy_alarm_history_reads_without_writing_and_freezes_new_translations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path)
    try:
        batch = history.select_batch("Original Batch").result(3)
        assert batch is not None
    finally:
        history.request_stop()
        assert history.join(3)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE alarm_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL,
                signal TEXT NOT NULL, state TEXT NOT NULL, category TEXT NOT NULL,
                zh TEXT NOT NULL, en TEXT NOT NULL, source TEXT NOT NULL,
                station INTEGER, address INTEGER, bit INTEGER, definition TEXT NOT NULL,
                batch_id TEXT, batch_label TEXT, detail TEXT NOT NULL);
            INSERT INTO alarm_events VALUES
                (17,100,'air','activated','equipment','低气压','Original air alarm','PLC',1,18,3,
                 'equipment/old',NULL,NULL,'');
        """)
    history = ProductionHistory(path, clock=lambda: 100, alarm_access=lambda _: 0)
    try:
        assert history.initial_selection.result(3) == batch
        (old,) = history.alarm_page(AlarmQuery()).result(3)
        assert old.cursor == 17 and old.event.label("ja") == "Original air alarm"
        assert (old.event.station, old.event.address) == (1, 18)
    finally:
        history.request_stop()
        assert history.join(3)
    with closing(sqlite3.connect(path)) as db:
        assert "translations" not in [
            row[1] for row in db.execute("PRAGMA table_info(alarm_events)")
        ]
    local = tmp_path / "alarm_definitions.json"
    definitions(local, signal(translations={"ja": "旧ラベル"}))
    history = ProductionHistory(path, clock=lambda: 100, alarm_access=lambda _: 0)
    try:
        definitions_at_event = load_alarm_definitions(local)
        alarm = definitions_at_event.signals[0]
        history.offer_alarms(
            (
                replace(
                    event(),
                    signal=alarm.id,
                    zh=alarm.zh,
                    en=alarm.en,
                    translations=alarm.translations,
                    definition=definitions_at_event.digest,
                ),
            )
        )
        definitions(
            local, signal(en="Changed live name", translations={"ja": "新ラベル"})
        )
        assert [row.cursor for row in history.alarm_page(AlarmQuery()).result(3)] == [
            18,
            17,
        ]
    finally:
        history.request_stop()
        assert history.join(3)
    history = ProductionHistory(path, clock=lambda: 100, alarm_access=lambda _: 0)
    try:
        assert history.initial_selection.result(3) == batch
        new, original = history.alarm_page(AlarmQuery()).result(3)
        assert original == old and new.event.definition == definitions_at_event.digest
        assert (
            new.event.label("ja") == "旧ラベル"
            and new.event.label("fr") == "Low air pressure"
        )
        assert load_alarm_definitions(local).signals[0].label("ja") == "新ラベル"
    finally:
        history.request_stop()
        assert history.join(3)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)


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
