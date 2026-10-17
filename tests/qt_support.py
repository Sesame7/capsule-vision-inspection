"""Small Qt test facilities, without pytest-qt or sleeps."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase

if TYPE_CHECKING:
    from vimo_demo.ui.production_window import ProductionWindow


def ensure_test_fonts() -> None:
    """Load existing Windows fonts when offscreen Qt exposes no font families."""
    if not QFontDatabase.families():
        for filename in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / filename
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))


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


def close_window(window: ProductionWindow) -> None:
    window.close()
    until(lambda: window.bridge.closed and window.communication.closed)
    assert window.bridge.application.join(1)
    window.deleteLater()
