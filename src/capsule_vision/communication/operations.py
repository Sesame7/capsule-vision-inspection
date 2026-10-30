"""Session-local register requests/watch definitions; no Qt or scheduling."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from time import perf_counter

from .endpoints import Endpoint, open_client, protocol_name, validate_endpoint
from .fins import (
    MAX_READ_WORDS,
    MAX_WRITE_WORDS,
    FinsMemoryArea,
    validate_integer,
    validate_word_range,
)
from .modbus_tcp import (
    MAX_READ_REGISTERS,
    MAX_WRITE_REGISTERS,
    HoldingRegisters,
    ModbusSettings,
    ModbusTcpClient,
    validate_register_range,
)

MAX_WATCH_GROUPS = 16
MAX_WATCH_WORDS = 2048


@dataclass(frozen=True, slots=True)
class RegisterWatchGroup:
    """One consecutive read; session-local name, no merging or PLC meaning."""

    area: FinsMemoryArea | HoldingRegisters
    start: int
    count: int = 1
    name: str = ""

    def __post_init__(self) -> None:
        if self.area is HoldingRegisters.HOLDING:
            validate_register_range(self.start, self.count, MAX_READ_REGISTERS)
        else:
            validate_word_range(self.start, self.count, MAX_READ_WORDS, self.area)
        if len(self.name) > 64:
            raise ValueError("Watch name must contain at most 64 characters")


class RequestKind(Enum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class CommunicationRequest:
    kind: RequestKind
    endpoint: Endpoint
    start: int
    count: int = 1
    values: tuple[int, ...] = ()
    area: FinsMemoryArea | HoldingRegisters = FinsMemoryArea.DM

    @property
    def protocol_name(self) -> str:
        return protocol_name(self.endpoint)

    def validate(self) -> None:
        """Cheap dispatch validation, repeated at the execution boundary."""
        modbus = isinstance(validate_endpoint(self.endpoint), ModbusSettings)
        maximum = (
            (
                MAX_WRITE_REGISTERS
                if self.kind is RequestKind.WRITE
                else MAX_READ_REGISTERS
            )
            if modbus
            else (MAX_WRITE_WORDS if self.kind is RequestKind.WRITE else MAX_READ_WORDS)
        )
        if modbus:
            if self.area is not HoldingRegisters.HOLDING:
                raise ValueError(
                    "Modbus requires an explicit Holding Register selection; FINS watches cannot execute"
                )
            validate_register_range(self.start, self.count, maximum)
        else:
            validate_word_range(self.start, self.count, maximum, self.area)
        if self.kind is RequestKind.WRITE:
            validate_integer("write count", len(self.values), 1, maximum)
            if self.count != len(self.values):
                raise ValueError("Write count must equal the number of values")
            for value in self.values:
                validate_integer("word", value, 0, 65535)
        else:
            validate_integer("read count", self.count, 1, maximum)
            if self.values:
                raise ValueError("Read requests carry no write values")


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
    with open_client(request.endpoint) as client:
        if isinstance(client, ModbusTcpClient):
            if request.kind is RequestKind.WRITE:
                client.write_words(request.start, request.values)
                values = None
            else:
                values = client.read_words(request.start, request.count)
        else:
            assert isinstance(request.area, FinsMemoryArea)
            if request.kind is RequestKind.WRITE:
                client.write_words(request.start, request.values, area=request.area)
                values = None
            else:
                values = client.read_words(
                    request.start, request.count, area=request.area
                )
    return CommunicationResponse(
        request, values, (perf_counter() - started) * 1000, datetime.now(UTC)
    )
