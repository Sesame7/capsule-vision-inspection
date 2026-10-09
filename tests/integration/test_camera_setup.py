"""Unified Settings and real-local Folder lifecycle, with fake inference."""

from collections.abc import Iterator
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, QSize
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox
from run_local_production_ui import SourceNotifications, compose
from run_production_ui import demo_images
from tests.integration.test_local_production_ui import ControlledEngine, input_files
from tests.qt_support import until
from tests.unit.test_folder_replay import folders, write_image
from tests.unit.test_production_application import WAIT

from vimo_demo.application import ApplicationState, ProductionCounters
from vimo_demo.application.folder_replay import FolderSourceError, decode_image
from vimo_demo.configuration import (
    ConfigurationIOError,
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import Language, Style, camera_icon, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.roi_editor import MachineRoiWorkspace


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
) -> Iterator[tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine]]:
    if not QFontDatabase.families():
        for font in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / font
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = folder_composition(tmp_path, engine)
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=5)
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.resize(1080, 1920)
    window.show()
    window.navigate(4)
    workspace = window.parameters.roi_workspace
    assert workspace is not None
    until(lambda: not window.parameters.busy)
    yield window, workspace, engine
    composition.editor.machine_editor.discard()
    assert composition.editor.camera_sources is not None
    composition.editor.camera_sources.discard()
    window.close()
    until(lambda: bridge.closed)
    assert composition.application.join(0)
    window.deleteLater()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_unified_layout_camera_reference_and_frozen_refresh(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    language: Language,
    size: tuple[int, int],
    tmp_path: Path,
) -> None:
    window, workspace, engine = camera_screen
    window.resize(*size)
    window.set_language(language)
    QApplication.processEvents()
    assert (
        workspace.isVisible() and workspace.parentWidget() is window.parameters.settings
    )
    assert workspace.reference_modes["folder"].accessibleName() == text(
        "reference_folder", language
    )
    assert workspace.canvas.height() > workspace.height() * 0.65
    assert window.grab().save(
        str(tmp_path / f"settings-{language}-{size[0]}x{size[1]}.png")
    )
    assert workspace.canvas.width() >= 600
    for control in (
        workspace.folder,
        workspace.browse,
        workspace.loop,
        *workspace.buttons.values(),
    ):
        assert workspace.rect().contains(control.mapTo(workspace, QPoint(0, 0)))
    assert [workspace.camera_tabs.tabText(i) for i in range(6)] == [
        str(i) for i in range(1, 7)
    ]
    for index in range(6):
        name = text("camera_number", language).format(number=index + 1)
        assert (
            workspace.camera_tabs.tabIcon(index)
            .pixmap(Style.icon, Style.icon)
            .toImage()
            == camera_icon().pixmap(Style.icon, Style.icon).toImage()
        )
        assert workspace.camera_tabs.tabToolTip(index) == name
        assert workspace.camera_tabs.accessibleTabName(index) == name
        assert workspace.camera_tabs.tabRect(index).width() >= Style.touch
        assert workspace.camera_tabs.tabRect(index).height() >= Style.touch
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 1
    workspace.camera_tabs.setCurrentIndex(2)
    until(lambda: not window.parameters.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 21
    old = workspace.frame
    source = workspace.sources
    assert source is not None
    key = source.definition.camera(workspace.camera).key(source.path.parent)
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(1), workspace.camera, 1),
            1,
            np.full((20, 60, 3), 88, dtype=np.uint8),
        ),
        generation=1,
        source_key=key,
    )
    window.bridge.refresh()
    QApplication.processEvents()
    assert workspace.frame is old
    workspace.reference_modes["runtime"].click()
    assert workspace.frame is window.bridge.frames.snapshot().frames[0]
    assert int(workspace.frame.image[0, 0, 0]) == 88
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )
    assert not source.dirty and not workspace.draft.dirty
    assert not workspace.frame.image.flags.writeable


