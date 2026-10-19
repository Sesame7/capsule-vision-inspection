"""Opt-in scalar observers around existing owners; no alternative runtime.

Install only in a dedicated backend diagnostic process, before constructing any
owners, and retain the scope until all owners join. No Qt event loop or Web is
started. Production modules on disk and their capacities remain unchanged.
"""

import hashlib
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Event, get_ident
from time import monotonic
from unittest.mock import patch

import numpy as np
from numpy.typing import NDArray

from vimo_demo.application import PreparedConfiguration, ProductionApplication
from vimo_demo.application.basler import AcquisitionMetadata, CameraCleanup
from vimo_demo.application.image_archive import (
    ArchiveImage,
    ArchiveSnapshot,
    _component,  # pyright: ignore[reportPrivateUsage] -- diagnostic reuses archive's exact reversible path escaping.
    camera_archive_status,
    write_png,
)
from vimo_demo.application.session import EngineFactory, ProductionCycle
from vimo_demo.communication.fins import FinsMemoryArea, FinsTransport, FinsUdpClient
from vimo_demo.communication.reporting import (
    FirstStationReport,
    FirstStationReportingWorker,
    LocalReportingSettings,
    ReportIdentity,
    ReportObservation,
)
from vimo_demo.configuration import ImageArchiveSettings
from vimo_demo.inference import ImageResult
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import ProductionInspectionResult
from vimo_demo.runtime import InferenceEngine
from vimo_demo.runtime.early_inspection import EarlyInspectionCoordinator

from .ledger import Fields, Ledger


def identity(generation: int, frame: FrameId) -> Fields:
    return {
        "generation": generation,
        "cycle": int(frame.cycle_id),
        "camera": str(frame.camera_id),
        "sequence": frame.sequence,
    }


