"""Explicit application-owned camera lifetime, separate from inspection sessions.

Blocking connect/disconnect belong on an application IO owner, never Qt dispatch.
Only the acquisition thread touches SDK objects or changes its session binding.
"""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum, auto
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Protocol, runtime_checkable

from vimo_demo.inspection import CameraId, CycleId, Frame
from vimo_demo.runtime import CoordinatorStoppedError

from .basler import CameraSettings
from .basler_control import BaslerAdmissionControl
from .continuous_basler import ContinuousArmedAcquisition, ContinuousBaslerSource
from .models import ApplicationCapacityError, ApplicationStateError


class ConnectionState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    DISCONNECTING = auto()
    FAULT = auto()


@dataclass(frozen=True, slots=True)
class ConnectedCamera:
    camera_id: CameraId
    serial: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SourceConnectionSnapshot:
    state: ConnectionState
    cameras: tuple[ConnectedCamera, ...]
    thread_alive: bool
    cleanup_safe: bool
    error: str
    inspection_generation: int | None
    received_frames: int
    discarded_frames: int

    @property
    def locked(self) -> bool:
        return self.state is not ConnectionState.DISCONNECTED or self.thread_alive


@dataclass(frozen=True, slots=True)
class InspectionBinding:
    control: BaslerAdmissionControl
    stop: Event
    begin: Callable[[CycleId], None]
    submit: Callable[[Frame], None]
    failed: Callable[[BaseException], None]
    admitting: Callable[[], bool]