def test_source_change_invalidates_reference_no_geometry_repair(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    tmp_path: Path,
) -> None:
    window, workspace, _ = camera_screen
    sources = workspace.sources
    assert sources is not None
    saved = workspace.draft.definition
    smaller = tmp_path / "new-reference"
    smaller.mkdir()
    write_image(smaller / "unrelated.png", 77, (8, 15))
    workspace.folder.setText(str(smaller))
    workspace.source_edited(str(smaller))
    assert workspace.frame is None and workspace.camera not in workspace.draft.bounds
    assert sources.dirty and not window.buttons["operation"].isEnabled()
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    assert workspace.frame is not None and workspace.frame.image.shape == (8, 15, 3)
    assert workspace.draft.definition == saved and workspace.message.text()
    workspace.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert (
        sources.dirty
        and not sources.path.read_text(encoding="utf-8").find("new-reference") >= 0
    )
    assert (
        load_machine_layout(workspace.draft.path) == saved
        if workspace.draft.path is not None
        else False
    )
    assert "CAM1" in workspace.message.text()


@pytest.mark.parametrize("bad", ["empty", "corrupt"])
def test_reference_error_no_substitution(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    tmp_path: Path,
    bad: str,
) -> None:
    window, workspace, _ = camera_screen
    directory = tmp_path / bad
    directory.mkdir()
    if bad == "corrupt":
        (directory / "first.png").write_bytes(b"broken")
        write_image(directory / "second.png", 1)
    workspace.folder.setText(str(directory))
    workspace.source_edited(str(directory))
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    assert workspace.frame is None and workspace.message.text()
    assert not workspace.editable() and not workspace.buttons["roi_common"].isEnabled()
    assert not window.bridge.application.snapshot().counters.cycles_started


