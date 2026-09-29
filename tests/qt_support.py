"""Small Qt test facilities, without pytest-qt or sleeps."""

from collections.abc import Callable
from threading import get_ident

from PySide6.QtCore import QEventLoop, QObject, QTimer, Slot

from vimo_demo.inference import ImageResult


def until(predicate: Callable[[], bool], timeout_ms: int = 5000) -> None:
    """Pump Qt while boundedly waiting; polling is a test assertion aid only."""
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def check() -> None:
        if predicate():
            loop.quit()

    poll.timeout.connect(check)
    poll.start(5)
    deadline.start(timeout_ms)
    loop.exec()
    poll.stop()
    deadline.stop()
    assert predicate(), "Qt condition timed out"


class Receiver(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.ready = False
        self.finished = False
        self.results: list[ImageResult] = []
        self.errors: list[BaseException] = []
        self.thread_ids: list[int] = []

    @Slot()
    def on_ready(self) -> None:
        self.thread_ids.append(get_ident())
        self.ready = True

    @Slot(object)
    def on_result(self, result: object) -> None:
        self.thread_ids.append(get_ident())
        assert isinstance(result, ImageResult)
        self.results.append(result)

    @Slot(object)
    def on_error(self, error: object) -> None:
        self.thread_ids.append(get_ident())
        assert isinstance(error, BaseException)
        self.errors.append(error)

    @Slot()
    def on_finished(self) -> None:
        self.thread_ids.append(get_ident())
        self.finished = True
