"""Real demo wiring and launch failures, with no vendor SDK or equipment."""

import hashlib
import os
import subprocess
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
from tests.support.application import WAIT
from tests.support.qt import close_window
from tests.support.source_inputs import input_files
from tests.support.startup import saved_data

from capsule_vision.application import ApplicationState
from capsule_vision.application.composition import EngineeringComposition, compose
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.startup import ApplicationData, Startup, load_startup
from capsule_vision.ui import bootstrap, recipe_page
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow


@pytest.mark.parametrize("entry", ["normal", "demo"])
@pytest.mark.parametrize(
    "stage", ["qapplication", "window", "event_loop", "active_metadata"]
)
def test_launch_failure_joins_owners_created_before_qt(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    stage: str,
) -> None:
    original_threads = {t.ident for t in threads()}
    owners: list[Startup | EngineeringComposition] = []
    monkeypatch.chdir(tmp_path)

    def failed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"Injected {stage} failure")

    def qt(_arguments: Sequence[str]) -> QApplication:
        if stage == "qapplication":
            failed()
        return qapp

    if entry == "normal":
        saved_data(tmp_path)
        real_load, normal_window = load_startup, bootstrap.create_window

        def load(
            data: ApplicationData,
            *,
            observe_source: Callable[[Future[None]], None],
        ) -> Startup:
            startup = real_load(data, observe_source=observe_source)
            owners.append(startup)
            return startup

        def make_window(startup: Startup, delivery: FutureDelivery) -> MainWindow:
            if stage == "window":
                failed()
            result = normal_window(startup, delivery)
            return result

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
        if stage == "window":
            monkeypatch.setattr(demo, "MainWindow", failed)
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
            from tests.support.qt import until

            (window,) = [
                w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)
            ]
            window.navigate(1)
            until(reading.is_set)
            QTimer.singleShot(0, release.set)
        failed()

    if stage == "active_metadata":
        monkeypatch.setattr(recipe_page, "read_model_labels", metadata)
        monkeypatch.setattr(demo, "demo_model_labels", metadata)
    monkeypatch.setattr(qapp, "exec", failed_loop)
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
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, MainWindow):
                close_window(widget)
    assert not {t.ident for t in threads()} - original_threads


def test_explicit_composition_failure_releases_created_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners: list[SourceProductionApplication] = []

    def fail(self: SourceProductionApplication, _candidate: object) -> None:
        owners.append(self)
        raise RuntimeError("Injected configuration failure")

    monkeypatch.setattr(SourceProductionApplication, "configure", fail)
    with pytest.raises(RuntimeError, match="Injected configuration failure"):
        compose(*input_files(tmp_path))
    assert len(owners) == 1 and owners[0].join(0)
    assert owners[0].snapshot().state is ApplicationState.CLOSED


def test_selected_demo_images_are_frozen_and_originals_unchanged(
    tmp_path: Path,
) -> None:
    import cv2

    external = tmp_path / "originals"
    external.mkdir()
    for index in range(6):
        ok, image = cv2.imencode(
            ".png", np.full((20, 60, 3), index + 1, dtype=np.uint8)
        )
        assert ok
        image.tofile(external / f"{index}.png")
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in external.iterdir()}
    images = demo.demo_images(external)
    composition = demo.compose(images, tmp_path / "demo", "normal", 0)
    try:
        assert composition.editor.cli_sources
        frames = composition.store.snapshot().frames
        assert tuple(f.frame_id.camera_id for f in frames) == tuple(
            f"CAM{i}" for i in range(1, 7)
        )
        for index, frame in enumerate(frames):
            assert frame.generation == 0 and not frame.image.flags.writeable
            assert np.all(frame.image == index + 1)
        images[0][:] = 99
        assert np.all(frames[0].image == 1)
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
    finally:
        composition.application.close().result(WAIT)
        assert composition.application.join(WAIT)


def test_standalone_demo_metadata_preparation_and_inference_never_use_sdk(
    tmp_path: Path,
) -> None:
    probe = """
import hashlib, pathlib, threading
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import run_demo as demo
from tests.support.qt import until
from capsule_vision.application import ApplicationState, ModelPreparationState
from capsule_vision.inference import VimoSegmentationEngine
from capsule_vision.ui import recipe_page
from capsule_vision.ui.main_window import MainWindow

before = {t.ident for t in threading.enumerate()}
def forbidden(*args, **kwargs):
    raise AssertionError('Real SDK boundary reached')

def drive(*_ignored):
    window, = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]
    app, page = window.bridge.application, window.recipe_page
    try:
        assert 'SIMULATED INPUT' in window.windowTitle()
        assert len(window.bridge.frames.snapshot().frames) == 6
        assert page.session is not None and page.session.cli_sources
        assert len(page.session.catalog.discover().entries) == 2
        assert len(page.session.registry.models) == 2
        protected = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in pathlib.Path('outputs/production_ui_demo').rglob('model.*')}
        window.navigate(1)
        until(lambda: not page.labels_loading and bool(page.label_checks))
        assert tuple(page.label_checks) == ('demo-defect',)
        page.buttons['cache_prepare'].click()
        until(lambda: app.preparation_snapshot().state is ModelPreparationState.SUCCEEDED)
        assert not app.preparation_snapshot().owns_resources
        assert app.preparation_snapshot().result.cleanup_complete
        alternate = 1 + next(i for i, m in enumerate(page.session.registry.models)
                             if m.model_id == 'synthetic-alt')
        page.model.setCurrentIndex(alternate)
        page.model.activated.emit(alternate)
        until(lambda: not page.labels_loading and bool(page.label_checks))
        page.buttons['save_apply'].click()
        until(lambda: not window.editing.busy)
        assert not page.session.dirty
        assert app.snapshot().candidate.model_id == 'synthetic-alt'
        window.navigate(0)
        window.bridge.start()
        until(lambda: app.snapshot().counters.cycles_completed > 0)
        result = app.snapshot().latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.cycle_id > 0
        window.bridge.stop()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected} == protected
    finally:
        page.session.discard()
        window.close()
        until(lambda: window.bridge.closed)
        assert app.join(0)
        window.deleteLater()
    return 0

with patch.object(VimoSegmentationEngine, '__init__', forbidden), \
     patch.object(VimoSegmentationEngine, 'from_managed_model', forbidden), \
     patch.object(recipe_page, 'read_model_labels', forbidden), \
     patch.object(QApplication, 'exec', drive):
    assert demo.main(['--language', 'en', '--size', '1080x970']) == 0
assert not {t.ident for t in threading.enumerate()} - before
print('Demo SDK isolation, metadata, alternate Model Apply, cache cleanup and inference passed')
"""
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = os.pathsep.join(str(root / p) for p in ("", "src", "scripts"))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
