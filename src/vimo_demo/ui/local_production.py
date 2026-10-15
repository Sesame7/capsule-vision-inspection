"""Real ViMo developer composition: Folder inputs or an opt-in Basler cycle."""

import json
import sys
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import asdict, dataclass
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
from vimo_demo.application.basler import (
    AcquisitionReport,
    ArmedAcquisition,
    BaslerAcquisitionCancelled,
    BaslerAcquisitionError,
    BaslerAcquisitionSource,
)
from vimo_demo.application.folder_cache import (
    CacheState,
    FolderCacheStatus,
    FolderReplayCache,
)
from vimo_demo.application.folder_replay import (
    FolderReplay,
    FolderSourceError,
    start_interval_wait,
)
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
from vimo_demo.inspection import CycleId, Frame, FrameId
from vimo_demo.inspection.production import ProductionInspectionResult
from vimo_demo.runtime import CoordinatorStoppedError, InferenceEngine
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


def _basler_notice(observation: ArmedAcquisition | AcquisitionReport) -> None:
    """Source-thread console evidence; no widgets or PLC operations."""
    if isinstance(observation, ArmedAcquisition):
        print(json.dumps({"event": "ARMED", **asdict(observation)}), flush=True)
        print(
            "Perform ONE manual flash+photo press-and-release; do not press again.",
            flush=True,
        )
    else:
        print(
            json.dumps(
                {
                    "event": "basler_acquisition",
                    "serials": observation.serials,
                    "missing": observation.missing_serials,
                    "extra": observation.extra_serials,
                    "observations": [asdict(row) for row in observation.observations],
                    "issues": [asdict(row) for row in observation.issues],
                    "cleanup": [asdict(row) for row in observation.cleanup],
                    "cleanup_safe": observation.cleanup_safe,
                }
            ),
            flush=True,
        )


