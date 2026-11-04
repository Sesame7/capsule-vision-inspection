"""First-station projection and two acknowledged writes; no Production wiring."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING

from .fins import (
    MAX_WRITE_WORDS,
    FinsError,
    FinsMemoryArea,
    FinsTransportError,
    FinsUdpClient,
    validate_integer,
    validate_word_range,
)
from .fins_tcp import FinsTcpClient
from .modbus_tcp import (
    ModbusError,
    ModbusTcpClient,
    ModbusTransportError,
    validate_register_range,
)

type FirstStationClient = FinsUdpClient | FinsTcpClient | ModbusTcpClient

if TYPE_CHECKING:
    from capsule_vision.inspection.production import ChannelBusinessStatus, ChannelId

    from .reporting import FirstStationReport


def _rejects(status: object, verified_empty: bool) -> bool:
    # Lazy runtime validation keeps standalone transport imports independent.
    from capsule_vision.inspection.production import ChannelBusinessStatus

    if not isinstance(status, ChannelBusinessStatus):
        raise ValueError("status must be a ChannelBusinessStatus instance")
    return status is not ChannelBusinessStatus.OK and not (
        status is ChannelBusinessStatus.EMPTY and verified_empty
    )


def first_station_reject_words(
    statuses: Iterable[tuple[ChannelId, ChannelBusinessStatus]],
    *,
    verified_empty_channels: frozenset[ChannelId] = frozenset(),
) -> tuple[int, int] | tuple[int, int, int]:
    """Project CH1..30 or CH1..36. Status-only EMPTY remains conservative.

    Import the domain enum only when projecting. Low-level FINS/CLI use remains
    standard-library-only, without importing the inspection image stack.
    """
    seen: set[int] = set()
    words = [0, 0, 0]
    for channel_id, status in statuses:
        validate_integer("channel_id", channel_id, 1, 36)
        if channel_id in seen:
            raise ValueError(f"Duplicate ChannelId {channel_id}")
        seen.add(channel_id)
        if _rejects(status, channel_id in verified_empty_channels):
            word, bit = divmod(channel_id - 1, 16)
            words[word] |= 1 << bit
    expected = 30 if len(seen) == 30 else 36
    missing = sorted(set(range(1, expected + 1)) - seen)
    if missing:
        raise ValueError(f"Missing ChannelIds: {missing}")
    if expected == 30:
        return words[0], words[1]
    return words[0], words[1], words[2]


class SubmissionStage(Enum):
    BITMAP = "bitmap"
    SEQUENCE = "sequence"


@dataclass(frozen=True, slots=True)
class FirstStationCommit:
    """Both protocol writes acknowledged; does not assert PLC scan consumption."""

    sequence: int
    reject_words: tuple[int, int] | tuple[int, int, int]
    bitmap_ms: float = 0.0
    sequence_ms: float = 0.0


class FirstStationSubmissionError(Exception):
    """Failed stage with original protocol error chained; not a successful commit."""

    def __init__(
        self,
        stage: SubmissionStage,
        sequence: int,
        reject_words: tuple[int, int] | tuple[int, int, int],
        cause: FinsError | ModbusError,
    ) -> None:
        self.stage = stage
        self.sequence = sequence
        self.reject_words = reject_words
        super().__init__(
            f"First-station {stage.value} write failed "
            f"(sequence={sequence}): {type(cause).__name__}: {cause}"
        )


class FirstStationReporter:
    """One serial owner borrows a client and explicitly seeds the first sequence.

    Valid submissions reserve a sequence before I/O, including failed attempts,
    so an uncertain sequence write is never implicitly reused. Gaps are possible.
    No read/synchronization, retry, reconnect or client close is hidden here.
    The owner must not use the borrowed client elsewhere during submission.
    """

    def __init__(
        self,
        client: FirstStationClient,
        *,
        initial_sequence: int,
        base_address: int | None = None,
    ) -> None:
        validate_integer("initial_sequence", initial_sequence, 0, 65535)
        if isinstance(client, ModbusTcpClient):
            if base_address is None:
                raise ValueError("Modbus reporting requires an explicit base_address")
            validate_register_range(base_address, 4, 4)
        else:
            base_address = 49 if base_address is None else base_address
            validate_word_range(base_address, 4, MAX_WRITE_WORDS, FinsMemoryArea.W)
        self._client = client
        self._base_address = base_address
        self._next_sequence = initial_sequence
        self._operation = Lock()

    def _write(self, address: int, words: tuple[int, ...]) -> None:
        if isinstance(self._client, ModbusTcpClient):
            self._client.write_words(address, words)
        else:
            self._client.write_words(address, words, area=FinsMemoryArea.W)

    @property
    def next_sequence(self) -> int:
        """Next attempted sequence, not a remote confirmed/consumed sequence."""
        return self._next_sequence

    def submit(
        self,
        statuses: Iterable[tuple[ChannelId, ChannelBusinessStatus]],
        *,
        commit_allowed: Callable[[], bool] = lambda: True,
    ) -> FirstStationCommit:
        return self._submit(
            lambda: first_station_reject_words(statuses), commit_allowed
        )

    def submit_report(
        self,
        report: FirstStationReport,
        *,
        commit_allowed: Callable[[], bool] = lambda: True,
    ) -> FirstStationCommit:
        """Write the validated bitmap already recorded for this cycle."""
        return self._submit(lambda: report.reject_words, commit_allowed)

    def _submit(
        self,
        projection: Callable[[], tuple[int, int] | tuple[int, int, int]],
        commit_allowed: Callable[[], bool],
    ) -> FirstStationCommit:
        if not self._operation.acquire(blocking=False):
            error = (
                ModbusTransportError
                if isinstance(self._client, ModbusTcpClient)
                else FinsTransportError
            )
            raise error("A first-station submission is already active")
        try:
            words = (
                projection()
            )  # Validate standalone statuses before reserving a sequence.
            sequence = self._next_sequence
            self._next_sequence = (self._next_sequence + 1) & 0xFFFF
            stage = SubmissionStage.BITMAP
            try:
                started = perf_counter()
                self._write(self._base_address + 1, words)
                bitmap_ms = (perf_counter() - started) * 1000
                stage = SubmissionStage.SEQUENCE
                if not commit_allowed():
                    error = (
                        ModbusError
                        if isinstance(self._client, ModbusTcpClient)
                        else FinsError
                    )
                    raise error("Cycle attribution lost before sequence commit")
                started = perf_counter()
                self._write(self._base_address, (sequence,))
                sequence_ms = (perf_counter() - started) * 1000
            except (FinsError, ModbusError) as exc:
                raise FirstStationSubmissionError(stage, sequence, words, exc) from exc
            return FirstStationCommit(sequence, words, bitmap_ms, sequence_ms)
        finally:
            self._operation.release()
