"""Session-local register requests/watch definitions; no Qt or scheduling."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from ipaddress import IPv4Address
from time import perf_counter

from .fins import (
    MAX_READ_WORDS,
    MAX_WRITE_WORDS,
    FinsAddress,
    FinsMemoryArea,
    FinsTransport,
    FinsUdpClient,
    validate_integer,
    validate_transport,
    validate_word_range,
)
from .fins_tcp import FinsTcpClient

MAX_WATCH_GROUPS = 16
MAX_WATCH_WORDS = 2048


@dataclass(frozen=True, slots=True)
class RegisterWatchGroup:
    """One consecutive read; session-local name, no merging or PLC meaning."""

    area: FinsMemoryArea
    start: int
    count: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        validate_word_range(self.start, self.count, MAX_READ_WORDS, self.area)
        if len(self.name) > 64:
            raise ValueError("Watch name must contain at most 64 characters")


class RequestKind(Enum):
    TEST = "test"
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class CommunicationRequest:
    kind: RequestKind
    host: str
    port: int
    timeout: float
    destination: FinsAddress
    source: FinsAddress
    start: int
    count: int = 1
    values: tuple[int, ...] = ()
    area: FinsMemoryArea = FinsMemoryArea.DM
    transport: FinsTransport = FinsTransport.UDP

    def validate(self) -> None:
        """Cheap dispatch validation, repeated at the execution boundary."""
        validate_transport(self.transport)
        address = IPv4Address(self.host)
        if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
            raise ValueError("Target must be a numeric unicast IPv4 address")
        validate_integer("port", self.port, 1, 65535)
        if (
            isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or not 0.05 <= self.timeout <= 30
        ):
            raise ValueError("Request timeout must be in 0.05..30 seconds")
        validate_word_range(
            self.start,
            self.count,
            MAX_WRITE_WORDS if self.kind is RequestKind.WRITE else MAX_READ_WORDS,
            self.area,
        )
        if self.kind is RequestKind.WRITE:
            validate_integer("write count", len(self.values), 1, MAX_WRITE_WORDS)
            if self.count != len(self.values):
                raise ValueError("Write count must equal the number of values")
            for value in self.values:
                validate_integer("word", value, 0, 65535)
        else:
            validate_integer("read count", self.count, 1, MAX_READ_WORDS)
            if self.values or (self.kind is RequestKind.TEST and self.count != 1):
                raise ValueError(
                    "Read requests carry no write values; test reads one word"
                )


@dataclass(frozen=True, slots=True)
class CommunicationResponse:
    request: CommunicationRequest
    values: tuple[int, ...] | None
    elapsed_ms: float
    completed_at: datetime


def execute_request(request: CommunicationRequest) -> CommunicationResponse:
    """One caller constructs, uses and closes the selected client, without retries.

    Protocol parameters are validated before socket creation. A write response
    is acknowledgement, never implicit readback or PLC consumption.
    """
    request.validate()
    started = perf_counter()
    connection = (
        FinsTcpClient(request.host, port=request.port, timeout=request.timeout)
        if request.transport is FinsTransport.TCP
        else FinsUdpClient(
            request.host,
            port=request.port,
            timeout=request.timeout,
            destination=request.destination,
            source=request.source,
        )
    )
    with connection as client:
        if request.kind is RequestKind.WRITE:
            client.write_words(request.start, request.values, area=request.area)
            values = None
        else:
            values = client.read_words(request.start, request.count, area=request.area)
    return CommunicationResponse(
        request, values, (perf_counter() - started) * 1000, datetime.now(UTC)
    )
