"""Production source connection, session admission and frozen service selections."""

from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock

from capsule_vision.application.basler_connection import (
    BaslerConnection,
    CameraSnapshot,
    ConnectionState,
    SourceConnectionSnapshot,
)
from capsule_vision.application.basler_control import BaslerAdmissionControl
from capsule_vision.application.folder_cache import (
    FolderReplayCache,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.image_archive import ArchiveSnapshot, ImageArchive
from capsule_vision.application.source_control import (
    FolderReplayControl,
    SourceControlSnapshot,
)
from capsule_vision.communication import FinsMemoryArea, HoldingRegisters
from capsule_vision.communication.modbus_tcp import ModbusSettings
from capsule_vision.communication.operations import CommunicationRequest, RequestKind
from capsule_vision.communication.reporting import (
    FirstStationReportingWorker,
    ReportingSelection,
    ReportingSettings,
    ReportingSnapshot,
    ReportingWriteProtectedError,
)
from capsule_vision.configuration import (
    load_camera_sources,
)
from capsule_vision.configuration.drafts import CameraSourceDraft
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.inspection import CameraId

from .application import ProductionApplication
from .model_preparation import ModelPreparationFactory, real_preparation_engine
from .models import (
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
)
from .production_session import SourceRuntimeSession


class SourceProductionApplication(ProductionApplication):
    """Own connections and service selections; base lifecycle/activation stays authoritative."""

    def __init__(
        self,
        factory: Callable[[PreparedConfiguration, int], SourceRuntimeSession],
        cache: FolderReplayCache,
        store: LatestDisplayFrameStore | None = None,
        *,
        preparation_factory: ModelPreparationFactory = real_preparation_engine,
        replay_controls: bool = False,
        reporting: ReportingSettings | None = None,
        reporting_editable: bool = False,
        source_draft: CameraSourceDraft | None = None,
        source_cameras: tuple[CameraId, ...] = (),
        source_camera_ids: Callable[[], tuple[CameraId, ...]] | None = None,
        history: ProductionHistory | None = None,
    ) -> None:
        self.replay_cache = cache
        self._display_store = store
        self._reporting_lock = Lock()
        self._reporting_worker: FirstStationReportingWorker | None = None
        self._archive_owner: ImageArchive | None = None
        self._reporting_selection = ReportingSelection(
            reporting is not None,
            reporting or ReportingSettings(),
        )
        self._reporting_editable = reporting_editable
        self._reporting_generation = 0
        self._session_reporting: ReportingSettings | None = None
        self._session_reporting_words = 4
        self._replay_lock = Lock()
        self.replay_controls = replay_controls
        self._source_draft = source_draft
        self._replay_control: FolderReplayControl | None = None
        self._basler_control: BaslerAdmissionControl | None = None
        self.connection = BaslerConnection()
        self._source_cameras = source_cameras
        self._source_camera_ids = source_camera_ids
        super().__init__(
            factory, preparation_factory=preparation_factory, history=history
        )

    def configure(self, candidate: PreparedConfiguration) -> None:
        with self._condition:
            cameras = candidate.configuration.layout.camera_ids
            changed = bool(self._source_cameras) and cameras != self._source_cameras
            if changed:
                self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
                if self.connection.snapshot().locked:
                    raise ApplicationStateError(
                        "Disconnect before changing configured cameras"
                    )
                if self._source_draft is None:
                    raise ApplicationStateError(
                        "Fixed image inputs cannot change camera identities"
                    )
                self._source_draft.saved.validate_cameras(
                    cameras, complete=self._source_draft.saved.mode == "basler"
                )
                self._source_draft.saved.validate_mapping()
            super().configure(candidate)
            if changed:
                self._source_cameras = cameras
            if (
                self._display_store is not None
                and self._display_store.cameras != cameras
            ):
                self._display_store.reconfigure(cameras)

    def source_snapshot(self) -> SourceConnectionSnapshot:
        return self.connection.snapshot()

    def archive_snapshot(self) -> ArchiveSnapshot | None:
        with self._reporting_lock:
            owner = self._archive_owner
        return owner.snapshot() if owner is not None else None

    def set_archive_owner(self, owner: ImageArchive | None) -> None:
        with self._reporting_lock:
            self._archive_owner = owner

    def capture_image(
        self, camera: CameraId, *, timeout: float = 10.0
    ) -> Future[CameraSnapshot]:
        with self._condition:
            self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
            if self._closing:
                raise ApplicationStateError("Application is closing")
            return self.connection.capture_image(camera, timeout=timeout)

    def close(self) -> Future[None]:
        with self._condition:
            completed = super().close()
            self.connection.cancel_capture()
            return completed

    def connect_source(self) -> None:
        with self._condition:
            self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
            draft = self._source_draft
            if draft is None or draft.dirty or draft.saved.mode != "basler":
                raise ApplicationStateError("Save/apply Basler mappings before Connect")
            saved = load_camera_sources(draft.path)
            if saved != draft.saved:
                raise ApplicationStateError("Sources changed on disk; apply or restart")
            cameras = (
                self._source_camera_ids()
                if self._source_camera_ids
                else self._source_cameras
            )
            serials = saved.basler_serials(cameras)
            # Register ownership under the same lock as Start/Close, then wait
            # outside it so GUI commands never wait for native camera startup.
            self.connection.begin_connect(cameras, serials)
            self._source_cameras = cameras
        self.connection.wait_connected()

    def disconnect_source(self) -> None:
        with self._condition:
            self._require(
                ApplicationState.READY,
                ApplicationState.UNCONFIGURED,
                ApplicationState.FAULT,
            )
            self.connection.request_disconnect()
        self.connection.wait_disconnected()

    def start(self) -> Future[None]:
        return self._start_replay(single_step=False)

    def _start_replay(self, *, single_step: bool) -> Future[None]:
        if self.basler_available:
            if single_step:
                raise ApplicationStateError("Start Basler before requesting Pause/Step")
            with self._replay_lock:
                if self.connection.snapshot().state is not ConnectionState.CONNECTED:
                    raise ApplicationStateError(
                        "Connect the Basler source in Vision before Start"
                    )
                control = BaslerAdmissionControl(self.snapshot().generation + 1)
                startup = self._start_reporting_session()
                self._basler_control = control
            startup.add_done_callback(
                lambda future: (
                    control.stop(future.exception()) if future.exception() else None
                )
            )
            return startup
        if not self._folder_controls_available:
            if single_step:
                raise ApplicationStateError("Folder replay controls are not enabled")
            return self._start_reporting_session()
        with self._replay_lock:
            generation = self.snapshot().generation + 1
            control = FolderReplayControl(generation, single_step=single_step)
            step = control.step() if single_step else None
            startup = self._start_reporting_session()
            self._replay_control = control

        def ready(future: Future[None]) -> None:
            error = future.exception()
            if error is not None:
                control.stop(error)

        startup.add_done_callback(ready)
        return step if step is not None else startup

    def _start_reporting_session(self) -> Future[None]:
        with self._condition:
            startup = super().start()  # Same authoritative readiness/activation.
            self.connection.cancel_capture()
            prepared = self._candidate or self._active
            assert prepared is not None
            with self._reporting_lock:
                self._session_reporting_words = (
                    3 if len(prepared.configuration.layout.cameras) == 5 else 4
                )
                self._reporting_generation = self.snapshot().generation
                selection = self._reporting_selection
                self._session_reporting = (
                    selection.settings if selection.enabled else None
                )
            return startup

    @property
    def reporting_editable(self) -> bool:
        return self._reporting_editable

    def reporting_selection(self) -> ReportingSelection:
        with self._reporting_lock:
            return self._reporting_selection

    def set_reporting_selection(self, selection: ReportingSelection) -> None:
        with self._condition:
            self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
            if not self.reporting_editable:
                raise ApplicationStateError(
                    "Reporting settings are fixed by this launcher"
                )
            with self._reporting_lock:
                self._reporting_selection = selection

    def persist_reporting_selection(
        self, selection: ReportingSelection, persist: Callable[[], None]
    ) -> None:
        """Publish saved preferences under the same lock that freezes Start.

        Unchanged reporting permits saving independent Web/diagnostic preferences
        during Production. Failed persistence never changes next-Start admission.
        """
        with self._condition:
            changed = selection != self.reporting_selection()
            if changed:
                self._require(ApplicationState.READY, ApplicationState.UNCONFIGURED)
                if not self.reporting_editable:
                    raise ApplicationStateError(
                        "Reporting settings are fixed by this launcher"
                    )
            persist()
            if changed:
                with self._reporting_lock:
                    self._reporting_selection = selection

    def reporting_for_generation(self, generation: int) -> ReportingSettings | None:
        with self._reporting_lock:
            return (
                self._session_reporting
                if generation == self._reporting_generation
                else None
            )

    def validate_manual_request(self, request: CommunicationRequest) -> None:
        if request.kind is not RequestKind.WRITE:
            return
        with self._condition:
            with self._reporting_lock:
                starting = (
                    self.snapshot().state is ApplicationState.STARTING
                    and self._session_reporting is not None
                )
                worker = self._reporting_worker
            if starting or (worker is not None and worker.snapshot().thread_alive):
                settings = (
                    self._session_reporting
                    if starting
                    else worker.settings
                    if worker is not None
                    else None
                )
                if settings is None:
                    return
                base = settings.require_base_address()
                endpoint = settings.endpoint
                manual = request.endpoint
                same_space = (
                    isinstance(manual, ModbusSettings)
                    and request.area is HoldingRegisters.HOLDING
                    and manual.unit_id == endpoint.unit_id
                    if isinstance(endpoint, ModbusSettings)
                    else not isinstance(manual, ModbusSettings)
                    and request.area is FinsMemoryArea.W
                )
                if (
                    same_space
                    and request.start < base + self._session_reporting_words
                    and request.start + request.count > base
                    and (manual.host, manual.port)
                    == (
                        endpoint.host,
                        endpoint.port,
                    )
                ):
                    prefix = "HR " if isinstance(endpoint, ModbusSettings) else "W"
                    raise ReportingWriteProtectedError(
                        f"Manual write overlaps active reporting {prefix}{base}.."
                        f"{prefix}{base + self._session_reporting_words - 1}"
                    )

    def folder_control_for(self, generation: int) -> FolderReplayControl | None:
        with self._replay_lock:
            control = self._replay_control
        return (
            control
            if control is not None and control.generation == generation
            else None
        )

    def basler_control_for(self, generation: int) -> BaslerAdmissionControl | None:
        with self._replay_lock:
            control = self._basler_control
        return (
            control
            if control is not None and control.generation == generation
            else None
        )

    @property
    def basler_available(self) -> bool:
        return (
            self._source_draft is not None and self._source_draft.saved.mode == "basler"
        )

    @property
    def control_available(self) -> bool:
        return self.basler_available or self._folder_controls_available

    def control_snapshot(self) -> SourceControlSnapshot | None:
        if self.basler_available:
            control = self.basler_control_for(self.snapshot().generation)
            return control.snapshot() if control is not None else None
        if not self._folder_controls_available:
            return None
        with self._replay_lock:
            folder = self._replay_control
        return folder.snapshot() if folder is not None else None

    def _running_basler_control(self) -> BaslerAdmissionControl:
        snapshot = self.snapshot()
        control = self.basler_control_for(snapshot.generation)
        if (
            not self.basler_available
            or snapshot.state is not ApplicationState.RUNNING
            or control is None
        ):
            raise ApplicationStateError("Basler operation requires its RUNNING session")
        return control

    def pause_source(self) -> Future[None]:
        control = (
            self._running_basler_control()
            if self.basler_available
            else self._running_folder_control()
        )
        return control.pause()

    def resume_source(self) -> Future[None]:
        control = (
            self._running_basler_control()
            if self.basler_available
            else self._running_folder_control()
        )
        return control.resume()

    def step_source(self) -> Future[None]:
        if self.basler_available:
            return self._running_basler_control().step()
        if self.snapshot().state is ApplicationState.READY:
            return self._start_replay(single_step=True)
        return self._running_folder_control().step()

    @property
    def _folder_controls_available(self) -> bool:
        return self.replay_controls and (
            self._source_draft is None or self._source_draft.saved.mode == "folder"
        )

    def _running_folder_control(self) -> FolderReplayControl:
        snapshot = self.snapshot()
        control = self.folder_control_for(snapshot.generation)
        if (
            not self._folder_controls_available
            or snapshot.state is not ApplicationState.RUNNING
            or control is None
        ):
            raise ApplicationStateError(
                "Folder replay operation requires its RUNNING session"
            )
        return control

    def set_reporting_owner(self, worker: FirstStationReportingWorker | None) -> None:
        with self._reporting_lock:
            self._reporting_worker = worker

    def reporting_snapshot(self) -> ReportingSnapshot | None:
        """Current/last generation only; no report/result history or Qt dependency."""
        with self._reporting_lock:
            worker = self._reporting_worker
        return worker.snapshot() if worker else None

    def _close_resources(self) -> None:
        self.connection.disconnect()
        self.replay_cache.clear()
        super()._close_resources()
