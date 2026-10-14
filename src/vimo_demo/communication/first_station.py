"""First-station projection and two acknowledged writes; no Production wiring."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import TYPE_CHECKING

from .fins import (
    FinsError,
    FinsMemoryArea,
    FinsTransportError,
    FinsUdpClient,
    validate_integer,
)
from .fins_tcp import FinsTcpClient

if TYPE_CHECKING:
    from vimo_demo.inspection.production import ChannelBusinessStatus, ChannelId


def _rejects(status: object) -> bool:
    # Lazy runtime validation keeps standalone transport imports independent.
    from vimo_demo.inspection.production import ChannelBusinessStatus

    if not isinstance(status, ChannelBusinessStatus):
        raise ValueError("status must be a ChannelBusinessStatus instance")
    return status is not ChannelBusinessStatus.OK


def first_station_reject_words(
    statuses: Iterable[tuple[ChannelId, ChannelBusinessStatus]],
) -> tuple[int, int, int]:
    """Project exactly CH1..36 by identity; only business OK clears a bit.

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
        if _rejects(status):
            word, bit = divmod(channel_id - 1, 16)
            words[word] |= 1 << bit
    missing = sorted(set(range(1, 37)) - seen)
    if missing:
        raise ValueError(f"Missing ChannelIds: {missing}")
    return words[0], words[1], words[2]


class SubmissionStage(Enum):
    BITMAP = "bitmap"
    SEQUENCE = "sequence"


@dataclass(frozen=True, slots=True)
class FirstStationCommit:
    """Both FINS writes acknowledged; does not assert PLC scan consumption."""

    sequence: int
    reject_words: tuple[int, int, int]


class FirstStationSubmissionError(FinsError):
    """Failed stage with original FINS error chained; not a successful commit."""

    def __init__(
        self,
        stage: SubmissionStage,
        sequence: int,
        reject_words: tuple[int, int, int],
        cause: FinsError,
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
        self, client: FinsUdpClient | FinsTcpClient, *, initial_sequence: int
    ) -> None:
        validate_integer("initial_sequence", initial_sequence, 0, 65535)
        self._client = client
        self._next_sequence = initial_sequence
        self._operation = Lock()

    @property
    def next_sequence(self) -> int:
        """Next attempted sequence, not a remote confirmed/consumed sequence."""
        return self._next_sequence

    def submit(
        self, statuses: Iterable[tuple[ChannelId, ChannelBusinessStatus]]
    ) -> FirstStationCommit:
        if not self._operation.acquire(blocking=False):
            raise FinsTransportError("A first-station submission is already active")
        try:
            words = first_station_reject_words(statuses)  # Validate before I/O.
            sequence = self._next_sequence
            self._next_sequence = (self._next_sequence + 1) & 0xFFFF
            stage = SubmissionStage.BITMAP
            try:
                self._client.write_words(50, words, area=FinsMemoryArea.W)
                stage = SubmissionStage.SEQUENCE
                self._client.write_words(49, (sequence,), area=FinsMemoryArea.W)
            except FinsError as exc:
                raise FirstStationSubmissionError(stage, sequence, words, exc) from exc
            return FirstStationCommit(sequence, words)
        finally:
            self._operation.release()
