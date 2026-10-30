"""Window-owned stopped editing dispatch; one setup/preparation job, no queue."""

from collections.abc import Callable
from concurrent.futures import Future
from threading import Thread

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from capsule_vision.application import ApplicationState, PreparedConfiguration
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.configuration import ConfigurationError, ModelArtifactError

from .future_delivery import FutureDelivery
from .production_bridge import QtProductionBridge


class EditingCoordinator(QObject):
    changed = Signal()
    applied = Signal(object)
    failed = Signal(object, str)

    def __init__(
        self,
        bridge: QtProductionBridge,
        session: RecipeEditorSession | None,
        *,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.bridge, self.session = bridge, session
        self.closing = False
        self._thread: Thread | None = None
        self._completion: tuple[object, object] | None = None
        self._completed: Callable[[object, BaseException | None], None] | None = None
        self._cleanup = False
        self._request = 0
        self._after_idle: Callable[[], None] | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._delivered, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self._monitor = QTimer(self)
        self._monitor.setInterval(20)
        self._monitor.timeout.connect(self._finish)

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def editable(self) -> bool:
        return (
            self.session is not None
            and not self.busy
            and not self.closing
            and self.bridge.application.snapshot().state
            in (ApplicationState.UNCONFIGURED, ApplicationState.READY)
        )

    def run_io(
        self,
        work: Callable[[], object],
        completed: Callable[[object, BaseException | None], None],
        *,
        closing_cleanup: bool = False,
        name: str = "CameraSetupIO",
    ) -> bool:
        if self.busy or (self.closing and not closing_cleanup):
            return False
        self._request += 1
        token = str(self._request)
        self._completed, self._cleanup = completed, closing_cleanup
        future: Future[object] = Future()

        def run() -> None:
            try:
                future.set_result(work())
            except BaseException as exc:
                future.set_exception(exc)

        self._delivery.watch(token, future)
        self._thread = Thread(target=run, name=name, daemon=False)
        self._thread.start()
        self._monitor.start()
        self.changed.emit()
        return True

    def run_model_io(
        self,
        work: Callable[[], object],
        completed: Callable[[object, BaseException | None], None],
        *,
        model_id: str | None = None,
        deleting: bool = False,
    ) -> bool:
        if not self.editable():
            return False

        def owned() -> object:
            with self.bridge.application.model_management(model_id, deleting=deleting):
                return work()

        return self.run_io(owned, completed)

    def apply(self, completed: Callable[[BaseException | None], None]) -> None:
        session = self.session
        try:
            if not self.editable() or session is None:
                raise ConfigurationError(
                    "Editing is unavailable in the current application state"
                )
            recipe = session.apply_recipe()
            entry = session.entry
        except ConfigurationError as exc:
            self.failed.emit(exc, "recipe_error")
            completed(exc)
            return

        def prepared(value: object, error: BaseException | None) -> None:
            try:
                if error is not None:
                    raise ConfigurationError(str(error)) from error
                if not isinstance(value, PreparedConfiguration):
                    raise ConfigurationError("Missing prepared configuration")
                if (
                    session.entry is not entry
                    or session.dirty
                    or session.machine_editor.dirty
                    or session.machine_editor.saved.layout != value.configuration.layout
                ):
                    raise ConfigurationError(
                        "Recipe selection changed during preparation"
                    )
                self.bridge.application.configure(value)
                self.applied.emit(value)
                self.bridge.refresh()
            except Exception as exc:
                self.failed.emit(
                    exc,
                    "model_prepare_error"
                    if isinstance(error, ModelArtifactError)
                    else "recipe_prepare_error",
                )
                completed(exc)
            else:
                completed(None)

        self.run_io(lambda: session.prepare(recipe), prepared, name="RecipePreparation")

    @Slot(str, object, object)
    def _delivered(self, token: str, value: object, error: object) -> None:
        if self.busy and token == str(self._request):
            self._completion = value, error
            self._finish()

    def _finish(self) -> None:
        thread = self._thread
        if thread is None or thread.is_alive() or self._completion is None:
            return
        thread.join(0)
        self._thread = None
        self._monitor.stop()
        value, error = self._completion
        self._completion = None
        completed, self._completed = self._completed, None
        failure = error if isinstance(error, BaseException) else None
        if self.closing and not self._cleanup:
            failure = failure or RuntimeError("Closing")
        if completed is not None:
            completed(value, failure)
        self.changed.emit()
        if not self.busy and self._after_idle is not None:
            after, self._after_idle = self._after_idle, None
            after()

    def when_idle(self, after: Callable[[], None]) -> None:
        if self.busy:
            self._after_idle = after
        else:
            after()

    def drain(self, after: Callable[[], None]) -> None:
        """Reject late activation; wait for the actual setup thread and cleanup chain."""
        self.closing = True
        if self.busy:
            self._after_idle = after
        else:
            after()
        self.changed.emit()

    def resume(self, *, notify: bool = True) -> None:
        self.closing = False
        if notify:
            self.changed.emit()

    def dispose(self) -> None:
        if self.busy:
            raise RuntimeError("Setup job must actually join before disposal")
        self.closing = True
        self._monitor.stop()
        self._delivery.detach()