class Observations:
    def __init__(
        self,
        ledger: Ledger,
        source_epoch: str,
        cameras: tuple[tuple[CameraId, str], ...] = (),
        *,
        strict_run: bool = False,
    ) -> None:
        if not source_epoch:
            raise ValueError("Explicit source connection epoch required")
        self.ledger, self.source_epoch = ledger, source_epoch
        self._cameras = {serial: str(camera) for camera, serial in cameras}
        self.strict_run = strict_run
        self.stop = Event()
        self._terminal = Condition()
        self.terminal_count = 0
        if cameras and (len(cameras) != 6 or len(self._cameras) != 6):
            raise ValueError("Six configured camera/serial identities required")

    def fail(self, reason: str) -> None:
        if self.strict_run:
            self.ledger.emit("run_guard", issue=reason[:1200])
            self.stop.set()
            with self._terminal:
                self._terminal.notify_all()

    def wait_cycles(self, expected: int, timeout: float) -> bool:
        with self._terminal:
            self._terminal.wait_for(
                lambda: self.terminal_count >= expected or self.stop.is_set(), timeout
            )
            return self.terminal_count == expected and not self.stop.is_set()

    def sdk(self, metadata: AcquisitionMetadata) -> None:
        # Explicit fields avoid SDK objects and preserve raw clock metadata.
        self.ledger.emit(
            "sdk_metadata",
            source_epoch=self.source_epoch,
            serial=metadata.serial,
            camera=self._cameras.get(metadata.serial),
            received_at=metadata.received_at,
            succeeded=metadata.succeeded,
            error_code=metadata.error_code,
            error=metadata.error_description[:1200],
            width=metadata.width,
            height=metadata.height,
            pixel_type=metadata.pixel_type,
            payload_bytes=metadata.payload_bytes,
            block_id=metadata.block_id,
            image_number=metadata.image_number,
            camera_timestamp=metadata.camera_timestamp,
            skipped_images=metadata.skipped_images,
        )

    def cycle(self, generation: int, cycle_id: CycleId, cycle: ProductionCycle) -> None:
        self.ledger.emit("cycle_requested", generation=generation, cycle=int(cycle_id))

        def accepted(future: Future[None]) -> None:
            error = (
                future.exception()
                if not future.cancelled()
                else RuntimeError("cancelled")
            )
            self.ledger.emit(
                "cycle_rejected" if error else "cycle_admitted",
                generation=generation,
                cycle=int(cycle_id),
                issue=str(error)[:1200] if error else None,
            )
            if error:
                self.fail(f"Cycle admission: {error}")

        def terminal(future: Future[ProductionInspectionResult]) -> None:
            try:
                result = future.result()  # Done callback only; no unfinished wait.
            except BaseException as exc:
                self.ledger.emit(
                    "cycle_exception",
                    generation=generation,
                    cycle=int(cycle_id),
                    issue=f"{type(exc).__name__}: {exc}"[:1200],
                )
                self.fail(f"Cycle terminal exception: {exc}")
                return
            self.result(generation, result)

        cycle.accepted.add_done_callback(accepted)
        cycle.result.add_done_callback(terminal)

    def result(self, generation: int, result: ProductionInspectionResult) -> None:
        self.ledger.emit(
            "cycle_terminal",
            generation=generation,
            cycle=int(result.cycle_id),
            status=result.cycle_status.name,
            cameras=tuple(str(row.camera_id) for row in result.cameras),
            channel_ids=tuple(
                int(row.channel_id) for cam in result.cameras for row in cam.channels
            ),
            business=tuple(
                row.business_status.name
                for cam in result.cameras
                for row in cam.channels
            ),
            technical=tuple(
                row.assessment.technical_status.name
                for cam in result.cameras
                for row in cam.channels
            ),
            inspection_started_at=result.inspection_started_at,
            result_ready_at=result.result_ready_at,
            aggregation_ms=result.aggregation_ms,
        )
        for camera in result.cameras:
            self.ledger.emit(
                "camera_result",
                generation=generation,
                cycle=int(result.cycle_id),
                camera=str(camera.camera_id),
                sequence=camera.frame_id.sequence if camera.frame_id else None,
                shape=tuple(camera.image_shape) if camera.image_shape else None,
                collection_issue=str(camera.collection_issue)[:1200]
                if camera.collection_issue
                else None,
                vimo_ms=camera.timing.vimo_ms,
                traditional_ms=camera.timing.traditional_ms,
                occupancy_ms=camera.timing.occupancy_ms,
                mapping_ms=camera.timing.mapping_ms,
            )
        with self._terminal:
            self.terminal_count += 1
            self._terminal.notify_all()
        if result.cycle_status.name != "COMPLETE" or any(
            row.assessment.technical_status.name == "ERROR"
            for cam in result.cameras
            for row in cam.channels
        ):
            self.fail("Incomplete/technical ERROR result; no further stimulus")

    def frame(self, generation: int, frame: Frame, admitted: Future[None]) -> None:
        fields = identity(generation, frame.id)
        self.ledger.emit(
            "frame_submitted",
            **fields,
            source_epoch=self.source_epoch,
            shape=tuple(frame.image.shape),
            raw_bytes=frame.image.nbytes,
        )

        def completed(future: Future[None]) -> None:
            error = (
                future.exception()
                if not future.cancelled()
                else RuntimeError("cancelled")
            )
            self.ledger.emit(
                "frame_rejected" if error else "frame_admitted",
                **fields,
                issue=str(error)[:1200] if error else None,
            )
            if error:
                self.fail(f"Frame admission: {error}")

        admitted.add_done_callback(completed)  # Closure contains scalars, never Frame.

    def report(self, row: ReportObservation) -> None:
        self.ledger.emit(
            "report_outcome",
            generation=row.identity.generation,
            cycle=int(row.identity.cycle_id),
            outcome=row.outcome.value,
            sequence=row.sequence,
            words=row.reject_words,
            stage=row.stage.value if row.stage else None,
            issue=row.issue.message if row.issue else None,
            end_code=row.issue.end_code if row.issue else None,
            duration_ms=row.duration_ms,
            bitmap_ms=row.bitmap_ms,
            sequence_ms=row.sequence_ms,
            completion_to_ack_ms=row.completion_to_ack_ms,
        )
        if row.outcome.value != "acknowledged":
            self.fail(f"Report {row.outcome.value}")

    def target(self, sequence: int, words: tuple[int, int, int]) -> None:
        self.ledger.emit("target_commit", sequence=sequence, words=words)

    def archive_snapshot(self, snapshot: ArchiveSnapshot) -> None:
        self.ledger.emit(
            "archive_snapshot",
            generation=snapshot.generation,
            written=snapshot.written,
            dropped=snapshot.dropped,
            failed=snapshot.failed,
            retained_bytes=snapshot.retained_bytes,
            pending=snapshot.pending,
            collecting=snapshot.collecting,
            alive=snapshot.alive,
            issue=snapshot.last_issue,
        )

    def write_archive(self, root: Path, item: ArchiveImage) -> None:
        # Runs on the existing archive writer, not inference/completion callbacks.
        fields = identity(item.generation, item.frame.id)
        started = monotonic()
        stamp = item.observed_at
        directory = (
            root / stamp.strftime("%Y-%m-%d") / _component(item.frame.id.camera_id)
        )
        prefix = f"{item.status}_{stamp:%H%M%S}{stamp.microsecond // 1000:03d}_{item.frame.id.cycle_id}"
        before = set(directory.glob(prefix + "*.png"))
        self.ledger.emit(
            "archive_write_started",
            **fields,
            status=item.status,
            raw_bytes=item.frame.image.nbytes,
        )
        try:
            write_png(root, item)
        except Exception as exc:
            self.ledger.emit("archive_failed", **fields, issue=str(exc)[:1200])
            self.fail(f"Archive write: {exc}")
            raise
        self.ledger.emit(
            "archive_written",
            **fields,
            status=item.status,
            duration_ms=(monotonic() - started) * 1000,
            pixel_sha256=hashlib.sha256(item.frame.image).hexdigest(),
        )
        created = set(directory.glob(prefix + "*.png")) - before
        if len(created) == 1:
            path = created.pop()
            self.ledger.emit(
                "archive_file",
                **fields,
                path=str(path.resolve()),
                bytes=path.stat().st_size,
            )
        else:
            self.ledger.emit(
                "missing", observation="unambiguous archive output filename", **fields
            )

    def owner(self, name: str, terminated: bool, cleanup_safe: bool) -> None:
        self.ledger.emit(
            "owner_stopped",
            owner=name,
            terminated=terminated,
            cleanup_safe=cleanup_safe,
        )

    def engine(self, factory: EngineFactory) -> EngineFactory:
        observer = self

        class MeasuredEngine:
            def __init__(self, prepared: PreparedConfiguration) -> None:
                start = monotonic()
                observer.ledger.emit("engine_initializing", thread=get_ident())
                try:
                    self.engine = factory(prepared)
                except Exception as exc:
                    observer.ledger.emit(
                        "engine_initialization_failed", issue=str(exc)[:1200]
                    )
                    raise
                observer.ledger.emit(
                    "engine_created",
                    duration_ms=(monotonic() - start) * 1000,
                    thread=get_ident(),
                )

            def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
                started = monotonic()
                succeeded = False
                try:
                    result = self.engine.infer(image_bgr)
                    succeeded = True
                    return result
                except Exception as exc:
                    observer.ledger.emit("inference_error", issue=str(exc)[:1200])
                    raise
                finally:
                    observer.ledger.emit(
                        "inference_finished",
                        thread=get_ident(),
                        shape=tuple(image_bgr.shape),
                        duration_ms=(monotonic() - started) * 1000,
                        succeeded=succeeded,
                        attribution="includes warm-up; FrameId unavailable at engine API",
                    )

            def close(self) -> None:
                self.engine.close()
                observer.ledger.emit("engine_closed", thread=get_ident())

        def create(prepared: PreparedConfiguration) -> InferenceEngine:
            return MeasuredEngine(prepared)

        return create

    @contextmanager
    def composition_scope(self) -> Generator[None]:
        """Transparent process-local seams, restored only after actual owners exit.

        Imports do not create an engine/source. This scope must never surround an
        unrelated GUI session. Use compose(..., engine_factory=obs.engine(real_engine)).
        """
        from vimo_demo.application import session as session_module
        from vimo_demo.application._basler_sdk import PylonBackend
        from vimo_demo.application.basler_connection import BaslerConnection
        from vimo_demo.application.image_archive import ImageArchive
        from vimo_demo.ui import local_production

        obs = self
        original_begin = ProductionApplication.begin_cycle
        original_submit = ProductionApplication.submit_frame
        original_disconnect = BaslerConnection.wait_disconnected

        def disconnected(connection: BaslerConnection) -> None:
            try:
                original_disconnect(connection)
            finally:
                snapshot = connection.snapshot()
                obs.owner("source", not snapshot.thread_alive, snapshot.cleanup_safe)

        def begin(app: ProductionApplication, cycle_id: CycleId) -> ProductionCycle:
            generation = app.snapshot().generation
            try:
                cycle = original_begin(app, cycle_id)
            except Exception as exc:
                obs.ledger.emit(
                    "cycle_rejected",
                    generation=generation,
                    cycle=int(cycle_id),
                    issue=str(exc)[:1200],
                )
                obs.fail(f"Cycle submission: {exc}")
                raise
            obs.cycle(generation, cycle_id, cycle)
            return cycle

        def submit(app: ProductionApplication, frame: Frame) -> Future[None]:
            try:
                admitted = original_submit(app, frame)
            except Exception as exc:
                obs.ledger.emit(
                    "frame_submission_exception",
                    **identity(app.snapshot().generation, frame.id),
                    issue=str(exc)[:1200],
                )
                obs.fail(f"Frame submission: {exc}")
                raise
            obs.frame(app.snapshot().generation, frame, admitted)
            return admitted

        class Backend(PylonBackend):
            def retrieve(
                self, serial: str
            ) -> tuple[AcquisitionMetadata, NDArray[np.uint8] | None]:
                metadata, pixels = super().retrieve(serial)
                obs.sdk(metadata)
                return metadata, pixels

            def cleanup(self) -> tuple[CameraCleanup, ...]:
                rows = super().cleanup()
                for row in rows:
                    obs.ledger.emit(
                        "camera_cleanup",
                        serial=row.serial,
                        stopped=row.stopped,
                        settings_restored=row.settings_restored,
                        closed=row.closed,
                        detached=row.detached,
                        safe=row.safe,
                    )
                return rows

        class EarlyRuntime(EarlyInspectionCoordinator):
            def sample(self) -> None:
                row = self.snapshot()
                obs.ledger.emit(
                    "runtime_snapshot",
                    cycles=row.cycles,
                    queued_cameras=row.queued_cameras,
                    peak_cycles=row.peak_cycles,
                    peak_queued_cameras=row.peak_queued_cameras,
                    late_results=row.late_results,
                    active_cycle=int(row.active_frame.cycle_id)
                    if row.active_frame
                    else None,
                    active_camera=str(row.active_frame.camera_id)
                    if row.active_frame
                    else None,
                    active_sequence=row.active_frame.sequence
                    if row.active_frame
                    else None,
                )

            def submit_frame(self, frame: Frame) -> Future[None]:
                admitted = super().submit_frame(frame)
                admitted.add_done_callback(lambda _: self.sample())
                return admitted

            def join(self, timeout: float | None = None) -> bool:
                joined = super().join(timeout)
                self.sample()
                obs.owner("early_runtime", joined, joined)
                return joined

        class Reporter(FirstStationReportingWorker):
            def __init__(
                self,
                generation: int,
                settings: LocalReportingSettings,
                *,
                attributable: Callable[[ReportIdentity], bool] = lambda _: True,
            ) -> None:
                if (
                    settings.host != "127.0.0.1"
                    or settings.transport is not FinsTransport.UDP
                ):
                    raise ValueError(
                        "Benchmark reporting requires the isolated localhost UDP target"
                    )
                reporter = self
                self._observed_cycle: int | None = None

                class Client(FinsUdpClient):
                    def read_words(
                        self,
                        start: int,
                        count: int = 1,
                        *,
                        area: FinsMemoryArea = FinsMemoryArea.DM,
                    ) -> tuple[int, ...]:
                        words = super().read_words(start, count, area=area)
                        if start == 49 and count == 1 and area is FinsMemoryArea.W:
                            obs.ledger.emit(
                                "report_initial",
                                generation=generation,
                                cycle=reporter._observed_cycle,
                                sequence=words[0],
                            )
                        return words

                    def write_words(
                        self,
                        start: int,
                        values: Sequence[int],
                        *,
                        area: FinsMemoryArea = FinsMemoryArea.DM,
                    ) -> None:
                        obs.ledger.emit(
                            "report_write_send",
                            generation=generation,
                            cycle=reporter._observed_cycle,
                            start=start,
                            words=tuple(values),
                        )
                        super().write_words(start, values, area=area)
                        obs.ledger.emit(
                            "report_write_ack",
                            generation=generation,
                            cycle=reporter._observed_cycle,
                            start=start,
                        )

                def client(config: LocalReportingSettings) -> FinsUdpClient:
                    return Client(
                        config.host,
                        port=config.port,
                        timeout=config.timeout,
                        destination=config.destination,
                        source=config.source,
                    )

                super().__init__(
                    generation,
                    settings,
                    client_factory=client,
                    attributable=attributable,
                )

            def submit(self, report: FirstStationReport) -> Future[ReportObservation]:
                obs.ledger.emit(
                    "report_eligible",
                    generation=report.identity.generation,
                    cycle=int(report.identity.cycle_id),
                    words=report.reject_words,
                )
                try:
                    future = super().submit(report)
                except Exception as exc:
                    obs.ledger.emit(
                        "report_rejected",
                        generation=report.identity.generation,
                        cycle=int(report.identity.cycle_id),
                        issue=str(exc)[:1200],
                    )
                    obs.fail(f"Report submission: {exc}")
                    raise
                future.add_done_callback(lambda f: obs.report(f.result()))
                return future

            def _attempt(self, report: FirstStationReport) -> ReportObservation:
                self._observed_cycle = int(report.identity.cycle_id)
                obs.ledger.emit(
                    "report_attempt",
                    generation=report.identity.generation,
                    cycle=int(report.identity.cycle_id),
                )
                try:
                    return super()._attempt(report)
                finally:
                    self._observed_cycle = None

            def note_skipped(
                self, identity: ReportIdentity, error: BaseException
            ) -> None:
                obs.ledger.emit(
                    "report_skipped",
                    generation=identity.generation,
                    cycle=int(identity.cycle_id),
                    issue=str(error)[:1200],
                )
                super().note_skipped(identity, error)
                obs.fail(f"Report skipped: {error}")

            def join(self, timeout: float | None = None) -> bool:
                joined = super().join(timeout)
                snapshot = self.snapshot()
                obs.ledger.emit(
                    "report_snapshot",
                    generation=self.generation,
                    pending=snapshot.pending,
                    peak_pending=snapshot.peak_pending,
                    attempted=snapshot.attempted_count,
                    acknowledged=snapshot.acknowledged_count,
                    failed=snapshot.failed_count,
                    alive=snapshot.thread_alive,
                )
                obs.owner("reporter", joined, self.cleanup_safe)
                return joined

        class Archive(ImageArchive):
            def _drop(self, message: str) -> None:
                obs.ledger.emit(
                    "archive_drop",
                    generation=self.generation,
                    issue=message,
                    frame_identity=None,
                )
                super()._drop(message)
                obs.fail(f"Archive drop: {message}")

            def retain(self, frame: Frame) -> None:
                obs.ledger.emit(
                    "archive_retain_attempt",
                    **identity(self.generation, frame.id),
                    raw_bytes=frame.image.nbytes,
                )
                super().retain(frame)
                obs.archive_snapshot(self.snapshot())

            def complete(
                self, cycle: CycleId, result: ProductionInspectionResult | None
            ) -> None:
                if (
                    result is not None
                    and result.cycle_id == cycle
                    and result.layout.camera_ids == self.cameras
                ):
                    for camera in result.cameras:
                        status = camera_archive_status(camera)
                        if camera.frame_id is not None and status is not None:
                            enabled = (
                                self.settings.save_ok,
                                self.settings.save_ng,
                                self.settings.save_error,
                                self.settings.save_empty,
                            )[status]
                            if enabled:
                                obs.ledger.emit(
                                    "archive_eligible",
                                    **identity(self.generation, camera.frame_id),
                                    status=status,
                                )
                super().complete(cycle, result)
                obs.archive_snapshot(self.snapshot())

            def join(self, timeout: float | None = None) -> bool:
                joined = super().join(timeout)
                obs.archive_snapshot(self.snapshot())
                obs.owner("archive", joined, joined)
                return joined

        class Session(local_production.LocalRuntimeSession):
            def start(self) -> Future[None]:
                ready = super().start()

                def completed(future: Future[None]) -> None:
                    error = (
                        future.exception()
                        if not future.cancelled()
                        else RuntimeError("cancelled")
                    )
                    obs.ledger.emit(
                        "session_readiness",
                        generation=self.generation,
                        succeeded=error is None,
                        issue=str(error)[:1200] if error else None,
                    )

                ready.add_done_callback(completed)
                return ready

            def request_stop(self) -> None:
                obs.ledger.emit("stop_requested", generation=self.generation)
                super().request_stop()

            def join(self, timeout: float | None = None) -> bool:
                joined = super().join(timeout)
                obs.owner("session", joined, self.cleanup_safe)
                return joined

        # Constructor's existing writer seam preserves every bound and lifecycle.
        def archive(
            settings: ImageArchiveSettings,
            generation: int,
            cameras: tuple[CameraId, ...],
        ) -> ImageArchive:
            return Archive(settings, generation, cameras, writer=obs.write_archive)

        with (
            patch.object(ProductionApplication, "begin_cycle", begin),
            patch.object(ProductionApplication, "submit_frame", submit),
            patch.object(BaslerConnection, "wait_disconnected", disconnected),
            patch("vimo_demo.application._basler_sdk.PylonBackend", Backend),
            patch.object(local_production, "FirstStationReportingWorker", Reporter),
            patch.object(local_production, "ImageArchive", archive),
            patch.object(local_production, "LocalRuntimeSession", Session),
            patch.object(session_module, "EarlyInspectionCoordinator", EarlyRuntime),
        ):
            yield
