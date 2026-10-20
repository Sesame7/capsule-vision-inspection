"""Real ViMo composition: independent sources and disposable inspection sessions."""

import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep

import cv2
import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Slot

from vimo_demo.application import (
    ApplicationState,
    ApplicationStateError,
    LocalCycleIngress,
    PreparedConfiguration,
    ProductionApplication,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.application.basler_connection import (
    BaslerConnection,
    CameraSnapshot,
    ConnectionState,
    InspectionBinding,
    SourceConnectionSnapshot,
)
from vimo_demo.application.basler_control import BaslerAdmissionControl
from vimo_demo.application.continuous_basler import (
    ContinuousArmedAcquisition,
)
from vimo_demo.application.folder_cache import (
    FolderCacheStatus,
    FolderReplayCache,
)
from vimo_demo.application.folder_replay import (
    FolderReplay,
    FolderSourceError,
    start_interval_wait,
)
from vimo_demo.application.image_archive import ArchiveSnapshot, ImageArchive
from vimo_demo.application.local_replay import LocalReplayControl, ReplaySnapshot
from vimo_demo.application.session import EngineFactory, ProductionCycle
from vimo_demo.communication import FinsMemoryArea
from vimo_demo.communication.operations import CommunicationRequest, RequestKind
from vimo_demo.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    LocalReportingSettings,
    ReportIdentity,
    ReportingCapacityError,
    ReportingSelection,
    ReportingSnapshot,
    ReportingStateError,
    ReportingWriteProtectedError,
)
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import ProductionInspectionResult
from vimo_demo.runtime import CoordinatorStoppedError, InferenceEngine
from vimo_demo.runtime.early_inspection import EarlyInspectionCoordinator
from vimo_demo.ui.camera_source_editor import CameraSourceDraft
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeEditorSession


def real_engine(prepared: PreparedConfiguration) -> InferenceEngine:
    return VimoSegmentationEngine(
        prepared.model_path, module_id=prepared.configuration.model.module_id
    )


def _cli_image(path: Path) -> NDArray[np.uint8]:
    # Preserve the explicit-path launcher's ValueError/OSError decoding contract.
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode uint8 BGR image: {path}")
    return uint8_array(image)


def _wait_for_source_interval(stop: Event, started: float, fps: float) -> bool:
    """B12 pacing: one absolute deadline, interruptible coarse wait, short sleeps."""
    if stop.is_set():
        return True
    deadline = started + 1 / fps
    coarse = max(0.0, start_interval_wait(started, monotonic(), fps) - 0.012)
    if coarse > 0 and stop.wait(coarse):
        return True
    while not stop.is_set():
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        # Stop is observed between slices; scheduling can exceed this request.
        sleep(min(0.0005, remaining))
    return True


