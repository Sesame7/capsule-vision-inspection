"""One independently negotiated, read-only PLC connection for saved monitoring."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Event, Thread

from capsule_vision.communication.fins import FinsError, FinsMemoryArea
from capsule_vision.communication.fins_tcp import FinsTcpClient
from capsule_vision.configuration.alarm_definitions import (
    AlarmDefinitions,
    AlarmSignal,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import AlarmPreferences
from capsule_vision.configuration.errors import ConfigurationError

from .alarm_records import AlarmEvent


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    state: str = "off"
    active: tuple[AlarmSignal, ...] = ()
    issue: str = ""


class AlarmMonitor:
    def __init__(
        self,
        definitions: Path,
        offer: Callable[[tuple[AlarmEvent, ...]], None],
        context: Callable[[], tuple[float, str | None, str | None]],
        *,
        interval: float = 1.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("Alarm polling interval must be positive")
        self._definitions, self._offer, self._context = definitions, offer, context
        self._interval = interval
        self._condition = Condition()
        self._selection = AlarmPreferences()
        self._snapshot = AlarmSnapshot()
        self._cancel = Event()
        self._closed = False
        self._thread: Thread | None = None

    def apply_saved(self, value: AlarmPreferences) -> None:
        """Called only by composition or the committed preferences owner."""
        with self._condition:
            if self._closed or value == self._selection:
                return
            self._cancel.set()
            self._cancel = Event()
            self._selection = value
            self._snapshot = AlarmSnapshot("waiting" if value.enabled else "off")
            if value.enabled and self._thread is None:
                self._thread = Thread(
                    target=self._run, name="PLCAlarmMonitor", daemon=False
                )
                try:
                    self._thread.start()
                except RuntimeError as exc:
                    self._thread = None
                    self._snapshot = AlarmSnapshot("unavailable", issue=str(exc))
            self._condition.notify_all()

    def snapshot(self) -> AlarmSnapshot:
        with self._condition:
            return self._snapshot

    def request_stop(self) -> None:
        with self._condition:
            self._closed = True
            self._snapshot = AlarmSnapshot()
            self._cancel.set()
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _unavailable(self, cancel: Event, exc: Exception) -> None:
        with self._condition:
            if not cancel.is_set():
                self._snapshot = replace(
                    self._snapshot, state="unavailable", issue=str(exc)
                )

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._closed or self._selection.enabled
                )
                if self._closed:
                    return
                selection, cancel = self._selection, self._cancel
            try:
                definitions = load_alarm_definitions(self._definitions)
            except ConfigurationError as exc:
                self._unavailable(cancel, exc)
                cancel.wait()  # Invalid assets are reread only at a new activation.
                continue
            self._poll(selection, cancel, definitions)

    def _poll(
        self, selection: AlarmPreferences, cancel: Event, definitions: AlarmDefinitions
    ) -> None:
        endpoint = selection.endpoint
        source = f"fins_tcp://{endpoint.host}:{endpoint.port}"
        provenance = (
            f"{definitions.origin}/{definitions.version}  SHA-256 {definitions.digest}"
        )
        previous: tuple[AlarmSignal, ...] | None = None
        gap = True
        client: FinsTcpClient | None = None
        try:
            while not cancel.is_set():
                try:
                    if client is None:
                        client = FinsTcpClient(
                            endpoint.host, port=endpoint.port, timeout=endpoint.timeout
                        )
                    if cancel.is_set():
                        return
                    (word,) = client.read_words(
                        selection.address, 1, area=FinsMemoryArea.W
                    )
                except (FinsError, OSError) as exc:
                    self._unavailable(cancel, exc)
                    gap = True
                    if client is not None:
                        client.close()
                        client = None
                else:
                    observed, batch_id, label = self._context()
                    active = tuple(
                        s
                        for s in definitions.signals
                        if bool(word & (1 << s.bit)) == s.active_high
                    )
                    events = tuple(
                        AlarmEvent(
                            observed,
                            s.id,
                            ("observed_active" if gap else "activated")
                            if s in active
                            else ("observed_recovered" if gap else "recovered"),
                            s.category,
                            s.zh,
                            s.en,
                            source,
                            selection.station,
                            selection.address,
                            s.bit,
                            provenance,
                            batch_id,
                            label,
                        )
                        for s in definitions.signals
                        if (s in active) != (s in (previous or ()))
                    )
                    with self._condition:
                        if cancel.is_set():
                            return
                        self._snapshot = AlarmSnapshot("online", active)
                        if events:
                            self._offer(events)
                    previous, gap = active, False
                cancel.wait(self._interval)
        finally:
            if client is not None:
                client.close()
