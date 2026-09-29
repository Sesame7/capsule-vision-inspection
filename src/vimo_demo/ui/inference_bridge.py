"""Future notifications cross to Qt through an explicitly queued connection."""

from concurrent.futures import Future
from threading import Lock
from time import monotonic

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from vimo_demo.inference import ImageResult
from vimo_demo.runtime import InferenceWorker, WorkerState


class _Emitter(QObject):
    completed = Signal(str, object, object)


class _Delivery:
    """Callbacks retain this gate, never a window/bridge. Detach serializes emit.

    The unparented sender survives destruction of the receiving bridge until
    detach disables callbacks and schedules its deletion on the Qt thread.
    """

    def __init__(self) -> None:
        self.emitter = _Emitter()
        self._lock = Lock()
        self._active = True

    def detach(self) -> None:
        with self._lock:
            if self._active:
                self._active = False
                self.emitter.deleteLater()

    def watch[T](self, kind: str, future: Future[T]) -> None:
        def done(completed: Future[T]) -> None:
            value: object = None
            error: BaseException | None = None
            try:
                value = completed.result(timeout=0)  # Already complete; never wait.
            except BaseException as exc:
                error = exc
            with self._lock:
                if self._active:
                    self.emitter.completed.emit(kind, value, error)

        future.add_done_callback(done)


class QtInferenceBridge(QObject):
    """All public methods belong to the GUI thread; retain until shutdown_finished.

    detach() revokes delivery only, not worker ownership or shutdown. An owner
    destroying a bridge early must still stop/join its worker independently.
    """

    startup_succeeded = Signal()
    startup_failed = Signal(object)
    result_ready = Signal(object)
    inference_failed = Signal(object)
    submission_failed = Signal(object)
    state_changed = Signal(object)
    shutdown_failed = Signal(object)
    shutdown_finished = Signal()

    def __init__(
        self,
        worker: InferenceWorker,
        parent: QObject | None = None,
        *,
        shutdown_timeout_ms: int = 30_000,
    ) -> None:
        super().__init__(parent)
        if shutdown_timeout_ms <= 0:
            raise ValueError("shutdown_timeout_ms must be positive")
        self.worker = worker
        self._timeout = shutdown_timeout_ms / 1000
        self._deadline: float | None = None
        self._timeout_reported = False
        self._stopped = False
        self._detached = False
        self._state = worker.state
        self._delivery = _Delivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        # The sender is NOT a child of this bridge: destruction must disable
        # callbacks before deleting their sender, including unexpected deletion.
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._monitor = QTimer(self)
        self._monitor.setInterval(250)  # Lifecycle only; never polls task results.
        self._monitor.timeout.connect(self._observe_lifecycle)

    @property
    def stopped(self) -> bool:
        """Actual thread termination observed, not merely a terminal state."""
        return self._stopped

    def _check_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("QtInferenceBridge methods require its GUI thread")
        if self._detached:
            raise RuntimeError("QtInferenceBridge is detached")

    @Slot()
    def start(self) -> None:
        self._check_thread()
        try:
            future = self.worker.start()
        except Exception as exc:
            self.startup_failed.emit(exc)
            return
        self._publish_state()
        self._monitor.start()
        self._delivery.watch("startup", future)

    def submit(self, image_bgr: NDArray[np.generic]) -> None:
        self._check_thread()
        try:
            future = self.worker.submit(image_bgr)
        except Exception as exc:
            self.submission_failed.emit(exc)
            return
        self._delivery.watch("inference", future)

    @Slot()
    def request_stop(self) -> None:
        self._check_thread()
        if self._stopped:
            return
        if self._deadline is None:
            self._deadline = monotonic() + self._timeout
        self.worker.request_stop()
        self._publish_state()
        if not self._monitor.isActive():
            self._monitor.start()
        # Do not synchronously finish in closeEvent's call stack.

    def detach(self) -> None:
        """GUI-thread only; discard queued and future notifications, idempotently."""
        if self._detached:
            return
        self._check_thread()
        self._detached = True
        self._monitor.stop()
        self._delivery.detach()

    @Slot(str, object, object)
    def _completed(self, kind: str, value: object, error: object) -> None:
        if self._detached:
            return
        self._publish_state()
        if kind == "startup":
            if isinstance(error, BaseException):
                self.startup_failed.emit(error)
            elif self._deadline is None and self.worker.state is WorkerState.RUNNING:
                self.startup_succeeded.emit()
        elif isinstance(error, BaseException):
            self.inference_failed.emit(error)
        elif isinstance(value, ImageResult):
            self.result_ready.emit(value)

    def _publish_state(self) -> None:
        state = self.worker.state
        if state is not self._state:
            self._state = state
            self.state_changed.emit(state)

    @Slot()
    def _observe_lifecycle(self) -> None:
        if self._detached:
            return
        self._publish_state()
        if self.worker.state is not WorkerState.NEW and self.worker.join(timeout=0):
            self._monitor.stop()
            self._stopped = True
            if self.worker.failure is not None:
                self.shutdown_failed.emit(self.worker.failure)
            self.shutdown_finished.emit()
        elif (
            self._deadline is not None
            and not self._timeout_reported
            and monotonic() >= self._deadline
        ):
            self._timeout_reported = True
            self.shutdown_failed.emit(
                TimeoutError(
                    "Shutdown timed out; worker still alive. Waiting for native work."
                )
            )
