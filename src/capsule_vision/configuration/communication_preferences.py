"""Operator communication preferences, separate from live owners/observations."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import cast

from capsule_vision.communication import FinsAddress
from capsule_vision.communication.endpoints import (
    Endpoint,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
    validate_endpoint,
)
from capsule_vision.communication.fins import FinsMemoryArea, validate_word_range
from capsule_vision.communication.modbus_tcp import ModbusSettings
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)

from ._io import ConfigurationPublication, decode_json, read_json
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
class StatusPreferences:
    enabled: bool = False
    endpoint: FinsTcpEndpoint = FinsTcpEndpoint()
    word_address: int = 18
    heartbeat_address: int = 46
    fault_address: int = 47
    running_address: int = 16

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Expected status enabled flag")
        if not isinstance(validate_endpoint(self.endpoint), FinsTcpEndpoint):
            raise ValueError("PLC status requires FINS/TCP")
        if self.endpoint.timeout > 1.0:
            raise ValueError("PLC status timeout must be at most one second")
        addresses = (self.word_address, self.heartbeat_address, self.fault_address)
        for address in addresses:
            validate_word_range(address, 1, 1, FinsMemoryArea.W)
        if len(set(addresses)) != 3:
            raise ValueError("PLC status addresses must be distinct")
        validate_word_range(self.running_address, 1, 1, FinsMemoryArea.W)
        if self.running_address in (self.heartbeat_address, self.fault_address):
            raise ValueError("PLC running address overlaps automatic status writes")

    def validate_reporting(
        self, settings: ReportingSettings, *, running_only: bool = False
    ) -> None:
        """Reserve the configured FINS reporting window, including group counter."""
        endpoint = settings.endpoint
        if isinstance(endpoint, ModbusSettings) or (endpoint.host, endpoint.port) != (
            self.endpoint.host,
            self.endpoint.port,
        ):
            return
        base = settings.require_base_address()
        start = base if settings.mode is ReportingMode.PER_CYCLE else base - 1
        addresses = (
            (self.running_address,)
            if running_only
            else (
                self.word_address,
                self.heartbeat_address,
                self.fault_address,
            )
        )
        if any(start <= address < base + 4 for address in addresses):
            raise ValueError("PLC status ranges overlap automatic reporting")


@dataclass(frozen=True, slots=True)
class CommunicationPreferences:
    """Initial editing values; persisted omissions retain their legacy defaults."""

    reporting: ReportingSelection = ReportingSelection(
        settings=ReportingSettings(
            endpoint=FinsTcpEndpoint(), mode=ReportingMode.FOUR_CYCLE
        )
    )
    diagnostic: Endpoint = FinsTcpEndpoint()
    web: WebPreferences = WebPreferences(enabled=False)
    status: StatusPreferences = StatusPreferences()

    def __post_init__(self) -> None:
        if self.reporting.enabled and self.status.enabled:
            self.status.validate_reporting(self.reporting.settings)
        # Running is an independent read target, including with Status disabled.
        self.status.validate_reporting(self.reporting.settings, running_only=True)


# Existing documents, including {}, must keep their original UDP interpretation.
# Save compares with these schema defaults so initial TCP sections are explicit.
_PERSISTED_DEFAULTS = CommunicationPreferences(
    reporting=ReportingSelection(),
    diagnostic=FinsUdpEndpoint(),
    web=WebPreferences(enabled=False),
)


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
                if key not in ("enabled", "base_address", "mode")
            }
        ),
        _integer(value["base_address"], "base_address")
        if "base_address" in value
        else None,
        mode=ReportingMode(_text(value.get("mode", "per_cycle"), "reporting.mode")),
    )


def _status(value: object) -> StatusPreferences:
    status = _object(
        value,
        {
            "enabled",
            "word_address",
            "heartbeat_address",
            "fault_address",
            "running_address",
            "protocol",
            "host",
            "port",
            "timeout",
        },
        "status",
    )
    if "fault_address" not in status:
        raise ValueError("status.fault_address is required")
    endpoint = _endpoint(
        {
            "protocol": "fins_tcp",
            **{
                k: v
                for k, v in status.items()
                if k
                not in (
                    "enabled",
                    "word_address",
                    "heartbeat_address",
                    "fault_address",
                    "running_address",
                )
            },
        }
    )
    if not isinstance(endpoint, FinsTcpEndpoint):
        raise ValueError("PLC status requires FINS/TCP")
    return StatusPreferences(
        _boolean(status.get("enabled", False), "status.enabled"),
        endpoint,
        _integer(status.get("word_address", 18), "status.word_address"),
        _integer(status.get("heartbeat_address", 46), "status.heartbeat_address"),
        _integer(status["fault_address"], "status.fault_address"),
        _integer(status.get("running_address", 16), "status.running_address"),
    )


def load_communication_preferences(
    path: Path,
    *,
    status_error: Callable[[str], None] | None = None,
    content: bytes | None = None,
) -> CommunicationPreferences:
    """Missing/invalid files are reported; the startup owner chooses recovery."""
    try:
        raw = (
            read_json(path, duplicate_message="Duplicate field: {key}")
            if content is None
            else decode_json(content.decode("utf-8"), "Duplicate field: {key}")
        )
        obj = _object(
            raw,
            {"reporting", "diagnostic", "web", "status"},
            "communication",
        )
        reporting = _object(
            obj.get("reporting", {}),
            {
                "enabled",
                "mode",
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
        selected_reporting = ReportingSelection(
            _boolean(reporting.get("enabled", False), "reporting.enabled"),
            _reporting(reporting),
        )
        try:
            status = _status(obj["status"]) if "status" in obj else StatusPreferences()
            if selected_reporting.enabled and status.enabled:
                status.validate_reporting(selected_reporting.settings)
            status.validate_reporting(selected_reporting.settings, running_only=True)
        except (ValueError, TypeError) as exc:
            if status_error is None:
                raise
            status_error(f"Invalid PLC status settings {path}: {exc}")
            status = StatusPreferences()
            # Preserve valid reporting without creating a conflicting service.
            # The returned selection remains disabled; loading does not repair disk.
        defaults = _PERSISTED_DEFAULTS
        return CommunicationPreferences(
            selected_reporting,
            _endpoint(obj.get("diagnostic", {})),
            WebPreferences(
                _boolean(web.get("enabled", defaults.web.enabled), "web.enabled"),
                _text(web.get("host", defaults.web.host), "web.host"),
                _integer(web.get("port", defaults.web.port), "web.port"),
                _integer(
                    web.get("interval_ms", defaults.web.interval_ms), "web.interval_ms"
                ),
            ),
            status,
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
    communication_publication(path, value).publish()


def communication_publication(
    path: Path, value: CommunicationPreferences
) -> ConfigurationPublication:
    defaults = _PERSISTED_DEFAULTS
    payload: dict[str, object] = {}
    if value.reporting != defaults.reporting:
        reporting = _endpoint_json(value.reporting.settings.endpoint)
        if value.reporting.enabled:
            reporting["enabled"] = True
        if value.reporting.settings.mode is not ReportingMode.PER_CYCLE:
            reporting["mode"] = value.reporting.settings.mode.value
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
    if value.status != defaults.status:
        status: dict[str, object] = {
            **_endpoint_json(value.status.endpoint),
            "word_address": value.status.word_address,
            "heartbeat_address": value.status.heartbeat_address,
            "fault_address": value.status.fault_address,
            "running_address": value.status.running_address,
            "enabled": value.status.enabled,
        }
        payload["status"] = status
    content = (
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        .replace("\n", os.linesep)
        .encode("utf-8")
    )
    return ConfigurationPublication(
        path,
        content,
        create_parent=True,
        error_context="Cannot save communication settings",
    )
