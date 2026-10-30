"""Operator communication preferences, separate from live owners/observations."""

import json
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import cast

from capsule_vision.communication import FinsAddress
from capsule_vision.communication.endpoints import (
    Endpoint,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
)
from capsule_vision.communication.modbus_tcp import ModbusSettings
from capsule_vision.communication.reporting import (
    ReportingSelection,
    ReportingSettings,
)

from ._io import atomic_write, read_json
from .errors import ConfigurationError, ConfigurationIOError


@dataclass(frozen=True, slots=True)
class WebPreferences:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    interval_ms: int = 500

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Web enabled must be bool")
        address = IPv4Address(self.host)
        if self.host != "127.0.0.1" and (
            address.is_multicast
            or address.is_reserved
            or address.is_loopback
            or int(address) >> 24 == 0
        ):
            raise ValueError("Web host must be a specific local unicast IPv4")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Web port must be between 1 and 65535")
        if type(self.interval_ms) is not int or self.interval_ms not in (
            500,
            1000,
            2000,
        ):
            raise ValueError("Web interval_ms must be 500, 1000 or 2000")


@dataclass(frozen=True, slots=True)
class CommunicationPreferences:
    reporting: ReportingSelection = ReportingSelection()
    diagnostic: Endpoint = FinsUdpEndpoint()
    web: WebPreferences = WebPreferences(enabled=False)


def _object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    # JSON objects have string keys; keep the untyped decoder at this boundary.
    result = cast(dict[str, object], value)
    if set(result) - fields:
        raise ValueError(f"Unknown {context} fields: {sorted(set(result) - fields)}")
    return result


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be bool")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _address(value: object, default: FinsAddress) -> FinsAddress:
    obj = _object(value, {"network", "node", "unit"}, "FINS address")
    return FinsAddress(
        *(
            _integer(obj.get(key, getattr(default, key)), key)
            for key in ("network", "node", "unit")
        )
    )


def _endpoint(value: object) -> Endpoint:
    obj = _object(
        value,
        {"host", "port", "timeout", "protocol", "destination", "source", "unit_id"},
        "endpoint",
    )
    protocol = _text(obj.get("protocol", "fins_udp"), "protocol")
    if protocol not in ("fins_udp", "fins_tcp", "modbus_tcp"):
        raise ValueError("Unsupported endpoint protocol")
    if protocol != "fins_udp" and {"source", "destination"} & obj.keys():
        raise ValueError("UDP routing fields are applicable only to FINS/UDP")
    if protocol != "modbus_tcp" and "unit_id" in obj:
        raise ValueError("unit_id is applicable only to Modbus")
    timeout = obj.get("timeout", 1.0)
    if type(timeout) not in (int, float) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be numeric")
    host = _text(obj.get("host", "127.0.0.1"), "host")
    port = _integer(obj.get("port", 502 if protocol == "modbus_tcp" else 9600), "port")
    if protocol == "modbus_tcp":
        return ModbusSettings(
            host, port, float(timeout), _integer(obj.get("unit_id", 255), "unit_id")
        )
    if protocol == "fins_tcp":
        return FinsTcpEndpoint(host, port, float(timeout))
    defaults = FinsUdpEndpoint()
    return FinsUdpEndpoint(
        host,
        port,
        float(timeout),
        _address(obj.get("destination", {}), defaults.destination),
        _address(obj.get("source", {}), defaults.source),
    )


def _reporting(value: dict[str, object]) -> ReportingSettings:
    return ReportingSettings(
        _endpoint(
            {
                key: item
                for key, item in value.items()
                if key not in ("enabled", "base_address")
            }
        ),
        _integer(value["base_address"], "base_address")
        if "base_address" in value
        else None,
    )


def load_communication_preferences(path: Path) -> CommunicationPreferences:
    """Missing/invalid files are reported; the startup owner chooses recovery."""
    try:
        raw = read_json(path, duplicate_message="Duplicate field: {key}")
        obj = _object(raw, {"reporting", "diagnostic", "web"}, "communication")
        reporting = _object(
            obj.get("reporting", {}),
            {
                "enabled",
                "protocol",
                "host",
                "port",
                "timeout",
                "destination",
                "source",
                "base_address",
                "unit_id",
            },
            "reporting",
        )
        web = _object(
            obj.get("web", {}), {"enabled", "host", "port", "interval_ms"}, "web"
        )
        defaults = CommunicationPreferences()
        return CommunicationPreferences(
            ReportingSelection(
                _boolean(reporting.get("enabled", False), "reporting.enabled"),
                _reporting(reporting),
            ),
            _endpoint(obj.get("diagnostic", {})),
            WebPreferences(
                _boolean(web.get("enabled", defaults.web.enabled), "web.enabled"),
                _text(web.get("host", defaults.web.host), "web.host"),
                _integer(web.get("port", defaults.web.port), "web.port"),
                _integer(
                    web.get("interval_ms", defaults.web.interval_ms), "web.interval_ms"
                ),
            ),
        )
    except OSError as exc:
        raise ConfigurationIOError(
            f"Cannot read communication settings {path}: {exc}"
        ) from exc
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"Invalid communication settings {path}: {exc}"
        ) from exc


def _endpoint_json(settings: Endpoint) -> dict[str, object]:
    default = (
        ModbusSettings() if isinstance(settings, ModbusSettings) else FinsUdpEndpoint()
    )
    result: dict[str, object] = {"protocol": settings.protocol}
    for field in ("host", "port", "timeout"):
        if getattr(settings, field) != getattr(default, field):
            result[field] = getattr(settings, field)
    if isinstance(settings, ModbusSettings):
        assert isinstance(default, ModbusSettings)
        if settings.unit_id != default.unit_id:
            result["unit_id"] = settings.unit_id
    elif isinstance(settings, FinsUdpEndpoint):
        assert isinstance(default, FinsUdpEndpoint)
        for field in ("destination", "source"):
            address = _address_json(getattr(settings, field), getattr(default, field))
            if address:
                result[field] = address
    return result


def _address_json(address: FinsAddress, default: FinsAddress) -> dict[str, int]:
    return {
        field: getattr(address, field)
        for field in ("network", "node", "unit")
        if getattr(address, field) != getattr(default, field)
    }


def save_communication_preferences(path: Path, value: CommunicationPreferences) -> None:
    """Atomic replacement of definitions only; no sockets, ACKs or runtime state."""
    defaults = CommunicationPreferences()
    payload: dict[str, object] = {}
    if value.reporting != defaults.reporting:
        reporting = _endpoint_json(value.reporting.settings.endpoint)
        if value.reporting.enabled:
            reporting["enabled"] = True
        if value.reporting.settings.base_address is not None and (
            isinstance(value.reporting.settings.endpoint, ModbusSettings)
            or value.reporting.settings.base_address != 49
        ):
            reporting["base_address"] = value.reporting.settings.base_address
        payload["reporting"] = reporting
    if value.diagnostic != defaults.diagnostic:
        payload["diagnostic"] = _endpoint_json(value.diagnostic)
    web = {
        field: getattr(value.web, field)
        for field in ("enabled", "host", "port", "interval_ms")
        if getattr(value.web, field) != getattr(defaults.web, field)
    }
    if web:
        payload["web"] = web
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise ConfigurationIOError(
            f"Cannot save communication settings {path}: {exc}"
        ) from exc
