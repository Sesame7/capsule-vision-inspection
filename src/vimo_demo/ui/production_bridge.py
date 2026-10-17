"""Application Futures/snapshots to queued Qt notifications, never runtime ownership."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from time import monotonic

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from vimo_demo.application import (
    PreparedConfiguration,
    ProductionApplication,
    ProductionApplicationSnapshot,
)
from vimo_demo.application.basler_connection import SourceConnectionCommands
from vimo_demo.application.local_replay import (
    LocalReplayCommands,
    ReplaySnapshot,
    SourceControlCommands,
)
from vimo_demo.communication.reporting import (
    ProductionReportingCommands,
    ReportingSnapshot,
)

from .display_frames import LatestDisplayFrameStore
from .inference_bridge import FutureDelivery


@dataclass(frozen=True, slots=True)
class RecipeChoice:
    prepared: PreparedConfiguration
    label: str


class QtProductionBridge(QObject):
    snapshot_changed = Signal(object)
    frames_changed = Signal(object)
    presentation_ready = Signal()
    replay_changed = Signal(object)
    source_progress_changed = Signal(int, str)
    reporting_changed = Signal(object)
    source_connection_changed = Signal(object)
    model_preparation_changed = Signal(object)
    command_finished = Signal(str)
    command_failed = Signal(str, object)
    shutdown_finished = Signal()

    def __init__(
        self,
        application: ProductionApplication,
        frames: LatestDisplayFrameStore,
        parent: QObject | None = None,
        *,
        poll_ms: int = 100,
        close_timeout_ms: int = 30_000,
        local_replay: LocalReplayCommands | None = None,
    ) -> None:
        super().__init__(parent)
        if min(poll_ms, close_timeout_ms) <= 0:
            raise ValueError("Timer intervals must be positive")
        self.application = application
        self.frames = frames
        self.reporting = (
            application
            if isinstance(application, ProductionReportingCommands)
            else None
        )
        self.reporting_snapshot: ReportingSnapshot | None = None
        self.source_connection = (
            application if isinstance(application, SourceConnectionCommands) else None
        )
        self._connection_snapshot = None
        self.local_replay = local_replay
        self.source_control = (
            application if isinstance(application, SourceControlCommands) else None
        )
        self._replay_enabled = self.controls_enabled
        self.replay_snapshot: ReplaySnapshot | None = None
        self._snapshot: ProductionApplicationSnapshot | None = None
        self._preparation_snapshot = application.preparation_snapshot()
        self._revision = -1
        self._refreshing = False
        self._closing = False
        self._closed = False
        self._close_succeeded = False
        self._deadline = 0.0
        self._timeout = close_timeout_ms / 1000
        self._timeout_reported = False
        self._detached = False
        # Reuse the established destruction-safe Future delivery gate unchanged.
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.timer = QTimer(self)
        self.timer.setInterval(poll_ms)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()

    @property
    def replay_enabled(self) -> bool:
        return self.local_replay is not None and self.local_replay.replay_available

    @property
    def controls_enabled(self) -> bool:
        return (
            self.source_control.control_available
            if self.source_control
            else self.replay_enabled
        )

    def control_snapshot(self) -> ReplaySnapshot | None:
        return (
            self.source_control.control_snapshot()
            if self.source_control
            else self.local_replay.replay_snapshot()
            if self.local_replay
            else None
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def refreshing(self) -> bool:
        """Batch snapshot/pixel notifications into one synchronous card presentation."""
        return self._refreshing

    @property
    def closing(self) -> bool:
        return self._closing

    def _check_thread(self) -> None:
        if self._detached or QThread.currentThread() != self.thread():
            raise RuntimeError("Bridge requires its live GUI thread")

    def _command(self, name: str, command: Callable[[], Future[None]]) -> None:
        self._check_thread()
        try:
            future = command()
        except Exception as exc:
            self.command_failed.emit(name, exc)
        else:
            self._delivery.watch(name, future)
        self.refresh()

    def _immediate(self, name: str, command: Callable[[], None]) -> None:
        # Prepared selection/reset/batch are cheap locked application operations;
        # no parsing, artifact IO, engine construction or waiting occurs here.
        def perform() -> Future[None]:
            command()
            future: Future[None] = Future()
            future.set_result(None)
            return future

        self._command(name, perform)

    @Slot()
    def start(self) -> None:
        self._command("start", self.application.start)

    @Slot()
    def stop(self) -> None:
        self._command("stop", self.application.stop)

    @Slot()
    def pause_replay(self) -> None:
        if self.source_control is not None:
            self._command("source_pause", self.source_control.pause_source)
        elif self.local_replay is not None:
            self._command("replay_pause", self.local_replay.pause_replay)

    @Slot()
    def continue_replay(self) -> None:
        if self.source_control is not None:
            self._command("source_resume", self.source_control.resume_source)
        elif self.local_replay is not None:
            self._command("replay_continue", self.local_replay.continue_replay)

    @Slot()
    def step_replay(self) -> None:
        if self.source_control is not None:
            self._command("source_step", self.source_control.step_source)
        elif self.local_replay is not None:
            self._command("replay_step", self.local_replay.step_replay)

    @Slot()
    def reset(self) -> None:
        self._immediate("reset", self.application.reset_fault)

    def select_recipe(self, choice: RecipeChoice) -> None:
        self._immediate("recipe", lambda: self.application.configure(choice.prepared))

    def start_batch(self, batch_id: str | None) -> None:
        self._immediate("batch", lambda: self.application.start_batch(batch_id))

    @Slot()
    def close(self) -> None:
        self._check_thread()
        if self._closing or self._closed:
            return
        self._closing = True
        self._deadline = monotonic() + self._timeout
        self._timeout_reported = False
        self._command("close", self.application.close)

    @Slot(str, object, object)
    def _completed(self, name: str, value: object, error: object) -> None:
        if self._detached:
            return
        if isinstance(error, BaseException):
            if name == "close":
                self._closing = False  # Explicit retry possible; never quit on failure.
            self.command_failed.emit(name, error)
        else:
            if name == "close":
                self._close_succeeded = True
            self.command_finished.emit(name)
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        self._check_thread()
        if self._refreshing:
            return
        self._refreshing = True
        changed = False
        try:
            snapshot = self.application.snapshot()
            connection = (
                self.source_connection.source_snapshot()
                if self.source_connection
                else None
            )
            if connection != self._connection_snapshot:
                self._connection_snapshot = connection
                self.source_connection_changed.emit(connection)
            replay = self.control_snapshot()
            replay_changed = (
                replay != self.replay_snapshot
                or self._replay_enabled != self.controls_enabled
            )
            self._replay_enabled = self.controls_enabled
            self.replay_snapshot = replay
            changed = snapshot != self._snapshot
            if changed:
                self._snapshot = snapshot
                self.snapshot_changed.emit(snapshot)
            preparation = self.application.preparation_snapshot()
            if preparation != self._preparation_snapshot:
                self._preparation_snapshot = preparation
                self.model_preparation_changed.emit(preparation)
            if replay_changed:
                self.replay_changed.emit(replay)
            reporting = (
                self.reporting.reporting_snapshot()
                if self.reporting is not None
                else None
            )
            if reporting != self.reporting_snapshot:
                self.reporting_snapshot = reporting
                self.reporting_changed.emit(reporting)
            display = self.frames.snapshot()
            revision = display.revision
            changed = changed or revision != self._revision
            if revision != self._revision:
                self._revision = revision
                self.frames_changed.emit(display)
            if self._closing:
                if self._close_succeeded and self.application.join(0):
                    self._closed = True
                    self.timer.stop()
                    self.shutdown_finished.emit()
                elif not self._timeout_reported and monotonic() >= self._deadline:
                    self._timeout_reported = True
                    self.command_failed.emit(
                        "close", TimeoutError("Still waiting for owned runtime")
                    )

        finally:
            self._refreshing = False
            if changed:
                self.presentation_ready.emit()

    def detach(self) -> None:
        if not self._detached:
            self._check_thread()
            self._detached = True
            self.timer.stop()
            self._delivery.detach()
