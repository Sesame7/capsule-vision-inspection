"""One cancellable, read-only process observation on the diagnostic worker."""

from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.communication.fins import MAX_READ_WORDS, validate_integer
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
    execute_request,
)
from capsule_vision.configuration import ConfigurationError
from capsule_vision.configuration.communication_preferences import (
    load_communication_preferences,
)
from capsule_vision.configuration.process_parameters import (
    ProcessCatalog,
    load_process_catalog,
)

from .identity import LocalIdentity, admit_engineering
from .manual_plc import execute_manual


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    endpoint: FinsTcpEndpoint
    values: tuple[tuple[str, int | None], ...]
    completed_at: datetime


def signed_dint(low: int, high: int) -> int:
    """Omron low word first; FINS has already decoded each big-endian word."""
    validate_integer("low word", low, 0, 65535)
    validate_integer("high word", high, 0, 65535)
    value = low | (high << 16)
    return value - (1 << 32) if value & (1 << 31) else value


def read_process_preview(
    preferences_path: Path,
    catalog: ProcessCatalog,
    catalog_path: Path | None,
    cancelled: Event,
    *,
    identity: LocalIdentity | None,
    revision: int | None,
    execute: Callable[[CommunicationRequest], CommunicationResponse] = execute_request,
    guard: Callable[[CommunicationRequest], None] | None = None,
) -> ProcessObservation:
    """Capture saved definitions, validate all ranges, then admit each grouped READ.

    A failed group invalidates the entire observation. No partial/stale
    publication, retries, polling or writes.
    Cancellation stops unadmitted reads; the current client's owner closes it.
    """

    def admit() -> None:
        if cancelled.is_set():
            raise CancelledError("PLC parameter preview ended")
        admit_engineering(identity, revision)

    admit()
    saved = load_communication_preferences(preferences_path)
    if load_process_catalog(catalog_path) != catalog:
        raise ConfigurationError("Process catalog changed; reload before PLC preview")
    endpoint = saved.status.endpoint
    addresses = {p.id: p.address for p in catalog.parameters}
    mapped = sorted(addresses.values())
    if not mapped:
        raise ConfigurationError("No process parameters configured")
    # At most one FINS read window per group, including intervening DM words.
    # The existing DM range and protocol count bounds also bound the total work.
    ranges: list[tuple[int, int]] = []
    start, end = mapped[0], mapped[0] + 2
    for address in mapped[1:]:
        if address + 2 - start > MAX_READ_WORDS:
            ranges.append((start, end - start))
            start = address
        end = address + 2
    ranges.append((start, end - start))
    requests = tuple(
        CommunicationRequest(RequestKind.READ, endpoint, start, count)
        for start, count in ranges
    )
    for request in requests:
        request.validate()
    words: dict[int, int] = {}
    observed: datetime | None = None
    for request in requests:
        admit()
        result = execute_manual(request, execute, guard, identity, revision)
        if (
            result.request != request
            or result.values is None
            or len(result.values) != request.count
        ):
            raise RuntimeError("Missing matching PLC parameter read response")
        words.update(
            (request.start + i, value) for i, value in enumerate(result.values)
        )
        observed = result.completed_at
    admit()
    assert observed is not None
    return ProcessObservation(
        endpoint,
        tuple(
            (
                key,
                signed_dint(words[address], words[address + 1]),
            )
            for key, address in addresses.items()
        ),
        observed,
    )
