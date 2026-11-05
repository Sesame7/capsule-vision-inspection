"""One bounded application owner; lifecycle work never runs on the caller thread."""

from collections.abc import Callable, Generator
from concurrent.futures import CancelledError, Future
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Event, Thread, current_thread
from uuid import uuid4

from capsule_vision.communication.reporting import ReportingSettings
from capsule_vision.configuration import CameraSources, MachineLayoutDefinition
from capsule_vision.inspection import CycleId, Frame
from capsule_vision.inspection.production import ProductionInspectionResult
from capsule_vision.localization.languages import Language

from .audit import AuditOperation, error_outcome
from .batch_records import BatchSelection, ReportBatches
from .history import HistoryContext, ProductionHistory
from .identity import LocalIdentity
from .identity_models import Role, SessionSnapshot, StaleSessionError
from .model_preparation import (
    ModelPreparation,
    ModelPreparationFactory,
    ModelPreparationRequest,
    ModelPreparationResult,
    ModelPreparationSnapshot,
    ModelPreparationState,
    real_preparation_engine,
)
from .models import (
    ApplicationCapacityError,
    ApplicationError,
    ApplicationIssue,
    ApplicationState,
    ApplicationStateError,
    ConfigurationIdentity,
    PilotCounters,
    PreparedConfiguration,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from .session import ProductionCycle, RuntimeSession, lifecycle_error
from .vision_dependencies import VisionPreparationRequired


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


@dataclass(frozen=True, slots=True)
class RecipeSelection:
    revision: int | None
    generation: int
    operation: AuditOperation | None = None


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
        preparation_factory: ModelPreparationFactory = real_preparation_engine,
        history: ProductionHistory | None = None,
        identity: LocalIdentity | None = None,
        initial_candidate: PreparedConfiguration | None = None,
    ) -> None:
        if type(max_pending_cycles) is not int or max_pending_cycles < 1:
            raise ValueError("max_pending_cycles must be positive")
        self._factory = session_factory
        self.history = history
        self._export_thread: Thread | None = None
        self._export_cancel = Event()
        self._identity = identity
        self._selection: RecipeSelection | None = None
        self._batch_segment = uuid4().hex
        self._run_id: str | None = None
        self._start_operation: AuditOperation | None = None
        self._durable_run: str | None = None
        self._batch_pending = False
        self._restoring = history is not None
        self._batch_uncertain = self._detail_expired = False
        self._pilot_counters = PilotCounters()
        self._pilot_counting = False
        self._batch_duration_ms: float | None = None
        self._preparation_factory = preparation_factory
        self._model_management = False
        self._preparation: ModelPreparation | None = None
        self._prepare_requested = False
        self._preparation_future: Future[ModelPreparationResult] | None = None
        self._preparation_snapshot = ModelPreparationSnapshot()
        self._max_pending = max_pending_cycles
        self._condition = Condition()
        self._state = ApplicationState.UNCONFIGURED
        self._candidate: PreparedConfiguration | None = None
        self._candidate_requires_preparation = False
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
        if initial_candidate is not None:
            self._install_candidate(initial_candidate)
        if history is not None:
            history.initial_selection.add_done_callback(self._restored)
        self._thread.start()

    @property
    def identity(self) -> LocalIdentity | None:
        return self._identity

    def _admission(
        self,
        minimum: Role,
        revision: int | None = None,
        *,
        action: str = "security.admission",
        target: str = "",
    ) -> AbstractContextManager[SessionSnapshot | None]:
        if self._closing:
            raise ApplicationStateError(f"Command unavailable in {self._state.name}")
        if self.identity is None:  # Explicit identity-free engineering composition.
            return nullcontext(None)
        return self.identity.admission(minimum, revision, action=action, target=target)

    def authorize_engineering(self, revision: int | None = None) -> int | None:
        with self._condition, self._admission(Role.ENGINEER, revision) as actor:
            return actor.revision if actor is not None else None

    @property
    def session_revision(self) -> int | None:
        return self.identity.snapshot().revision if self.identity is not None else None

    def begin_recipe_selection(self, recipe_id: str = "") -> RecipeSelection:
        with (
            self._condition,
            self._admission(
                Role.OPERATOR, action="production.recipe", target=recipe_id
            ) as actor,
        ):
            self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
            self._selection = RecipeSelection(
                actor.revision if actor else None,
                self._generation,
                AuditOperation.by(actor, "production.recipe", recipe_id)
                if actor
                else None,
            )
            return self._selection

    def install_recipe_selection(
        self, request: RecipeSelection, candidate: PreparedConfiguration
    ) -> None:
        with self._condition:
            try:
                with self._admission(Role.OPERATOR, request.revision):
                    if (
                        self._selection is not request
                        or self._generation != request.generation
                    ):
                        raise StaleSessionError("Production selection was superseded")
                    self._install_candidate(candidate)
            except Exception as exc:
                self.recipe_selection_failed(request, exc)
                raise
            if self.identity and request.operation:
                self.identity.audit.record(
                    replace(
                        request.operation, target=candidate.configuration.recipe_id
                    ),
                    "success",
                )

    def recipe_selection_failed(
        self, request: RecipeSelection, error: BaseException
    ) -> None:
        if self.identity and request.operation:
            self.identity.audit.record(
                request.operation, error_outcome(error), type(error).__name__
            )

    @property
    def thread_name(self) -> str:
        return self._thread.name

    def snapshot(self) -> ProductionApplicationSnapshot:
        with self._condition:
            uncertain = self._batch_uncertain or bool(
                self.history
                and self._durable_run
                and self.history.run_uncertain(self._durable_run)
            )
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
                self._active,
                self._candidate,
                self._candidate_requires_preparation,
                pilot_counters=self._pilot_counters,
                pilot_counting=self._pilot_counting,
                batch_duration_ms=self._batch_duration_ms,
                batch_uuid=self._batch_segment if self._batch is not None else None,
                run_id=self._run_id,
                batch_uncertain=self._batch is not None and uncertain,
                unbatched_run_uncertain=self._batch is None and uncertain,
                detail_expired=self._detail_expired
                or bool(self.history and self.history.expired_for(self._batch_segment)),
                batch_pending=self._batch_pending or self._restoring,
            )

    def configure(self, candidate: PreparedConfiguration) -> None:
        recipe = candidate.configuration.recipe_id
        with (
            self._condition,
            self._admission(
                Role.OPERATOR, action="production.recipe", target=recipe
            ) as actor,
        ):
            self._install_candidate(candidate)
            if actor and self.identity:
                self.identity.audit.record(
                    AuditOperation.by(actor, "production.recipe", recipe), "success"
                )

    def _install_candidate(self, candidate: PreparedConfiguration) -> None:
        """Cheap installation of an already resolved/artifact-checked candidate."""
        with self._condition:
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            if (
                self._candidate_requires_preparation
                and self._issue is not None
                and self._issue.exception_type == VisionPreparationRequired.__name__
            ):
                self._issue = None
            self._selection = None
            self._candidate = candidate
            self._candidate_requires_preparation = False
            self._state = ApplicationState.READY

    def observe_saved_vision(
        self,
        machine: MachineLayoutDefinition | None,
        sources: CameraSources | None,
        *,
        machine_known: bool = True,
        sources_known: bool = True,
    ) -> None:
        """Observe successfully saved/read definitions, without IO or activation."""
        with self._condition:
            dependencies = (
                self._candidate.vision_dependencies if self._candidate else None
            )
            if dependencies is not None and (
                (machine_known and machine != dependencies.machine)
                or (
                    sources_known
                    and dependencies.sources_path is not None
                    and sources != dependencies.sources
                )
            ):
                self._candidate_requires_preparation = True

    def _install_batch(self, selection: BatchSelection | None) -> None:
        self._batch = selection.label if selection else None
        self._batch_segment = selection.uuid if selection else uuid4().hex
        self._counters = selection.counters if selection else ProductionCounters()
        self._pilot_counters = selection.groups if selection else PilotCounters()
        if selection and selection.four_cycle is not None:
            self._pilot_counting = selection.four_cycle
        self._batch_duration_ms = selection.duration_ms if selection else None
        self._batch_uncertain = selection.uncertain if selection else False
        self._detail_expired = selection.detail_expired if selection else False
        self._latest = None

    def _restored(self, future: Future[BatchSelection | None]) -> None:
        with self._condition:
            if future.exception() is None:
                self._install_batch(future.result())
            self._restoring = False
            self._condition.notify_all()

    def report_batches(
        self,
        prefix: str = "",
        cursor: str | None = None,
        previous: bool = False,
        *,
        revision: int | None = None,
    ) -> Future[ReportBatches]:
        with self._condition, self._admission(Role.OPERATOR, revision):
            if self.history is None:
                raise ApplicationStateError("History unavailable")
            return self.history.report_batches(prefix, cursor, previous)

    def export_report(
        self,
        batch: str,
        path: Path,
        language: Language,
        *,
        revision: int | None = None,
        cancel: Event | None = None,
    ) -> Future[Path]:
        with self._condition, self._admission(Role.OPERATOR, revision) as actor:
            history = self.history
            if history is None:
                raise ApplicationStateError("History unavailable")
            if self._export_thread is not None and self._export_thread.is_alive():
                raise ApplicationStateError(
                    "A production report is already being exported"
                )
            if path.suffix.lower() != ".xlsx":
                raise ValueError("Select an .xlsx file")
            actor_revision = actor.revision if actor else None
            stop = cancel if cancel is not None else Event()
            future = _promise(Future[Path]())

            def admit_publication() -> None:
                with self._condition, self._admission(Role.OPERATOR, actor_revision):
                    if stop.is_set():
                        raise CancelledError()

            def export() -> None:
                try:
                    # The optional operation loads the spreadsheet library on its own worker.
                    from .production_report import write_report

                    write_report(
                        history, batch, path, language, stop, admit_publication
                    )
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(path)

            self._export_cancel = stop
            self._export_thread = Thread(
                target=export, name="ProductionReport", daemon=False
            )
            self._export_thread.start()
            return future

    def start_batch(
        self, batch_id: str, *, revision: int | None = None
    ) -> Future[None]:
        return self._change_batch(batch_id.strip(), revision)

    def end_batch(
        self, *, revision: int | None = None, expected_batch: str | None = None
    ) -> Future[None]:
        return self._change_batch(None, revision, expected_batch)

    def _change_batch(
        self, label: str | None, revision: int | None, expected_batch: str | None = None
    ) -> Future[None]:
        action = "batch.select" if label is not None else "batch.end"
        with (
            self._condition,
            self._admission(
                Role.OPERATOR,
                revision,
                action=action,
                target=label or self._batch_segment,
            ) as actor,
        ):
            operation = (
                AuditOperation.by(
                    actor,
                    action,
                    self._batch_segment,
                    self._batch or "",
                    label or "",
                )
                if actor
                else None
            )
            try:
                self._require(ApplicationState.READY)
                if expected_batch is not None and self._batch_segment != expected_batch:
                    raise ApplicationStateError(
                        "The selected Batch changed before confirmation"
                    )
                if self._batch_pending or (label is None and self._batch is None):
                    raise ApplicationStateError("Batch selection is unavailable")
                if label == "":
                    raise ApplicationError("Batch label must be nonempty")
            except ApplicationError as exc:
                if operation and self.identity:
                    self.identity.audit.record(operation, "failed", str(exc))
                raise
            done: Future[None] = _promise(Future())
            if self.history is None:
                if label != self._batch:
                    self._install_batch(None)
                    self._batch = label
                if operation and self.identity:
                    self.identity.audit.record(
                        replace(operation, target=self._batch_segment)
                        if label
                        else operation,
                        "success",
                    )
                done.set_result(None)
                return done
            self._batch_pending = True
            change = self.history.select_batch(label)

        def confirmed(future: Future[BatchSelection | None]) -> None:
            error = future.exception()
            completed = operation
            with self._condition:
                if error is None:
                    selection = future.result()
                    self._install_batch(selection)
                    if selection and operation:
                        completed = replace(operation, target=selection.uuid)
                if completed and self.identity:
                    self.identity.audit.record(
                        completed,
                        error_outcome(error) if error else "success",
                        type(error).__name__ if error else "",
                    )
                self._batch_pending = False
            if error is None:
                done.set_result(None)
            else:
                done.set_exception(error)

        change.add_done_callback(confirmed)
        return done

    def _require(self, *states: ApplicationState) -> None:
        if self._closing or self._model_management or self._state not in states:
            raise ApplicationStateError(f"Command unavailable in {self._state.name}")

    def start(self) -> Future[None]:
        with (
            self._condition,
            self._admission(Role.OPERATOR, action="production.start") as actor,
        ):
            try:
                return self._admit_start(actor)
            except Exception as exc:
                self._rejected_start(actor, exc)
                raise

    def _rejected_start(self, actor: SessionSnapshot | None, error: Exception) -> None:
        if actor and self.identity:
            recipe = self._candidate or self._active
            self.identity.audit.record(
                AuditOperation.by(
                    actor,
                    "production.start",
                    recipe.configuration.recipe_id if recipe else "",
                ),
                error_outcome(error),
                type(error).__name__,
            )

    def _admit_start(self, actor: SessionSnapshot | None) -> Future[None]:
        with self._condition:
            self._require(ApplicationState.READY)
            if self._batch_pending:
                raise ApplicationStateError(
                    "Batch change awaiting storage confirmation"
                )
            if self._candidate_requires_preparation:
                raise ApplicationStateError(
                    "Vision requires re-preparation; reselect the saved Recipe"
                )
            if self._preparation is not None:
                raise ApplicationStateError("Model preparation still owns resources")
            self._generation += 1
            self._state = ApplicationState.STARTING
            self._latest = None
            self._issue = None
            self._startup = _promise(Future())
            prepared = self._candidate or self._active
            recipe = prepared.configuration.recipe_id if prepared else ""
            self._start_operation = (
                AuditOperation.by(actor, "production.start", recipe) if actor else None
            )
            self._stopped = None
            self._start_requested = True
            self._stop_requested = False
            self._condition.notify_all()
            return self._startup

    def preparation_snapshot(self) -> ModelPreparationSnapshot:
        with self._condition:
            return self._preparation_snapshot

    @contextmanager
    def model_management(
        self,
        model_id: str | None = None,
        *,
        deleting: bool = False,
        revision: int | None = None,
    ) -> Generator[None]:
        with self._model_files(
            Role.ENGINEER, revision, model_id=model_id, deleting=deleting
        ):
            yield

    @contextmanager
    def recipe_preparation(self, request: RecipeSelection) -> Generator[None]:
        with self._model_files(Role.OPERATOR, request.revision, selection=request):
            yield

    @contextmanager
    def _model_files(
        self,
        minimum: Role,
        revision: int | None,
        *,
        model_id: str | None = None,
        deleting: bool = False,
        selection: RecipeSelection | None = None,
    ) -> Generator[None]:
        """Fence file/SDK metadata work against Start/Prepare/Close, without IO under lock."""
        with self._condition, self._admission(minimum, revision):
            if selection is not None and (
                selection is not self._selection
                or self._generation != selection.generation
            ):
                raise StaleSessionError("Production selection was superseded")
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            if self._session is not None or self._preparation is not None:
                raise ApplicationStateError(
                    "Native resources still own the model/cache"
                )
            if deleting and any(
                candidate is not None
                and candidate.configuration.model.model_id == model_id
                for candidate in (self._candidate, self._active)
            ):
                raise ApplicationStateError("Candidate/active Model cannot be deleted")
            self._model_management = True
        try:
            yield
        finally:
            with self._condition:
                self._model_management = False
                self._condition.notify_all()

    def prepare_model_cache(
        self, request: ModelPreparationRequest
    ) -> Future[ModelPreparationResult]:
        """Admit one explicit model identity, without sources/Recipe activation."""
        with self._condition, self._admission(Role.ENGINEER) as actor:
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            if self._preparation is not None or self._session is not None:
                raise ApplicationStateError("An engine owner is already admitted")
            self._preparation = ModelPreparation(request, self._preparation_factory)
            self._cache_revision = actor.revision if actor else None
            self._preparation_future = _promise(Future())
            self._preparation_snapshot = ModelPreparationSnapshot(
                ModelPreparationState.PREPARING, request, owns_resources=True
            )
            self._prepare_requested = True
            self._condition.notify_all()
            return self._preparation_future

    def _prepare_model(self, operation: ModelPreparation) -> None:
        result = ModelPreparationResult(operation.request, False, True)
        try:
            scope = (
                self.identity.change(
                    "model.cache",
                    operation.request.model.model_id,
                    self._cache_revision,
                    after=f"device {operation.request.device_id}",
                )
                if self.identity
                else nullcontext()
            )
            with scope:
                result, error = operation.run()
                if error is not None:
                    raise error
        except BaseException as exc:
            error = exc
        with self._condition:
            future = self._preparation_future
            if result.cleanup_complete:
                self._preparation = None
            elif error is not None:
                self._fault("model_preparation", error)
            self._preparation_snapshot = ModelPreparationSnapshot(
                ModelPreparationState.FAILED
                if error
                else ModelPreparationState.SUCCEEDED,
                operation.request,
                result,
                ApplicationIssue(
                    "model_preparation",
                    "preparation_failed",
                    type(error).__name__,
                    str(error),
                )
                if error
                else None,
                not result.cleanup_complete,
            )
        assert future is not None
        if error is not None:
            future.set_exception(
                lifecycle_error(
                    "Model cache preparation failed"
                    if result.cleanup_complete
                    else "Model cache preparation failed; cleanup remains incomplete",
                    error,
                )
            )
        else:
            future.set_result(result)

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
            if self.identity:
                self.identity.audit.observe(
                    AuditOperation.by(
                        self.identity.snapshot(),
                        "production.stop",
                        self._run_id
                        or (
                            f"Start {self._start_operation.id}"
                            if self._start_operation
                            else ""
                        ),
                    ),
                    future,
                )
            self._stop_requested = True
            if self._state is not ApplicationState.FAULT:
                self._state = ApplicationState.STOPPING
            session = self._session
            preparation = self._preparation
            self._condition.notify_all()
        if session is not None:
            session.request_stop()
        if preparation is not None:
            preparation.request_stop()
        return future

    def close(self) -> Future[None]:
        with self._condition:
            if self._closed is not None:
                return self._closed
            self._closed = _promise(Future())
            self._closing = True
            self._export_cancel.set()
            self._stop_requested = True
            if self._state is not ApplicationState.FAULT:
                self._state = ApplicationState.STOPPING
            session = self._session
            preparation = self._preparation
            self._condition.notify_all()
            future = self._closed
        if session is not None:
            session.request_stop()
        if preparation is not None:
            preparation.request_stop()
        return future

    def join(self, timeout: float | None = None) -> bool:
        """Confirm lifecycle-thread exit after close; a timeout never changes state."""
        if current_thread() is self._thread:
            raise ApplicationStateError("Application thread cannot join itself")
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def reset_fault(self) -> None:
        with self._condition, self._admission(Role.OPERATOR):
            self._require(ApplicationState.FAULT)
            if (
                self._session is not None
                or self._pending
                or self._preparation is not None
            ):
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
            if self._durable_run and self.history:
                self.history.admitted(self._durable_run, pending.cycle_id)
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
                if self.history is not None:
                    assert self._active is not None and self._run_id is not None
                    self.history.offer(
                        HistoryContext(
                            self.history.clock(),
                            self._batch_segment,
                            self._batch,
                            self._run_id,
                            ConfigurationIdentity.from_prepared(self._active),
                            self._active.configuration.display_name,
                        ),
                        result,
                    )
            except BaseException as exc:
                error = exc
                if self.history and self._durable_run:
                    self.history.missing_terminal(self._durable_run)
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

    def _settle_startup(self) -> None:
        """Settle canceled pre-session work on the lifecycle owner before Stop completes."""

    def _records_unbatched_runs(self) -> bool:
        return False

    def _run_reporting(self) -> ReportingSettings | None:
        return None

    def _close_resources(self) -> None:
        """Application-lifetime resources; called only by the lifecycle owner."""
        if self._export_thread is not None:
            self._export_thread.join()
        if self.history is not None:
            self.history.request_stop()
            if not self.history.join():
                raise ApplicationError("History persistence owner remains alive")

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        not self._model_management
                        and (
                            self._start_requested
                            or self._stop_requested
                            or self._closing
                            or self._prepare_requested
                        )
                    )
                )
                starting = self._start_requested
                self._start_requested = False
                prepared = self._candidate or self._active
                generation = self._generation
                startup = self._startup if starting else None
                start_operation = self._start_operation if starting else None
                preparation = self._preparation if self._prepare_requested else None
                self._prepare_requested = False
            if preparation is not None:
                self._prepare_model(preparation)
                continue
            startup_error: BaseException | None = None
            if starting:
                try:
                    assert prepared is not None
                    if prepared.vision_dependencies is not None:
                        prepared.vision_dependencies.validate_current()
                    with self._condition:
                        self._condition.wait_for(lambda: not self._restoring)
                        batch = self._batch_segment if self._batch is not None else None
                        record_unbatched = self._records_unbatched_runs()
                        if (
                            batch is not None
                            and record_unbatched
                            and self.identity is not None
                            and (
                                self.history is None
                                or not self.history.snapshot().accepting
                            )
                        ):
                            raise ApplicationStateError(
                                "Named Production Batch requires available History storage"
                            )
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
                    run_id = uuid4().hex
                    durable_run = None
                    if self.history is not None and (
                        record_unbatched or batch is not None
                    ):
                        recording = self.history.start_run(
                            batch,
                            run_id,
                            ConfigurationIdentity.from_prepared(prepared),
                            self._run_reporting(),
                        )
                        try:
                            recording.result()
                        except Exception:
                            if batch is not None:
                                raise
                            # History exposes the storage failure. An unbatched
                            # inspection can proceed with volatile accounting.
                        else:
                            durable_run = run_id
                    with self._condition:
                        self._durable_run = durable_run
                        if self._stop_requested or session.finished.done():
                            raise ApplicationStateError(
                                "Startup stopped before activation"
                            )
                        self._active = prepared
                        self._run_id = run_id
                        if record_unbatched and batch is None:
                            # Anonymous History filtering uses the Run itself.
                            self._batch_segment = run_id
                            self._batch_uncertain |= durable_run is None
                        self._state = ApplicationState.RUNNING
                    if start_operation and self.identity:
                        self.identity.audit.record(
                            replace(
                                start_operation,
                                target=run_id,
                                after=prepared.configuration.recipe_id,
                            ),
                            "success",
                        )
                    assert startup is not None
                    startup.set_result(None)
                except BaseException as exc:
                    startup_error = exc
                    with self._condition:
                        if isinstance(exc, VisionPreparationRequired):
                            self._candidate_requires_preparation = True
                        self._issue = ApplicationIssue(
                            "startup", "startup_failed", type(exc).__name__, str(exc)
                        )
                        self._stop_requested = True
            with self._condition:
                self._condition.wait_for(lambda: self._stop_requested or self._closing)
                session = self._session
            cleanup_error: BaseException | None = None
            resource_error = False
            resources_checked = False
            session_settled = session is None
            try:
                self._settle_startup()
                if self._preparation is not None:
                    if not self._preparation.settle():
                        raise ApplicationError(
                            "Model preparation cleanup remains incomplete"
                        )
                    with self._condition:
                        self._preparation = None
                        self._preparation_snapshot = replace(
                            self._preparation_snapshot, owns_resources=False
                        )
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
                    session_settled = True
            except BaseException as exc:
                cleanup_error = exc
            if session_settled:
                with self._condition:
                    run, active = self._durable_run, self._run_id
                    faulty = (
                        self._state is ApplicationState.FAULT
                        or cleanup_error is not None
                        or bool(self._pending)
                    )
                if run and self.history:
                    outcome = (
                        "unactivated"
                        if active is None
                        else "fault"
                        if faulty
                        else "stopped"
                    )
                    try:
                        self.history.finish_run(run, outcome, faulty).result()
                        with self._condition:
                            self._batch_uncertain |= (
                                faulty or self.history.run_uncertain(run)
                            )
                    except Exception as exc:
                        with self._condition:
                            self._batch_uncertain = True
                            self._issue = ApplicationIssue(
                                "history",
                                "run_end_unconfirmed",
                                type(exc).__name__,
                                str(exc),
                            )
            if self._closing and session_settled and self._preparation is None:
                resources_checked = True
                try:
                    self._close_resources()
                except BaseException as exc:
                    cleanup_error = exc
                    resource_error = True
            with self._condition:
                if session_settled:
                    self._session = None
                    self._durable_run = self._run_id = None
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
                # Close may arrive after the resource check above. In that case
                # leave its request pending for the next owner iteration; never
                # announce CLOSED without running application-resource cleanup.
                closing = self._closing and (
                    resources_checked
                    or self._session is not None
                    or self._preparation is not None
                )
                closed = self._closed
                can_close = (
                    self._session is None
                    and self._preparation is None
                    and not resource_error
                )
                if closing and not can_close:
                    self._closing = False
                    self._closed = (
                        None  # Permit an explicit later cleanup/close attempt.
                    )
            if startup is not None and not startup.done():
                if start_operation and self.identity:
                    cause = startup_error or cleanup_error
                    self.identity.audit.record(
                        start_operation,
                        error_outcome(cause) if cause else "cancelled",
                        type(cause).__name__ if cause else "Stopped",
                    )
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
                if can_close:
                    # Pending Start/Stop outcomes above must reach audit before its final drain.
                    try:
                        if self.identity:
                            self.identity.request_stop()
                            if not self.identity.join():
                                raise ApplicationError(
                                    "Local identity owner remains alive"
                                )
                    except BaseException as exc:
                        cleanup_error = exc
                        with self._condition:
                            self._fault("shutdown", exc)
                            self._closing, self._closed = False, None
                    else:
                        with self._condition:
                            self._state = ApplicationState.CLOSED
                        closed.set_result(None)
                        return
                closed.set_exception(
                    lifecycle_error(
                        "Close failed",
                        cleanup_error or ApplicationError("Runtime still owned"),
                    )
                )
