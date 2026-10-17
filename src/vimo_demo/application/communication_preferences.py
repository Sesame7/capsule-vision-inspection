"""Operator communication preferences, separate from live owners/observations."""

import json
import math
import os
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from vimo_demo.communication import FinsAddress, FinsTransport
from vimo_demo.communication.reporting import LocalReportingSettings, ReportingSelection
from vimo_demo.configuration import ConfigurationError, ConfigurationIOError


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
    diagnostic: LocalReportingSettings = LocalReportingSettings()
    web: WebPreferences = WebPreferences()

    def __post_init__(self) -> None:
        for endpoint in (self.reporting.settings, self.diagnostic):
            if not 0.05 <= endpoint.timeout <= 30:
                raise ValueError("timeout must be between 0.05 and 30 seconds")


def _object(value: object, fields: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    # JSON objects have string keys; keep the untyped decoder at this boundary.
    result = cast(dict[str, object], value)
    if set(result) != fields:
        raise ValueError(f"{context} requires exactly {sorted(fields)}")
    return result


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate field: {key}")
        result[key] = value
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


def _address(value: object) -> FinsAddress:
    obj = _object(value, {"network", "node", "unit"}, "FINS address")
    return FinsAddress(
        *(_integer(obj[key], key) for key in ("network", "node", "unit"))
    )


def _endpoint(value: object) -> LocalReportingSettings:
    obj = _object(
        value,
        {"host", "port", "timeout", "transport", "destination", "source"},
        "endpoint",
    )
    timeout = obj["timeout"]
    if type(timeout) not in (int, float) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be numeric")
    if not math.isfinite(timeout) or not 0.05 <= timeout <= 30:
        raise ValueError("timeout must be between 0.05 and 30 seconds")
    return LocalReportingSettings(
        host=_text(obj["host"], "host"),
        port=_integer(obj["port"], "port"),
        timeout=float(timeout),
        transport=FinsTransport(_text(obj["transport"], "transport")),
        destination=_address(obj["destination"]),
        source=_address(obj["source"]),
    )


def load_communication_preferences(path: Path) -> CommunicationPreferences:
    """Missing/invalid files are reported; the startup owner chooses recovery."""
    try:
        raw: object = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique
        )
        obj = _object(
            raw, {"schema_version", "reporting", "diagnostic", "web"}, "communication"
        )
        if _integer(obj["schema_version"], "schema_version") != 1:
            raise ValueError("Unsupported communication schema_version")
        reporting = _object(obj["reporting"], {"enabled", "endpoint"}, "reporting")
        web = _object(obj["web"], {"enabled", "host", "port", "interval_ms"}, "web")
        return CommunicationPreferences(
            ReportingSelection(
                _boolean(reporting["enabled"], "reporting.enabled"),
                _endpoint(reporting["endpoint"]),
            ),
            _endpoint(obj["diagnostic"]),
            WebPreferences(
                _boolean(web["enabled"], "web.enabled"),
                _text(web["host"], "web.host"),
                _integer(web["port"], "web.port"),
                _integer(web["interval_ms"], "web.interval_ms"),
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


def _endpoint_json(settings: LocalReportingSettings) -> dict[str, object]:
    return {
        "host": settings.host,
        "port": settings.port,
        "timeout": settings.timeout,
        "transport": settings.transport.value,
        "destination": _address_json(settings.destination),
        "source": _address_json(settings.source),
    }


def _address_json(address: FinsAddress) -> dict[str, int]:
    return {"network": address.network, "node": address.node, "unit": address.unit}


def save_communication_preferences(path: Path, value: CommunicationPreferences) -> None:
    """Atomic replacement of definitions only; no sockets, ACKs or runtime state."""
    payload = {
        "schema_version": 1,
        "reporting": {
            "enabled": value.reporting.enabled,
            "endpoint": _endpoint_json(value.reporting.settings),
        },
        "diagnostic": _endpoint_json(value.diagnostic),
        "web": {
            "enabled": value.web.enabled,
            "host": value.web.host,
            "port": value.web.port,
            "interval_ms": value.web.interval_ms,
        },
    }
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f"Temporary settings cleanup: {cleanup_error}")
        raise ConfigurationIOError(
            f"Cannot save communication settings {path}: {exc}"
        ) from exc
