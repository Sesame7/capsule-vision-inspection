"""Explicit Recipe test screens and fake-demo capture lifecycle."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget
from run_production_ui import DemoComposition

from tests.application_support import FakeSession
from tests.qt_support import ensure_test_fonts, until
from tests.recipe_support import editor
from vimo_demo.application import (
    PreparedConfiguration,
    ProductionApplication,
)
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_style import Language
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_editor import RecipeEditorSession


@pytest.fixture
def configured_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]]]:
    ensure_test_fonts()
    session = editor(tmp_path)
    assert session.entry is not None
    ready = session.prepare(session.entry.recipe)
    runtimes: list[FakeSession] = []

    def factory(_p: PreparedConfiguration, _g: int) -> FakeSession:
        runtime = FakeSession(automatic_start=False)
        runtimes.append(runtime)
        return runtime

    app = ProductionApplication(factory)
    app.configure(ready)
    bridge = QtProductionBridge(
        app, LatestDisplayFrameStore(ready.configuration.layout.camera_ids), poll_ms=10
    )
    window = ProductionWindow(
        bridge, (RecipeChoice(ready, "A"),), recipe_editor=session
    )
    window.resize(1080, 1920)
    window.show()
    window.navigate(1)
    yield window, session, runtimes
    for runtime in runtimes:
        if not runtime.ready.done():
            runtime.ready.set_result(None)
    session.discard()
    window.close()
    until(lambda: bridge.closed)
    assert app.join(0)
    window.deleteLater()


def edit(line: QLineEdit, value: str) -> None:
    line.setText(value)
    line.textEdited.emit(value)


type Screen = tuple[ProductionWindow, RecipeEditorSession, list[FakeSession]]


def assert_inside(widget: QWidget, parent: QWidget) -> None:
    assert parent.rect().contains(widget.mapTo(parent, QPoint(0, 0)))
    assert parent.rect().contains(
        widget.mapTo(parent, QPoint(widget.width() - 1, widget.height() - 1))
    )


@contextmanager
def demo_recipe_window(
    composition: DemoComposition, *, language: Language, size: tuple[int, int]
) -> Generator[ProductionWindow]:
    """Present a real fake-demo composition and wait for its actual cleanup."""
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=5)
    window = ProductionWindow(
        bridge,
        composition.choices,
        language=language,
        simulation=True,
        recipe_editor=composition.editor,
    )
    window.resize(*size)
    window.show()
    try:
        yield window
    finally:
        composition.editor.discard()
        window.close()
        until(lambda: bridge.closed)
        composition.source.close()
        assert composition.application.join(0)
        window.deleteLater()
