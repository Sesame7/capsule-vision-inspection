"""Future notifications cross to Qt through an explicitly queued connection."""

from concurrent.futures import Future
from threading import Lock

from PySide6.QtCore import QObject, Signal


class _Emitter(QObject):
    completed = Signal(str, object, object)


class FutureDelivery:
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
