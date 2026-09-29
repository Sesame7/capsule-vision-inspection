"""One real image through Qt delivery and the existing worker ownership path."""

from threading import enumerate as threads
from threading import get_ident

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_reference import unavailable, verify_result
from tests.integration.test_worker_vimo import RecordedEngine, reference_image
from tests.qt_support import Receiver, until

from vimo_demo.inference import VimoInitializationError
from vimo_demo.runtime import InferenceWorker
from vimo_demo.ui import QtInferenceBridge


@pytest.mark.gpu
def test_qt_one_image(qapp: QApplication, pytestconfig: pytest.Config) -> None:
    assert qapp.thread() is not None
    reference, row, image = reference_image(pytestconfig)
    trace: list[tuple[str, int]] = []
    worker = InferenceWorker(lambda: RecordedEngine(trace))
    bridge = QtInferenceBridge(worker)
    receiver = Receiver()
    bridge.startup_succeeded.connect(receiver.on_ready)
    bridge.startup_failed.connect(receiver.on_error)
    bridge.result_ready.connect(receiver.on_result)
    bridge.inference_failed.connect(receiver.on_error)
    bridge.shutdown_failed.connect(receiver.on_error)
    bridge.shutdown_finished.connect(receiver.on_finished)
    try:
        bridge.start()
        until(lambda: receiver.ready or bool(receiver.errors), 120_000)
        if receiver.errors:
            error = receiver.errors[0]
            cause = error.__cause__
            if isinstance(error, VimoInitializationError) and (
                isinstance(cause, (ImportError, OSError))
                or (
                    type(cause).__name__ == "VimoException"
                    and getattr(cause, "code", None) == 40000
                )
            ):
                unavailable(pytestconfig, f"ViMo runtime/license unavailable: {error}")
            raise error
        bridge.submit(image)
        until(lambda: bool(receiver.results) or bool(receiver.errors), 120_000)
        assert not receiver.errors
        verify_result(receiver.results[0], row, reference["image_shape"])
    finally:
        bridge.request_stop()
        until(lambda: receiver.finished, 120_000)
        bridge.detach()
        bridge.deleteLater()
    assert not receiver.errors
    assert worker.join(0) and worker.failure is None
    assert not any(t.name == worker.thread_name for t in threads())
    assert set(receiver.thread_ids) == {get_ident()}
    assert [action for action, _ in trace] == ["construct", "infer", "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    print(f"GUI thread={get_ident()}, engine thread={trace[0][1]}, no residual worker")
