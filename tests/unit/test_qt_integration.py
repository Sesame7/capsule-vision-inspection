"""Fake-engine Qt responsiveness, delivery, destruction and close races."""

from collections.abc import Iterator
from pathlib import Path
from threading import enumerate as threads
from threading import get_ident

import cv2
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from tests.qt_support import Receiver, until
from tests.unit.test_inference_worker import Controls

from vimo_demo.runtime import (
    InferenceWorker,
    WorkerQueueFullError,
    WorkerState,
    WorkerStateError,
)
from vimo_demo.ui import QtInferenceBridge
from vimo_demo.ui.app import DemoApplication
from vimo_demo.ui.demo_window import DemoWindow


@pytest.fixture
def qt_case(qapp: QApplication) -> Iterator[tuple[Controls, DemoWindow, Receiver]]:
    assert qapp.thread() is not None
    c = Controls()
    worker = InferenceWorker(c.make_engine)
    window = DemoWindow(worker, shutdown_timeout_ms=50)
    receiver = Receiver()
    bridge = window.bridge
    bridge.startup_succeeded.connect(receiver.on_ready)
    bridge.result_ready.connect(receiver.on_result)
    for signal in (
        bridge.startup_failed,
        bridge.inference_failed,
        bridge.submission_failed,
        bridge.shutdown_failed,
    ):
        signal.connect(receiver.on_error)
    bridge.shutdown_finished.connect(receiver.on_finished)
    window.show()
    try:
        yield c, window, receiver
    finally:
        c.construct_release.set()
        c.infer_release.set()
        c.close_release.set()
        worker.request_stop()
        until(lambda: not worker.is_alive)
        assert worker.join(0)
        bridge.detach()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not any(t.name == worker.thread_name for t in threads())


def ready(window: DemoWindow, receiver: Receiver) -> None:
    window.bridge.start()
    until(lambda: receiver.ready)


def test_delivery_errors_and_thread_ownership(
    qt_case: tuple[Controls, DemoWindow, Receiver],
) -> None:
    c, window, receiver = qt_case
    ready(window, receiver)
    error = RuntimeError("inference error")
    error.__cause__ = ValueError("native detail")
    c.infer_error = error
    window.bridge.submit(c.image)
    until(lambda: bool(receiver.errors))
    assert receiver.errors[0] is error
    assert receiver.errors[0].__cause__ is error.__cause__
    assert "inference error" in window.error.text()
    window.bridge.submit(c.image)
    until(lambda: len(receiver.results) == 1)
    assert receiver.results[0] is c.result
    assert "has_defects: False" in window.summary.text()
    window.close()  # Idle close.
    until(lambda: receiver.finished)
    assert not window.isVisible()
    assert set(receiver.thread_ids) == {get_ident()}
    assert len({ident for _, ident in c.trace}) == 1
    assert c.trace[0][1] != get_ident()
    assert [name for name, _ in c.trace] == ["construct", "infer", "infer", "close"]


def test_startup_failure_preserves_exception(
    qt_case: tuple[Controls, DemoWindow, Receiver],
) -> None:
    c, window, receiver = qt_case
    c.start_error = RuntimeError("constructor failed")
    window.bridge.start()
    until(lambda: receiver.finished)
    assert c.start_error in receiver.errors
    assert not receiver.ready
    assert window.bridge.worker.state is WorkerState.FAILED
    assert "constructor failed" in window.error.text()
    window.close()
    assert not window.isVisible()


@pytest.mark.parametrize("operation", ["startup", "inference"])
def test_blocked_work_keeps_qt_responsive_and_close_waits(
    qt_case: tuple[Controls, DemoWindow, Receiver],
    operation: str,
) -> None:
    c, window, receiver = qt_case
    if operation == "startup":
        c.construct_release.clear()
        window.bridge.start()
        entered, release = c.construct_entered, c.construct_release
    else:
        ready(window, receiver)
        c.infer_release.clear()
        window.bridge.submit(c.image)
        entered, release = c.infer_entered, c.infer_release
    until(entered.is_set)
    ticks: list[int] = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(get_ident()))
    timer.start(10)
    try:
        until(lambda: len(ticks) >= 3)
        assert not release.is_set() and not receiver.results
        window.close()
        window.close()  # Repeated close must not reset timeout or destroy ownership.
        assert window.isVisible() and not window.run_button.isEnabled()
        assert window.bridge.worker.state is WorkerState.STOPPING
        until(lambda: any(isinstance(e, TimeoutError) for e in receiver.errors))
        assert window.isVisible() and window.bridge.worker.is_alive
        assert not receiver.finished
        assert "worker still alive" in window.error.text()
        count = len(ticks)
        until(lambda: len(ticks) > count + 2)
        release.set()
        until(lambda: receiver.finished)
        assert not window.isVisible() and not window.bridge.worker.is_alive
        assert window.summary.text() == "No result"  # Late result cannot resurrect UI.
        assert set(ticks + receiver.thread_ids) == {get_ident()}
        assert [name for name, _ in c.trace].count("close") == 1
        window.close()  # Already stopped, repeated close is harmless.
    finally:
        timer.stop()