@runtime_checkable
class SourceConnectionCommands(Protocol):
    def source_snapshot(self) -> SourceConnectionSnapshot: ...
    def connect_source(self) -> None: ...
    def disconnect_source(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Command:
    binding: InspectionBinding
    attach: bool
    completed: Future[None]


class BaslerConnection:
    def __init__(self) -> None:
        self._lock = Lock()
        self._state = ConnectionState.DISCONNECTED
        self._cameras: tuple[ConnectedCamera, ...] = ()
        self._error = ""
        self._thread: Thread | None = None
        self._source: ContinuousBaslerSource | None = None
        self._binding: InspectionBinding | None = None
        self._stop, self._retry_cleanup = Event(), Event()
        self._cleanup_observed = Event()
        self._ready: Future[None] = Future()
        self._commands: Queue[_Command] = Queue(maxsize=4)

    def snapshot(self) -> SourceConnectionSnapshot:
        with self._lock:
            return SourceConnectionSnapshot(
                self._state,
                self._cameras,
                self._thread is not None and self._thread.is_alive(),
                self._source is None or self._source.cleanup_safe,
                self._error,
                self._binding.control.generation if self._binding else None,
                self._source.received_frames if self._source else 0,
                self._source.discarded_frames if self._source else 0,
            )

    def begin_connect(
        self,
        cameras: tuple[CameraId, ...],
        serials: tuple[str, ...],
        observe: Callable[[ContinuousArmedAcquisition], None],
    ) -> Future[None]:
        with self._lock:
            if self._state is not ConnectionState.DISCONNECTED:
                raise ApplicationStateError("Disconnect the previous source first")
            source = ContinuousBaslerSource(cameras, serials)
            inactive = BaslerAdmissionControl(0)
            inactive.stop()
            source.control = inactive
            self._source = source
            self._cameras, self._error = (), ""
            self._state = ConnectionState.CONNECTING
            self._stop, self._retry_cleanup = Event(), Event()
            self._cleanup_observed = Event()
            self._ready = Future()
            self._thread = Thread(
                target=self._run,
                args=(source, observe),
                name="BaslerSourceOwner",
                daemon=False,
            )
            self._thread.start()
            return self._ready

    def wait_connected(self) -> None:
        try:
            self._ready.result()
        except BaseException:
            # Failed connect is not finished while a safe cleanup thread is alive.
            source, thread = self._source, self._thread
            if source is not None and source.cleanup_safe and thread is not None:
                thread.join()
            raise

    def request_disconnect(self) -> None:
        with self._lock:
            if self._binding is not None:
                raise ApplicationStateError("Stop inspection before disconnecting")
            thread = self._thread
            if thread is None:
                return
            self._state = ConnectionState.DISCONNECTING
            self._cleanup_observed.clear()
            self._stop.set()
            self._retry_cleanup.set()

    def disconnect(self) -> None:
        self.request_disconnect()
        self.wait_disconnected()

    def wait_disconnected(self) -> None:
        thread = self._thread
        if thread is None:
            return
        # A failed cleanup remains owned on the same SDK thread for an explicit
        # later disconnect attempt. It is never retried automatically.
        if thread.is_alive():
            self._cleanup_observed.wait()
        if self._source is not None and not self._source.cleanup_safe:
            raise ApplicationStateError("Camera cleanup still owns resources")
        thread.join()
        with self._lock:
            source = self._source
            if source is not None and not source.cleanup_safe:
                raise ApplicationStateError("Camera cleanup still owns resources")
            self._state = ConnectionState.DISCONNECTED
            self._cameras, self._source = (), None
            self._thread = None

    def command(self, binding: InspectionBinding, *, attach: bool) -> Future[None]:
        completed: Future[None] = Future()
        completed.set_running_or_notify_cancel()
        with self._lock:
            if self._state is not ConnectionState.CONNECTED:
                if attach:
                    completed.set_exception(
                        ApplicationStateError("Basler source is not connected")
                    )
                else:
                    completed.set_result(None)
                return completed
            try:
                self._commands.put_nowait(_Command(binding, attach, completed))
            except Full:
                completed.set_exception(
                    ApplicationCapacityError("Source command capacity reached")
                )
        return completed

    def attributable(self, cycle: CycleId) -> bool:
        source = self._source
        return source is not None and source.attributable(cycle)

    def _poll(self, clean: Callable[[], bool]) -> bool:
        changed = False
        while True:
            try:
                command = self._commands.get_nowait()
            except Empty:
                return changed
            try:
                with self._lock:
                    changes_binding = command.attach or self._binding is command.binding
                    if command.attach:
                        if self._binding is not None or command.binding.stop.is_set():
                            raise ApplicationStateError(
                                "Inspection binding is unavailable"
                            )
                        if not clean():
                            raise ApplicationStateError(
                                "Basler Start requires an idle complete SDK boundary"
                            )
                        self._binding = command.binding
                    elif self._binding is command.binding:
                        self._binding = None
                    if changes_binding:
                        assert self._source is not None
                        control = command.binding.control
                        if not command.attach:
                            control.stop()
                        self._source.control = control
                        self._source.admission_ready = (
                            command.binding.admitting
                            if command.attach
                            else lambda: False
                        )
                        changed = True
                command.completed.set_result(None)
            except Exception as exc:
                command.completed.set_exception(exc)
            finally:
                self._commands.task_done()

    def _begin(self, cycle: CycleId) -> None:
        binding = self._binding
        if binding is not None and not binding.stop.is_set():
            try:
                binding.begin(cycle)
            except ApplicationStateError, CoordinatorStoppedError:
                if not binding.stop.is_set() and binding.admitting():
                    raise

    def _submit(self, frame: Frame) -> None:
        binding = self._binding
        if binding is not None and not binding.stop.is_set():
            try:
                binding.submit(frame)
            except ApplicationStateError, CoordinatorStoppedError:
                if not binding.stop.is_set() and binding.admitting():
                    raise

    def _prepared(self, settings: tuple[CameraSettings, ...]) -> None:
        assert self._source is not None
        rows: list[ConnectedCamera] = []
        for camera, serial, setting in zip(
            self._source.cameras, self._source.serials, settings, strict=True
        ):
            width, height = (
                setting.features.get("Width"),
                setting.features.get("Height"),
            )
            if (
                setting.serial != serial
                or type(width) is not int
                or type(height) is not int
                or min(width, height) <= 0
            ):
                raise ValueError(f"Actual camera dimensions unavailable: {serial}")
            rows.append(ConnectedCamera(camera, serial, width, height))
        with self._lock:
            self._cameras = tuple(rows)

    def _run(
        self,
        source: ContinuousBaslerSource,
        observe: Callable[[ContinuousArmedAcquisition], None],
    ) -> None:
        def armed(event: ContinuousArmedAcquisition) -> None:
            observe(event)
            with self._lock:
                if self._stop.is_set():
                    raise ApplicationStateError("Connection stopped before ARMED")
                self._state = ConnectionState.CONNECTED
            self._ready.set_result(None)

        error: BaseException | None = None
        try:
            source.run(
                self._stop,
                begin=self._begin,
                submit=self._submit,
                on_armed=armed,
                on_prepared=self._prepared,
                on_poll=self._poll,
            )
            if not self._stop.is_set():
                raise ApplicationStateError("Camera owner terminated unexpectedly")
        except BaseException as exc:
            error = exc
        finally:
            if not source.cleanup_safe:
                error = error or ApplicationStateError("Camera cleanup unresolved")
            with self._lock:
                binding, self._binding = self._binding, None
                self._state = (
                    ConnectionState.FAULT if error else ConnectionState.DISCONNECTING
                )
                self._error = str(error) if error else ""
            if binding is not None and error is not None:
                binding.failed(error)
            if not self._ready.done():
                self._ready.set_exception(
                    error or ApplicationStateError("Connection cancelled")
                )
            while True:
                try:
                    command = self._commands.get_nowait()
                except Empty:
                    break
                command.completed.set_exception(
                    error or ApplicationStateError("Source stopped")
                )
                self._commands.task_done()
            while not source.cleanup_safe:
                self._retry_cleanup.wait()
                self._retry_cleanup.clear()
                try:
                    source.retry_cleanup()
                    if not source.cleanup_safe:
                        raise ApplicationStateError(
                            "Camera cleanup still owns resources"
                        )
                except BaseException as exc:
                    with self._lock:
                        self._state, self._error = ConnectionState.FAULT, str(exc)
                self._cleanup_observed.set()
            self._cleanup_observed.set()
