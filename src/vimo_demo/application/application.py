"""One bounded application owner; lifecycle work never runs on the caller thread."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Condition, Thread, current_thread

from vimo_demo.inspection import CycleId, Frame
from vimo_demo.inspection.production import ProductionInspectionResult

from .models import (
    ApplicationCapacityError,
    ApplicationError,
    ApplicationIssue,
    ApplicationState,
    ApplicationStateError,
    ConfigurationIdentity,
    PreparedConfiguration,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from .session import ProductionCycle, RuntimeSession, lifecycle_error


def _promise[T](future: Future[T]) -> Future[T]:
    future.set_running_or_notify_cancel()
    return future


@dataclass
class _Pending:
    generation: int
    cycle_id: CycleId
    source: ProductionCycle
    public: ProductionCycle
    admitted: bool = False


class ProductionApplication:
    """Thread-safe commands/snapshots; Futures are noncancellable observations.

    Factories must return an unstarted session or raise without owning resources.
    Future callbacks must be short/nonblocking; never join this owner in one.
    """

    def __init__(
        self,
        session_factory: Callable[[PreparedConfiguration, int], RuntimeSession],
        *,
        max_pending_cycles: int = 8,
    ) -> None:
        if type(max_pending_cycles) is not int or max_pending_cycles < 1:
            raise ValueError("max_pending_cycles must be positive")
        self._factory = session_factory
        self._max_pending = max_pending_cycles
        self._condition = Condition()
        self._state = ApplicationState.UNCONFIGURED
        self._candidate: PreparedConfiguration | None = None
        self._active: PreparedConfiguration | None = None
        self._session: RuntimeSession | None = None
        self._generation = 0
        self._batch: str | None = None
        self._counters = ProductionCounters()
        self._latest: ProductionInspectionResult | None = None
        self._issue: ApplicationIssue | None = None
        self._pending: dict[object, _Pending] = {}
        self._start_requested = self._stop_requested = self._closing = False
        self._startup: Future[None] | None = None
        self._stopped: Future[None] | None = None
        self._closed: Future[None] | None = None
        self._thread = Thread(
            target=self._run, name=f"ProductionApplication-{id(self):x}", daemon=False
        )
        self._thread.start()

    @property
    def thread_name(self) -> str:
        return self._thread.name

    def snapshot(self) -> ProductionApplicationSnapshot:
        with self._condition:
            return ProductionApplicationSnapshot(
                self._state,
                self._generation,
                ConfigurationIdentity.from_prepared(self._active)
                if self._active
                else None,
                ConfigurationIdentity.from_prepared(self._candidate)
                if self._candidate
                else None,
                self._batch,
                self._counters,
                self._latest,
                self._session is not None,
                self._issue,
            )

    def configure(self, candidate: PreparedConfiguration) -> None:
        """Cheap installation of an already resolved/artifact-checked candidate."""
        with self._condition:
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            self._candidate = candidate
            self._state = ApplicationState.READY

    def start_batch(self, batch_id: str | None) -> None:
        with self._condition:
            self._require(ApplicationState.READY)
            if batch_id is not None and not batch_id.strip():
                raise ApplicationError("batch_id must be nonempty or None")
            self._batch = batch_id
            self._counters = ProductionCounters()
            self._latest = None

    def _require(self, *states: ApplicationState) -> None:
        if self._closing or self._state not in states:
            raise ApplicationStateError(f"Command unavailable in {self._state.name}")

    def start(self) -> Future[None]:
        with self._condition:
            self._require(ApplicationState.READY)
            self._generation += 1
            self._state = ApplicationState.STARTING
            self._latest = None
            self._issue = None
            self._startup = _promise(Future())
            self._stopped = None
            self._start_requested = True
            self._stop_requested = False
            self._condition.notify_all()
            return self._startup

    def stop(self) -> Future[None]:
        with self._condition:
            self._require(
                ApplicationState.UNCONFIGURED,
                ApplicationState.READY,
                ApplicationState.STARTING,
                ApplicationState.RUNNING,
                ApplicationState.STOPPING,
                ApplicationState.FAULT,
            )
            if self._stopped is not None and not self._stopped.done():
                return self._stopped
            self._stopped = _promise(Future())
            future = self._stopped
            if self._session is None and self._state in (
                ApplicationState.READY,
                ApplicationState.UNCONFIGURED,
            ):
                future.set_result(None)
                return future
            self._stop_requested = True
            if self._state is not ApplicationState.FAULT:
                self._state = ApplicationState.STOPPING
            session = self._session
            self._condition.notify_all()
        if session is not None:
            session.request_stop()
        return future

    def close(self) -> Future[None]:
        with self._condition:
            if self._closed is not None:
                return self._closed
            self._closed = _promise(Future())
            self._closing = True
            self._stop_requested = True
            if self._state is not ApplicationState.FAULT:
                self._state = ApplicationState.STOPPING
            session = self._session
            self._condition.notify_all()
            future = self._closed
        if session is not None:
            session.request_stop()
        return future

    def join(self, timeout: float | None = None) -> bool:
        """Confirm lifecycle-thread exit after close; a timeout never changes state."""
        if current_thread() is self._thread:
            raise ApplicationStateError("Application thread cannot join itself")
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def reset_fault(self) -> None:
        with self._condition:
            self._require(ApplicationState.FAULT)
            if self._session is not None or self._pending:
                raise ApplicationStateError(
                    "Runtime ownership must be safely released before reset"
                )
            self._issue = None
            self._state = (
                ApplicationState.READY
                if self._candidate or self._active
                else ApplicationState.UNCONFIGURED
            )

    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle:
        with self._condition:
            self._require(ApplicationState.RUNNING)
            session = self._session
            assert session is not None
            if len(self._pending) >= self._max_pending:
                raise ApplicationCapacityError(
                    "Outstanding application cycle capacity reached"
                )
            source = session.begin_cycle(cycle_id)
            public = ProductionCycle(_promise(Future()), _promise(Future()))
            token = object()
            self._pending[token] = _Pending(self._generation, cycle_id, source, public)
            source.accepted.add_done_callback(lambda _: self._admitted(token))
            source.result.add_done_callback(lambda _: self._completed(token))
            return public

    def submit_frame(self, frame: Frame) -> Future[None]:
        with self._condition:
            self._require(ApplicationState.RUNNING)
            assert self._session is not None
            return self._session.submit_frame(frame)

    def expire(self) -> Future[None]:
        with self._condition:
            self._require(ApplicationState.RUNNING)
            assert self._session is not None
            return self._session.expire()

    def _admitted(self, token: object) -> None:
        with self._condition:
            pending = self._pending.get(token)
            if (
                pending is None
                or pending.generation != self._generation
                or pending.admitted
            ):
                return
            try:
                pending.source.accepted.result()
            except BaseException as exc:
                del self._pending[token]
                pending.public.accepted.set_exception(exc)
                pending.public.result.set_exception(exc)
                return
            pending.admitted = True
            self._counters = self._counters.admitted()
        pending.public.accepted.set_result(None)
        if pending.source.result.done():
            self._completed(token)

    def _completed(self, token: object) -> None:
        error: BaseException | None = None
        result: ProductionInspectionResult | None = None
        with self._condition:
            pending = self._pending.get(token)
            if (
                pending is None
                or pending.generation != self._generation
                or not pending.admitted
            ):
                return
            del self._pending[token]
            try:
                result = pending.source.result.result()
                if result.cycle_id != pending.cycle_id:
                    raise ApplicationError(
                        "Runtime returned a different cycle identity"
                    )
                self._counters = self._counters.completed(result)
                self._latest = result
            except BaseException as exc:
                error = exc
                if isinstance(exc, ApplicationError) or (
                    self._session is not None and self._session.failure is not None
                ):
                    self._fault("runtime", exc)
        if error is not None:
            pending.public.result.set_exception(error)
        else:
            assert result is not None
            pending.public.result.set_result(result)

    def _fault(self, stage: str, error: BaseException) -> None:
        self._state = ApplicationState.FAULT
        self._issue = ApplicationIssue(
            stage, "lifecycle_failure", type(error).__name__, str(error)
        )
        self._stop_requested = True
        self._condition.notify_all()

    def _session_finished(self, generation: int, session: RuntimeSession) -> None:
        with self._condition:
            if generation != self._generation or session is not self._session:
                return
            if self._state is ApplicationState.RUNNING:
                self._fault(
                    "runtime",
                    session.failure
                    or ApplicationError("Runtime terminated unexpectedly"),
                )
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        self._start_requested or self._stop_requested or self._closing
                    )
                )
                starting = self._start_requested
                self._start_requested = False
                prepared = self._candidate or self._active
                generation = self._generation
                startup = self._startup if starting else None
            startup_error: BaseException | None = None
            if starting:
                try:
                    assert prepared is not None
                    session = self._factory(prepared, generation)
                    with self._condition:
                        self._session = session
                        stopped = self._stop_requested
                    session.finished.add_done_callback(
                        lambda _, g=generation, s=session: self._session_finished(g, s)
                    )
                    if stopped:
                        session.request_stop()
                        raise ApplicationStateError("Stopped before runtime startup")
                    session.start().result()
                    with self._condition:
                        if self._stop_requested or session.finished.done():
                            raise ApplicationStateError(
                                "Startup stopped before activation"
                            )
                        self._active = prepared
                        self._state = ApplicationState.RUNNING
                    assert startup is not None
                    startup.set_result(None)
                except BaseException as exc:
                    startup_error = exc
                    with self._condition:
                        self._issue = ApplicationIssue(
                            "startup", "startup_failed", type(exc).__name__, str(exc)
                        )
                        self._stop_requested = True
            with self._condition:
                self._condition.wait_for(lambda: self._stop_requested or self._closing)
                session = self._session
            cleanup_error: BaseException | None = None
            try:
                if session is not None:
                    session.request_stop()
                    if not session.join():
                        raise ApplicationError("Runtime threads remain alive")
                    if not session.cleanup_safe:
                        raise ApplicationError(
                            "Runtime cleanup could not establish safe ownership release"
                        )
                    # Startup errors can be recoverable. Failures after activation
                    # include terminal worker/engine-close faults and are not ignored.
                    if startup_error is None and session.failure is not None:
                        cleanup_error = lifecycle_error(
                            "Runtime failed", session.failure
                        )
                    with self._condition:
                        self._session = None
            except BaseException as exc:
                cleanup_error = exc
            with self._condition:
                if self._pending:
                    cleanup_error = cleanup_error or ApplicationError(
                        "Runtime terminated with unresolved cycle observations"
                    )
                    for pending in self._pending.values():
                        for future in (pending.public.accepted, pending.public.result):
                            if not future.done():
                                future.set_exception(cleanup_error)
                    self._pending.clear()
                if cleanup_error is not None:
                    self._fault("shutdown", cleanup_error)
                elif self._state is not ApplicationState.FAULT:
                    self._state = (
                        ApplicationState.READY
                        if self._candidate or self._active
                        else ApplicationState.UNCONFIGURED
                    )
                self._stop_requested = False
                stopped = self._stopped
                closing = self._closing
                closed = self._closed
                if closing and self._session is None:
                    self._state = ApplicationState.CLOSED
                elif closing:
                    self._closing = False
                    self._closed = (
                        None  # Permit an explicit later cleanup/close attempt.
                    )
            if startup is not None and not startup.done():
                startup.set_exception(
                    lifecycle_error(
                        "Startup did not activate candidate",
                        startup_error
                        or cleanup_error
                        or ApplicationStateError("Stopped"),
                    )
                )
            if stopped is not None and not stopped.done():
                if cleanup_error:
                    stopped.set_exception(lifecycle_error("Stop failed", cleanup_error))
                else:
                    stopped.set_result(None)
            if closing and closed is not None:
                if self._session is None:
                    closed.set_result(None)
                    return
                closed.set_exception(
                    lifecycle_error(
                        "Close failed",
                        cleanup_error or ApplicationError("Runtime still owned"),
                    )
                )
