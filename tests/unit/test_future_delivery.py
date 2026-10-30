"""Queued completion and detached-sender safety used by the Production shell."""

from concurrent.futures import Future
from threading import Thread, get_ident

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Slot
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from tests.support.qt import until

from capsule_vision.ui.future_delivery import FutureDelivery


@pytest.mark.parametrize("fail", [False, True])
def test_background_completion_delivers_original_value_or_error_on_gui_thread(
    qapp: QApplication, fail: bool
) -> None:
    observed: list[tuple[str, object, object, int]] = []

    class Receiver(QObject):
        @Slot(str, object, object)
        def completed(self, kind: str, value: object, error: object) -> None:
            observed.append((kind, value, error, get_ident()))

    assert qapp.thread() is not None
    receiver = Receiver()
    delivery = FutureDelivery()
    delivery.emitter.completed.connect(
        receiver.completed, Qt.ConnectionType.QueuedConnection
    )
    future: Future[object] = Future()
    value, error = object(), ValueError("original failure")
    delivery.watch("inspection", future)

    def complete() -> None:
        assert get_ident() != caller
        if fail:
            future.set_exception(error)
        else:
            future.set_result(value)

    caller = get_ident()
    producer = Thread(target=complete)
    try:
        producer.start()
        producer.join(2)
        assert not producer.is_alive()
        until(lambda: len(observed) == 1)
        assert observed == [
            ("inspection", None if fail else value, error if fail else None, caller)
        ]
    finally:
        delivery.detach()


@pytest.mark.parametrize("detach_before_watch", [False, True])
def test_late_completion_after_sender_deletion_is_discarded(
    qapp: QApplication, detach_before_watch: bool, caplog: pytest.LogCaptureFixture
) -> None:
    assert qapp.thread() is not None
    delivery = FutureDelivery()
    destroyed = QSignalSpy(delivery.emitter.destroyed)
    future: Future[int] = Future()
    if detach_before_watch:
        delivery.detach()
    delivery.watch("late", future)
    delivery.detach()
    delivery.detach()  # Idempotent even after emission is disabled.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed.count() == 1
    producer = Thread(target=lambda: future.set_result(7))
    producer.start()
    producer.join(2)
    assert not producer.is_alive() and future.result(timeout=0) == 7
    assert not [record for record in caplog.records if record.levelno >= 40]
