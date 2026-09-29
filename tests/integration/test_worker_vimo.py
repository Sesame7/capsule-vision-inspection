"""Opt-in real engine ownership and repeated lifecycle checks; no benchmark."""

import json
from threading import current_thread, get_ident
from threading import enumerate as threads
from typing import cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from tests.integration.test_reference import (
    ROOT,
    Reference,
    ReferenceImage,
    short_cases,
    unavailable,
    verify_files,
    verify_result,
)

from vimo_demo.inference import (
    ImageResult,
    VimoInitializationError,
    VimoSegmentationEngine,
)
from vimo_demo.inference._model_store import sha256
from vimo_demo.runtime import InferenceWorker, WorkerState

MODEL = ROOT / "models/model_trt_fp16.vimosln"
TIMEOUT = 120.0


class RecordedEngine(VimoSegmentationEngine):
    """Record real engine entry points without changing SDK behavior."""

    def __init__(self, trace: list[tuple[str, int]]) -> None:
        self.trace = trace
        self.trace.append(("construct", get_ident()))
        assert not current_thread().daemon
        super().__init__(MODEL, cache_dir=ROOT / "outputs/vimo_cache")

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.trace.append(("infer", get_ident()))
        return super().infer(image_bgr)

    def close(self) -> None:
        self.trace.append(("close", get_ident()))
        try:
            super().close()
        except BaseException:
            self.trace.append(("close_failed", get_ident()))
            raise


def reference_image(
    config: pytest.Config,
) -> tuple[Reference, ReferenceImage, NDArray[np.generic]]:
    reference = cast(
        Reference,
        json.loads((ROOT / "tests/reference/phase0.json").read_text(encoding="utf-8")),
    )
    if not MODEL.is_file() or not (ROOT / "images").is_dir():
        unavailable(config, "Local reference model/images unavailable")
    if sha256(MODEL) != reference["model_sha256"]:
        unavailable(config, "Model hash differs; golden semantics do not apply")
    row = next(row for row in short_cases(reference) if row["regions_per_label"])
    verify_files([row], config, full=False)
    image = cv2.imdecode(
        np.fromfile(ROOT / "images" / row["filename"], dtype=np.uint8), cv2.IMREAD_COLOR
    )
    assert image is not None, row["filename"]
    return reference, row, image


def run_cycle(
    image: NDArray[np.generic],
    row: ReferenceImage,
    reference: Reference,
    config: pytest.Config,
    failures: dict[str, int],
) -> None:
    trace: list[tuple[str, int]] = []
    worker = InferenceWorker(lambda: RecordedEngine(trace))
    phase = "initialization"
    try:
        worker.start().result(TIMEOUT)
        phase = "inference"
        result = worker.submit(image).result(TIMEOUT)
        verify_result(result, row, reference["image_shape"])
    except VimoInitializationError as exc:
        failures[phase] += 1
        cause = exc.__cause__
        if isinstance(cause, (ImportError, OSError)) or (
            type(cause).__name__ == "VimoException"
            and getattr(cause, "code", None) == 40000
        ):
            unavailable(config, f"ViMo runtime/license unavailable: {exc}")
        raise
    except BaseException:
        failures[phase] += 1
        raise
    finally:
        worker.request_stop()
        joined = worker.join(TIMEOUT)
        if not joined or any(action == "close_failed" for action, _ in trace):
            failures["shutdown"] += 1
        assert joined, f"Worker did not terminate: {worker.thread_name}"
    assert worker.state is WorkerState.STOPPED, worker.failure
    assert worker.failure is None and not worker.is_alive
    assert not any(thread.name == worker.thread_name for thread in threads())
    assert [action for action, _ in trace] == ["construct", "infer", "close"]
    assert len({ident for _, ident in trace}) == 1
    assert trace[0][1] != get_ident()
    # Results still belong to the caller after engine/thread cleanup.
    verify_result(result, row, reference["image_shape"])


def run_cycles(iterations: int, config: pytest.Config) -> None:
    reference, row, image = reference_image(config)
    failures = {"initialization": 0, "inference": 0, "shutdown": 0}
    completed = 0
    try:
        for _ in range(iterations):
            run_cycle(image, row, reference, config, failures)
            completed += 1
    finally:
        remaining = [
            thread.name
            for thread in threads()
            if thread.name.startswith("VimoInferenceWorker-")
        ]
        print(
            json.dumps(
                {
                    "iterations_completed": completed,
                    "failures": failures,
                    "remaining_worker_threads": remaining,
                }
            )
        )
    assert completed == iterations and not remaining


@pytest.mark.gpu
def test_worker_one_image(pytestconfig: pytest.Config) -> None:
    run_cycles(1, pytestconfig)


@pytest.mark.gpu
def test_worker_repeated_lifecycle(pytestconfig: pytest.Config) -> None:
    run_cycles(10, pytestconfig)