class LocalRuntimeSession(ProductionRuntimeSession):
    """Launcher composition only: the existing session also joins its local source.

    No engine calls here: construction, warm-up, inference and close remain on
    InferenceWorker. Source waits never run on Qt. READY cannot precede source exit.
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
        basler_source: BaslerAcquisitionSource | None = None,
        observe_acquisition: Callable[
            [ArmedAcquisition | AcquisitionReport], None
        ] = _basler_notice,
    ) -> None:
        if basler_source is not None and (
            replay_control is not None or reporting is not None
        ):
            raise ValueError("Basler single-cycle mode cannot replay or report to PLC")

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
        )
        self._application, self._images, self._store = application, images, store
        self._layout = prepared.configuration.layout
        self._replay, self._sources, self._source_base = replay, sources, source_base
        self._basler_source, self._observe_acquisition = (
            basler_source,
            observe_acquisition,
        )
        self._source_stop = Event()
        self.replay_control = replay_control
        self.reporting = reporting
        self._reporting_overload: RuntimeError | None = None
        self.source_done: Future[None] = Future()
        self.source_done.set_running_or_notify_cancel()
        self._source = Thread(
            target=self._produce,
            name=f"LocalProductionSource-{generation}",
            daemon=False,
        )

    def start(self) -> Future[None]:
        if self.reporting is not None:
            self.reporting.start()
        ready = super().start()
        self._source.start()
        return ready

    def request_stop(self) -> None:
        self._source_stop.set()
        if self.replay_control is not None:
            self.replay_control.stop()
        if self.reporting is not None:
            self.reporting.request_stop()
        super().request_stop()

    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle:
        cycle = super().begin_cycle(cycle_id)
        if self.reporting is not None:
            cycle.result.add_done_callback(
                lambda future: self._report_result(cycle_id, future)
            )
        return cycle

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
            report = FirstStationReport.from_result(self.generation, result)
        except BaseException as exc:
            reporter.note_skipped(
                identity, exc
            )  # Scalar diagnostic, no traceback retention.
            return
        try:
            reporter.submit(report)  # Nonblocking FIFO admission; never socket work.
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
            remaining = None if deadline is None else max(0, deadline - monotonic())
            joined = self.reporting.join(remaining) and joined
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
            and (self._basler_source is None or self._basler_source.cleanup_safe)
        )

    @property
    def thread_names(self) -> tuple[str, ...]:
        return (
            *super().thread_names,
            self._source.name,
            *((self.reporting.thread_name,) if self.reporting else ()),
        )

    def _produce(self) -> None:
        try:
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
            if self._basler_source is not None:
                self._produce_basler(ingress)
                return
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
            if not (
                (
                    self._source_stop.is_set()
                    or self._application.snapshot().state
                    in (ApplicationState.STOPPING, ApplicationState.CLOSED)
                )
                and isinstance(exc, (ApplicationStateError, CoordinatorStoppedError))
            ):
                if (
                    isinstance(exc, BaslerAcquisitionCancelled)
                    and self._source_stop.is_set()
                    and exc.report.cleanup_safe
                ):
                    return
                self.source_done.set_exception(exc)
                # Stop a partially admitted cycle; never retry or fabricate NG.
                self._application.stop()
        finally:
            if not self.source_done.done():
                with self._lock:
                    reporting_error = self._reporting_overload
                if reporting_error is not None:
                    self.source_done.set_exception(reporting_error)
                else:
                    self.source_done.set_result(None)
            if self.replay_control is not None:
                self.replay_control.stop(self.source_done.exception())

    def _produce_basler(self, ingress: LocalCycleIngress) -> None:
        source = self._basler_source
        assert source is not None
        print(
            json.dumps(
                {
                    "event": "basler_mapping",
                    "generation": self.generation,
                    "mapping": dict(
                        zip(self._layout.camera_ids, source.serials, strict=True)
                    ),
                    "note": "Explicit provisional developer mapping; not calibrated configuration",
                }
            ),
            flush=True,
        )
        try:
            group = source.acquire(
                on_armed=self._observe_acquisition, cancel=self._source_stop
            )
        except BaslerAcquisitionError as exc:
            self._observe_acquisition(exc.report)
            raise
        self._observe_acquisition(group.report)
        if self._source_stop.is_set():
            return  # Stop during cleanup/report notification never admits a late group.
        if tuple(image.serial for image in group.images) != source.serials:
            raise ValueError("Basler image serial order differs from explicit mapping")
        images = tuple(image.pixels for image in group.images)
        for camera, image in zip(self._layout.cameras, images, strict=True):
            camera.validate_shape((image.shape[0], image.shape[1], 3))
        if self._source_stop.is_set():
            return
        captured = monotonic()  # Admission metadata, not SDK timestamp conversion.
        result = ingress.submit(images).result.result()
        for camera, image in zip(result.cameras, images, strict=True):
            if camera.frame_id is not None:
                self._store.publish_pixels(
                    camera.frame_id, captured, image, generation=self.generation
                )
        # Existing accounting has observed the terminal Future before Stop/READY.
        self._application.stop()


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
    ) -> None:
        self.replay_cache = cache
        self._reporting_lock = Lock()
        self._reporting_worker: FirstStationReportingWorker | None = None
        self._reporting_selection = ReportingSelection(
            reporting is not None, reporting or LocalReportingSettings()
        )
        self._reporting_editable = reporting_editable
        self._reporting_generation = 0
        self._session_reporting: LocalReportingSettings | None = None
        self._replay_lock = Lock()
        self.replay_controls = replay_controls
        self._replay_control: LocalReplayControl | None = None
        super().__init__(factory)

    def start(self) -> Future[None]:
        return self._start_replay(single_step=False)

    def _start_replay(self, *, single_step: bool) -> Future[None]:
        if not self.replay_controls:
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
                raise ReportingWriteProtectedError(
                    "Manual W49..W52 writes are reserved while automatic reporting owns the session"
                )

    def replay_control_for(self, generation: int) -> LocalReplayControl | None:
        with self._replay_lock:
            control = self._replay_control
        return (
            control
            if control is not None and control.generation == generation
            else None
        )

    def replay_snapshot(self) -> ReplaySnapshot | None:
        with self._replay_lock:
            control = self._replay_control
        return control.snapshot() if control is not None else None

    def _running_replay(self) -> LocalReplayControl:
        snapshot = self.snapshot()
        control = self.replay_control_for(snapshot.generation)
        if snapshot.state is not ApplicationState.RUNNING or control is None:
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

    def close(self) -> Future[None]:
        completed = super().close()
        completed.add_done_callback(self._release_cache)
        return completed

    def _release_cache(self, completed: Future[None]) -> None:
        # Close success follows safe source/native/runtime joins. A failed Close
        # keeps ownership/cache; application.join still proves lifecycle-thread exit.
        if completed.exception(timeout=0) is None:
            self.replay_cache.clear()


def _cache_notice(status: FolderCacheStatus) -> None:
    if status.state in (
        CacheState.LOADING,
        CacheState.READY,
        CacheState.HIT,
        CacheState.FALLBACK,
    ):
        print(
            f"FolderReplay cache {status.state.name}: {status.loaded_images}/{status.image_count} images, "
            f"{status.decoded_bytes} bytes, load={status.load_seconds:.3f}s"
            + (f"; on-demand fallback: {status.reason}" if status.reason else ""),
            file=sys.stderr,
        )


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
    observe_cache: Callable[[FolderCacheStatus], None] = _cache_notice,
    reporting: LocalReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
    basler_serials: Sequence[str] | None = None,
    basler_timeout: float = 30.0,
    observe_acquisition: Callable[
        [ArmedAcquisition | AcquisitionReport], None
    ] = _basler_notice,
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
        basler_serials=basler_serials,
        basler_timeout=basler_timeout,
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
    observe_cache: Callable[[FolderCacheStatus], None] = _cache_notice,
    reporting: LocalReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
    basler_serials: Sequence[str] | None = None,
    basler_timeout: float = 30.0,
    observe_acquisition: Callable[
        [ArmedAcquisition | AcquisitionReport], None
    ] = _basler_notice,
) -> tuple[_LocalApplication, LatestDisplayFrameStore]:
    """Shared serial composition; no session/source/engine is created until Start."""
    if paths and len(paths) != 6:
        raise ValueError("Exactly six local image paths are required")
    if basler_serials is not None:
        if replay_controls or reporting is not None or reporting_editable:
            raise ValueError("Basler single-cycle mode cannot replay or report to PLC")
        # Validate without SDK access; create a fresh source for each generation.
        basler_serials = BaslerAcquisitionSource(
            basler_serials, timeout=basler_timeout
        ).serials
    if editor.camera_sources is None:
        raise ValueError("Local production requires camera sources")
    sources_file = editor.camera_sources.path
    sources = editor.camera_sources.saved
    images = tuple(_cli_image(path) for path in paths)
    store = LatestDisplayFrameStore(editor.machine.layout.camera_ids)
    cache = FolderReplayCache(notify=observe_cache)
    for camera, image in zip(
        editor.machine.layout.cameras if images else (), images, strict=True
    ):
        camera.validate_shape((image.shape[0], image.shape[1], 3))
        image.setflags(write=False)
        if basler_serials is None:
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
        replay = (
            None
            if paths
            else FolderReplay(
                config.configuration.layout, snapshot, sources_file.parent
            )
            if basler_serials is not None
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
            FirstStationReportingWorker(generation, session_reporting)
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
            basler_source=BaslerAcquisitionSource(
                basler_serials, timeout=basler_timeout
            )
            if basler_serials is not None
            else None,
            observe_acquisition=observe_acquisition,
        )
        app.set_reporting_owner(report_worker)
        observe_source(session.source_done)
        return session

    app = _LocalApplication(
        factory,
        cache,
        replay_controls=replay_controls,
        reporting=reporting,
        reporting_editable=reporting_editable,
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

    @Slot(str, object, object)
    def completed(self, name: str, _value: object, error: object) -> None:
        if isinstance(error, BaseException):
            traceback.print_exception(error)
            self._bridge.command_failed.emit(name, error)
