"""Explicit Recipe test screens and interaction helpers."""

from collections.abc import Iterator
from concurrent.futures import Future
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from capsule_vision.application import (
    PreparedConfiguration,
    ProductionApplication,
)
from capsule_vision.application.composition import compose
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.configuration import save_camera_sources
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.vision_page import VisionPage
from tests.support.application import ControlledEngine, FakeSession
from tests.support.qt import ensure_test_fonts, until
from tests.support.recipe import editor
from tests.support.source_inputs import folders, input_files


@pytest.fixture
def configured_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[MainWindow, RecipeEditorSession, list[FakeSession]]]:
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
    window = MainWindow(bridge, recipe_editor=session)
    window.resize(1080, 1920)
    window.show()
    window.navigate(1)
    until(
        lambda: (
            bool(window.recipe_page.label_checks)
            and not window.recipe_page.labels_loading
        )
    )
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


type Screen = tuple[MainWindow, RecipeEditorSession, list[FakeSession]]


def assert_inside(widget: QWidget, parent: QWidget) -> None:
    assert parent.rect().contains(widget.mapTo(parent, QPoint(0, 0)))
    assert parent.rect().contains(
        widget.mapTo(parent, QPoint(widget.width() - 1, widget.height() - 1))
    )


def folder_composition(
    base: Path,
    engine: ControlledEngine,
    *,
    loop: bool = True,
    observe: list[Future[None]] | None = None,
):
    model, machine, recipe, _ = input_files(base)
    sources = folders(base, loop=loop)
    save_camera_sources(base / "camera_sources.json", sources)
    return compose(
        model,
        machine,
        recipe,
        engine_factory=lambda _: engine,
        observe_source=observe.append if observe is not None else lambda _: None,
    )


@pytest.fixture
def camera_screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[MainWindow, VisionPage, ControlledEngine]]:
    ensure_test_fonts()
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = folder_composition(tmp_path, engine)
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=5)
    window = MainWindow(
        bridge,
        recipe_editor=composition.editor,
    )
    window.resize(1080, 1920)
    window.show()
    window.navigate(4)
    workspace = window.vision_workspace
    assert workspace is not None
    until(lambda: not window.recipe_page.busy)
    yield window, workspace, engine
    composition.editor.machine_editor.discard()
    assert composition.editor.camera_sources is not None
    composition.editor.camera_sources.discard()
    window.close()
    until(lambda: bridge.closed)
    assert composition.application.join(0)
    window.deleteLater()


def select_production_recipe(window: MainWindow) -> None:
    session = window.editing.session
    assert session is not None and session.entry is not None
    window.production_recipes.reload()
    window.production_recipes.select(
        window.production_recipes.recipes.findData(session.entry.recipe.recipe_id)
    )
