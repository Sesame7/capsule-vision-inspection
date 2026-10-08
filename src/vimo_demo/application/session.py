"""One disposable coordinator/worker lifetime, including worker-owned warm-up."""

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from threading import Event, Lock
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from vimo_demo.configuration import validate_model_artifact
from vimo_demo.inference import ImageResult, VimoSegmentationEngine
from vimo_demo.inference._validation import validate_image
from vimo_demo.inspection import CollectedCycle, CycleId, Frame, InspectionCycleResult
from vimo_demo.inspection.production import ProductionInspectionResult
from vimo_demo.runtime import (
    CoordinatedCycleResult,
    InferenceEngine,
    InferenceWorker,
    RuntimeCoordinator,
)

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


def _real_engine(prepared: PreparedConfiguration) -> InferenceEngine:
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
        engine_factory: EngineFactory = _real_engine,
        cycle_timeout: float = 5.0,
        max_collecting: int = 2,
        max_ready: int = 2,
        max_commands: int = 32,
    ) -> None:
        if type(warmup_iterations) is not int or warmup_iterations < 1:
            raise ValueError("warmup_iterations must be positive")
        self.generation = generation
        self._lock = Lock()
        self._cleanup_safe = True
        self._stop = Event()
        self._results: dict[CycleId, ProductionInspectionResult] = {}
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
            try:
                shapes: set[tuple[int, ...]] = set()
                for supplied in warmup(prepared):
                    image = validate_image(supplied)
                    if image.shape in shapes:
                        continue
                    shapes.add(image.shape)
                    for _ in range(warmup_iterations):
                        if self._stop.is_set():
                            raise ApplicationStateError("Stopped during warm-up")
                        result = engine.infer(image)
                        if result.image_shape != image.shape:
                            raise ValueError("Warm-up result shape mismatch")
                        del result
                if not shapes:
                    raise ValueError("Warm-up provider returned no input shapes")
            except BaseException as exc:
                try:
                    engine.close()
                except BaseException as cleanup:
                    exc.add_note(f"Warm-up cleanup failed: {cleanup}")
                raise
            return engine

        def inspect(
            cycle: CollectedCycle, infer: Callable[[NDArray[np.uint8]], ImageResult]
        ) -> InspectionCycleResult:
            raw, result = pipeline.inspect(cycle, infer)
            with self._lock:
                self._results[cycle.cycle_id] = result
            return raw

        self._coordinator = RuntimeCoordinator(
            InferenceWorker(factory),
            prepared.configuration.layout.camera_ids,
            timeout=cycle_timeout,
            max_collecting=max_collecting,
            max_ready=max_ready,
            max_commands=max_commands,
            cycle_inspector=inspect,
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
        admission = self._coordinator.admit_cycle(cycle_id)
        result: Future[ProductionInspectionResult] = Future()
        result.set_running_or_notify_cancel()

        def done(future: Future[CoordinatedCycleResult]) -> None:
            try:
                future.result()
                with self._lock:
                    production = self._results.pop(cycle_id)
                result.set_result(production)
            except BaseException as exc:
                result.set_exception(exc)

        admission.result.add_done_callback(done)
        return ProductionCycle(admission.accepted, result)

    def submit_frame(self, frame: Frame) -> Future[None]:
        return self._coordinator.submit_frame(frame)

    def expire(self) -> Future[None]:
        return self._coordinator.expire()

    def request_stop(self) -> None:
        self._stop.set()
        self._coordinator.request_stop()

    def join(self, timeout: float | None = None) -> bool:
        joined = self._coordinator.join(timeout)
        if joined:
            with self._lock:
                self._results.clear()
        return joined


def lifecycle_error(message: str, cause: BaseException) -> ApplicationError:
    error = ApplicationError(f"{message}: {type(cause).__name__}: {cause}")
    error.__cause__ = cause
    return error
