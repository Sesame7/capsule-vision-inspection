"""Strict global settings codec; no sockets, Qt or production resources."""

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from capsule_vision.communication import (
    FinsAddress,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
)
from capsule_vision.communication.modbus_tcp import ModbusSettings
from capsule_vision.communication.reporting import (
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.configuration import ConfigurationError, ConfigurationIOError
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    load_communication_preferences,
    save_communication_preferences,
)


@pytest.mark.parametrize("base", [0, 49, 65532])
def test_modbus_reporting_sparse_round_trip_and_independent_diagnostics(
    tmp_path: Path, base: int
) -> None:
    path = tmp_path / "communication.json"
    path.write_text(
        json.dumps(
            {
                "reporting": {
                    "protocol": "modbus_tcp",
                    "host": "192.0.2.10",
                    "enabled": True,
                    "base_address": base,
                }
            }
        ),
        encoding="utf-8",
    )
    value = load_communication_preferences(path)
    assert value.reporting == ReportingSelection(
        True,
        ReportingSettings(
            base_address=base, endpoint=ModbusSettings(host="192.0.2.10")
        ),
    )
    assert isinstance(value.reporting.settings.endpoint, ModbusSettings)
    assert (
        value.reporting.settings.endpoint.port == 502
        and value.reporting.settings.endpoint.unit_id == 255
    )
    save_communication_preferences(path, value)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "reporting": {
            "protocol": "modbus_tcp",
            "host": "192.0.2.10",
            "enabled": True,
            "base_address": base,
        }
    }
    value = replace(
        value,
        reporting=ReportingSelection(
            True,
            replace(
                value.reporting.settings,
                endpoint=replace(
                    value.reporting.settings.endpoint, port=1502, timeout=2, unit_id=7
                ),
            ),
        ),
        diagnostic=ModbusSettings(unit_id=17),
    )
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    assert json.loads(path.read_text(encoding="utf-8"))["reporting"] == {
        "protocol": "modbus_tcp",
        "host": "192.0.2.10",
        "port": 1502,
        "timeout": 2,
        "unit_id": 7,
        "base_address": base,
        "enabled": True,
    }


