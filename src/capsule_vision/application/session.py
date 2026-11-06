"""One disposable coordinator/worker lifetime, including worker-owned warm-up."""

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from threading import Event, Lock
from time import monotonic
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from capsule_vision.configuration import validate_model_artifact
from capsule_vision.diagnostics import RunDiagnostics
from capsule_vision.inference import ImageResult, VimoSegmentationEngine
from capsule_vision.inference._validation import validate_image
from capsule_vision.inspection import CycleId, Frame
from capsule_vision.inspection.production import (
    ConfiguredCycleCompletion,
    ProductionInspectionResult,
)
from capsule_vision.runtime import (
    CoordinatedCycleResult,
    InferenceEngine,
    InferenceWorker,
    RuntimeCoordinator,
)
from capsule_vision.runtime.early_inspection import EarlyInspectionCoordinator

from .models import ApplicationError, ApplicationStateError, PreparedConfiguration
from .pipeline import ConfiguredProductionPipeline


@dataclass(frozen=True, slots=True)
class ProductionCycle:
    accepted: Future[None]
    result: Future[ProductionInspectionResult]


class RuntimeSession(Protocol):
    @property
    def finished(self) -> Future[None]: ...
    @property
    def failure(self) -> BaseException | None: ...
    @property
    def cleanup_safe(self) -> bool: ...
    def start(self) -> Future[None]: ...
    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle: ...
    def submit_frame(self, frame: Frame) -> Future[None]: ...
    def expire(self) -> Future[None]: ...
    def request_stop(self) -> None: ...
    def join(self, timeout: float | None = None) -> bool: ...


type WarmupProvider = Callable[[PreparedConfiguration], Sequence[NDArray[np.generic]]]
type EngineFactory = Callable[[PreparedConfiguration], InferenceEngine]


def real_engine(prepared: PreparedConfiguration) -> InferenceEngine:
    if prepared.managed_model:
        return VimoSegmentationEngine.from_managed_model(
            prepared.model_path,
            expected_sha256=prepared.configuration.model.expected_sha256,
            module_id=prepared.configuration.model.module_id,
        )
    return VimoSegmentationEngine(
        prepared.model_path, module_id=prepared.configuration.model.module_id
    )


class _OwnedEngine:
    def __init__(
        self, engine: InferenceEngine, cleanup_failed: Callable[[], None]
    ) -> None:
        self._engine = engine
        self._cleanup_failed = cleanup_failed

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        return self._engine.infer(image_bgr)

    def close(self) -> None:
        try:
            self._engine.close()
        except BaseException:
            self._cleanup_failed()
            raise


