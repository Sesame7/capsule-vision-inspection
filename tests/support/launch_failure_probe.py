"""One failed launch with a fresh QApplication and synthetic data."""

import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from threading import enumerate as threads

import numpy as np
import pytest
import run_demo as demo
from numpy.typing import NDArray
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from capsule_vision.application import ApplicationState
from capsule_vision.application.composition import EngineeringComposition
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.startup import ApplicationData, Startup, load_startup
from capsule_vision.diagnostics import RuntimeDiagnostics
from capsule_vision.localization.languages import Language
from capsule_vision.ui import bootstrap, recipe_page
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.recipe_page import ModelLabelReader
from tests.support.application import WAIT
from tests.support.qt import close_window, until
from tests.support.startup import saved_data


def exercise_startup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    stage: str,
) -> None:
    original_threads = {t.ident for t in threads()}
    application: QApplication | None = None
    owners: list[Startup | EngineeringComposition] = []
    windows: list[MainWindow] = []
    monkeypatch.chdir(tmp_path)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"Injected {stage} failure")

    def qt(arguments: Sequence[str]) -> QApplication:
        nonlocal application
        if stage == "qapplication":
            failed()
        application = QApplication(arguments)
        application.setQuitOnLastWindowClosed(False)
        monkeypatch.setattr(application, "exec", failed_loop)
        return application

    if entry == "normal":
        saved_data(tmp_path)
        normal_window = bootstrap.create_window

        def load(
            data: ApplicationData,
            *,
            observe_source: Callable[[Future[None]], None],
            diagnostics: RuntimeDiagnostics | None = None,
        ) -> Startup:
            startup = load_startup(
                data, observe_source=observe_source, diagnostics=diagnostics
            )
            owners.append(startup)
            return startup

        def make_window(startup: Startup, delivery: FutureDelivery) -> MainWindow:
            if stage == "window":
                failed()
            window = normal_window(startup, delivery)
            windows.append(window)
            return window

        monkeypatch.setattr(bootstrap, "load_startup", load)
        monkeypatch.setattr(bootstrap, "create_window", make_window)
        monkeypatch.setattr(bootstrap, "QApplication", qt)
    else:
        real_compose = demo.compose

        def build(
            images: tuple[NDArray[np.uint8], ...],
            folder: Path,
            scenario: demo.Scenario,
            delay: float,
            *,
            observe_source: Callable[[Future[None]], None],
        ) -> EngineeringComposition:
            result = real_compose(
                images, folder, scenario, delay, observe_source=observe_source
            )
            owners.append(result)
            return result

        monkeypatch.setattr(demo, "compose", build)

        def demo_window(
            bridge: QtProductionBridge,
            *,
            language: Language,
            recipe_editor: RecipeEditorSession,
            label_reader: ModelLabelReader,
        ) -> MainWindow:
            if stage == "window":
                failed()
            window = MainWindow(
                bridge,
                language=language,
                recipe_editor=recipe_editor,
                label_reader=label_reader,
            )
            windows.append(window)
            return window

        monkeypatch.setattr(demo, "MainWindow", demo_window)
        monkeypatch.setattr(demo, "QApplication", qt)
    reading, release = Event(), Event()

    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        reading.set()
        assert release.wait(WAIT)
        return ("demo-defect",)

    def failed_loop() -> None:
        if stage == "active_metadata":
            (window,) = windows
            window.navigate(1)
            until(reading.is_set)
            QTimer.singleShot(0, release.set)
        failed()

    if stage == "active_metadata":
        monkeypatch.setattr(recipe_page, "read_model_labels", metadata)
        monkeypatch.setattr(demo, "demo_model_labels", metadata)
    try:
        with pytest.raises(RuntimeError, match=f"Injected {stage} failure"):
            if entry == "normal":
                bootstrap.main(tmp_path, [])
            else:
                demo.main([])
        assert len(owners) == 1
        assert owners[0].application.snapshot().state is ApplicationState.CLOSED
        assert owners[0].application.join(0)
        assert not {t.ident for t in threads()} - original_threads
    finally:
        release.set()
        for window in windows:
            close_window(window)
    assert not {t.ident for t in threads()} - original_threads


if __name__ == "__main__":
    entry, stage = sys.argv[1:]
    with pytest.MonkeyPatch.context() as patch:
        exercise_startup_failure(Path.cwd(), patch, entry, stage)