def test_state_queue_rejection_and_pending_cancel(
    qt_case: tuple[Controls, DemoWindow, Receiver],
) -> None:
    c, window, receiver = qt_case
    bridge = window.bridge
    bridge.submit(c.image)
    assert isinstance(receiver.errors[-1], WorkerStateError)
    ready(window, receiver)
    c.infer_release.clear()
    bridge.submit(c.image)
    until(c.infer_entered.is_set)
    bridge.submit(c.image)  # One pending request.
    bridge.submit(c.image)
    assert isinstance(receiver.errors[-1], WorkerQueueFullError)
    assert "queue is full" in window.error.text()
    window.close()
    bridge.submit(c.image)
    assert isinstance(receiver.errors[-1], WorkerStateError)
    c.infer_release.set()
    until(lambda: receiver.finished)
    assert [name for name, _ in c.trace].count("infer") == 1
    assert set(receiver.thread_ids) == {get_ident()}


def test_cleanup_failure_and_already_stopped_close(
    qt_case: tuple[Controls, DemoWindow, Receiver],
) -> None:
    c, window, receiver = qt_case
    c.close_error = RuntimeError("cleanup failed")
    ready(window, receiver)
    window.bridge.worker.request_stop()
    until(lambda: receiver.finished)
    assert c.close_error in receiver.errors
    assert "cleanup failed" in window.error.text()
    window.close()
    assert not window.isVisible()


def test_close_before_start(qt_case: tuple[Controls, DemoWindow, Receiver]) -> None:
    c, window, receiver = qt_case
    window.close()
    until(lambda: receiver.finished)
    assert not c.trace and not window.isVisible()


def test_window_run_disables_repeat_and_keeps_decoded_input(
    qt_case: tuple[Controls, DemoWindow, Receiver],
    tmp_path: Path,
) -> None:
    c, window, receiver = qt_case
    image_path = tmp_path / "测试.png"
    ok, encoded = cv2.imencode(".png", c.image)
    assert ok
    encoded.tofile(image_path)
    window.image_path.setText(str(image_path))
    ready(window, receiver)
    c.infer_release.clear()
    window.run_button.click()
    until(c.infer_entered.is_set)
    assert not window.run_button.isEnabled()
    window.run_button.click()
    c.infer_release.set()
    until(lambda: bool(receiver.results))
    assert len(c.images) == 1 and window.run_button.isEnabled()
    assert c.images[0].shape == c.image.shape


def test_application_quit_defers_until_worker_exits(
    qapp: QApplication,
    qt_case: tuple[Controls, DemoWindow, Receiver],
) -> None:
    c, window, receiver = qt_case
    assert isinstance(qapp, DemoApplication)
    qapp.window = window
    c.construct_release.clear()
    window.bridge.start()
    until(c.construct_entered.is_set)
    ticks: list[bool] = []
    pulse = QTimer()
    watchdog = QTimer()
    watchdog.setSingleShot(True)

    def tick() -> None:
        ticks.append(window.isVisible() and window.bridge.worker.is_alive)
        if len(ticks) == 3:
            c.construct_release.set()

    def abort() -> None:
        c.construct_release.set()
        qapp.exit(99)

    pulse.timeout.connect(tick)
    watchdog.timeout.connect(abort)
    window.bridge.shutdown_finished.connect(qapp.quit)
    pulse.start(10)
    watchdog.start(5000)
    QTimer.singleShot(0, qapp.quit)
    try:
        assert qapp.exec() == 0
        assert receiver.finished and not window.bridge.worker.is_alive
        assert len(ticks) >= 3 and all(ticks[:3])
        # A subsequent quit, with the worker already stopped, is harmless.
        QTimer.singleShot(0, qapp.quit)
        assert qapp.exec() == 0
    finally:
        pulse.stop()
        watchdog.stop()
        window.bridge.shutdown_finished.disconnect(qapp.quit)
        qapp.window = None


@pytest.mark.parametrize("detach_explicitly", [True, False])
def test_destroyed_bridge_discards_late_future(
    qapp: QApplication,
    detach_explicitly: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert qapp.thread() is not None
    c = Controls()
    worker = InferenceWorker(c.make_engine)
    bridge = QtInferenceBridge(worker)
    receiver = Receiver()
    bridge.startup_succeeded.connect(receiver.on_ready)
    bridge.result_ready.connect(receiver.on_result)
    destroyed = QSignalSpy(bridge.destroyed)
    try:
        bridge.start()
        until(lambda: receiver.ready)
        c.infer_release.clear()
        bridge.submit(c.image)
        until(c.infer_entered.is_set)
        if detach_explicitly:
            bridge.detach()
        bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed.count() == 1
        # Callbacks still hold the gate, whose sender may now be deleted.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        worker.request_stop()
        c.infer_release.set()
        until(lambda: not worker.is_alive)
        assert not receiver.results and worker.failure is None
        assert not [record for record in caplog.records if record.levelno >= 40]
    finally:
        c.infer_release.set()
        worker.request_stop()
        until(lambda: not worker.is_alive)
        assert worker.join(0)
        assert not any(t.name == worker.thread_name for t in threads())
