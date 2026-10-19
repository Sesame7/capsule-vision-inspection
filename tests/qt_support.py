"""Small Qt test facilities, without pytest-qt or sleeps."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QFontDatabase

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from tests.application_support import FakeSession
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


@pytest.fixture
def screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[ProductionWindow, FakeSession]]:
    # Keep application/widget imports local for low-level Qt helper consumers.
    from dataclasses import replace

    from tests.application_support import FakeSession, prepared
    from vimo_demo.application import ProductionApplication
    from vimo_demo.ui.display_frames import LatestDisplayFrameStore
    from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
    from vimo_demo.ui.production_window import ProductionWindow

    # Windows' offscreen plugin exposes no system fonts in this environment.
    # Read existing OS fonts for realistic metrics/Chinese glyphs; install nothing.
    ensure_test_fonts()
    candidate = prepared(tmp_path)
    session = FakeSession(automatic_start=False)
    app = ProductionApplication(lambda _p, _g: session)
    app.configure(candidate)
    store = LatestDisplayFrameStore(candidate.configuration.layout.camera_ids)
    bridge = QtProductionBridge(app, store, poll_ms=10, close_timeout_ms=1000)
    choices = (
        RecipeChoice(candidate, "Recipe A"),
        RecipeChoice(
            replace(
                candidate, configuration=replace(candidate.configuration, recipe_id="B")
            ),
            "Recipe B",
        ),
    )
    window = ProductionWindow(bridge, choices)
    window.resize(1080, 1920)
    window.show()
    yield window, session
    if not session.ready.done():
        session.ready.set_result(None)
    if session.join_gate is not None:
        session.join_gate.set()
    session.cleanup_safe = True
    window.close()
    until(lambda: window.bridge.closed)
    assert app.join(0)
    window.deleteLater()
