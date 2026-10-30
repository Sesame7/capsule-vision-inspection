"""One serialized, Qt-independent owner for explicit Web service operations."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import Enum, auto
from threading import Condition, Thread

from capsule_vision.configuration.communication_preferences import WebPreferences

from .service import ProductionSnapshotService


class WebState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class WebRuntimeSnapshot:
    state: WebState = WebState.STOPPED
    configuration: WebPreferences | None = None
    url: str = ""
    error: str = ""
    restored: bool = False
    owned: bool = False
    busy: bool = False
    closing: bool = False


class WebRuntime:
    """One command at a time, no queued restarts or completed-result history.

    The control thread constructs services and waits for their actual termination.
    Futures describe completed operations, not saved preferences or Production.
    Close fences admission immediately, including a pending/active startup; it
    never abandons native or socket owners because a caller's join timed out.
    """

    def __init__(
        self, factory: Callable[[WebPreferences], ProductionSnapshotService]
    ) -> None:
        self._factory = factory
        self._condition = Condition()
        self._snapshot = WebRuntimeSnapshot()
        self._service: ProductionSnapshotService | None = None
        self._pending: (
            tuple[
                WebPreferences | None, WebPreferences | None, Future[WebRuntimeSnapshot]
            ]
            | None
        ) = None
        self._close_requested = False
        self._thread = Thread(
            target=self._run, name="ProductionWebControl", daemon=False
        )
        self._thread.start()

    def snapshot(self) -> WebRuntimeSnapshot:
        with self._condition:
            value = self._snapshot
            service = self._service
            if value.state is WebState.RUNNING and service is not None:
                error = service.publisher.error()
                if error or not (service.http_alive and service.publisher_alive):
                    return replace(
                        value,
                        state=WebState.FAILED,
                        error=error or "Web service thread exited",
                    )
            return value

    @property
    def closed(self) -> bool:
        return not self._thread.is_alive()

    def join(self, timeout: float | None = None) -> bool:
        """Non-GUI callers only; False retains ownership and is not shutdown."""
        self._thread.join(timeout)
        return self.closed

    def start(self, settings: WebPreferences) -> Future[WebRuntimeSnapshot]:
        if not settings.enabled:
            raise ValueError("Save enabled Web settings before Start")
        return self._submit("start", settings)

    def apply(self, settings: WebPreferences) -> Future[WebRuntimeSnapshot]:
        return self._submit("apply", settings)

    def stop(self) -> Future[WebRuntimeSnapshot]:
        return self._submit("stop", None)

    def _submit(
        self, action: str, settings: WebPreferences | None
    ) -> Future[WebRuntimeSnapshot]:
        with self._condition:
            if self._snapshot.closing or self.closed:
                raise RuntimeError("Web runtime is closing")
            if self._snapshot.busy:
                raise RuntimeError("A Web operation is already in progress")
            if action == "start" and self._service is not None:
                raise RuntimeError("Stop or Apply/Restart the existing Web owner")
            future: Future[WebRuntimeSnapshot] = Future()
            future.set_running_or_notify_cancel()  # Accepted operations cannot be cancelled.
            previous = self.snapshot()
            restore = (
                previous.configuration
                if action == "apply" and previous.state is WebState.RUNNING
                else None
            )
            self._pending = (settings, restore, future)
            self._snapshot = replace(
                self._snapshot,
                state=WebState.STOPPING
                if self._service is not None
                or action == "stop"
                or (settings is not None and not settings.enabled)
                else WebState.STARTING,
                busy=True,
                error="",
                restored=False,
            )
            self._condition.notify()
            return future

    def request_close(self) -> None:
        with self._condition:
            if self.closed:
                return
            self._snapshot = replace(
                self._snapshot, closing=True, state=WebState.STOPPING
            )
            self._close_requested = True
            self._condition.notify()

    def _observe(
        self,
        *,
        state: WebState | None = None,
        url: str | None = None,
        error: str | None = None,
        restored: bool | None = None,
        owned: bool | None = None,
        busy: bool | None = None,
    ) -> None:
        with self._condition:
            old = self._snapshot
            self._snapshot = replace(
                old,
                state=old.state
                if state is None
                else WebState.STOPPING
                if old.closing and state in (WebState.STARTING, WebState.RUNNING)
                else state,
                url=old.url if url is None else url,
                error=old.error if error is None else error[:2000],
                restored=old.restored if restored is None else restored,
                owned=old.owned if owned is None else owned,
                busy=old.busy if busy is None else busy,
            )

    def _release(self) -> None:
        service = self._service
        if service is not None:
            self._observe(state=WebState.STOPPING)
            service.request_stop()
            if not service.join():
                raise RuntimeError(
                    "Web service termination is incomplete; owner retained"
                )
            with self._condition:
                self._service = None
                self._snapshot = replace(self._snapshot, configuration=None)
        self._observe(url="", owned=False)

    def _launch(self, settings: WebPreferences) -> None:
        self._observe(state=WebState.STARTING)
        service = self._factory(settings)
        with self._condition:
            self._service = service  # Retain partially started resources on failure.
            self._snapshot = replace(self._snapshot, configuration=settings, owned=True)
        if self.snapshot().closing:
            self._release()
            self._observe(state=WebState.STOPPED)
            return
        service.start()
        self._observe(
            state=WebState.RUNNING, url=f"http://{service.host}:{service.port}/"
        )

    def _operation(
        self, settings: WebPreferences | None, restore: WebPreferences | None
    ) -> None:
        try:
            self._release()
        except Exception as exc:
            self._observe(state=WebState.FAILED, error=f"{type(exc).__name__}: {exc}")
            return
        try:
            if (
                settings is not None
                and settings.enabled
                and not self.snapshot().closing
            ):
                self._launch(settings)
            else:
                self._observe(state=WebState.STOPPED)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._release()
            except Exception as cleanup:
                self._observe(
                    state=WebState.FAILED,
                    error=error + f"; cleanup: {type(cleanup).__name__}: {cleanup}",
                )
                return
            if restore is not None and not self.snapshot().closing:
                try:
                    self._launch(restore)
                except Exception as failure:
                    error += f"; restoration: {type(failure).__name__}: {failure}"
                    try:
                        self._release()
                    except Exception as cleanup:
                        error += f"; cleanup: {type(cleanup).__name__}: {cleanup}"
                else:
                    self._observe(error=error, restored=True)
                    return
            self._observe(state=WebState.FAILED, error=error)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._pending is not None or self._close_requested
                )
                command, self._pending = self._pending, None
                close, self._close_requested = self._close_requested, False
            if command is not None:
                settings, restore, future = command
                self._operation(settings, restore)
                self._observe(busy=False)
                future.set_result(self.snapshot())
            if close or self.snapshot().closing:
                try:
                    self._release()
                except Exception as exc:
                    self._observe(
                        state=WebState.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        busy=False,
                    )
                else:
                    self._observe(state=WebState.STOPPED, busy=False)
                    return
