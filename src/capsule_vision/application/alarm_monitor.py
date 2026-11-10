"""One serial auxiliary FINS/TCP owner for software status and alarm reads."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Event, Thread

from capsule_vision.communication.fins import FinsError, FinsMemoryArea, FinsPlcError
from capsule_vision.communication.fins_tcp import FinsTcpClient
from capsule_vision.configuration.alarm_definitions import (
    AlarmDefinitions,
    AlarmSignal,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import StatusPreferences
from capsule_vision.configuration.errors import ConfigurationError
from capsule_vision.diagnostics import RuntimeDiagnostics

from .alarm_records import AlarmEvent
from .models import ApplicationState


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    state: str = "off"
    active: tuple[AlarmSignal, ...] = ()
    issue: str = ""
    selection: StatusPreferences = StatusPreferences()
    definitions: AlarmDefinitions | None = None
    raw_word: int | None = None
    observed_at: float | None = None
    read_ok: bool = False
    write_ok: bool = False
    read_issue: str = ""
    write_issue: str = ""
    heartbeat: int | None = None
    fault: int | None = None
    written_at: float | None = None


class AlarmMonitor:
    def __init__(
        self,
        definitions: Path,
        offer: Callable[[tuple[AlarmEvent, ...]], None],
        context: Callable[[], tuple[float, str | None, str | None]],
        *,
        state: Callable[[], ApplicationState],
        interval: float = 1.0,
        diagnostics: RuntimeDiagnostics | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("Alarm polling interval must be positive")
        self.definitions_path, self._offer, self._context = definitions, offer, context
        self._interval = interval
        self._state = state
        self._heartbeat = 0
        self._owned_selection: StatusPreferences | None = None
        self._diagnostics = diagnostics
        self._condition = Condition()
        self._selection = StatusPreferences()
        self._snapshot = AlarmSnapshot()
        self._cancel = Event()
        self._closed = False
        self._thread: Thread | None = None

    def apply_saved(self, value: StatusPreferences) -> None:
        """Called only by composition or the committed preferences owner."""
        with self._condition:
            if self._closed or value == self._selection:
                return
            self._cancel.set()
            self._cancel = Event()
            self._selection = value
            self._snapshot = AlarmSnapshot(
                "waiting" if value.enabled else "off", selection=value
            )
            if value.enabled and self._thread is None:
                self._thread = Thread(
                    target=self._run, name="PLCStatusService", daemon=False
                )
                try:
                    self._thread.start()
                except RuntimeError as exc:
                    self._thread = None
                    self._snapshot = replace(
                        self._snapshot,
                        state="unavailable",
                        issue=str(exc),
                        read_ok=False,
                        write_ok=False,
                    )
                    if self._diagnostics:
                        self._diagnostics.record(
                            "alarm_monitor.unavailable",
                            stage="start",
                            error_type=type(exc).__name__,
                        )
            self._condition.notify_all()

    def protected_settings(self) -> tuple[StatusPreferences, ...]:
        """Keep old write reservations until its socket has actually retired."""
        with self._condition:
            values = (
                self._selection
                if self._selection.enabled and not self._closed
                else None,
                self._owned_selection,
            )
            return tuple(dict.fromkeys(value for value in values if value is not None))

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

    def _unavailable(self, cancel: Event, exc: Exception, stage: str) -> None:
        with self._condition:
            if not cancel.is_set():
                if self._diagnostics and self._snapshot.state != "unavailable":
                    self._diagnostics.record(
                        "alarm_monitor.unavailable",
                        stage=stage,
                        error_type=type(exc).__name__,
                    )
                self._snapshot = replace(
                    self._snapshot,
                    state="unavailable",
                    issue=str(exc),
                    read_ok=False,
                    write_ok=False,
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
                definitions = load_alarm_definitions(self.definitions_path)
            except ConfigurationError as exc:
                self._unavailable(cancel, exc, "definitions")
                cancel.wait()  # Invalid assets are reread only at a new activation.
                continue
            with self._condition:
                if cancel.is_set():
                    continue
                self._snapshot = replace(self._snapshot, definitions=definitions)
            with self._condition:
                if cancel.is_set():
                    continue
                self._owned_selection = selection
            try:
                self._poll(selection, cancel, definitions)
            finally:
                with self._condition:
                    self._owned_selection = None

    def _poll(
        self, selection: StatusPreferences, cancel: Event, definitions: AlarmDefinitions
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
                # Advance once per scheduled attempt, including failed negotiation.
                # A lost ACK is never replayed with the old counter.
                self._heartbeat = (self._heartbeat + 1) & 0xFFFF
                heartbeat, fault = self._heartbeat, 0
                written_at = None
                write_issue = read_issue = ""
                word: int | None = None
                observed: float | None = None
                write_target = f"Fault W{selection.fault_address}"
                try:
                    if client is None:
                        client = FinsTcpClient(
                            endpoint.host, port=endpoint.port, timeout=endpoint.timeout
                        )
                    if cancel.is_set():
                        return
                    fault = int(self._state() is ApplicationState.FAULT)
                    client.write_words(
                        selection.fault_address,
                        (fault,),
                        area=FinsMemoryArea.W,
                    )
                    if cancel.is_set():
                        return
                    write_target = f"Heartbeat W{selection.heartbeat_address}"
                    client.write_words(
                        selection.heartbeat_address,
                        (heartbeat,),
                        area=FinsMemoryArea.W,
                    )
                    written_at = self._context()[0]
                except (FinsError, OSError) as exc:
                    write_issue = f"{write_target}: {exc}"
                    # A validated PLC rejection leaves the serial connection usable.
                    if client is not None and not isinstance(exc, FinsPlcError):
                        client.close()
                        client = None
                if cancel.is_set():
                    return
                if client is not None:
                    try:
                        (word,) = client.read_words(
                            selection.word_address, 1, area=FinsMemoryArea.W
                        )
                    except (FinsError, OSError) as exc:
                        read_issue = str(exc)
                        client.close()
                        client = None
                else:
                    read_issue = "Alarm read unavailable after status transport failure"
                events: tuple[AlarmEvent, ...] = ()
                if word is not None:
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
                            None,
                            selection.word_address,
                            s.bit,
                            provenance,
                            batch_id,
                            label,
                            translations=s.translations,
                        )
                        for s in definitions.signals
                        if (s in active) != (s in (previous or ()))
                    )
                    previous, gap = active, False
                else:
                    gap = True
                write_ok, read_ok = written_at is not None, word is not None
                health = (
                    "online"
                    if write_ok and read_ok
                    else "partial"
                    if write_ok or read_ok
                    else "unavailable"
                )
                issue = "\n".join(
                    part
                    for part in (
                        f"Status write: {write_issue}" if write_issue else "",
                        f"Alarm read: {read_issue}" if read_issue else "",
                    )
                    if part
                )
                with self._condition:
                    if cancel.is_set():
                        return
                    if self._diagnostics and self._snapshot.state != health:
                        if health in ("partial", "unavailable"):
                            self._diagnostics.record(
                                "alarm_monitor.unavailable",
                                stage="status_write" if write_issue else "plc_read",
                            )
                        elif self._snapshot.state in ("partial", "unavailable"):
                            self._diagnostics.record(
                                "alarm_monitor.recovered",
                                address=selection.word_address,
                            )
                    old = self._snapshot
                    self._snapshot = replace(
                        old,
                        state=health,
                        issue=issue,
                        read_ok=read_ok,
                        write_ok=write_ok,
                        read_issue=read_issue,
                        write_issue=write_issue,
                        heartbeat=heartbeat if write_ok else old.heartbeat,
                        fault=fault if write_ok else old.fault,
                        written_at=written_at if write_ok else old.written_at,
                        active=previous
                        if read_ok and previous is not None
                        else old.active,
                        raw_word=word if read_ok else old.raw_word,
                        observed_at=observed if read_ok else old.observed_at,
                    )
                    if events:
                        self._offer(events)
                cancel.wait(self._interval)
        finally:
            if client is not None:
                client.close()
