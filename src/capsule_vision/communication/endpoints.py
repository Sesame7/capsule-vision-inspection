"""Immutable protocol-specific connection values; no connection owners or registry."""

import math
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Literal

from .fins import FinsAddress, FinsUdpClient, validate_address, validate_integer
from .fins_tcp import FinsTcpClient
from .modbus_tcp import ModbusSettings, ModbusTcpClient


def _validate_fins_target(host: str, port: int, timeout: float) -> None:
    address = IPv4Address(host)
    if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
        raise ValueError("Target must be a numeric unicast IPv4 address")
    validate_integer("port", port, 1, 65535)
    if (
        isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0.05 <= timeout <= 30
    ):
        raise ValueError("timeout must be in 0.05..30 seconds")


@dataclass(frozen=True, slots=True)
class FinsUdpEndpoint:
    host: str = "127.0.0.1"
    port: int = 9600
    timeout: float = 1.0
    destination: FinsAddress = FinsAddress(0, 2, 0)
    source: FinsAddress = FinsAddress(0, 1, 0)

    def __post_init__(self) -> None:
        _validate_fins_target(self.host, self.port, self.timeout)
        validate_address(self.destination)
        validate_address(self.source)

    @property
    def protocol(self) -> Literal["fins_udp"]:
        return "fins_udp"


@dataclass(frozen=True, slots=True)
class FinsTcpEndpoint:
    host: str = "127.0.0.1"
    port: int = 9600
    timeout: float = 1.0

    def __post_init__(self) -> None:
        _validate_fins_target(self.host, self.port, self.timeout)

    @property
    def protocol(self) -> Literal["fins_tcp"]:
        return "fins_tcp"


type Endpoint = FinsUdpEndpoint | FinsTcpEndpoint | ModbusSettings


def validate_endpoint(value: object) -> Endpoint:
    if not isinstance(value, (FinsUdpEndpoint, FinsTcpEndpoint, ModbusSettings)):
        raise ValueError("endpoint must be a protocol-specific endpoint value")
    return value


def protocol_name(endpoint: Endpoint) -> str:
    return (
        f"Modbus TCP · Unit ID {endpoint.unit_id}"
        if isinstance(endpoint, ModbusSettings)
        else "FINS/TCP"
        if isinstance(endpoint, FinsTcpEndpoint)
        else "FINS/UDP"
    )


def open_client(endpoint: Endpoint) -> FinsUdpClient | FinsTcpClient | ModbusTcpClient:
    """Construct one existing serial client; the caller owns its actual close."""
    if isinstance(endpoint, ModbusSettings):
        return ModbusTcpClient(
            endpoint.host,
            port=endpoint.port,
            timeout=endpoint.timeout,
            unit_id=endpoint.unit_id,
        )
    if isinstance(endpoint, FinsTcpEndpoint):
        return FinsTcpClient(
            endpoint.host, port=endpoint.port, timeout=endpoint.timeout
        )
    return FinsUdpClient(
        endpoint.host,
        port=endpoint.port,
        timeout=endpoint.timeout,
        destination=endpoint.destination,
        source=endpoint.source,
    )
