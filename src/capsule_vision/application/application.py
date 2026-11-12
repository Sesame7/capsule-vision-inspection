"""One bounded application owner; lifecycle work never runs on the caller thread."""

from collections.abc import Callable, Generator
from concurrent.futures import CancelledError, Future
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Event, Thread, current_thread
from uuid import uuid4

from capsule_vision.communication.reporting import ReportingMode, ReportingSettings
from capsule_vision.configuration import (
    CameraSources,
    MachineLayoutDefinition,
    RecipeDefinition,
)
from capsule_vision.configuration.alarm_definitions import (
    TEMPLATE,
    AlarmDefinitions,
    create_local_alarm_definitions,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import (
    StatusPreferences,
    load_communication_preferences,
)
from capsule_vision.diagnostics import RunDiagnostics, RuntimeDiagnostics
from capsule_vision.inspection import CycleId, CycleStatus, Frame
from capsule_vision.inspection.production import ProductionInspectionResult
from capsule_vision.localization.languages import Language

from .alarm_monitor import AlarmMonitor, AlarmSnapshot
from .alarm_records import AlarmEvent, AlarmQuery, AlarmRecord
from .alarm_report import AlarmReportRequest
from .audit import AuditOperation, error_outcome
from .audit_report import AuditReportRequest
from .batch_records import BatchSelection, ReportBatches
from .batch_review import BatchInformation, BatchSetup, StartAccount
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
from .process_application import apply_process, process_target
from .recipe_dependencies import RecipePreparationRequired
from .report_scope import DefectScope, HistoryQuery, ReportRequest
from .run_configuration import RunConfiguration, capture_run
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
    recipe_id: str = ""
    automatic: bool = False


@dataclass(frozen=True, slots=True)
class _ProcessApplication:
    candidate: PreparedConfiguration
    revision: int | None
    cancelled: Event
    future: Future[None]


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
        initial_status: StatusPreferences | None = None,
        diagnostics: RuntimeDiagnostics | None = None,
        communication_path: Path | None = None,
    ) -> None:
        if type(max_pending_cycles) is not int or max_pending_cycles < 1:
            raise ValueError("max_pending_cycles must be positive")
        self._factory = session_factory
        self.diagnostics = diagnostics
        self.run_diagnostics: RunDiagnostics | None = None
        self.history = history
        self._export_thread: Thread | None = None
        self._export_cancel = Event()
        self._identity = identity
        self._selection: RecipeSelection | None = None
        self._communication_path = communication_path
        self._process_job: _ProcessApplication | None = None
        self._process_pending: _ProcessApplication | None = None
        self._process_active: _ProcessApplication | None = None
        self._process_required = False
        self._process_settings: StatusPreferences | None = None
        self._batch_segment = uuid4().hex
        self._run_id: str | None = None
        self._start_operation: AuditOperation | None = None
        self._durable_run: str | None = None
        self._batch_pending = False
        self._restoring = history is not None
        self._batch_uncertain = self._detail_expired = False
        self._pilot_counters = PilotCounters()
        self._pilot_counting = False
        self._counting_mode = ReportingMode.PER_CYCLE
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
        self._alarms = (
            AlarmMonitor(
                history.path.with_name(TEMPLATE.name),
                history.offer_alarms,
                self._alarm_context,
                state=self._status_state,
                diagnostics=diagnostics,
            )
            if history is not None and initial_status is not None
            else None
        )
        if initial_candidate is not None:
            self._install_candidate(initial_candidate)
            # Guest restoration is observation only; an authorized re-selection
            # admits writes. Explicit identity-free compositions remain trusted.
            if identity is None:
                self._queue_process(None)
        if history is not None:
            history.initial_selection.add_done_callback(self._restored)
        self._thread.start()
        if self._alarms is not None and initial_status is not None:
            self._alarms.apply_saved(initial_status)

    def _status_state(self) -> ApplicationState:
        with self._condition:
            return self._state

    def _alarm_context(self) -> tuple[float, str | None, str | None]:
        with self._condition:
            assert self.history is not None
            return (
                self.history.clock(),
                self._batch_segment if self._batch is not None else None,
                self._batch,
            )

    def alarm_snapshot(self) -> AlarmSnapshot:
        return self._alarms.snapshot() if self._alarms is not None else AlarmSnapshot()

    def preview_alarm_definitions(self) -> AlarmDefinitions:
        """Read-only worker operation; neither activates nor changes the monitor."""
        if self._alarms is None:
            raise ApplicationStateError(
                "Alarm definitions unavailable for this launcher"
            )
        return load_alarm_definitions(self._alarms.definitions_path)

    def alarm_definitions_path(self, revision: int | None = None) -> Path:
        with self._condition, self._admission(Role.ENGINEER, revision):
            if self._alarms is None:
                raise ApplicationStateError(
                    "Alarm definitions unavailable for this launcher"
                )
            return self._alarms.definitions_path

    def create_alarm_definitions(self, revision: int | None = None) -> None:
        """Worker command. Publishing a local copy does not activate the monitor."""
        ticket = self.session_revision if revision is None else revision
        with (
            self.identity.change(
                "alarm_definitions.create",
                TEMPLATE.name,
                ticket,
                after="Bundled template",
            )
            if self.identity is not None
            else nullcontext()
        ):
            create_local_alarm_definitions(self.alarm_definitions_path(ticket))

    def alarm_page(
        self, query: AlarmQuery, revision: int | None = None
    ) -> Future[tuple[AlarmRecord, ...]]:
        with self._condition, self._admission(Role.OPERATOR, revision) as actor:
            ticket = actor.revision if actor is not None else None
            if self.history is None:
                raise ApplicationStateError("Alarm history unavailable")

        return self.history.alarm_page(query, ticket)

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
            self.cancel_process_application()
            self._selection = RecipeSelection(
                actor.revision if actor else None,
                self._generation,
                AuditOperation.by(actor, "production.recipe", recipe_id)
                if actor
                else None,
                recipe_id,
            )
            return self._selection

    def observe_saved_recipe(
        self, recipe: RecipeDefinition, revision: int | None
    ) -> RecipeSelection | None:
        """Observe a committed changed Save while its stopped file slot is owned.

        Invalidate before releasing that slot, even if authorization was lost.
        Reading and installation still require the saving session's admission.
        This is internal refresh evidence, not an operator selection audit event.
        """
        with self._condition:
            candidate = self._candidate
            if (
                candidate is None
                or candidate.configuration.recipe_id != recipe.recipe_id
            ):
                return None
            self._candidate_requires_preparation = True
            self.cancel_process_application()
            self._selection = None
            if self._closing or self._state is not ApplicationState.READY:
                return None
            self._selection = RecipeSelection(
                revision, self._generation, recipe_id=recipe.recipe_id, automatic=True
            )
            return self._selection

    def install_recipe_selection(
        self, request: RecipeSelection, candidate: PreparedConfiguration
    ) -> Future[None]:
        with self._condition:
            try:
                with self._admission(
                    Role.ENGINEER if request.automatic else Role.OPERATOR,
                    request.revision,
                ):
                    if (
                        self._selection is not request
                        or self._generation != request.generation
                    ):
                        raise StaleSessionError("Production selection was superseded")
                    if (
                        request.recipe_id
                        and candidate.configuration.recipe_id != request.recipe_id
                    ):
                        raise ApplicationStateError(
                            "Prepared Recipe identity does not match request"
                        )
                    self._install_candidate(candidate)
                    process = self._queue_process(request.revision)
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
            return process

    def recipe_selection_failed(
        self, request: RecipeSelection, error: BaseException
    ) -> None:
        if self.diagnostics:
            self.diagnostics.record(
                "recipe.preparation",
                generation=request.generation,
                recipe_id=request.operation.target if request.operation else None,
                outcome="failed",
                error_type=type(error).__name__,
            )
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
                reporting_mode=self._counting_mode,
                batch_duration_ms=self._batch_duration_ms,
                batch_uuid=self._batch_segment if self._batch is not None else None,
                run_id=self._run_id,
                batch_uncertain=self._batch is not None and uncertain,
                unbatched_run_uncertain=self._batch is None and uncertain,
                detail_expired=self._detail_expired
                or bool(self.history and self.history.expired_for(self._batch_segment)),
                batch_pending=self._batch_pending or self._restoring,
                process_required=self._process_required,
                process_applying=self._process_active is not None
                or self._process_pending is not None,
            )

    def configure(self, candidate: PreparedConfiguration) -> Future[None]:
        recipe = candidate.configuration.recipe_id
        with (
            self._condition,
            self._admission(
                Role.OPERATOR, action="production.recipe", target=recipe
            ) as actor,
        ):
            self._install_candidate(candidate)
            process = self._queue_process(actor.revision if actor else None)
            if actor and self.identity:
                self.identity.audit.record(
                    AuditOperation.by(actor, "production.recipe", recipe), "success"
                )
            return process

    def _install_candidate(self, candidate: PreparedConfiguration) -> None:
        """Cheap installation of an already resolved/artifact-checked candidate."""
        with self._condition:
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            if (
                self._candidate_requires_preparation
                and self._issue is not None
                and self._issue.exception_type
                in (
                    VisionPreparationRequired.__name__,
                    RecipePreparationRequired.__name__,
                )
            ):
                self._issue = None
            self._selection = None
            self.cancel_process_application()
            self._process_job = None
            self._candidate = candidate
            self._process_required = bool(candidate.configuration.process_parameters)
            self._process_settings = None
            if self._issue is not None and self._issue.stage == "process_application":
                self._issue = None
            if self.diagnostics:
                self.diagnostics.record(
                    "recipe.preparation",
                    recipe_id=candidate.configuration.recipe_id,
                    generation=self._generation,
                    outcome="installed",
                )
            self._candidate_requires_preparation = False
            self._state = ApplicationState.READY

    def cancel_process_application(self) -> None:
        """End unadmitted work; already admitted IO still belongs to the lifecycle owner."""
        with self._condition:
            if self._process_job is not None:
                self._process_job.cancelled.set()
            if self._process_pending is not None:
                self._process_pending.future.set_exception(
                    CancelledError("PLC process application superseded or closing")
                )
                self._process_pending = None
            self._condition.notify_all()

    def _queue_process(self, revision: int | None) -> Future[None]:
        with self._condition:
            future: Future[None] = _promise(Future())
            if not self._process_required:
                future.set_result(None)
                return future
            assert self._candidate is not None
            self._process_job = _ProcessApplication(
                self._candidate, revision, Event(), future
            )
            self._process_pending = self._process_job
            self._condition.notify_all()
            return future

    def _apply_process(self, job: _ProcessApplication) -> None:
        def guard() -> None:
            with self._condition:
                if (
                    job.cancelled.is_set()
                    or self._closing
                    or self._process_job is not job
                    or self._candidate is not job.candidate
                    or self._candidate_requires_preparation
                    or self._state is not ApplicationState.READY
                ):
                    raise CancelledError(
                        "PLC process application superseded or closing"
                    )
                with self._admission(Role.OPERATOR, job.revision, action=""):
                    pass

        error: BaseException | None = None
        try:
            guard()
            if job.candidate.recipe_dependencies is not None:
                job.candidate.recipe_dependencies.validate_current()
            if job.candidate.vision_dependencies is not None:
                job.candidate.vision_dependencies.validate_current()
            if self._communication_path is None:
                raise ApplicationStateError(
                    "Saved Status endpoint unavailable for process application"
                )
            settings = load_communication_preferences(self._communication_path).status
            apply_process(
                job.candidate,
                settings,
                guard,
                identity=self.identity,
                revision=job.revision,
            )
            # A concurrent file edit cannot earn eligibility for an older definition.
            if job.candidate.recipe_dependencies is not None:
                job.candidate.recipe_dependencies.validate_current()
            if job.candidate.vision_dependencies is not None:
                job.candidate.vision_dependencies.validate_current()
            if process_target(
                load_communication_preferences(self._communication_path).status
            ) != process_target(settings):
                raise ApplicationStateError(
                    "Saved Status target changed; reselect the saved Recipe"
                )
            with self._condition:
                guard()
                with self._admission(Role.OPERATOR, job.revision, action=""):
                    self._process_required = False
                    self._process_settings = settings
        except BaseException as exc:
            error = exc
        with self._condition:
            self._process_active = None
            if error is not None and self._process_job is job and not self._closing:
                self._issue = ApplicationIssue(
                    "process_application",
                    "process_apply_failed",
                    type(error).__name__,
                    str(error),
                )
            self._condition.notify_all()
        if error is None:
            job.future.set_result(None)
        else:
            job.future.set_exception(error)

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
                self.cancel_process_application()

    def _install_batch(self, selection: BatchSelection | None) -> None:
        self._batch = selection.label if selection else None
        self._batch_segment = selection.uuid if selection else uuid4().hex
        self._counters = selection.counters if selection else ProductionCounters()
        self._pilot_counters = selection.groups if selection else PilotCounters()
        if selection and selection.reporting_mode is not None:
            self._counting_mode = selection.reporting_mode
            self._pilot_counting = self._counting_mode.group_size is not None
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

    def defect_report_scope(
        self, filters: HistoryQuery, *, revision: int | None = None
    ) -> Future[DefectScope]:
        with self._condition, self._admission(Role.OPERATOR, revision):
            if self.history is None:
                raise ApplicationStateError("History unavailable")
            return self.history.defect_report_scope(filters)

    def export_report(
        self,
        batch: str | ReportRequest | AuditReportRequest | AlarmReportRequest,
        path: Path,
        language: Language,
        *,
        revision: int | None = None,
        cancel: Event | None = None,
    ) -> Future[Path]:
        minimum = (
            Role.ENGINEER if isinstance(batch, AuditReportRequest) else Role.OPERATOR
        )
        with self._condition, self._admission(minimum, revision) as actor:
            history = self.history
            if isinstance(batch, AuditReportRequest):
                if self.identity is None:
                    raise ApplicationStateError("Audit unavailable")
            elif history is None:
                raise ApplicationStateError("History unavailable")
            if self._export_thread is not None and self._export_thread.is_alive():
                raise ApplicationStateError(
                    "A production report is already being exported"
                )
            if path.suffix.lower() not in (".xlsx", ".pdf"):
                raise ValueError("Select an .xlsx or .pdf file")
            actor_revision = actor.revision if actor else None
            stop = cancel if cancel is not None else Event()
            future = _promise(Future[Path]())

            @contextmanager
            def admit_publication() -> Generator[None]:
                with self._condition, self._admission(minimum, actor_revision):
                    if stop.is_set():
                        raise CancelledError()
                    yield

            def export() -> None:
                try:
                    # Load document libraries only on the export worker.
                    from .production_report import write_report

                    write_report(
                        history,
                        batch,
                        path,
                        language,
                        stop,
                        admit_publication,
                        identity=self.identity,
                        revision=actor_revision,
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
        self,
        batch_id: str,
        *,
        revision: int | None = None,
        expected: BatchSetup | None = None,
        information: BatchInformation | None = None,
    ) -> Future[None]:
        if (expected is None) != (information is None):
            raise ValueError("Batch setup requires the original information")
        return self._change_batch(
            batch_id.strip(), revision, setup=expected, information=information
        )

    def update_batch_information(
        self,
        batch: str,
        expected: BatchInformation,
        requested: BatchInformation,
        *,
        revision: int | None = None,
    ) -> Future[None]:
        action = "batch.information"
        with (
            self._condition,
            self._admission(
                Role.OPERATOR, revision, action=action, target=batch
            ) as actor,
        ):
            self._require(ApplicationState.UNCONFIGURED, ApplicationState.READY)
            if self.history is None or self._restoring or self._batch_pending:
                raise ApplicationStateError("Batch editing is unavailable")
            identity = self.identity
            admitted = actor.revision if actor is not None else None

            @contextmanager
            def authorize(label: str, actual: BatchInformation) -> Generator[None]:
                # Audit and SQLite work run on their own workers. Reserve the Batch
                # change until commit so Start cannot race the off-dispatch edit.
                before, after = actual.changes(requested)
                with (
                    identity.change(
                        action, label, admitted, before, after, role=Role.OPERATOR
                    )
                    if identity
                    else nullcontext()
                ):
                    with self._condition:
                        self._require(
                            ApplicationState.UNCONFIGURED, ApplicationState.READY
                        )
                    yield

            done = _promise(Future[None]())
            self._batch_pending = True
            change = self.history.update_batch_information(
                batch, expected, requested, authorize
            )

        def finished(result: Future[None]) -> None:
            error = result.exception()
            with self._condition:
                self._batch_pending = False
            if error:
                done.set_exception(error)
            else:
                done.set_result(None)

        change.add_done_callback(finished)
        return done

    def end_batch(
        self, *, revision: int | None = None, expected_batch: str | None = None
    ) -> Future[None]:
        return self._change_batch(None, revision, expected_batch)

    def clear_statistics(
        self, *, revision: int | None = None, expected_generation: int | None = None
    ) -> None:
        """Start fresh displayed counts without changing durable Run or Batch records."""
        action = "production.clear_statistics"
        with (
            self._condition,
            self._admission(
                Role.OPERATOR,
                revision,
                action=action,
                target="unbatched",
            ) as actor,
        ):
            self._require(ApplicationState.READY)
            if self._batch is not None or self._batch_pending or self._restoring:
                raise ApplicationStateError(
                    "Clearing statistics requires no selected Batch"
                )
            if (
                expected_generation is not None
                and expected_generation != self._generation
            ):
                raise ApplicationStateError("Production changed before confirmation")
            cycles = self._counters.cycles_completed
            groups = self._pilot_counters.batches_completed
            self._counters = ProductionCounters()
            self._pilot_counters = PilotCounters()
            self._latest = None
            self._batch_duration_ms = None
            if actor and self.identity:
                self.identity.audit.record(
                    AuditOperation.by(
                        actor,
                        action,
                        "unbatched",
                        f"cycles={cycles}, groups={groups}",
                        "cycles=0, groups=0",
                    ),
                    "success",
                )

    def _change_batch(
        self,
        label: str | None,
        revision: int | None,
        expected_batch: str | None = None,
        *,
        setup: BatchSetup | None = None,
        information: BatchInformation | None = None,
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
                if setup is not None:
                    raise ApplicationStateError(
                        "Batch information storage is unavailable"
                    )
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
            if setup is not None and information is not None and label is not None:
                identity = self.identity
                admitted = actor.revision if actor is not None else None

                @contextmanager
                def authorize(target: str, actual: BatchInformation) -> Generator[None]:
                    before, after = actual.changes(information)
                    if identity and actual == information:
                        with identity.admission(Role.OPERATOR, admitted):
                            pass
                    with (
                        identity.change(
                            "batch.information",
                            target,
                            admitted,
                            before,
                            after,
                            role=Role.OPERATOR,
                        )
                        if identity and actual != information
                        else nullcontext()
                    ):
                        with self._condition:
                            self._require(ApplicationState.READY)
                        yield

                change = self.history.setup_batch(label, setup, information, authorize)
            else:
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
        recipe = self._candidate or self._active
        if self.diagnostics:
            self.diagnostics.record(
                "production.start",
                outcome="rejected",
                recipe_id=recipe.configuration.recipe_id if recipe else None,
                error_type=type(error).__name__,
            )
        if actor and self.identity:
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
                    "Saved configuration requires re-preparation; reselect the saved Recipe"
                )
            if (
                self._process_required
                or self._process_active is not None
                or self._process_pending is not None
            ):
                raise ApplicationStateError(
                    "PLC process values require successful application and readback; reselect the saved Recipe"
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
            if self.diagnostics:
                self.run_diagnostics = RunDiagnostics(
                    self.diagnostics, self._generation, recipe
                )
                self.run_diagnostics.record("production.start", outcome="admitted")
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
        with self._model_files(
            Role.ENGINEER if request.automatic else Role.OPERATOR,
            request.revision,
            selection=request,
        ):
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
        if self.diagnostics:
            self.diagnostics.record(
                "model.preparation",
                model_id=operation.request.model.model_id,
                outcome="failed" if error else "completed",
                error_type=type(error).__name__ if error else None,
                cleanup_complete=result.cleanup_complete,
            )
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
            if self.diagnostics:
                self.diagnostics.record(
                    "production.stop_requested",
                    generation=self._generation,
                    run_id=self._run_id,
                    state=self._state.name,
                )
            if self._session is None and self._state in (
                ApplicationState.READY,
                ApplicationState.UNCONFIGURED,
            ):
                self.cancel_process_application()
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
            self.cancel_process_application()
            if self.diagnostics:
                self.diagnostics.record(
                    "application.close",
                    outcome="requested",
                    generation=self._generation,
                    run_id=self._run_id,
                )
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
            self._record_application_alarm("recovered")
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
                if self.run_diagnostics:
                    self.run_diagnostics.record(
                        "cycle.rejected",
                        cycle_id=cycle_id,
                        error_type="ApplicationCapacityError",
                    )
                raise ApplicationCapacityError(
                    "Outstanding application cycle capacity reached"
                )
            try:
                source = session.begin_cycle(cycle_id)
            except Exception as exc:
                if self.run_diagnostics:
                    self.run_diagnostics.record(
                        "cycle.rejected",
                        cycle_id=cycle_id,
                        error_type=type(exc).__name__,
                    )
                raise
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
                if self.run_diagnostics:
                    self.run_diagnostics.record(
                        "cycle.rejected",
                        cycle_id=pending.cycle_id,
                        error_type=type(exc).__name__,
                    )
                pending.public.accepted.set_exception(exc)
                pending.public.result.set_exception(exc)
                return
            pending.admitted = True
            if self.run_diagnostics and self.run_diagnostics.writer.detailed:
                self.run_diagnostics.record("cycle.admitted", cycle_id=pending.cycle_id)
            self._counters = self._counters.admitted()
            if self._durable_run and self.history:
                self.history.admitted(self._durable_run, pending.cycle_id)
        pending.public.accepted.set_result(None)
        if pending.source.result.done():
            self._completed(token)

    def _completed(self, token: object) -> None:
        error: BaseException | None = None
        result: ProductionInspectionResult | None = None
        technical_error = False
        with self._condition:
            trace = self.run_diagnostics
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
                previous_errors = self._counters.technical_error_cycles
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
                if self.run_diagnostics:
                    self.run_diagnostics.record(
                        "cycle.failed",
                        cycle_id=pending.cycle_id,
                        error_type=type(exc).__name__,
                    )
                if self.history and self._durable_run:
                    self.history.missing_terminal(self._durable_run)
                if isinstance(exc, ApplicationError) or (
                    self._session is not None and self._session.failure is not None
                ):
                    self._fault("runtime", exc)
            else:
                technical_error = (
                    self._counters.technical_error_cycles > previous_errors
                )
        if error is not None:
            pending.public.result.set_exception(error)
        else:
            assert result is not None
            pending.public.result.set_result(result)
            if trace:
                _observe_inspection(trace, result, technical_error)

    def _fault(self, stage: str, error: BaseException) -> None:
        entered = self._state is not ApplicationState.FAULT
        self._state = ApplicationState.FAULT
        self._issue = ApplicationIssue(
            stage, "lifecycle_failure", type(error).__name__, str(error)
        )
        if entered:
            if self.diagnostics:
                self.diagnostics.record(
                    "production.fault",
                    stage=stage,
                    generation=self._generation,
                    run_id=self.run_diagnostics.run_id
                    if self.run_diagnostics
                    else None,
                    error_type=type(error).__name__,
                )
            self._record_application_alarm("activated")
        self._stop_requested = True
        self._condition.notify_all()

    def _record_application_alarm(self, state: str) -> None:
        if self.history is not None and self._issue is not None:
            observed, batch_id, label = self._alarm_context()
            self.history.offer_alarms(
                (
                    AlarmEvent(
                        observed,
                        "production_fault",
                        state,
                        "software",
                        "生产应用故障",
                        "Production application fault",
                        "application",
                        None,
                        None,
                        None,
                        "application/1",
                        batch_id,
                        label,
                        f"{self._issue.stage} · {self._issue.exception_type}",
                    ),
                )
            )

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

    def _capture_run_configuration(
        self, prepared: PreparedConfiguration
    ) -> RunConfiguration:
        return capture_run(
            prepared, None, None, self.identity.audit if self.identity else None
        )

    def _bind_run(self, session: RuntimeSession, run_id: str, durable: bool) -> None:
        """Bind session-owned evidence before opening Cycle admission."""

    def _close_resources(self) -> None:
        """Application-lifetime resources; called only by the lifecycle owner."""
        if self._export_thread is not None:
            self._export_thread.join()
        if self._alarms is not None:
            self._alarms.request_stop()
            if not self._alarms.join():
                raise ApplicationError("Alarm monitoring owner remains alive")
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
                            or self._process_pending is not None
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
                process = self._process_pending if preparation is None else None
                if process is not None:
                    self._process_pending = None
                self._process_active = process
            if process is not None:
                self._apply_process(process)
                continue
            if preparation is not None:
                self._prepare_model(preparation)
                continue
            startup_error: BaseException | None = None
            if starting:
                try:
                    assert prepared is not None
                    if prepared.recipe_dependencies is not None:
                        prepared.recipe_dependencies.validate_current()
                    if prepared.vision_dependencies is not None:
                        prepared.vision_dependencies.validate_current()
                    if prepared.configuration.process_parameters:
                        try:
                            if (
                                self._communication_path is None
                                or self._process_settings is None
                                or process_target(
                                    load_communication_preferences(
                                        self._communication_path
                                    ).status
                                )
                                != process_target(self._process_settings)
                            ):
                                raise ApplicationStateError(
                                    "Saved Status target changed; reselect the saved Recipe"
                                )
                        except Exception:
                            with self._condition:
                                self._process_required = True
                            raise
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
                        manifest = self._capture_run_configuration(prepared)
                        recording = self.history.start_run(
                            batch,
                            run_id,
                            ConfigurationIdentity.from_prepared(prepared),
                            self._run_reporting(),
                            StartAccount(
                                start_operation.actor_id, start_operation.username
                            )
                            if start_operation
                            else None,
                            configuration=manifest,
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
                        self._bind_run(session, run_id, durable_run is not None)
                        self._active = prepared
                        self._run_id = run_id
                        if record_unbatched and batch is None:
                            # Anonymous History filtering uses the Run itself.
                            self._batch_segment = run_id
                            self._batch_uncertain |= durable_run is None
                        self._state = ApplicationState.RUNNING
                        if self.run_diagnostics:
                            self.run_diagnostics.run_id = run_id
                            self.run_diagnostics.record(
                                "production.start",
                                outcome="activated",
                                durable=durable_run is not None,
                            )
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
                    if self.run_diagnostics:
                        self.run_diagnostics.record(
                            "production.start",
                            outcome="failed",
                            error_type=type(exc).__name__,
                            stop_requested=self._stop_requested,
                        )
                    with self._condition:
                        if isinstance(
                            exc, (VisionPreparationRequired, RecipePreparationRequired)
                        ):
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
                if self.run_diagnostics:
                    self.run_diagnostics.record(
                        "production.stop",
                        outcome="failed" if cleanup_error else "settled",
                        error_type=type(cleanup_error).__name__
                        if cleanup_error
                        else None,
                    )
                    if session_settled:
                        self.run_diagnostics = None
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
                            if self.diagnostics:
                                self.diagnostics.record(
                                    "application.close", outcome="closed"
                                )
                        closed.set_result(None)
                        return
                closed.set_exception(
                    lifecycle_error(
                        "Close failed",
                        cleanup_error or ApplicationError("Runtime still owned"),
                    )
                )


def _observe_inspection(
    trace: RunDiagnostics, result: ProductionInspectionResult, technical_error: bool
) -> None:
    detailed = trace.writer.detailed
    if (
        not detailed
        and result.cycle_status is CycleStatus.COMPLETE
        and not technical_error
    ):
        return
    trace.record(
        "cycle.terminal",
        cycle_id=result.cycle_id,
        status=result.cycle_status.name,
        technical_error=technical_error,
        inspection_started_at_s=result.inspection_started_at,
        result_ready_at_s=result.result_ready_at,
        aggregation_ms=result.aggregation_ms,
    )
    for camera in result.cameras:
        issue = camera.collection_issue
        code = issue.code if issue else None
        error_type = issue.exception_type if issue else None
        if technical_error or result.cycle_status is not CycleStatus.COMPLETE:
            # One representative technical issue per camera. Never include arbitrary
            # exception messages, defect labels or model output.
            for row in camera.channels:
                if code:
                    break
                if row.occupancy and row.occupancy.error_code:
                    code, error_type = (
                        row.occupancy.error_code,
                        row.occupancy.exception_type,
                    )
                else:
                    issue = next((r for r in row.reasons if r.exception_type), None)
                    if issue:
                        code, error_type = issue.code, issue.exception_type
        if detailed or code:
            timing = camera.timing
            trace.record(
                "camera.inspection",
                cycle_id=result.cycle_id,
                camera_id=camera.camera_id,
                frame_sequence=camera.frame_id.sequence if camera.frame_id else None,
                issue=code,
                error_type=error_type,
                occupancy_ms=timing.occupancy_ms,
                traditional_ms=timing.traditional_ms,
                vimo_ms=timing.vimo_ms,
                mapping_ms=timing.mapping_ms,
            )