def test_save_reload_partial_failure_and_running_guards(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    other = tmp_path / "replacement"
    other.mkdir()
    write_image(other / "1.png", 99)
    workspace.folder.setText(str(other))
    workspace.source_edited(str(other))
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    workspace.loop.setChecked(False)
    candidate = window.bridge.application.snapshot().candidate
    original = source.save

    def fail() -> None:
        raise ConfigurationIOError("test source write failure")

    monkeypatch.setattr(source, "save", fail)
    workspace.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert source.dirty and not workspace.draft.dirty
    assert text("source_partial_save", window.language) in workspace.message.text()
    assert window.bridge.application.snapshot().candidate == candidate
    monkeypatch.setattr(source, "save", original)
    workspace.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert not source.dirty and load_camera_sources(source.path) == source.definition
    assert not workspace.session.dirty
    # Loop OFF only one replacement image: block its active call to inspect RUNNING.
    engine.release_active.clear()
    window.buttons["operation"].click()
    until(engine.active.is_set)
    before = source.definition
    assert window.bridge.application.snapshot().state is ApplicationState.RUNNING
    assert not workspace.folder.isEnabled() and not workspace.browse.isEnabled()
    assert not workspace.loop.isEnabled() and not workspace.editable()
    workspace.source_edited("forbidden")
    workspace.loop_changed(True)
    workspace.save_and_apply()
    assert source.definition == before
    engine.release_active.set()
    until(lambda: window.bridge.application.snapshot().state is ApplicationState.READY)
    assert [v for v, _ in engine.calls][2:] == [99, 11, 21, 31, 41, 51]


@pytest.mark.parametrize("action", ["switch", "close"])
def test_reference_io_off_gui_stale_completion_and_close_wait(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:

    window, workspace, _ = camera_screen
    entered, release = Event(), Event()
    owner = get_ident()
    original = decode_image
    ids: list[int] = []

    def blocked(path: Path):
        ids.append(get_ident())
        entered.set()
        assert release.wait(WAIT)
        return original(path)

    monkeypatch.setattr("vimo_demo.ui.roi_editor.decode_image", blocked)
    workspace.refresh_frame()
    until(entered.is_set)
    assert not workspace.buttons["roi_refresh"].isEnabled()
    assert not workspace.buttons["reference_next"].isEnabled()
    assert not workspace.reference_modes["folder"].isEnabled()
    if action == "switch":
        workspace.camera_tabs.setCurrentIndex(4)
    else:
        window.close()
        assert not window.bridge.closed
    release.set()
    until(lambda: not window.parameters.busy)
    assert all(i != owner for i in ids)
    if action == "switch":
        assert (
            workspace.frame is not None
            and workspace.frame.frame_id.camera_id == CameraId("CAM5")
        )
        assert int(workspace.frame.image[0, 0, 0]) == 41
    else:
        until(lambda: window.bridge.closed)
        assert not any(t.name == "CameraSetupIO" for t in threads())


@pytest.mark.parametrize("decision", ["discard", "cancel"])
def test_source_dirty_navigation(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    decision: str,
) -> None:
    window, workspace, _ = camera_screen
    assert workspace.sources is not None
    workspace.loop.setChecked(False)
    assert workspace.sources.dirty
    window.navigate(0)
    box = workspace.findChildren(QMessageBox)[0]
    next(
        b for b in box.buttons() if b.text() == text(decision, window.language)
    ).click()
    QApplication.processEvents()
    if decision == "discard":
        assert window.parameters.roi_workspace is None and not workspace.sources.dirty
    else:
        assert (
            window.pages.currentWidget() is window.parameters.settings
            and workspace.sources.dirty
        )


def test_finite_replay_restart_snapshot_order_generation_and_cleanup(
    qapp: QApplication, tmp_path: Path
) -> None:
    before = {t.ident for t in threads()}
    engines: list[ControlledEngine] = []

    def factory(_config: object) -> ControlledEngine:
        engine = ControlledEngine()
        engine.release_warm.set()
        if engines:
            engine.release_active.set()
        engines.append(engine)
        return engine

    model, machine, recipe, _ = input_files(tmp_path)
    sources = folders(tmp_path, loop=False)
    sources_path = tmp_path / "camera_sources.json"
    save_camera_sources(sources_path, sources)
    composition = compose(model, machine, recipe, engine_factory=factory)
    app = composition.application
    try:
        app.start().result(WAIT)
        until(engines[0].active.is_set)
        replacement = tmp_path / "next-start"
        replacement.mkdir()
        write_image(replacement / "one.png", 90)
        write_image(replacement / "two.png", 91)
        updated = replace(
            sources,
            cameras=tuple(
                replace(c, folder=replacement.name)
                if c.camera_id == CameraId("CAM1")
                else c
                for c in sources.cameras
            ),
            target_fps=20,
        )
        save_camera_sources(sources_path, updated)
        engines[0].release_active.set()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert app.snapshot().counters.cycles_completed == 2
        assert [v for v, _ in engines[0].calls][2:] == [
            1,
            11,
            21,
            31,
            41,
            51,
            2,
            12,
            22,
            32,
            42,
            52,
        ]
        first_generation = app.snapshot().generation
        app.start().result(WAIT)
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert app.snapshot().counters.cycles_completed == 4
        assert app.snapshot().generation > first_generation
        assert engines[1].calls[2][0] == 90
        snapshot = app.snapshot()
        assert all(
            f.generation == snapshot.generation and f.frame_id.cycle_id == 2
            for f in composition.store.snapshot().frames
        )
    finally:
        for engine in engines:
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
    assert all(engine.closed for engine in engines)
    assert not {t.ident for t in threads()} - before


def test_replay_bad_later_file_stops_without_partial_cycle(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    observed: list[Future[None]] = []
    composition = folder_composition(tmp_path, engine, observe=observed)
    (tmp_path / "independent-3" / "unrelated2.png").write_bytes(b"broken")
    app = composition.application
    delivery = FutureDelivery()
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    notifications = SourceNotifications(bridge, delivery)
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    errors: list[object] = []

    def failed(_name: str, error: object) -> None:
        errors.append(error)

    bridge.command_failed.connect(failed)
    try:
        # Observe the same source Future through the existing queued error presenter.
        app.start().result(WAIT)
        delivery.watch("folder_source", observed[0])
        until(lambda: bool(errors) and app.snapshot().state is ApplicationState.READY)
        assert isinstance(errors[0], FolderSourceError)
        assert (
            "CAM4" in window.event_label.text()
            and "unrelated2.png" in window.event_label.text()
        )
        assert (
            app.snapshot().counters.cycles_started
            == app.snapshot().counters.cycles_completed
            == 1
        )
        assert app.snapshot().counters.production_ng_cycles == 0
        assert len(engine.calls) == 8
    finally:
        window.close()
        until(lambda: bridge.closed)
        delivery.detach()
        window.deleteLater()
        del notifications


def test_open_without_sources_and_failed_start_without_engine(
    qapp: QApplication, tmp_path: Path
) -> None:
    model, machine, recipe, _ = input_files(tmp_path)
    engine = ControlledEngine()
    composition = compose(model, machine, recipe, engine_factory=lambda _: engine)
    app = composition.application
    try:
        assert not composition.store.snapshot().frames
        assert composition.editor.camera_sources is not None
        assert not any(
            c.folder for c in composition.editor.camera_sources.definition.cameras
        )
        startup = app.start()
        until(startup.done)
        assert startup.exception() is not None
        assert app.snapshot().state is ApplicationState.READY and not engine.calls
        assert app.snapshot().counters == ProductionCounters()
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("closing", [False, True])
def test_folder_stop_close_wait_active_native_call(
    qapp: QApplication, tmp_path: Path, closing: bool
) -> None:
    engine = ControlledEngine()
    engine.release_warm.set()
    composition = folder_composition(tmp_path, engine)
    app = composition.application
    before = {t.ident for t in threads()}
    try:
        app.start().result(WAIT)
        until(engine.active.is_set)
        stopped = app.close() if closing else app.stop()
        assert not stopped.done() and app.snapshot().runtime_owned
        engine.release_active.set()
        until(stopped.done)
        assert stopped.exception() is None
        assert app.snapshot().state is (
            ApplicationState.CLOSED if closing else ApplicationState.READY
        )
        assert engine.closed and not app.snapshot().runtime_owned
    finally:
        engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
    assert not {t.ident for t in threads()} - before


def test_explicit_cli_precedence_and_reference_provenance(
    qapp: QApplication, tmp_path: Path
) -> None:
    model, machine, recipe, paths = input_files(tmp_path)
    sources = folders(tmp_path, loop=False)
    save_camera_sources(tmp_path / "camera_sources.json", sources)
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = compose(
        model, machine, recipe, paths, engine_factory=lambda _: engine
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store)
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.show()
    window.navigate(4)
    workspace = window.parameters.roi_workspace
    assert workspace is not None
    try:
        until(lambda: not window.parameters.busy)
        assert workspace.source_notice.isVisible()
        assert (
            workspace.frame is not None
            and workspace.frame.source_key
            != composition.store.snapshot().frames[0].source_key
        )
        app.start().result(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed >= 1)
        app.stop().result(WAIT)
        assert [v for v, _ in engine.calls][2:8] == [1, 2, 3, 4, 5, 6]
        assert load_camera_sources(tmp_path / "camera_sources.json") == sources
    finally:
        window.close()
        until(lambda: bridge.closed)
        window.deleteLater()


def test_startup_source_error_visible_before_engine_creation(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine()
    composition = folder_composition(tmp_path, engine)
    for path in (tmp_path / "independent-4").iterdir():
        path.unlink()
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.show()
    try:
        window.buttons["operation"].click()
        until(lambda: "CAM5" in window.event_label.text())
        assert app.snapshot().state is ApplicationState.READY
        assert text("source_error", window.language) in window.event_label.text()
        assert not engine.calls and app.snapshot().counters == ProductionCounters()
    finally:
        window.close()
        until(lambda: bridge.closed)
        window.deleteLater()


def test_setup_save_waits_for_actual_prepare_and_reports_failure(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, _ = camera_screen
    source = workspace.sources
    assert source is not None
    workspace.loop.setChecked(False)
    entered, release = Event(), Event()
    candidate = window.bridge.application.snapshot().candidate

    def fail(_recipe: object) -> None:
        entered.set()
        assert release.wait(WAIT)
        raise ValueError("post-save preparation failed")

    monkeypatch.setattr(workspace.session, "prepare", fail)
    try:
        workspace.save_and_apply()
        until(entered.is_set)
        assert window.parameters.busy
        assert workspace.status.text() == text("setup_saving", window.language)
        assert workspace.message.text() != text("roi_applied", window.language)
        assert not workspace.buttons["roi_save_apply"].isEnabled()
    finally:
        release.set()
    until(lambda: not window.parameters.busy)
    assert not source.dirty and load_camera_sources(source.path) == source.definition
    assert "post-save preparation failed" in workspace.message.text()
    assert workspace.status.text() == text("setup_failed", window.language)
    assert window.bridge.application.snapshot().candidate == candidate


def test_folder_navigation_refresh_and_camera_positions(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    disk_before = source.path.read_bytes()
    geometry = workspace.draft.definition
    assert workspace.reference.text().startswith("1/2")
    assert not workspace.buttons["reference_previous"].isEnabled()
    workspace.buttons["reference_next"].click()
    until(lambda: not window.parameters.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 2
    assert not workspace.buttons["reference_next"].isEnabled()
    workspace.browse_reference(1)
    assert int(workspace.frame.image[0, 0, 0]) == 2  # Boundary never wraps.
    current = Path(workspace.reference.toolTip())
    write_image(current, 77)
    write_image(current.parent / "0.png", 66)
    workspace.buttons["roi_refresh"].click()
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 77
    assert Path(workspace.reference.toolTip()) == current
    assert workspace.reference.text().startswith("3/3")
    workspace.camera_tabs.setCurrentIndex(1)
    until(lambda: not window.parameters.busy)
    workspace.buttons["reference_next"].click()
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 12
    workspace.camera_tabs.setCurrentIndex(0)
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 77
    workspace.buttons["reference_previous"].click()
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 1
    workspace.camera_tabs.setCurrentIndex(1)
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 12
    assert source.path.read_bytes() == disk_before and not source.dirty
    assert workspace.draft.definition == geometry and not engine.calls
    assert not window.bridge.frames.snapshot().frames
    assert window.bridge.application.snapshot().counters == ProductionCounters()
    # The viewed position is workspace state, not persisted configuration.
    window.navigate(0)
    window.navigate(4)
    reopened = window.parameters.roi_workspace
    assert reopened is not None and reopened is not workspace
    until(lambda: not window.parameters.busy)
    assert reopened.reference.text().startswith("1/")


def test_runtime_reference_eligibility_frozen_refresh_and_folder_restore(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    key = source.definition.camera(workspace.camera).key(source.path.parent)

    def publish(sequence: int, value: int, source_key: str) -> None:
        window.bridge.frames.publish(
            Frame(
                FrameId(CycleId(sequence), workspace.camera, sequence),
                sequence,
                np.full((20, 60, 3), value, dtype=np.uint8),
            ),
            generation=1,
            source_key=source_key,
        )
        window.bridge.refresh()
        QApplication.processEvents()

    publish(1, 70, "other-source")
    assert not workspace.reference_modes["runtime"].isEnabled()
    workspace.set_reference_mode("runtime")
    assert workspace.reference_mode == "folder"
    workspace.buttons["reference_next"].click()
    until(lambda: not window.parameters.busy)
    publish(2, 80, key)
    assert workspace.reference_mode == "folder"  # No unsolicited pixel change.
    assert workspace.reference_modes["runtime"].isEnabled()
    workspace.reference_modes["runtime"].click()
    assert workspace.reference_modes["runtime"].isChecked()
    assert not workspace.reference_modes["folder"].isChecked()
    first = workspace.frame
    assert first is not None and int(first.image[0, 0, 0]) == 80
    assert not workspace.buttons["reference_next"].isEnabled()
    publish(3, 90, key)
    assert workspace.frame is first
    workspace.buttons["roi_refresh"].click()
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 90
    workspace.reference_modes["folder"].click()
    until(lambda: not window.parameters.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 2
    window.navigate(0)
    window.navigate(4)
    reopened = window.parameters.roi_workspace
    assert reopened is not None
    until(lambda: not window.parameters.busy)
    assert reopened.reference_mode == "runtime"  # New session prefers eligible runtime.
    assert reopened.frame is not None and int(reopened.frame.image[0, 0, 0]) == 90
    assert not engine.calls and not source.dirty and not reopened.draft.dirty
    assert window.bridge.application.snapshot().counters == ProductionCounters()


def test_removed_reference_refresh_and_explicit_corrupt_navigation(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
) -> None:
    window, workspace, _ = camera_screen
    current = Path(workspace.reference.toolTip())
    current.write_bytes(b"broken")
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    assert workspace.frame is None and workspace.message.isVisible()
    assert not workspace.editable() and workspace.buttons["reference_next"].isEnabled()
    workspace.buttons[
        "reference_next"
    ].click()  # Explicit choice; no automatic substitution.
    until(lambda: not window.parameters.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 2
    chosen = Path(workspace.reference.toolTip())
    chosen.unlink()
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    assert workspace.frame is None
    assert "no longer exists" in workspace.message.text()
    assert not workspace.editable()


@pytest.mark.parametrize(
    "invalid",
    [
        ("-1", "0", "5", "8"),
        ("0", "0", "0", "8"),
        ("59", "0", "5", "8"),
        ("x", "0", "5", "8"),
    ],
)
def test_selected_xywh_edit_and_atomic_rejection(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    invalid: tuple[str, str, str, str],
) -> None:
    window, workspace, engine = camera_screen
    workspace.select_roi(2)
    before = workspace.draft.camera(workspace.camera).slots
    for field, value in zip(
        workspace.coordinates.values(), ("20", "2", "5", "8"), strict=True
    ):
        field.setText(value)
    workspace.buttons["roi_set_rect"].click()
    after = workspace.draft.camera(workspace.camera).slots
    assert after[2].rect == BoundingRect(20, 2, 5, 8)
    assert all(after[i] == before[i] for i in (0, 1, 3, 4, 5))
    assert workspace.status.text() == text("unsaved", window.language)
    for field, value in zip(workspace.coordinates.values(), invalid, strict=True):
        field.setText(value)
    workspace.buttons["roi_set_rect"].click()
    assert workspace.draft.camera(workspace.camera).slots == after
    assert workspace.message.isVisible() and workspace.message.text()
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_settings_representative_screenshot(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    language: Language,
    size: tuple[int, int],
    tmp_path: Path,
) -> None:
    window, workspace, engine = camera_screen
    image = demo_images()[0]
    path = Path(workspace.reference.toolTip())
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    encoded.tofile(path)
    workspace.refresh_frame()
    until(lambda: not window.parameters.busy)
    height, width = image.shape[:2]
    workspace.draft.replace_rects(
        workspace.camera,
        tuple(
            BoundingRect(
                i * width // 6, 0, (i + 1) * width // 6 - i * width // 6, height
            )
            for i in range(6)
        ),
    )
    workspace.draft.save()
    workspace.select_roi(0)
    window.resize(*size)
    window.set_language(language)
    QApplication.processEvents()
    assert workspace.canvas.height() > workspace.height() * 0.65
    assert len(workspace.canvas.rois) == 6 and workspace.frame is not None
    for control in (
        *workspace.coordinates.values(),
        *workspace.reference_modes.values(),
        workspace.status,
        *workspace.buttons.values(),
    ):
        assert workspace.rect().contains(control.mapTo(workspace, QPoint(0, 0)))
        assert workspace.rect().contains(
            control.mapTo(workspace, control.rect().bottomRight())
        )
    for key in ("roi_set_rect", "roi_common", "roi_distribute"):
        button = workspace.buttons[key]
        parent = button.parentWidget()
        assert parent is not None and parent.rect().contains(button.geometry())
    assert not workspace.message.isVisible()
    assert workspace.reference_toolbar.height() <= Style.touch + Style.small
    assert workspace.geometry_toolbar.height() == Style.touch
    save = workspace.buttons["roi_save_apply"]
    for toolbar, center_control, right_control in (
        (
            workspace.reference_toolbar,
            workspace.reference,
            workspace.buttons["roi_actual"],
        ),
        (workspace.geometry_toolbar, workspace.buttons["roi_common"], save),
    ):
        assert right_control.mapTo(toolbar, right_control.rect().topRight()).x() == (
            toolbar.width() - 1
        )
        if size[0] == 1920:
            center = center_control.parentWidget()
            assert center is not None
            assert (
                abs(
                    center.mapTo(toolbar, center.rect().center()).x()
                    - toolbar.rect().center().x()
                )
                <= 1
            )
    assert workspace.folder.mapTo(workspace, QPoint()).y() > (
        workspace.reference_toolbar.geometry().bottom()
    )
    assert workspace.canvas.y() > workspace.folder.mapTo(workspace, QPoint()).y()
    assert window.grab().save(
        str(tmp_path / f"settings-{language}-{size[0]}x{size[1]}.png")
    )
    assert not engine.calls and not workspace.draft.dirty


@pytest.mark.parametrize("language", ["zh", "en"])
def test_compact_icon_states_and_loop_click_preserve_configuration(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    language: Language,
) -> None:
    window, workspace, engine = camera_screen
    window.set_language(language)
    sources = workspace.sources
    assert sources is not None
    saved = sources.path.read_bytes()
    geometry = workspace.draft.definition
    assert workspace.loop.isChecked()
    assert workspace.loop.text() == text("source_loop_on", language)
    assert text("source_loop", language) in workspace.loop.accessibleName()
    workspace.loop.click()
    assert not workspace.loop.isChecked() and not sources.definition.loop
    assert workspace.loop.text() == text("source_loop_off", language)
    assert sources.dirty and sources.path.read_bytes() == saved
    workspace.loop.click()
    assert sources.definition.loop and not sources.dirty
    assert not workspace.loop.icon().isNull()
    for mode, button in workspace.reference_modes.items():
        assert button.text() == "" and not button.icon().isNull()
        assert button.accessibleName() == text("reference_" + mode, language)
        assert button.isChecked() == (workspace.reference_mode == mode)
        on = (
            button.icon()
            .pixmap(QSize(22, 22), QIcon.Mode.Normal, QIcon.State.On)
            .toImage()
        )
        off = (
            button.icon()
            .pixmap(QSize(22, 22), QIcon.Mode.Normal, QIcon.State.Off)
            .toImage()
        )
        assert on != off  # Selection is also indicated by a check mark.
    for key in ("roi_common", "roi_distribute"):
        button = workspace.buttons[key]
        assert button.text() == "" and not button.icon().isNull()
        assert button.accessibleName() == button.toolTip() == text(key, language)
    numeric_apply = workspace.buttons["roi_set_rect"]
    assert numeric_apply.text() == text("roi_set_rect", language)
    assert (
        numeric_apply.width()
        >= numeric_apply.fontMetrics().horizontalAdvance(numeric_apply.text())
        + numeric_apply.iconSize().width()
        + 24
    )
    assert workspace.buttons["roi_save_apply"].text() == text(
        "roi_save_apply", language
    )
    assert workspace.draft.definition == geometry
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_roi_toolbar_wraps_without_shrinking_fields(
    camera_screen: tuple[ProductionWindow, MachineRoiWorkspace, ControlledEngine],
    language: Language,
) -> None:
    window, workspace, engine = camera_screen
    window.set_language(language)
    QApplication.processEvents()
    toolbar = workspace.geometry_toolbar
    numeric = workspace.buttons["roi_set_rect"]
    bulk = workspace.buttons["roi_common"]
    widths = [field.width() for field in workspace.coordinates.values()]
    before = workspace.draft.definition
    save = workspace.buttons["roi_save_apply"]
    upper = workspace.reference_toolbar
    reference = workspace.reference_modes["folder"]
    assert numeric.mapTo(toolbar, QPoint()).y() == bulk.mapTo(toolbar, QPoint()).y()
    assert save.mapTo(toolbar, QPoint()).y() == numeric.mapTo(toolbar, QPoint()).y()
    assert upper.height() <= Style.touch + Style.small
    narrow = toolbar.minimumSizeHint().width() + Style.small
    workspace.setFixedWidth(narrow)
    QApplication.processEvents()
    assert bulk.mapTo(toolbar, QPoint()).y() > numeric.mapTo(toolbar, QPoint()).y()
    assert toolbar.height() >= numeric.height() + bulk.height()
    assert reference.mapTo(upper, QPoint()).y() > workspace.camera_tabs.y()
    assert workspace.camera_tabs.width() >= workspace.camera_tabs.sizeHint().width()
    for control in (
        *workspace.coordinates.values(),
        numeric,
        bulk,
        workspace.buttons["roi_distribute"],
        workspace.status,
        save,
    ):
        assert toolbar.rect().contains(control.mapTo(toolbar, QPoint()))
        assert toolbar.rect().contains(
            control.mapTo(toolbar, control.rect().bottomRight())
        )
    upper_controls = (
        workspace.camera_tabs,
        *workspace.reference_modes.values(),
        workspace.reference,
        *(
            workspace.buttons[key]
            for key in (
                "reference_previous",
                "reference_next",
                "roi_refresh",
                "roi_fit",
                "roi_actual",
            )
        ),
    )
    for control in upper_controls:
        assert upper.rect().contains(control.mapTo(upper, QPoint()))
        assert upper.rect().contains(control.mapTo(upper, control.rect().bottomRight()))
    assert [field.width() for field in workspace.coordinates.values()] == widths
    assert save.mapTo(toolbar, save.rect().topRight()).x() == toolbar.width() - 1
    workspace.setFixedWidth(1000)
    QApplication.processEvents()
    assert numeric.mapTo(toolbar, QPoint()).y() == bulk.mapTo(toolbar, QPoint()).y()
    assert save.mapTo(toolbar, QPoint()).y() == numeric.mapTo(toolbar, QPoint()).y()
    assert upper.height() <= Style.touch + Style.small
    upper.setFixedWidth(upper.minimumSizeHint().width() + Style.small)
    QApplication.processEvents()
    fit, actual = workspace.buttons["roi_fit"], workspace.buttons["roi_actual"]
    assert actual.mapTo(upper, QPoint()).y() > reference.mapTo(upper, QPoint()).y()
    assert reference.mapTo(upper, QPoint()).y() > workspace.camera_tabs.y()
    assert fit.mapTo(upper, QPoint()).y() == actual.mapTo(upper, QPoint()).y()
    assert actual.mapTo(upper, actual.rect().topRight()).x() == upper.width() - 1
    for control in upper_controls:
        assert upper.rect().contains(control.mapTo(upper, QPoint()))
        assert upper.rect().contains(control.mapTo(upper, control.rect().bottomRight()))
    upper.setFixedWidth(1000)
    QApplication.processEvents()
    assert upper.height() <= Style.touch + Style.small
    assert workspace.draft.definition == before and not engine.calls
