"""Strict global settings codec; no sockets, Qt or production resources."""

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from vimo_demo.application.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from vimo_demo.communication import FinsAddress, FinsTransport
from vimo_demo.communication.reporting import LocalReportingSettings, ReportingSelection
from vimo_demo.configuration import ConfigurationError, ConfigurationIOError


def test_independent_endpoint_round_trip_and_definitions_only(tmp_path: Path) -> None:
    path = tmp_path / "data" / "communication.json"
    value = CommunicationPreferences(
        ReportingSelection(
            True, LocalReportingSettings(host="192.0.2.10", transport=FinsTransport.TCP)
        ),
        LocalReportingSettings(port=19600, destination=FinsAddress(2, 23, 5)),
        WebPreferences(False, "127.0.0.1", 8766, 2000),
    )
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    encoded = path.read_text(encoding="utf-8")
    assert "acknowledged" not in encoded and "connected" not in encoded
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
        ("endpoint", "transport", "modbus"),
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
            dict[str, object], obj["reporting" if section == "endpoint" else section]
        )
    )
    if section == "endpoint":
        target = cast(dict[str, object], target["endpoint"])
    target[field] = value
    path.write_text(json.dumps(obj), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigurationError):
        load_communication_preferences(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw", ['{"schema_version":1,"schema_version":1}', "{}", "[]", "{"]
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

    monkeypatch.setattr(
        "vimo_demo.application.communication_preferences.os.replace", denied
    )
    with pytest.raises(ConfigurationIOError, match="controlled atomic"):
        save_communication_preferences(
            path, replace(original, web=WebPreferences(enabled=False))
        )
    assert path.read_bytes() == before and set(tmp_path.iterdir()) == {path}