class LocalRuntimeSession(ProductionRuntimeSession):
    """Launcher composition only: the existing session also joins its local source.

    No engine calls here: construction, warm-up, inference and close remain on
    InferenceWorker. Source waits never run on Qt. READY requires session-binding exit, not camera disconnection.
    """

    def __init__(
        self,
        prepared: PreparedConfiguration,
        generation: int,
        application: ProductionApplication,
        images: tuple[NDArray[np.uint8], ...],
        store: LatestDisplayFrameStore,
        engine_factory: EngineFactory = real_engine,
        *,
        replay: FolderReplay | None = None,
        sources: CameraSources | None = None,
        source_base: Path = Path("."),
        reporting: FirstStationReportingWorker | None = None,
        replay_control: LocalReplayControl | None = None,
        continuous_source: BaslerConnection | None = None,
        basler_control: BaslerAdmissionControl | None = None,
    ) -> None:
        if continuous_source is not None and replay_control is not None:
            raise ValueError("Basler acquisition cannot use Folder replay controls")

        def warmup(config: PreparedConfiguration) -> Sequence[NDArray[np.generic]]:
            for camera, image in zip(
                config.configuration.layout.cameras, self._images, strict=True
            ):
                camera.validate_shape((image.shape[0], image.shape[1], 3))
            return self._images

        super().__init__(
            prepared,
            generation,
            warmup=warmup,
            engine_factory=engine_factory,
            cycle_timeout=30,
            max_collecting=1,
            max_ready=1,
            early_camera=continuous_source is not None,
        )
        self._application, self._images, self._store = application, images, store
        self._layout = prepared.configuration.layout
        self._replay, self._sources, self._source_base = replay, sources, source_base
        self._continuous_source = continuous_source
        self.basler_control = basler_control
        self._source_stop = Event()
        self.replay_control = replay_control
        self.reporting = reporting
        settings = prepared.configuration.image_archive
        self.archive = (
            ImageArchive(settings, generation, self._layout.camera_ids)
            if continuous_source is not None and settings.enabled
            else None
        )
        self._reporting_overload: RuntimeError | None = None
        self.source_done: Future[None] = Future()
        self.source_done.set_running_or_notify_cancel()
        self._inspection_ready: Future[None] = Future()
        self._runtime_ready: Future[None] | None = None
        self._source = Thread(
            target=self._produce,
            name=f"LocalProductionSource-{generation}",
            daemon=False,
        )

    def start(self) -> Future[None]:
        if self.archive is not None:
            self.archive.start()
        if self.reporting is not None:
            self.reporting.start()
        ready = super().start()
        self._runtime_ready = ready
        self._source.start()
        return self._inspection_ready if self._continuous_source is not None else ready

    def request_stop(self) -> None:
        self._source_stop.set()
        if self.basler_control is not None:
            self.basler_control.stop()
        if self.replay_control is not None:
            self.replay_control.stop()
        if self.reporting is not None and self._continuous_source is None:
            self.reporting.request_stop()
        super().request_stop()

    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle:
        archive = self.archive
        retained = archive.begin(cycle_id) if archive is not None else False
        try:
            cycle = super().begin_cycle(cycle_id)
        except BaseException:
            if retained and archive is not None:
                archive.complete(cycle_id, None)
            raise
        if self.reporting is not None:
            cycle.result.add_done_callback(
                lambda future: self._report_result(cycle_id, future)
            )
        if retained:
            cycle.result.add_done_callback(
                lambda future: self._archive_result(cycle_id, future)
            )
        return cycle

    def _archive_result(
        self, cycle_id: CycleId, future: Future[ProductionInspectionResult]
    ) -> None:
        assert self.archive is not None
        try:
            result = (
                future.result()
            )  # Terminal only; archive handoff never encodes/writes.
        except BaseException:
            result = None
        if (
            self._continuous_source is not None
            and not self._continuous_source.attributable(cycle_id)
        ):
            result = None
        self.archive.complete(cycle_id, result)

    def _report_result(
        self, cycle_id: CycleId, future: Future[ProductionInspectionResult]
    ) -> None:
        reporter = self.reporting
        assert reporter is not None
        identity = ReportIdentity(self.generation, cycle_id)
        try:
            result = future.result()  # Terminal Future only; no I/O or unfinished wait.
            if result.cycle_id != cycle_id:
                raise ValueError("Reporting result belongs to another cycle")
            if (
                self._continuous_source is not None
                and not self._continuous_source.attributable(cycle_id)
            ):
                raise ValueError("Ambiguous Basler cycle attribution; no report")
            report = FirstStationReport.from_result(self.generation, result)
        except BaseException as exc:
            reporter.note_skipped(
                identity, exc
            )  # Scalar diagnostic, no traceback retention.
            if self.basler_control is not None:
                self.basler_control.reported(cycle_id)
            return
        try:
            pending = reporter.submit(report)  # Nonblocking; never socket work.
            control = self.basler_control
            if control is not None:
                pending.add_done_callback(lambda _: control.reported(cycle_id))
        except (ReportingCapacityError, ReportingStateError) as exc:
            # Keep a fresh, traceback-free source issue. The completed inspection
            # still reaches accounting; overload is not model NG/application FAULT.
            with self._lock:
                self._reporting_overload = type(exc)(str(exc))
            self._source_stop.set()
            try:
                self._application.stop()
            except ApplicationStateError:
                pass  # Close/Stop may already own the shutdown boundary.

    def join(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + timeout
        if not super().join(timeout):
            return False
        if self._source.ident is not None:
            remaining = None if deadline is None else max(0, deadline - monotonic())
            self._source.join(remaining)
        joined = not self._source.is_alive()
        if self.reporting is not None:
            if joined and self._continuous_source is not None:
                # Runtime terminal callbacks have now handed off every admitted
                # cycle, including shutdown partials. Drain them before release.
                self.reporting.request_stop(drain=True)
            remaining = None if deadline is None else max(0, deadline - monotonic())
            joined = self.reporting.join(remaining) and joined
        if self.archive is not None:
            if joined:
                # All runtime/source callbacks have handed off terminal outcomes.
                self.archive.request_stop()
            remaining = None if deadline is None else max(0, deadline - monotonic())
            joined = self.archive.join(remaining) and joined
        if (
            joined
            and self.cleanup_safe
            and self._replay is not None
            and self._replay.cached
        ):
            # No owner can read source pixels now. The application cache survives
            # Stop, but an old disposable session must not retain the full dataset.
            self._images = ()
            self._replay = None
        return joined

    @property
    def cleanup_safe(self) -> bool:
        return (
            super().cleanup_safe
            and (self.reporting is None or self.reporting.cleanup_safe)
            and (self.archive is None or not self.archive.snapshot().alive)
        )

    @property
    def thread_names(self) -> tuple[str, ...]:
        return (
            *super().thread_names,
            self._source.name,
            *((self.reporting.thread_name,) if self.reporting else ()),
            *((self.archive.thread_name,) if self.archive else ()),
        )

    def _produce(self) -> None:
        try:
            if self._continuous_source is not None:
                assert self._runtime_ready is not None
                self._runtime_ready.result()
                if self._source_stop.is_set():
                    return
                self._produce_continuous()
                return
            while not self._source_stop.is_set():
                snapshot = self._application.snapshot()
                if snapshot.state is ApplicationState.RUNNING:
                    if snapshot.generation != self.generation:
                        raise ApplicationStateError("Local source generation mismatch")
                    break
                if self._source_stop.wait(0.05):
                    return
            if self._source_stop.is_set():
                return
            ingress = LocalCycleIngress(self._application, self._layout)
            started: float | None = None
            first = True
            while not self._source_stop.is_set():
                fps = self._sources.target_fps if self._sources else 8.0
                control = self.replay_control
                if control is not None:
                    if not control.wait_for_turn():
                        return
                    previous = control.pacing_start(started)
                    if previous is not None and _wait_for_source_interval(
                        control.changed, previous, fps
                    ):
                        continue  # Pause/Continue/Step/Stop: recheck the gate.
                    if not control.claim():
                        continue
                elif started is not None and _wait_for_source_interval(
                    self._source_stop, started, fps
                ):
                    return
                started = monotonic()
                if self._replay is not None and not first:
                    group = self._replay.next_group()
                    if group is None:
                        self._application.stop()  # Normal exhaustion, never FAULT.
                        return
                    self._images = group
                first = False
                if self._source_stop.is_set():
                    return
                captured = monotonic()  # Local submission time, not camera exposure.
                result = ingress.submit(self._images).result.result()
                for camera, image in zip(result.cameras, self._images, strict=True):
                    if camera.frame_id is not None:
                        # Reuse the actual admitted identity; these source pixels
                        # equal the ingress copy. Store owns a separate display copy.
                        self._store.publish_pixels(
                            camera.frame_id,
                            captured,
                            image,
                            generation=self.generation,
                            source_key=self._sources.camera(camera.camera_id).key(
                                self._source_base
                            )
                            if self._sources
                            else None,
                        )
                del result  # Keep no cycle/result history.
                if control is not None:
                    control.complete_cycle()
                    if (
                        self._replay is not None
                        and not self._replay.sources.loop
                        and any(
                            index >= len(paths)
                            for index, paths in zip(
                                self._replay.indices, self._replay.paths, strict=True
                            )
                        )
                    ):
                        # End-of-source wins over operator waiting. Inspecting the
                        # owned cursor needs no extra decode or look-ahead group.
                        self._application.stop()
                        return
        except BaseException as exc:
            if not self._inspection_ready.done():
                self._inspection_ready.set_exception(exc)
            if not (
                (
                    self._source_stop.is_set()
                    or self._application.snapshot().state
                    in (ApplicationState.STOPPING, ApplicationState.CLOSED)
                )
                and isinstance(exc, (ApplicationStateError, CoordinatorStoppedError))
            ):
                self.source_done.set_exception(exc)
                # Stop a partially admitted cycle; never retry or fabricate NG.
                try:
                    self._application.stop()
                except ApplicationStateError:
                    if self._application.snapshot().state not in (
                        ApplicationState.STOPPING,
                        ApplicationState.CLOSED,
                    ):
                        raise
        finally:
            if not self._inspection_ready.done():
                self._inspection_ready.set_exception(
                    ApplicationStateError("Inspection startup stopped")
                )
            if not self.source_done.done():
                with self._lock:
                    reporting_error = self._reporting_overload
                if reporting_error is not None:
                    self.source_done.set_exception(reporting_error)
                else:
                    self.source_done.set_result(None)
            if self.replay_control is not None:
                self.replay_control.stop(self.source_done.exception())

    def _produce_continuous(self) -> None:
        source = self._continuous_source
        assert source is not None

        def begin(cycle_id: CycleId) -> None:
            cycle = self._application.begin_cycle(cycle_id)
            cycle.accepted.result()
            control = self.basler_control
            if control is not None:
                if self.reporting is None:
                    control.reported(cycle_id)

                # Public completion follows application accounting, while the
                # independent report callback may arrive before or after it.
                def completed(_future: Future[ProductionInspectionResult]) -> None:
                    coordinator = self._coordinator
                    assert isinstance(coordinator, EarlyInspectionCoordinator)
                    coordinator.when_settled(cycle_id).add_done_callback(
                        lambda _: control.inspected(cycle_id)
                    )

                cycle.result.add_done_callback(completed)

        def submit(frame: Frame) -> None:
            if self.archive is not None:
                self.archive.retain(frame)
            self._application.submit_frame(frame).result()
            self._store.publish(
                frame,
                generation=self.generation,
                source_key=self._sources.source_key(
                    frame.id.camera_id, self._source_base
                )
                if self._sources
                else None,
            )

        failure: list[BaseException] = []

        def failed(error: BaseException) -> None:
            failure.append(error)
            self._source_stop.set()

        assert self.basler_control is not None
        binding = InspectionBinding(
            self.basler_control,
            self._source_stop,
            begin,
            submit,
            failed,
            lambda: (
                not self._source_stop.is_set()
                and self._application.snapshot().state is ApplicationState.RUNNING
            ),
        )
        try:
            source.command(binding, attach=True).result()
            self._inspection_ready.set_result(None)
            self._source_stop.wait()
            if failure:
                raise failure[0]
        finally:
            source.command(binding, attach=False).result()


class _LocalApplication(ProductionApplication):
    """Local composition resource only; core lifecycle/activation stays unchanged."""

    def __init__(
        self,
        factory: Callable[[PreparedConfiguration, int], LocalRuntimeSession],
        cache: FolderReplayCache,
        *,
        replay_controls: bool = False,
        reporting: LocalReportingSettings | None = None,
        reporting_editable: bool = False,
        source_draft: CameraSourceDraft | None = None,
        source_cameras: tuple[CameraId, ...] = (),
        observe_acquisition: Callable[[ContinuousArmedAcquisition], None] = lambda _: (
            None
        ),
    ) -> None:
        self.replay_cache = cache
        self._reporting_lock = Lock()
        self._reporting_worker: FirstStationReportingWorker | None = None
        self._archive_owner: ImageArchive | None = None
        self._reporting_selection = ReportingSelection(
            reporting is not None,
            reporting or LocalReportingSettings(),
        )
        self._reporting_editable = reporting_editable
        self._reporting_generation = 0
        self._session_reporting: LocalReportingSettings | None = None
        self._replay_lock = Lock()
        self.replay_controls = replay_controls
        self._source_draft = source_draft
        self._replay_control: LocalReplayControl | None = None
        self._basler_control: BaslerAdmissionControl | None = None
        self.connection = BaslerConnection()
        self._source_cameras = source_cameras
        self._observe_acquisition = observe_acquisition
        super().__init__(factory)

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
            serials = saved.basler_serials(self._source_cameras)
            # Register ownership under the same lock as Start/Close, then wait
            # outside it so GUI commands never wait for native camera startup.
            self.connection.begin_connect(
                self._source_cameras, serials, self._observe_acquisition
            )
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
        if not self.replay_available:
            if single_step:
                raise ApplicationStateError("Local replay controls are not enabled")
            return self._start_reporting_session()
        with self._replay_lock:
            generation = self.snapshot().generation + 1
            control = LocalReplayControl(generation, single_step=single_step)
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
            with self._reporting_lock:
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

    def reporting_for_generation(
        self, generation: int
    ) -> LocalReportingSettings | None:
        with self._reporting_lock:
            return (
                self._session_reporting
                if generation == self._reporting_generation
                else None
            )

    def validate_manual_request(self, request: CommunicationRequest) -> None:
        if (
            request.kind is not RequestKind.WRITE
            or request.area is not FinsMemoryArea.W
            or request.start > 52
            or request.start + request.count <= 49
        ):
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
                if settings is not None and (request.host, request.port) == (
                    settings.host,
                    settings.port,
                ):
                    raise ReportingWriteProtectedError(
                        "Manual W49..W52 writes conflict with the active reporting endpoint"
                    )

    def replay_control_for(self, generation: int) -> LocalReplayControl | None:
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
        return self.basler_available or self.replay_available

    def control_snapshot(self) -> ReplaySnapshot | None:
        if not self.basler_available:
            return self.replay_snapshot()
        control = self.basler_control_for(self.snapshot().generation)
        return control.snapshot() if control is not None else None

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
        return (
            self._running_basler_control().pause()
            if self.basler_available
            else self.pause_replay()
        )

    def resume_source(self) -> Future[None]:
        return (
            self._running_basler_control().resume()
            if self.basler_available
            else self.continue_replay()
        )

    def step_source(self) -> Future[None]:
        return (
            self._running_basler_control().step()
            if self.basler_available
            else self.step_replay()
        )

    @property
    def replay_available(self) -> bool:
        return self.replay_controls and (
            self._source_draft is None or self._source_draft.saved.mode == "folder"
        )

    def replay_snapshot(self) -> ReplaySnapshot | None:
        if not self.replay_available:
            return None
        with self._replay_lock:
            control = self._replay_control
        return control.snapshot() if control is not None else None

    def _running_replay(self) -> LocalReplayControl:
        snapshot = self.snapshot()
        control = self.replay_control_for(snapshot.generation)
        if (
            not self.replay_available
            or snapshot.state is not ApplicationState.RUNNING
            or control is None
        ):
            raise ApplicationStateError("Replay operation requires its RUNNING session")
        return control

    def pause_replay(self) -> Future[None]:
        return self._running_replay().pause()

    def continue_replay(self) -> Future[None]:
        return self._running_replay().resume()

    def step_replay(self) -> Future[None]:
        if self.snapshot().state is ApplicationState.READY:
            return self._start_replay(single_step=True)
        return self._running_replay().step()

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


@dataclass
class LocalComposition:
    application: _LocalApplication
    store: LatestDisplayFrameStore
    choice: RecipeChoice
    editor: RecipeEditorSession


def compose(
    model_file: Path,
    machine_file: Path,
    recipe_file: Path,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    sources_file: Path | None = None,
    observe_cache: Callable[[FolderCacheStatus], None] = lambda _: None,
    reporting: LocalReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
    observe_acquisition: Callable[[ContinuousArmedAcquisition], None] = lambda _: None,
) -> LocalComposition:
    """Parse/hash/decode before entering Qt; no fallback images or invented ROIs.

    Relative model paths are resolved against the model-description directory.
    Selected files follow configured camera order, not filename camera guesses.
    """
    if paths and len(paths) != 6:
        raise ValueError("Exactly six local image paths are required")
    model = load_model_definition(model_file)
    machine = load_machine_layout(machine_file)
    recipe = load_recipe(recipe_file)
    registry = ModelRegistry((model,))
    prepared = prepare_configuration(
        resolve_configuration(machine, recipe, registry, model.model_id),
        base_directory=model_file.resolve().parent,
    )
    if len(prepared.configuration.layout.cameras) != 6:
        raise ValueError("Local Production UI requires exactly six configured cameras")
    sources_file = (
        sources_file or machine_file.parent / "camera_sources.json"
    ).resolve()
    sources = (
        load_camera_sources(sources_file)
        if sources_file.exists()
        else CameraSources(
            tuple(
                CameraFolder(
                    camera, str(paths[index].resolve().parent) if paths else ""
                )
                for index, camera in enumerate(prepared.configuration.layout.camera_ids)
            )
        )
    )
    sources.validate_cameras(prepared.configuration.layout.camera_ids)
    editor = RecipeEditorSession(
        RecipeCatalog(recipe_file.resolve().parent),
        machine,
        registry,
        model.model_id,
        model_file.resolve().parent,
        machine_path=machine_file.resolve(),
        camera_sources=CameraSourceDraft(sources, sources_file),
        cli_sources=bool(paths),
    )
    app, store = local_application(
        editor,
        paths,
        engine_factory=engine_factory,
        observe_source=observe_source,
        observe_cache=observe_cache,
        reporting=reporting,
        replay_controls=replay_controls,
        reporting_editable=reporting_editable,
        observe_acquisition=observe_acquisition,
    )
    app.configure(prepared)
    return LocalComposition(
        app, store, RecipeChoice(prepared, recipe.display_name), editor
    )


def local_application(
    editor: RecipeEditorSession,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    observe_cache: Callable[[FolderCacheStatus], None] = lambda _: None,
    reporting: LocalReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
    observe_acquisition: Callable[[ContinuousArmedAcquisition], None] = lambda _: None,
) -> tuple[_LocalApplication, LatestDisplayFrameStore]:
    """Shared serial composition; no session/source/engine is created until Start."""
    if paths and len(paths) != 6:
        raise ValueError("Exactly six local image paths are required")
    if editor.camera_sources is None:
        raise ValueError("Local production requires camera sources")
    source_draft = editor.camera_sources
    sources_file = source_draft.path
    sources = source_draft.saved
    images = tuple(_cli_image(path) for path in paths)
    store = LatestDisplayFrameStore(editor.machine.layout.camera_ids)
    cache = FolderReplayCache(notify=observe_cache)
    for camera, image in zip(
        editor.machine.layout.cameras if images else (), images, strict=True
    ):
        camera.validate_shape((image.shape[0], image.shape[1], 3))
        image.setflags(write=False)
        store.publish(
            Frame(FrameId(CycleId(0), camera.camera_id, 0), monotonic(), image),
            generation=0,
            source_key=CameraFolder(
                camera.camera_id,
                str(
                    paths[editor.machine.layout.camera_ids.index(camera.camera_id)]
                    .resolve()
                    .parent
                ),
            ).key(sources_file.parent),
        )

    def factory(config: PreparedConfiguration, generation: int) -> LocalRuntimeSession:
        try:
            saved_sources = load_camera_sources(sources_file) if not paths else sources
            saved_sources.validate_cameras(config.configuration.layout.camera_ids)
            if not paths and saved_sources.mode != source_draft.saved.mode:
                raise ConfigurationError(
                    "Source mode changed on disk; apply in Vision or restart"
                )
        except ConfigurationError as exc:
            cache.clear()
            raise FolderSourceError(f"Camera sources {sources_file}: {exc}") from exc
        snapshot = (
            saved_sources
            if not paths
            else CameraSources(
                tuple(
                    CameraFolder(camera, str(path.resolve().parent))
                    for camera, path in zip(
                        config.configuration.layout.camera_ids, paths, strict=True
                    )
                )
            )
        )
        snapshot.validate_cameras(config.configuration.layout.camera_ids)
        serials = (
            snapshot.basler_serials(config.configuration.layout.camera_ids)
            if snapshot.mode == "basler"
            else None
        )
        continuous_source = app.connection if serials is not None else None
        if continuous_source is not None:
            assert serials is not None
            connected = continuous_source.snapshot()
            if connected.state is not ConnectionState.CONNECTED or tuple(
                (row.camera_id, row.serial) for row in connected.cameras
            ) != tuple(
                zip(config.configuration.layout.camera_ids, serials, strict=True)
            ):
                raise ApplicationStateError(
                    "Connected cameras differ from the saved source"
                )
            staged: list[NDArray[np.uint8]] = []
            for camera, actual in zip(
                config.configuration.layout.cameras, connected.cameras, strict=True
            ):
                camera.validate_shape((actual.height, actual.width, 3))
                image = np.zeros((actual.height, actual.width, 3), dtype=np.uint8)
                image.setflags(write=False)
                staged.append(image)
            group = tuple(staged)
            replay = None
        else:
            replay = (
                None
                if paths
                else cache.replay(
                    config.configuration.layout,
                    snapshot,
                    sources_file.parent,
                    cancelled=lambda: (
                        app.snapshot().state
                        in (
                            ApplicationState.STOPPING,
                            ApplicationState.CLOSED,
                            ApplicationState.FAULT,
                        )
                    ),
                )
            )
            group = images if replay is None else replay.next_group()
            if group is None:
                raise ValueError("No complete image group")
        session_reporting = app.reporting_for_generation(generation)
        report_worker = (
            FirstStationReportingWorker(
                generation,
                session_reporting,
                attributable=lambda identity: (
                    continuous_source is None
                    or continuous_source.attributable(identity.cycle_id)
                ),
            )
            if session_reporting is not None
            else None
        )
        session = LocalRuntimeSession(
            config,
            generation,
            app,
            group,
            store,
            engine_factory,
            replay=replay,
            sources=snapshot,
            source_base=sources_file.parent,
            reporting=report_worker,
            replay_control=app.replay_control_for(generation),
            continuous_source=continuous_source,
            basler_control=app.basler_control_for(generation)
            if continuous_source is not None
            else None,
        )
        app.set_reporting_owner(report_worker)
        app.set_archive_owner(session.archive)
        observe_source(session.source_done)
        return session

    app = _LocalApplication(
        factory,
        cache,
        replay_controls=replay_controls,
        reporting=reporting,
        reporting_editable=reporting_editable,
        source_draft=source_draft if not paths else None,
        source_cameras=editor.machine.layout.camera_ids,
        observe_acquisition=observe_acquisition,
    )
    return app, store


def unconfigured_application() -> ProductionApplication:
    """Normal stopped shell can edit session preferences without inventing inputs."""

    def unavailable(
        _config: PreparedConfiguration, _generation: int
    ) -> LocalRuntimeSession:
        raise ConfigurationError("Persistent configuration is unavailable")

    return _LocalApplication(unavailable, FolderReplayCache(), reporting_editable=True)


class SourceNotifications(QObject):
    """Source Future -> existing queued Qt command-error presentation."""

    def __init__(self, bridge: QtProductionBridge, delivery: FutureDelivery) -> None:
        super().__init__(bridge)
        self._bridge = bridge
        delivery.emitter.completed.connect(
            self.completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(delivery.detach, Qt.ConnectionType.DirectConnection)

    @staticmethod
    def watch_acquisition(
        delivery: FutureDelivery,
        generation: int,
        observation: ContinuousArmedAcquisition,
    ) -> None:
        """Source-thread notification through the existing queued delivery gate."""
        notice: Future[str] = Future()
        notice.set_result("basler_armed")
        delivery.watch(f"basler_source:{generation}", notice)

    @Slot(str, object, object)
    def completed(self, name: str, _value: object, error: object) -> None:
        if name.startswith("basler_source:") and _value in (
            "basler_armed",
            "basler_processing",
        ):
            self._bridge.source_progress_changed.emit(int(name.split(":")[1]), _value)
            return
        if isinstance(error, BaseException):
            traceback.print_exception(error)
            self._bridge.command_failed.emit(name, error)
