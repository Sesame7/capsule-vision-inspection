"""Normal and Demo entry lifecycle, with no vendor SDK or equipment."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_probe(tmp_path: Path, *arguments: str) -> None:
    root = Path(__file__).resolve().parents[2]
    env = dict(
        os.environ,
        QT_QPA_PLATFORM="offscreen",
        PYTHONPATH=os.pathsep.join(str(root / p) for p in ("", "src", "scripts")),
    )
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)


@pytest.mark.parametrize("entry", ["normal", "demo"])
@pytest.mark.parametrize(
    "stage", ["qapplication", "window", "event_loop", "active_metadata"]
)
def test_launch_failure_joins_owners_created_before_qt(
    tmp_path: Path, entry: str, stage: str
) -> None:
    # The launcher owns one QApplication per process. A child access violation
    # fails this case with its original exit status and faulthandler stack.
    run_probe(tmp_path, "-m", "tests.support.launch_failure_probe", entry, stage)


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
        page.buttons['save'].click()
        until(lambda: not window.editing.busy)
        window.navigate(0)
        window.production_recipes.select(window.production_recipes.recipes.findData(page.session.entry.recipe.recipe_id))
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
    run_probe(tmp_path, "-c", probe)
