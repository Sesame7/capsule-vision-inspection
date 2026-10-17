"""One disposable, worker-owned model initialization; no production/source work."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from vimo_demo.configuration import ModelDefinition, validate_model_artifact
from vimo_demo.inference import ImageResult, VimoSegmentationEngine
from vimo_demo.runtime import InferenceEngine, InferenceWorker

from .models import ApplicationError, ApplicationIssue


@dataclass(frozen=True, slots=True)
class ModelPreparationRequest:
    model: ModelDefinition
    model_path: Path
    device_id: int = 0
    sample_shape: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if not self.model_path.is_absolute():
            raise ValueError("Preparation requires an absolute model path")
        if type(self.device_id) is not int or self.device_id < 0:
            raise ValueError("device_id must be a nonnegative integer")
        shape = self.sample_shape
        if shape is not None and (
            type(shape) is not tuple
            or len(shape) != 3
            or any(type(n) is not int or n <= 0 for n in shape)
            or shape[2] != 3
        ):
            raise ValueError("sample_shape must be a known positive H x W x 3 shape")


class ModelPreparationState(Enum):
    UNKNOWN = auto()
    PREPARING = auto()
    SUCCEEDED = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class ModelPreparationResult:
    request: ModelPreparationRequest
    initialized: bool
    sample_inferences: int
    cleanup_complete: bool


@dataclass(frozen=True, slots=True)
class ModelPreparationSnapshot:
    state: ModelPreparationState = ModelPreparationState.UNKNOWN
    request: ModelPreparationRequest | None = None
    result: ModelPreparationResult | None = None
    issue: ApplicationIssue | None = None
    owns_resources: bool = False


type ModelPreparationFactory = Callable[[ModelPreparationRequest], InferenceEngine]


def real_preparation_engine(request: ModelPreparationRequest) -> InferenceEngine:
    # Exactly the Production default cache_dir (process-cwd outputs/vimo_cache),
    # module selection and serial batch. No cache inspection or direct TRT calls.
    return VimoSegmentationEngine(
        request.model_path,
        device_id=request.device_id,
        module_id=request.model.module_id,
    )


class _PreparationEngine:
    def __init__(self, engine: InferenceEngine, owner: ModelPreparation) -> None:
        self.engine, self.owner = engine, owner

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        return self.engine.infer(image_bgr)

    def close(self) -> None:
        try:
            self.engine.close()
        except BaseException:
            self.owner.cleanup_failed = True
            raise


class ModelPreparation:
    """Application lifecycle owner waits here; only InferenceWorker calls engine.

    Factories must construct on this worker and raise without retained resources.
    Keep this owner if close fails; termination alone cannot certify SDK cleanup.
    """

    def __init__(
        self, request: ModelPreparationRequest, factory: ModelPreparationFactory
    ) -> None:
        self.request = request
        self.initialized = False
        self.samples = 0
        self.cleanup_failed = False
        self.engine: _PreparationEngine | None = None

        def create() -> InferenceEngine:
            validate_model_artifact(
                replace(request.model, model_path=str(request.model_path)),
                base_directory=request.model_path.parent,
            )
            self.engine = _PreparationEngine(factory(request), self)
            try:
                engine = self.engine.engine
                if isinstance(engine, VimoSegmentationEngine) and (
                    engine.info.model_sha256 != request.model.expected_sha256
                    or engine.info.module_id != request.model.module_id
                    or engine.info.device_id != request.device_id
                ):
                    raise ApplicationError(
                        "Initialized model identity differs from request"
                    )
                self.initialized = True
                return self.engine
            except BaseException:
                self.engine.close()
                raise

        self.worker = InferenceWorker(create, max_pending=1)

    def request_stop(self) -> None:
        self.worker.request_stop()

    def settle(self) -> bool:
        self.worker.request_stop()
        return self.worker.join() and not self.cleanup_failed

    def run(self) -> tuple[ModelPreparationResult, BaseException | None]:
        error: BaseException | None = None
        clean = False
        try:
            self.worker.start().result()
            if self.request.sample_shape is not None:
                image = np.zeros(self.request.sample_shape, dtype=np.uint8)
                image.flags.writeable = False
                for _ in range(2):
                    result = self.worker.submit(image).result()
                    if result.image_shape != self.request.sample_shape:
                        raise ApplicationError(
                            "Preparation sample result shape mismatch"
                        )
                    self.samples += 1
                    del result
        except BaseException as exc:
            error = exc
            # The adapter preserves a constructor-cleanup failure as a note on
            # its chained cause. Never turn that explicit uncertainty into idle.
            cause: BaseException | None = exc
            for _ in range(8):
                if cause is None:
                    break
                if any(
                    "Cleanup also failed:" in note
                    for note in getattr(cause, "__notes__", ())
                ):
                    self.cleanup_failed = True
                cause = cause.__cause__
        finally:
            try:
                clean = self.settle()
                worker_error = self.worker.failure
                if (
                    error is not None
                    and worker_error is not None
                    and worker_error is not error
                ):
                    error.add_note(f"Preparation worker also failed: {worker_error!r}")
                error = error or worker_error
                if not clean:
                    error = error or ApplicationError(
                        "Preparation cleanup remains incomplete"
                    )
            except BaseException as exc:
                error = error or exc
        return ModelPreparationResult(
            self.request, self.initialized, self.samples, clean
        ), error