def test_disabled_modbus_reporting_can_be_saved_without_choosing_a_base(
    tmp_path: Path,
) -> None:
    path = tmp_path / "communication.json"
    value = CommunicationPreferences(
        reporting=ReportingSelection(
            False, ReportingSettings(endpoint=ModbusSettings())
        )
    )
    save_communication_preferences(path, value)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "reporting": {"protocol": "modbus_tcp"}
    }
    assert load_communication_preferences(path) == value


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_address", -1),
        ("base_address", 65533),
        ("base_address", True),
        ("base_address", None),
        ("base_address", "100"),
        ("unit_id", -1),
        ("unit_id", 256),
        ("unit_id", True),
        ("unit_id", "7"),
        ("source", {}),
        ("destination", {}),
        ("port", None),
        ("port", 0),
        ("timeout", True),
        ("timeout", 31),
        ("host", "localhost"),
        ("schema_version", 1),
    ],
)
def test_modbus_reporting_strict_fields_and_bounds(
    tmp_path: Path, field: str, value: object
) -> None:
    path = tmp_path / "communication.json"
    obj: dict[str, object] = {
        "protocol": "modbus_tcp",
        "enabled": True,
        "base_address": 100,
        field: value,
    }
    path.write_text(json.dumps({"reporting": obj}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    assert path.read_bytes() == before


def test_modbus_sparse_diagnostic_defaults_and_explicit_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "communication.json"
    path.write_text(
        '{"diagnostic":{"protocol":"modbus_tcp","host":"192.168.1.20"}}',
        encoding="utf-8",
    )
    value = load_communication_preferences(path)
    assert value.diagnostic == ModbusSettings("192.168.1.20")
    assert value.reporting == ReportingSelection()
    save_communication_preferences(path, value)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "diagnostic": {"protocol": "modbus_tcp", "host": "192.168.1.20"}
    }
    value = replace(value, diagnostic=ModbusSettings(port=1502, timeout=2, unit_id=7))
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    assert json.loads(path.read_text(encoding="utf-8"))["diagnostic"] == {
        "protocol": "modbus_tcp",
        "port": 1502,
        "timeout": 2,
        "unit_id": 7,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"reporting":{"protocol":"modbus_tcp","enabled":true}}',
        '{"reporting":{"unit_id":255}}',
        '{"diagnostic":{"protocol":"fins_udp","unit_id":1}}',
        '{"diagnostic":{"protocol":"fins_tcp","unit_id":1}}',
        '{"diagnostic":{"protocol":"modbus_tcp","source":{}}}',
        '{"diagnostic":{"protocol":"modbus_tcp","destination":{}}}',
        '{"diagnostic":{"protocol":"modbus_tcp","base_address":49}}',
        '{"diagnostic":{"protocol":"modbus_tcp","unit_id":1,"unit_id":2}}',
        '{"diagnostic":{"protocol":"modbus_tcp","unit_id":true}}',
        '{"diagnostic":{"protocol":"modbus_tcp","unit_id":256}}',
        '{"diagnostic":{"protocol":"modbus_tcp","unit_id":-1}}',
        '{"diagnostic":{"protocol":"modbus_tcp","unit_id":"1"}}',
        '{"diagnostic":{"protocol":"modbus_tcp","timeout":false}}',
        '{"diagnostic":{"protocol":"modbus_tcp","timeout":31}}',
        '{"diagnostic":{"protocol":"modbus_tcp","port":0}}',
        '{"diagnostic":{"protocol":"modbus_tcp","port":true}}',
        '{"diagnostic":{"protocol":"modbus_tcp","host":"localhost"}}',
        '{"diagnostic":{"protocol":"modbus_tcp","host":"0.0.0.0"}}',
    ],
)
def test_modbus_protocol_specific_strict_rejection(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "communication.json"
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("endpoint_type", [FinsUdpEndpoint, FinsTcpEndpoint])
def test_independent_endpoint_round_trip_and_definitions_only(
    tmp_path: Path, endpoint_type: type[FinsUdpEndpoint] | type[FinsTcpEndpoint]
) -> None:
    path = tmp_path / "data" / "communication.json"
    value = CommunicationPreferences(
        ReportingSelection(
            True,
            ReportingSettings(endpoint=endpoint_type(host="192.0.2.10")),
        ),
        FinsUdpEndpoint(port=19600, destination=FinsAddress(2, 23, 5)),
        WebPreferences(False, "127.0.0.1", 8766, 2000),
    )
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    encoded = path.read_text(encoding="utf-8")
    assert "acknowledged" not in encoded and "connected" not in encoded
    assert set(json.loads(encoded)) == {"reporting", "diagnostic", "web"}
    assert set(path.parent.iterdir()) == {path}


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("root", "schema_version", True),
        ("root", "schema_version", 2),
        ("root", "unknown", 1),
        ("reporting", "enabled", "true"),
        ("endpoint", "host", "localhost"),
        ("endpoint", "port", True),
        ("endpoint", "protocol", "modbus_rtu"),
        ("endpoint", "protocol", "udp"),
        ("endpoint", "transport", "udp"),
        ("endpoint", "endpoint", {}),
        ("endpoint", "base_address", True),
        ("endpoint", "base_address", -1),
        ("endpoint", "base_address", 509),
        ("endpoint", "timeout", float("nan")),
        ("endpoint", "timeout", True),
        ("endpoint", "timeout", 31),
        ("endpoint", "destination", {"network": 0, "node": 0, "unit": 0}),
        ("diagnostic", "port", 0),
        ("web", "enabled", 1),
        ("web", "host", "0.0.0.0"),
        ("web", "host", "224.0.0.1"),
        ("web", "port", 65536),
        ("web", "interval_ms", 100),
    ],
)
def test_invalid_preferences_rejected_without_rewriting(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    path = tmp_path / "communication.json"
    save_communication_preferences(path, CommunicationPreferences())
    obj = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    target = (
        obj
        if section == "root"
        else cast(
            dict[str, object],
            obj.setdefault("reporting" if section == "endpoint" else section, {}),
        )
    )
    target[field] = value
    path.write_text(json.dumps(obj), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        '{"web":{},"web":{}}',
        "[]",
        "{",
        '{"reporting":{"protocol":"fins_tcp","source":{}}}',
        '{"diagnostic":{"protocol":"fins_tcp","destination":{}}}',
    ],
)
def test_duplicate_missing_malformed_fields(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "communication.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)


def test_missing_file_and_failed_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "communication.json"
    with pytest.raises(ConfigurationIOError) as missing:
        load_communication_preferences(path)
    assert isinstance(missing.value.__cause__, FileNotFoundError)
    original = CommunicationPreferences()
    save_communication_preferences(path, original)
    before = path.read_bytes()

    def denied(source: object, destination: object) -> None:
        raise PermissionError("controlled atomic replacement failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    with pytest.raises(ConfigurationIOError, match="controlled atomic"):
        save_communication_preferences(
            path, replace(original, web=WebPreferences(enabled=False))
        )
    assert path.read_bytes() == before and set(tmp_path.iterdir()) == {path}


def test_sparse_defaults_and_protocol_specific_endpoint_round_trip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "communication.json"
    defaults = CommunicationPreferences()
    save_communication_preferences(path, defaults)
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    assert load_communication_preferences(path) == defaults
    path.write_text('{"reporting":{},"diagnostic":{},"web":{}}', encoding="utf-8")
    assert load_communication_preferences(path) == defaults
    value = replace(
        defaults,
        reporting=ReportingSelection(
            True,
            ReportingSettings(
                base_address=120, endpoint=FinsTcpEndpoint(host="192.0.2.10")
            ),
        ),
        diagnostic=FinsUdpEndpoint(destination=FinsAddress(0, 7, 0)),
        web=WebPreferences(),
    )
    save_communication_preferences(path, value)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "reporting": {
            "protocol": "fins_tcp",
            "host": "192.0.2.10",
            "enabled": True,
            "base_address": 120,
        },
        "diagnostic": {"protocol": "fins_udp", "destination": {"node": 7}},
        "web": {"enabled": True},
    }
    assert load_communication_preferences(path) == value
    # Switching and explicitly saving drops inapplicable routing fields.
    udp = replace(
        value,
        reporting=ReportingSelection(
            False,
            ReportingSettings(
                endpoint=FinsUdpEndpoint(destination=FinsAddress(2, 8, 3))
            ),
        ),
    )
    save_communication_preferences(path, udp)
    assert '"destination"' in path.read_text(encoding="utf-8")
    tcp = replace(
        udp,
        reporting=replace(
            udp.reporting,
            settings=replace(
                udp.reporting.settings,
                endpoint=FinsTcpEndpoint(
                    host=udp.reporting.settings.endpoint.host,
                    port=udp.reporting.settings.endpoint.port,
                    timeout=udp.reporting.settings.endpoint.timeout,
                ),
            ),
        ),
    )
    save_communication_preferences(path, tcp)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["reporting"] == {"protocol": "fins_tcp"}
    assert load_communication_preferences(path) == tcp
