"""Per-generation source, inspection, reporting/archive handoff and shutdown."""

from collections.abc import Sequence
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import numpy as np
from numpy.typing import NDArray

from capsule_vision.application.basler_connection import (
    BaslerConnection,
    InspectionBinding,
)
from capsule_vision.application.basler_control import BaslerAdmissionControl
from capsule_vision.application.folder_replay import (
    FolderReplay,
    start_interval_wait,
)
from capsule_vision.application.image_archive import ImageArchive
from capsule_vision.application.session import (
    EngineFactory,
    ProductionCycle,
    real_engine,
)
from capsule_vision.application.source_control import FolderReplayControl
from capsule_vision.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    ReportIdentity,
    ReportingCapacityError,
    ReportingStateError,
)
from capsule_vision.configuration import (
    CameraSources,
)
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.inspection import CycleId, Frame
from capsule_vision.inspection.production import ProductionInspectionResult
from capsule_vision.runtime import CoordinatorStoppedError
from capsule_vision.runtime.early_inspection import EarlyInspectionCoordinator

from .application import ProductionApplication
from .ingress import FrameSetIngress
from .models import (
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
)
from .session import ProductionRuntimeSession


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


class SourceRuntimeSession(ProductionRuntimeSession):
    """The runtime session also owns and joins its per-generation source.

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
        replay_control: FolderReplayControl | None = None,
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
            name=f"ProductionSource-{generation}",
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
            ingress = FrameSetIngress(self._application, self._layout)
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
