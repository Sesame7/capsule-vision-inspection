"""Session-local manual requests; no Qt, Production, polling or persistence."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from ipaddress import IPv4Address
from time import perf_counter

from .fins import (
    DM_WORDS,
    MAX_READ_WORDS,
    MAX_WRITE_WORDS,
    FinsAddress,
    FinsUdpClient,
    validate_integer,
)


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

    def validate(self) -> None:
        """Cheap dispatch validation, repeated at the execution boundary."""
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
        validate_integer("start", self.start, 0, DM_WORDS - 1)
        validate_integer(
            "count",
            self.count,
            1,
            MAX_WRITE_WORDS if self.kind is RequestKind.WRITE else MAX_READ_WORDS,
        )
        if self.kind is RequestKind.WRITE:
            if self.host != "127.0.0.1":
                raise PermissionError("Writes require the exact target 127.0.0.1")
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
        if self.start + self.count > DM_WORDS:
            raise ValueError("Word range exceeds D32767")


@dataclass(frozen=True, slots=True)
class CommunicationResponse:
    request: CommunicationRequest
    values: tuple[int, ...] | None
    elapsed_ms: float
    completed_at: datetime


def execute_request(request: CommunicationRequest) -> CommunicationResponse:
    """One caller constructs, uses and closes the H1A client, without retries.

    Write permission is enforced here, before socket creation, even if callers
    bypass the UI. A write response is acknowledgement, never implicit readback.
    """
    request.validate()
    started = perf_counter()
    with FinsUdpClient(
        request.host,
        port=request.port,
        timeout=request.timeout,
        destination=request.destination,
        source=request.source,
    ) as client:
        if request.kind is RequestKind.WRITE:
            client.write_words(request.start, request.values)
            values = None
        else:
            values = client.read_words(request.start, request.count)
    return CommunicationResponse(
        request, values, (perf_counter() - started) * 1000, datetime.now(UTC)
    )
