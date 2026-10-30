"""Production composition works in fresh processes without a Qt application."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("reverse", [False, True])
def test_core_imports_do_not_load_qt_or_create_owners(
    tmp_path: Path, reverse: bool
) -> None:
    probe = """
import importlib, pathlib, sys, threading
modules = [
    'capsule_vision.application.production_session',
    'capsule_vision.application.production_application',
    'capsule_vision.application.composition',
    'capsule_vision.application.startup',
]
if sys.argv[1] == 'True':
    modules.reverse()
before = {t.ident for t in threading.enumerate()}
files = tuple(pathlib.Path.cwd().iterdir())
for module in modules:
    importlib.import_module(module)
assert not any(n.startswith(('PySide6', 'capsule_vision.ui')) for n in sys.modules)
assert {t.ident for t in threading.enumerate()} == before
assert tuple(pathlib.Path.cwd().iterdir()) == files
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(reverse)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_startup_and_close_work_without_qt(tmp_path: Path) -> None:
    probe = """
import pathlib, sys, threading
from capsule_vision.application import ApplicationState
from capsule_vision.application.startup import ApplicationData, load_startup
before = {t.ident for t in threading.enumerate()}
def forbidden(_prepared):
    raise AssertionError('Startup must not construct an engine')
startup = load_startup(ApplicationData.at_entry(pathlib.Path.cwd()), engine_factory=forbidden)
try:
    assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
    assert startup.prepared is None
    assert startup.editor is not None
    assert startup.editor.machine_editor.camera_ids == tuple(f'CAM{i}' for i in range(1, 7))
    assert not startup.editor.machine_editor.complete
    assert not any(n.startswith(('PySide6', 'capsule_vision.ui')) for n in sys.modules)
finally:
    startup.application.close().result(5)
    assert startup.application.join(5)
assert {t.ident for t in threading.enumerate()} == before
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_composition_runs_and_joins_without_qt(tmp_path: Path) -> None:
    probe = """
import pathlib, sys, threading
from tests.support.application import ControlledEngine
from tests.support.source_inputs import input_files
from capsule_vision.application import ApplicationState
from capsule_vision.application.composition import compose
before = {t.ident for t in threading.enumerate()}
engine = ControlledEngine()
sources = []
composition = compose(*input_files(pathlib.Path(sys.argv[1])),
    engine_factory=lambda _: engine, observe_source=sources.append)
app = composition.application
try:
    assert not engine.calls and not sources
    assert app.snapshot().state is ApplicationState.READY
    assert composition.prepared.configuration.layout.camera_ids == tuple(f'CAM{i}' for i in range(1, 7))
    started = app.start()
    assert engine.warm.wait(5)
    assert app.snapshot().state is ApplicationState.STARTING
    assert app.snapshot().counters.cycles_completed == 0
    engine.release_warm.set()
    started.result(5)
    assert engine.active.wait(5)
    stopped = app.stop()
    assert not app.join(0)
    engine.release_active.set()
    stopped.result(5)
    assert app.snapshot().state is ApplicationState.READY
    assert len(sources) == 1
    sources[0].result(5)
    assert engine.closed
    assert not any(n.startswith(('PySide6', 'capsule_vision.ui')) for n in sys.modules)
finally:
    engine.release_warm.set()
    engine.release_active.set()
    app.close().result(5)
    assert app.join(5)
assert {t.ident for t in threading.enumerate()} == before
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