class ProductionRuntimeSession:
    """Factory/constructor starts no work. All native entry points stay on worker."""

    def __init__(
        self,
        prepared: PreparedConfiguration,
        generation: int,
        *,
        warmup: WarmupProvider,
        warmup_iterations: int = 2,
        engine_factory: EngineFactory = real_engine,
        cycle_timeout: float = 5.0,
        max_collecting: int = 2,
        max_ready: int = 2,
        max_commands: int = 32,
        early_camera: bool = False,
        diagnostics: RunDiagnostics | None = None,
    ) -> None:
        if type(warmup_iterations) is not int or warmup_iterations < 1:
            raise ValueError("warmup_iterations must be positive")
        self.generation = generation
        self.diagnostics = diagnostics
        self._lock = Lock()
        self._cleanup_safe = True
        self._stop = Event()
        pipeline = ConfiguredProductionPipeline(prepared.configuration)

        def factory() -> InferenceEngine:
            # Preparation is point-in-time: recheck the chosen artifact before
            # activation, using its resolved path rather than the process cwd.
            validate_model_artifact(
                replace(
                    prepared.configuration.model, model_path=str(prepared.model_path)
                ),
                base_directory=prepared.model_path.parent,
            )
            engine = _OwnedEngine(engine_factory(prepared), self._unsafe_cleanup)
            started = monotonic() if diagnostics else 0.0
            cancelled = False
            try:
                shapes: set[tuple[int, ...]] = set()
                for supplied in warmup(prepared):
                    image = validate_image(supplied)
                    if image.shape in shapes:
                        continue
                    shapes.add(image.shape)
                    for _ in range(warmup_iterations):
                        if self._stop.is_set():
                            cancelled = True
                            raise ApplicationStateError("Stopped during warm-up")
                        result = engine.infer(image)
                        if result.image_shape != image.shape:
                            raise ValueError("Warm-up result shape mismatch")
                        del result
                if not shapes:
                    raise ValueError("Warm-up provider returned no input shapes")
            except BaseException as exc:
                if diagnostics:
                    diagnostics.record(
                        "inspection.warmup",
                        outcome="cancelled" if cancelled else "failed",
                        error_type=type(exc).__name__,
                        stop_requested=self._stop.is_set(),
                        duration_ms=(monotonic() - started) * 1000,
                    )
                try:
                    engine.close()
                except BaseException as cleanup:
                    exc.add_note(f"Warm-up cleanup failed: {cleanup}")
                raise
            if diagnostics:
                diagnostics.record(
                    "inspection.warmup",
                    outcome="completed",
                    shapes=len(shapes),
                    iterations_per_shape=warmup_iterations,
                    duration_ms=(monotonic() - started) * 1000,
                )
            return engine

        self._coordinator = (
            EarlyInspectionCoordinator(
                InferenceWorker(factory),
                prepared.configuration.layout,
                pipeline.inspect_frame,
                pipeline.forced_camera,
            )
            if early_camera
            else RuntimeCoordinator(
                InferenceWorker(factory),
                prepared.configuration.layout.camera_ids,
                timeout=cycle_timeout,
                max_collecting=max_collecting,
                max_ready=max_ready,
                max_commands=max_commands,
                cycle_inspector=pipeline.inspect,
            )
        )

    def _unsafe_cleanup(self) -> None:
        with self._lock:
            self._cleanup_safe = False

    @property
    def cleanup_safe(self) -> bool:
        with self._lock:
            return self._cleanup_safe

    @property
    def finished(self) -> Future[None]:
        return self._coordinator.finished

    @property
    def failure(self) -> BaseException | None:
        return self._coordinator.failure

    @property
    def thread_names(self) -> tuple[str, ...]:
        return self._coordinator.thread_names

    def start(self) -> Future[None]:
        return self._coordinator.start()

    def begin_cycle(self, cycle_id: CycleId) -> ProductionCycle:
        if isinstance(self._coordinator, EarlyInspectionCoordinator):
            early = self._coordinator.begin(cycle_id)
            return ProductionCycle(early.accepted, early.result)
        admission = self._coordinator.admit_cycle(cycle_id)
        result: Future[ProductionInspectionResult] = Future()
        result.set_running_or_notify_cancel()

        def done(future: Future[CoordinatedCycleResult]) -> None:
            try:
                completion = future.result().result
                if not isinstance(completion, ConfiguredCycleCompletion):
                    raise ApplicationError("Configured cycle returned a raw result")
                result.set_result(completion.production)
            except BaseException as exc:
                result.set_exception(exc)

        admission.result.add_done_callback(done)
        return ProductionCycle(admission.accepted, result)

    def submit_frame(self, frame: Frame) -> Future[None]:
        trace = self.diagnostics
        if trace is None or not trace.writer.detailed:
            return self._coordinator.submit_frame(frame)
        identity, captured = frame.id, frame.captured_at
        started = monotonic()

        def observed(error: BaseException | None) -> None:
            trace.record(
                "frame.admission",
                cycle_id=identity.cycle_id,
                camera_id=identity.camera_id,
                frame_sequence=identity.sequence,
                source_captured_at_s=captured,
                outcome="rejected" if error else "accepted",
                error_type=type(error).__name__ if error else None,
                handoff_ms=(monotonic() - started) * 1000,
            )

        try:
            accepted = self._coordinator.submit_frame(frame)
        except Exception as exc:
            observed(exc)
            raise
        accepted.add_done_callback(lambda done: observed(done.exception()))
        return accepted

    def expire(self) -> Future[None]:
        return self._coordinator.expire()

    def request_stop(self) -> None:
        self._stop.set()
        self._coordinator.request_stop()

    def join(self, timeout: float | None = None) -> bool:
        return self._coordinator.join(timeout)


def lifecycle_error(message: str, cause: BaseException) -> ApplicationError:
    error = ApplicationError(f"{message}: {type(cause).__name__}: {cause}")
    error.__cause__ = cause
    return error
