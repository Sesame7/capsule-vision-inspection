"""Unified Settings and real-local Folder lifecycle, with fake inference."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton
from run_demo import demo_images
from tests.support.application import WAIT, ControlledEngine
from tests.support.qt import until
from tests.support.recipe_ui import camera_screen as camera_screen
from tests.support.recipe_ui import folder_composition
from tests.support.source_inputs import folders, input_files, write_image

from capsule_vision.application import (
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
)
from capsule_vision.application.basler import BaslerDevice
from capsule_vision.application.basler_connection import (
    ConnectedCamera,
    ConnectionState,
)
from capsule_vision.application.composition import compose
from capsule_vision.application.folder_replay import FolderSourceError, decode_image
from capsule_vision.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    RecipeDefinition,
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
    save_machine_layout,
    save_recipe,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId, CycleId, Frame, FrameId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui import vision_page
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.source_notifications import SourceNotifications
from capsule_vision.ui.style import Style, Theme
from capsule_vision.ui.vision_page import VisionPage


@pytest.fixture
def device_inventory(monkeypatch: pytest.MonkeyPatch) -> tuple[BaslerDevice, ...]:
    devices = tuple(BaslerDevice(f"100{i}", "acA1920-test") for i in range(6, 0, -1))
    gui_thread = get_ident()

    def discover() -> tuple[BaslerDevice, ...]:
        assert get_ident() != gui_thread
        return devices

    monkeypatch.setattr(vision_page, "discover_basler_devices", discover)
    return devices


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_vision_connection_buttons_never_show_as_top_level_windows(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    qapp: QApplication,
    mode: str,
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None
    if mode == "basler":
        for i, camera in enumerate(workspace.camera_cards):
            sources.set_serial(camera, f"100{i}")
        workspace.set_source_mode("basler")
        sources.save()
        workspace.refresh()
        until(lambda: not window.recipe_page.busy)
    window.navigate(0)
    unexpected: list[str] = []

    class ButtonShows(QObject):
        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if (
                isinstance(watched, QPushButton)
                and event.type() is QEvent.Type.Show
                and watched.isWindow()
            ):
                unexpected.append(repr(watched))
            return False

    watch = ButtonShows()
    qapp.installEventFilter(watch)
    before = window.bridge.application.snapshot()
    try:
        for _ in range(2):
            window.navigate(4)
            until(lambda: not window.recipe_page.busy)
            current = window.vision_workspace
            assert current is not None
            for button in (current.connect_source, current.disconnect_source):
                assert button.parentWidget() is current.source_area
                assert button.size().toTuple() == (Style.touch, Style.touch)
                assert button.minimumSize() == button.maximumSize() == button.size()
                assert not button.isWindow() and button.windowHandle() is None
                assert button.isVisible() == (mode == "basler")
            window.navigate(0)
        assert not unexpected  # Must catch transient shows during construction too.
        assert window.bridge.application.snapshot() == before and not engine.calls
    finally:
        qapp.removeEventFilter(watch)


def test_source_change_invalidates_reference_no_geometry_repair(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
    assert workspace.camera_cards[workspace.camera].detail == str(smaller)
    assert workspace.frame is None and workspace.camera not in workspace.draft.bounds
    assert sources.dirty and not window.buttons["operation"].isEnabled()
    workspace.refresh_frame()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and workspace.frame.image.shape == (8, 15, 3)
    assert workspace.draft.definition == saved and window.event_label.text()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not sources.dirty and load_camera_sources(sources.path).camera(
        workspace.camera
    ).folder == str(smaller)
    assert (
        load_machine_layout(workspace.draft.path) == saved
        if workspace.draft.path is not None
        else False
    )
    assert "CAM1" in window.event_label.toolTip()


def test_confirmed_camera_count_edit_save_discard_and_template_restoration(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, _ = camera_screen
    sources = workspace.sources
    assert sources is not None and workspace.draft.path is not None
    original = workspace.draft.saved
    workspace.camera_cards[CameraId("CAM6")].click()
    until(lambda: not window.recipe_page.busy)
    button = workspace.camera_cards[CameraId("CAM6")].configuration
    button.click()
    dialog = next(box for box in workspace.findChildren(QMessageBox) if box.isVisible())
    dialog.reject()
    assert workspace.draft.definition == original and not sources.dirty
    button.click()
    dialog = next(box for box in workspace.findChildren(QMessageBox) if box.isVisible())
    remove = next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) is QMessageBox.ButtonRole.DestructiveRole
    )
    remove.click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.camera == "CAM1" and workspace.draft.dirty and sources.dirty
    assert len(workspace.camera_cards) == 6
    assert not workspace.camera_cards[CameraId("CAM6")].isCheckable()
    assert workspace.camera_cards[CameraId("CAM6")].summary == text(
        "source_unconfigured", window.language
    )
    dark_plus = button.icon().pixmap(24, 24).toImage()
    window.set_theme("light")
    assert button.icon().pixmap(24, 24).toImage() != dark_plus
    window.set_theme("dark")
    assert button.icon().pixmap(24, 24).toImage() == dark_plus
    assert (
        len(window.cards) == 6 and load_machine_layout(workspace.draft.path) == original
    )
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert workspace.draft.definition == original and not sources.dirty
    workspace.change_camera_configuration()
    sources.set_serial(CameraId("CAM5"), "1005")
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not workspace.draft.dirty and not sources.dirty
    assert len(window.cards) == len(window.bridge.frames.cameras) == 5
    assert (
        load_machine_layout(workspace.draft.path).layout.cameras
        == original.layout.cameras[:5]
    )
    assert len(load_camera_sources(sources.path).cameras) == 5
    before = window.bridge.application.snapshot()
    assert before.active is None and not before.counters.cycles_started

    def no_file_selection(*_args: object, **_kwargs: object) -> tuple[str, str]:
        raise AssertionError("CAM6 initialization must not select a file")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", no_file_selection)
    workspace.camera_cards[CameraId("CAM5")].click()
    until(lambda: not window.recipe_page.busy)
    workspace.coordinates["X"].setText("41")
    workspace.apply_geometry()
    five = workspace.draft.definition
    button.click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.draft.definition.layout.cameras[:5] == five.layout.cameras
    cam5 = workspace.draft.camera(CameraId("CAM5"))
    cam6 = workspace.draft.camera(CameraId("CAM6"))
    assert cam5.slots[0].rect.x == cam6.slots[0].rect.x == 41
    assert [s.channel_id for s in cam6.slots] == list(range(31, 37))
    assert all(
        a.rect == b.rect and a.rect is not b.rect
        for a, b in zip(cam5.slots, cam6.slots, strict=True)
    )
    source = sources.definition.camera(CameraId("CAM6"))
    assert source.folder == source.basler_serial == ""
    dark_minus = button.icon().pixmap(24, 24).toImage()
    window.set_theme("light")
    assert button.icon().pixmap(24, 24).toImage() != dark_minus
    window.set_theme("dark")
    assert button.icon().pixmap(24, 24).toImage() == dark_minus
    workspace.camera_cards[CameraId("CAM6")].click()
    until(lambda: not window.recipe_page.busy)
    workspace.coordinates["X"].setText("2")
    workspace.apply_geometry()
    assert workspace.draft.camera(CameraId("CAM5")) == cam5
    assert workspace.draft.camera(CameraId("CAM6")).slots[0].rect.x == 2
    assert len(load_machine_layout(workspace.draft.path).layout.cameras) == 5
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert workspace.draft.definition.layout.cameras == original.layout.cameras[:5]
    assert not sources.dirty and not workspace.draft.dirty
    assert (
        workspace.camera == "CAM1"
        and not workspace.camera_cards[CameraId("CAM6")].isCheckable()
    )
    button.click()
    assert workspace.draft.camera(CameraId("CAM6")).slots[0].rect.x == 0
    workspace.source_edited(sources.saved.cameras[0].folder, CameraId("CAM6"))
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert len(window.cards) == len(window.bridge.frames.cameras) == 6
    assert not workspace.draft.dirty and not sources.dirty
    assert window.bridge.application.snapshot().counters == before.counters
    saved = load_machine_layout(workspace.draft.path)
    window.navigate(0)
    until(lambda: window.vision_workspace is None)
    window.navigate(4)
    until(lambda: not window.recipe_page.busy)
    reopened = window.vision_workspace
    assert reopened is not None and reopened.draft.definition == saved
    assert reopened.sources is not None and not reopened.sources.dirty
    assert len(reopened.sources.saved.cameras) == 6
    assert window.bridge.application.snapshot().active is None


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_cam6_template_needs_its_own_source_and_valid_image_bounds(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    tmp_path: Path,
    mode: str,
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None and workspace.draft.path is not None
    workspace.change_camera_configuration()
    if mode == "basler":
        for index, row in enumerate(sources.definition.cameras, 1):
            sources.set_serial(row.camera_id, str(1000 + index))
        workspace.set_source_mode("basler")
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert len(window.cards) == 5 and not sources.dirty
    before = window.bridge.application.snapshot()
    workspace.camera_cards[CameraId("CAM6")].configuration.click()
    assert sources.definition.camera(CameraId("CAM6")).folder == ""
    assert sources.definition.camera(CameraId("CAM6")).basler_serial == ""
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not workspace.vision_dirty
    assert len(load_machine_layout(workspace.draft.path).layout.cameras) == 6
    if mode == "basler":
        assert window.event_label.text()
        assert (
            load_camera_sources(sources.path).camera(CameraId("CAM6")).basler_serial
            == ""
        )
    else:
        assert len(window.cards) == 6
        assert window.bridge.application.snapshot().state is ApplicationState.READY
        failed = window.bridge.application.start()
        until(failed.done)
        assert isinstance(failed.exception(), Exception)
    current = window.bridge.application.snapshot()
    assert current.active == before.active and current.counters == before.counters
    assert len(window.cards) == (5 if mode == "basler" else 6) and not engine.calls
    if mode == "folder":
        small = tmp_path / "small-cam6"
        small.mkdir()
        write_image(small / "reference.png", 9, (8, 60))
        workspace.source_edited(str(small), CameraId("CAM6"))
        workspace.camera_cards[CameraId("CAM6")].click()
        until(lambda: not window.recipe_page.busy)
        assert workspace.frame is not None and workspace.frame.image.shape == (8, 60, 3)
        geometry = workspace.draft.definition
        saved_machine = workspace.draft.path.read_bytes()
        candidate = window.bridge.application.snapshot().candidate
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert (
            workspace.draft.definition == geometry
        )  # No clamping to the smaller image.
        assert workspace.draft.path.read_bytes() == saved_machine
        assert load_camera_sources(sources.path) == sources.definition
        assert not sources.dirty
        assert window.bridge.application.snapshot().candidate == candidate
        feedback = workspace.feedback
        assert feedback is not None and "CAM6" in feedback[1]
        workspace.loop.setChecked(not sources.saved.loop)
        confirm_vision_discard(workspace)
        until(lambda: not window.recipe_page.busy)
        assert workspace.camera == "CAM6" and not workspace.vision_dirty
    else:
        sources.set_serial(CameraId("CAM6"), "1006")
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
        assert len(window.cards) == 6 and not workspace.vision_dirty
        with pytest.raises(ApplicationStateError, match="Connect"):
            window.bridge.application.start()
        assert window.bridge.application.snapshot().state is ApplicationState.READY
        assert window.bridge.application.snapshot().active is None and not engine.calls
        assert window.bridge.application.snapshot().counters == before.counters


@pytest.mark.parametrize("model_id", [None, "unavailable"])
def test_camera_reduction_accepts_compatible_incomplete_recipe(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    tmp_path: Path,
    model_id: str | None,
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None and workspace.draft.path is not None
    unrelated = RecipeDefinition(
        "incomplete",
        "Incomplete",
        model_id=model_id,
    )
    path = tmp_path / "incomplete.json"
    save_recipe(path, unrelated)
    invalid = tmp_path / "unrelated-invalid.json"
    invalid.write_text("{", encoding="utf-8")
    original = path.read_bytes()
    before = window.bridge.application.snapshot()
    geometry = workspace.draft.saved.layout.cameras[:5]
    workspace.change_camera_configuration()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not workspace.vision_dirty
    assert load_machine_layout(workspace.draft.path).layout.cameras == geometry
    assert len(load_camera_sources(sources.path).cameras) == 5
    assert len(window.cards) == len(window.bridge.frames.cameras) == 5
    assert path.read_bytes() == original
    assert invalid.read_text(encoding="utf-8") == "{"
    assert any(i.path == invalid for i in workspace.session.catalog.discover().issues)
    after = window.bridge.application.snapshot()
    assert after.state is ApplicationState.READY and after.candidate is not None
    assert before.candidate is not None
    assert after.candidate.recipe_id == before.candidate.recipe_id == "local"
    assert after.candidate.model_id == before.candidate.model_id
    assert after.active == before.active and after.counters == before.counters
    assert not engine.calls
    with pytest.raises(ConfigurationError, match="model_id"):
        workspace.session.prepare(unrelated)
    assert window.bridge.application.snapshot() == after


@pytest.mark.parametrize("bad", ["empty", "corrupt"])
def test_reference_error_no_substitution(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is None and window.event_label.text()
    assert workspace.editable() and not workspace.canvas.model.can_drag
    assert not window.bridge.application.snapshot().counters.cycles_started


def test_save_reload_partial_failure_and_running_guards(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
    until(lambda: not window.recipe_page.busy)
    workspace.loop.setChecked(False)
    candidate = window.bridge.application.snapshot().candidate
    original = source.save

    def fail() -> None:
        raise ConfigurationIOError("test source write failure")

    monkeypatch.setattr(source, "save", fail)
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert source.dirty and not workspace.draft.dirty
    assert text("source_partial_save", window.language) in window.event_label.text()
    assert window.bridge.application.snapshot().candidate == candidate
    monkeypatch.setattr(source, "save", original)
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
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
    assert all(not button.isEnabled() for button in workspace.source_modes.values())
    assert not workspace.playback_options.isEnabled() and not workspace.fps.isEnabled()
    assert not workspace.camera_cards[CameraId("CAM6")].configuration.isEnabled()
    workspace.change_camera_configuration()
    assert len(workspace.draft.definition.layout.cameras) == 6
    assert all(
        not card.folder.isEnabled()
        and not card.device.isEnabled()
        and not card.browse.isEnabled()
        for card in workspace.camera_cards.values()
    )
    workspace.source_edited("forbidden")
    workspace.loop_changed(True)
    workspace.fps.setText("10")
    workspace.fps_changed()
    workspace.select_device(CameraId("CAM2"), 0)
    workspace.set_source_mode("basler")
    workspace.save_and_apply()
    assert source.definition == before
    engine.release_active.set()
    until(lambda: window.bridge.application.snapshot().state is ApplicationState.READY)
    assert [v for v, _ in engine.calls][2:] == [99, 11, 21, 31, 41, 51]


@pytest.mark.parametrize("action", ["switch", "close"])
def test_reference_io_off_gui_stale_completion_and_close_wait(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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

    monkeypatch.setattr("capsule_vision.ui.vision_page.decode_image", blocked)
    workspace.refresh_frame()
    until(entered.is_set)
    assert not workspace.buttons["roi_refresh"].isEnabled()
    assert not workspace.buttons["reference_next"].isEnabled()
    assert not workspace.buttons["vision_import"].isEnabled()
    if action == "switch":
        workspace.camera_cards[CameraId("CAM5")].click()
    else:
        window.close()
        assert not window.bridge.closed
    release.set()
    until(lambda: not window.recipe_page.busy)
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
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
        until(lambda: window.vision_workspace is None)
        assert window.vision_workspace is None and not workspace.sources.dirty
    else:
        assert (
            window.pages.currentWidget() is window.vision_page
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


def test_uncached_replay_bad_later_file_stops_without_partial_cycle(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    observed: list[Future[None]] = []
    # Loop=true now rejects every bad preload file before native startup. Keep
    # the original later-source-error assertion on the unchanged on-demand path.
    composition = folder_composition(tmp_path, engine, loop=False, observe=observed)
    (tmp_path / "independent-3" / "unrelated2.png").write_bytes(b"broken")
    app = composition.application
    delivery = FutureDelivery()
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    notifications = SourceNotifications(bridge, delivery)
    window = MainWindow(
        bridge,
        recipe_editor=composition.editor,
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
            and "FolderSourceError" in window.event_label.toolTip()
            and str(tmp_path) not in window.event_label.toolTip()
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
    window = MainWindow(
        bridge,
        recipe_editor=composition.editor,
    )
    window.show()
    window.navigate(4)
    workspace = window.vision_workspace
    assert workspace is not None
    try:
        until(lambda: not window.recipe_page.busy)
        assert workspace.mode_switch.toolTip() == text("cli_sources", window.language)
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
    window = MainWindow(
        bridge,
        recipe_editor=composition.editor,
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
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
        assert window.recipe_page.busy
        assert workspace.status.accessibleName() == text(
            "setup_saving", window.language
        )
        assert window.event_label.text() != text("roi_applied", window.language)
        assert not workspace.buttons["save_apply"].isEnabled()
    finally:
        release.set()
    until(lambda: not window.recipe_page.busy)
    assert not source.dirty and load_camera_sources(source.path) == source.definition
    assert "post-save preparation failed" in window.event_label.toolTip()
    assert workspace.status.accessibleName() == text("setup_failed", window.language)
    assert window.bridge.application.snapshot().candidate == candidate


def test_folder_status_is_derived_from_pixels_after_source_round_trip(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    assert workspace.frame is not None
    workspace.source_modes["basler"].click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is None
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None
    assert (
        text("vision_schematic", window.language)
        not in workspace.reference.currentText()
    )
    assert "60×20" in workspace.reference.currentText()
    assert workspace.reference.currentText().startswith("1/2")
    assert workspace.canvas.model.schematic_size is None and not engine.calls


def confirm_vision_discard(workspace: VisionPage, *, accept: bool = True) -> None:
    workspace.buttons["vision_discard"].click()
    dialog = next(box for box in workspace.findChildren(QMessageBox) if box.isVisible())
    choice = text("discard" if accept else "cancel", workspace.language)
    next(button for button in dialog.buttons() if button.text() == choice).click()


def test_async_image_status_follows_current_camera_not_stale_delivery(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, _ = camera_screen
    entered, release = Event(), Event()
    read = decode_image

    def blocked(path: Path):
        entered.set()
        assert release.wait(WAIT)
        return read(path)

    monkeypatch.setattr(vision_page, "decode_image", blocked)
    workspace.refresh_frame()
    until(entered.is_set)
    try:
        assert workspace.frame is None
        assert workspace.reference.currentText() == "ROI ≈"
        assert (
            text("vision_schematic", window.language)
            in workspace.reference.accessibleDescription()
        )
        workspace.camera_cards[CameraId("CAM2")].click()
    finally:
        release.set()
    until(lambda: not window.recipe_page.busy)
    assert (
        workspace.frame is not None
        and workspace.frame.frame_id.camera_id == CameraId("CAM2")
    )
    assert int(workspace.frame.image[0, 0, 0]) == 11
    assert workspace.reference.currentText() == "1/3 · 60×20"
    assert (
        workspace.canvas.model.size == (60, 20)
        and workspace.canvas.model.schematic_size is None
    )


@pytest.mark.parametrize("edits", ["source", "roi", "mixed"])
def test_vision_discard_restores_all_configuration_but_keeps_view_and_application(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    edits: str,
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None and workspace.draft.path is not None
    files = source.path.read_bytes(), workspace.draft.path.read_bytes()
    button = workspace.buttons["vision_discard"]
    assert button.isVisible() and not button.isEnabled()
    workspace.request_discard()
    assert not any(box.isVisible() for box in workspace.findChildren(QMessageBox))
    workspace.camera_cards[CameraId("CAM2")].click()
    until(lambda: not window.recipe_page.busy)
    workspace.select_roi(3)
    workspace.canvas.zoom(2)
    frame = workspace.frame
    view = (
        workspace.camera,
        workspace.selected_index,
        workspace.canvas.model.scale,
        workspace.canvas.model.pan,
    )
    if edits in ("source", "mixed"):
        workspace.source_edited("unused-draft-folder", CameraId("CAM3"))
        workspace.loop.setChecked(not source.saved.loop)
        workspace.fps.setText("9")
        workspace.fps_changed()
    if edits in ("roi", "mixed"):
        workspace.coordinates["X"].setText("31")
        workspace.apply_geometry()
    before = source.definition, workspace.draft.definition
    snapshot = window.bridge.application.snapshot()
    assert button.isEnabled()
    QApplication.processEvents()
    location = button.mapTo(workspace, QPoint()), button.size()
    confirm_vision_discard(workspace, accept=False)
    assert (source.definition, workspace.draft.definition) == before
    assert button.isEnabled()
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert source.saved == source.definition == load_camera_sources(source.path)
    assert (
        workspace.draft.saved
        == workspace.draft.definition
        == load_machine_layout(workspace.draft.path)
    )
    assert (source.path.read_bytes(), workspace.draft.path.read_bytes()) == files
    QApplication.processEvents()
    assert not button.isEnabled()
    assert (button.mapTo(workspace, QPoint()), button.size()) == location
    assert workspace.frame is frame
    assert (
        workspace.camera,
        workspace.selected_index,
        workspace.canvas.model.scale,
        workspace.canvas.model.pan,
    ) == view
    assert window.bridge.application.snapshot() == snapshot and not engine.calls


def test_vision_discard_uses_current_disk_files_not_initial_memory(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None and workspace.draft.path is not None
    initial = workspace.draft.saved
    workspace.loop.setChecked(not source.saved.loop)
    workspace.coordinates["X"].setText("1")
    workspace.apply_geometry()
    disk = replace(
        initial,
        layout=replace(
            initial.layout,
            cameras=tuple(
                replace(
                    c,
                    slots=(
                        replace(c.slots[0], rect=BoundingRect(2, 0, 8, 10)),
                        *c.slots[1:],
                    ),
                )
                if c.camera_id == workspace.camera
                else c
                for c in initial.layout.cameras
            ),
        ),
    )
    sources = replace(source.saved, target_fps=3.5)
    save_machine_layout(workspace.draft.path, disk)
    save_camera_sources(source.path, sources)
    files = source.path.read_bytes(), workspace.draft.path.read_bytes()
    candidate = window.bridge.application.snapshot().candidate
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert workspace.draft.saved == workspace.draft.definition == disk != initial
    assert source.saved == source.definition == sources
    assert workspace.fps.text() == "3.5"
    assert (source.path.read_bytes(), workspace.draft.path.read_bytes()) == files
    assert (
        window.bridge.application.snapshot().candidate == candidate and not engine.calls
    )


def test_vision_discard_mode_and_discovered_serial_without_connecting(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    device_inventory: tuple[BaslerDevice, ...],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    saved = source.definition
    before = window.bridge.application.snapshot()
    workspace.source_modes["basler"].click()
    workspace.discover.click()
    until(lambda: not window.recipe_page.busy)
    card = workspace.camera_cards[CameraId("CAM1")]
    card.device.activated.emit(card.device.findData("1001"))
    assert (
        source.definition.mode == "basler"
        and source.definition.camera(CameraId("CAM1")).basler_serial == "1001"
    )
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert source.saved == source.definition == saved
    assert workspace.source_modes["folder"].isChecked()
    assert (
        not workspace.vision_dirty
        and not workspace.buttons["vision_discard"].isEnabled()
    )
    assert window.bridge.application.snapshot() == before and not engine.calls


def test_vision_discard_reload_is_bounded_and_preserves_draft_if_edit_lock_changes(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, _ = camera_screen
    source = workspace.sources
    assert source is not None
    workspace.loop.setChecked(not source.saved.loop)
    draft = source.definition, workspace.draft.definition
    entered, release = Event(), Event()
    read = load_camera_sources
    gui_thread = get_ident()

    def blocked(path: Path):
        assert get_ident() != gui_thread
        entered.set()
        assert release.wait(WAIT)
        return read(path)

    monkeypatch.setattr(
        "capsule_vision.application.editing.load_camera_sources", blocked
    )
    confirm_vision_discard(workspace)
    until(entered.is_set)
    try:
        assert window.recipe_page.busy
        assert workspace.status.accessibleName() == text(
            "vision_reloading", window.language
        )
        assert not workspace.buttons["vision_discard"].isEnabled()
        assert not workspace.buttons["save_apply"].isEnabled()
        workspace.request_discard()
        assert not any(box.isVisible() for box in workspace.findChildren(QMessageBox))
        monkeypatch.setattr(workspace, "_allowed", lambda: False)
    finally:
        release.set()
    until(lambda: not window.recipe_page.busy)
    assert (source.definition, workspace.draft.definition) == draft
    assert window.event_label.accessibleName() == text(
        "vision_reload_error", window.language
    )


@pytest.mark.parametrize("broken", ["machine", "sources"])
def test_vision_discard_reload_failure_preserves_both_drafts_and_baselines(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    broken: str,
) -> None:
    window, workspace, _ = camera_screen
    source = workspace.sources
    assert source is not None and workspace.draft.path is not None
    workspace.loop.setChecked(not source.saved.loop)
    workspace.coordinates["X"].setText("1")
    workspace.apply_geometry()
    before = (
        source.definition,
        source.saved,
        workspace.draft.definition,
        workspace.draft.saved,
    )
    path = workspace.draft.path if broken == "machine" else source.path
    path.write_text("{broken", encoding="utf-8")
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert (
        source.definition,
        source.saved,
        workspace.draft.definition,
        workspace.draft.saved,
    ) == before
    assert path.read_text(encoding="utf-8") == "{broken"
    assert window.event_label.accessibleName() == text(
        "vision_reload_error", window.language
    )
    assert str(path) in window.event_label.toolTip()
    assert workspace.buttons["vision_discard"].isEnabled()


def test_vision_discard_preserves_partial_persistence_and_respects_locks(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None and workspace.draft.path is not None
    workspace.loop.setChecked(not source.saved.loop)
    workspace.coordinates["X"].setText("1")
    workspace.apply_geometry()
    snapshot = window.bridge.application.snapshot()

    def fail() -> None:
        raise ConfigurationIOError("source save rejected after machine persisted")

    with monkeypatch.context() as patch:
        patch.setattr(source, "save", fail)
        workspace.save_and_apply()
        until(lambda: not window.recipe_page.busy)
    persisted = load_machine_layout(workspace.draft.path)
    assert persisted == workspace.draft.saved
    assert persisted.layout.cameras[0].slots[0].rect.x == 1
    assert not workspace.draft.dirty and source.dirty
    workspace.coordinates["X"].setText("2")
    workspace.apply_geometry()
    with monkeypatch.context() as patch:
        patch.setattr(workspace, "_allowed", lambda: False)
        workspace.refresh()
        assert not workspace.buttons["vision_discard"].isEnabled()
        workspace.request_discard()
        assert not any(box.isVisible() for box in workspace.findChildren(QMessageBox))
    workspace.refresh()
    confirm_vision_discard(workspace)
    until(lambda: not window.recipe_page.busy)
    assert workspace.draft.definition == persisted and not workspace.draft.dirty
    assert source.definition == load_camera_sources(source.path) and not source.dirty
    assert window.bridge.application.snapshot() == snapshot and not engine.calls


def test_folder_navigation_refresh_and_camera_positions(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    disk_before = source.path.read_bytes()
    geometry = workspace.draft.definition
    assert workspace.reference.currentText().startswith("1/2")
    assert not workspace.buttons["reference_previous"].isEnabled()
    workspace.buttons["reference_next"].click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 2
    assert not workspace.buttons["reference_next"].isEnabled()
    assert workspace.reference.count() == 2
    workspace.reference.setCurrentIndex(0)
    workspace.reference.activated.emit(0)
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 1
    workspace.reference.setCurrentIndex(1)
    workspace.reference.activated.emit(1)
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 2
    workspace.channel.setCurrentIndex(2)
    workspace.channel.activated.emit(2)
    assert workspace.selected_index == 2 and workspace.channel.currentText() == "CH03"
    workspace.browse_reference(1)
    assert int(workspace.frame.image[0, 0, 0]) == 2  # Boundary never wraps.
    current = Path(workspace.reference.toolTip())
    write_image(current, 77)
    write_image(current.parent / "0.png", 66)
    workspace.buttons["roi_refresh"].click()
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 77
    assert Path(workspace.reference.toolTip()) == current
    assert workspace.reference.currentText().startswith("3/3")
    workspace.camera_cards[CameraId("CAM2")].click()
    until(lambda: not window.recipe_page.busy)
    workspace.buttons["reference_next"].click()
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 12
    workspace.camera_cards[CameraId("CAM1")].click()
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 77
    workspace.buttons["reference_previous"].click()
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 1
    workspace.camera_cards[CameraId("CAM2")].click()
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 12
    assert source.path.read_bytes() == disk_before and not source.dirty
    assert workspace.draft.definition == geometry and not engine.calls
    assert not window.bridge.frames.snapshot().frames
    assert window.bridge.application.snapshot().counters == ProductionCounters()
    # The viewed position is workspace state, not persisted configuration.
    window.navigate(0)
    window.navigate(4)
    reopened = window.vision_workspace
    assert reopened is not None and reopened is not workspace
    until(lambda: not window.recipe_page.busy)
    assert reopened.reference.currentText().startswith("1/")


def test_runtime_reference_eligibility_frozen_refresh_and_folder_restore(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
    assert not workspace.buttons["vision_runtime"].isEnabled()
    workspace.set_reference_mode("runtime")
    assert workspace.reference_mode == "folder"
    workspace.buttons["reference_next"].click()
    until(lambda: not window.recipe_page.busy)
    publish(2, 80, key)
    assert workspace.reference_mode == "folder"  # No unsolicited pixel change.
    assert workspace.buttons["vision_runtime"].isEnabled()
    workspace.buttons["vision_runtime"].click()
    assert workspace.reference_mode == "runtime"
    first = workspace.frame
    assert first is not None and int(first.image[0, 0, 0]) == 80
    assert not workspace.buttons["reference_next"].isEnabled()
    publish(3, 90, key)
    assert workspace.frame is first
    workspace.buttons["roi_refresh"].click()
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 90
    workspace.choose_reference(1)
    until(lambda: not window.recipe_page.busy)
    assert int(workspace.frame.image[0, 0, 0]) == 2
    window.navigate(0)
    window.navigate(4)
    reopened = window.vision_workspace
    assert reopened is not None
    until(lambda: not window.recipe_page.busy)
    assert reopened.reference_mode == "runtime"  # New session prefers eligible runtime.
    assert reopened.frame is not None and int(reopened.frame.image[0, 0, 0]) == 90
    assert not engine.calls and not source.dirty and not reopened.draft.dirty
    assert window.bridge.application.snapshot().counters == ProductionCounters()


def test_removed_reference_refresh_and_explicit_corrupt_navigation(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, _ = camera_screen
    current = Path(workspace.reference.toolTip())
    current.write_bytes(b"broken")
    workspace.refresh_frame()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is None and window.event_label.isVisible()
    assert workspace.editable() and workspace.buttons["reference_next"].isEnabled()
    workspace.buttons[
        "reference_next"
    ].click()  # Explicit choice; no automatic substitution.
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 2
    chosen = Path(workspace.reference.toolTip())
    chosen.unlink()
    workspace.refresh_frame()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is None
    assert "no longer exists" in window.event_label.toolTip()
    assert workspace.editable() and not workspace.canvas.model.can_drag


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
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
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
    assert workspace.status.accessibleName() == text("unsaved", window.language)
    for field, value in zip(workspace.coordinates.values(), invalid, strict=True):
        field.setText(value)
    workspace.buttons["roi_set_rect"].click()
    assert workspace.draft.camera(workspace.camera).slots == after
    assert window.event_label.isVisible() and window.event_label.text()
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_compact_icon_states_and_loop_click_preserve_configuration(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    language: Language,
) -> None:
    window, workspace, engine = camera_screen
    window.set_language(language)
    sources = workspace.sources
    assert sources is not None
    saved = sources.path.read_bytes()
    geometry = workspace.draft.definition
    assert workspace.loop.isChecked()
    assert (
        workspace.loop.text() == ""
        and text("source_loop_on", language) in workspace.loop.toolTip()
    )
    assert text("source_loop", language) in workspace.loop.accessibleName()
    workspace.loop.click()
    assert not workspace.loop.isChecked() and not sources.definition.loop
    assert (
        workspace.loop.text() == ""
        and text("source_loop_off", language) in workspace.loop.toolTip()
    )
    assert sources.dirty and sources.path.read_bytes() == saved
    workspace.loop.click()
    assert sources.definition.loop and not sources.dirty
    assert not workspace.loop.icon().isNull()
    for key in ("vision_import", "vision_runtime"):
        button = workspace.buttons[key]
        assert button.text() == "" and not button.icon().isNull()
        assert button.accessibleName() == text(key, language)
    for key in ("roi_common", "roi_distribute"):
        button = workspace.buttons[key]
        assert button.text() == "" and not button.icon().isNull()
        assert button.accessibleName() == button.toolTip() == text(key, language)
    numeric_apply = workspace.buttons["roi_set_rect"]
    assert numeric_apply.text() == "" and not numeric_apply.icon().isNull()
    assert numeric_apply.width() == numeric_apply.height() == Style.touch
    assert (
        numeric_apply.accessibleName()
        == numeric_apply.toolTip()
        == text("roi_set_rect", language)
    )
    assert workspace.buttons["save_apply"].text() == text("save_apply", language)
    assert workspace.draft.definition == geometry
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


def test_inline_folder_edit_and_browse_do_not_select_or_persist(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None
    for i in range(1, 7):
        sources.set_serial(CameraId(f"CAM{i}"), f"100{i}")
    sources.set_mode("folder")
    sources.save()
    workspace.refresh()
    QApplication.processEvents()
    saved, geometry = sources.path.read_bytes(), workspace.draft.definition
    snapshot = window.bridge.application.snapshot()
    frame = workspace.frame
    workspace.select_roi(2)
    workspace.canvas.zoom(2)
    target = CameraId("CAM3")
    card = workspace.camera_cards[target]
    assert card.folder.isVisible() and card.browse.isVisible()
    assert not card.serial_row.isVisible()
    assert card.folder.toolTip() == card.folder.text()
    folder = tmp_path / "inline-reference"
    folder.mkdir()
    write_image(folder / "1.png", 77)
    QTest.mouseClick(card.folder, Qt.MouseButton.LeftButton)
    card.folder.selectAll()
    QTest.keyClicks(card.folder, str(folder))
    assert sources.definition.camera(target).folder == str(folder)
    assert workspace.camera == CameraId("CAM1") and not card.isChecked()
    assert workspace.frame is frame and workspace.selected_index == 2
    assert workspace.canvas.model.scale == 2
    entered: list[str] = []

    def browse(*args: object) -> str:
        entered.append(str(args[2]))
        return str(folder)

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", browse)
    card.browse.click()
    assert entered == [str(folder)] and workspace.camera == CameraId("CAM1")
    assert workspace.frame is frame and sources.path.read_bytes() == saved
    card.click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.camera == target and card.isChecked()
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 77
    previous = sources.definition

    def cancel(*_args: object) -> str:
        return ""

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", cancel)
    card.browse.click()
    assert sources.definition == previous  # Cancel is not a source edit.
    assert workspace.draft.definition == geometry and not workspace.draft.dirty
    assert sources.path.read_bytes() == saved
    assert window.bridge.application.snapshot() == snapshot and not engine.calls


def test_scanned_mapping_prevents_duplicates_and_saves_existing_draft(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    device_inventory: tuple[BaslerDevice, ...],
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None and workspace.draft.path is not None
    saved, machine = sources.path.read_bytes(), workspace.draft.path.read_bytes()
    geometry = workspace.draft.definition
    folders_before = tuple(c.folder for c in sources.definition.cameras)
    workspace.source_modes["basler"].click()
    workspace.discover.click()
    until(lambda: not window.recipe_page.busy)
    for i, card in enumerate(workspace.camera_cards.values(), 1):
        assert not card.device.isEditable() and card.device.lineEdit() is None
        index = card.device.findData(f"100{i}")
        card.device.setCurrentIndex(index)
        card.device.activated.emit(index)
    assert workspace.camera == CameraId("CAM1")
    card = workspace.camera_cards[CameraId("CAM6")]
    duplicate = card.device.findData("1001")
    assert not (
        card.device.model().flags(card.device.model().index(duplicate, 0))
        & Qt.ItemFlag.ItemIsEnabled
    )
    before = sources.definition
    for invalid in (-1, card.device.count()):
        card.device.activated.emit(invalid)
        assert sources.definition == before
    card.device.setCurrentIndex(duplicate)
    card.device.activated.emit(
        duplicate
    )  # Even a synthetic activation cannot bypass validation.
    assert sources.definition == before
    assert card.device.currentData() == "1006"
    assert window.event_label.text() == text("source_duplicate", window.language)
    assert sources.path.read_bytes() == saved
    # Explicit unassignment can be saved, but cannot be prepared or connected.
    card.device.setCurrentIndex(0)
    card.device.activated.emit(0)
    other = workspace.camera_cards[CameraId("CAM5")].device
    assert (
        other.model().flags(other.model().index(other.findData("1006"), 0))
        & Qt.ItemFlag.ItemIsEnabled
    )
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert "six distinct" in window.event_label.toolTip()
    assert not sources.dirty
    assert (
        load_camera_sources(sources.path).camera(CameraId("CAM6")).basler_serial == ""
    )
    assert workspace.draft.path.read_bytes() == machine
    index = card.device.findData("1006")
    card.device.setCurrentIndex(index)
    card.device.activated.emit(index)
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    workspace.source_modes["basler"].click()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    loaded = load_camera_sources(sources.path)
    assert loaded.basler_serials(geometry.layout.camera_ids) == tuple(
        f"100{i}" for i in range(1, 7)
    )
    assert tuple(c.folder for c in loaded.cameras) == folders_before
    assert not sources.dirty and workspace.draft.definition == geometry
    assert window.bridge.application.snapshot().active is None and not engine.calls


@pytest.mark.parametrize("saved_fps", [3.141592653589793, 20.0, 0.5])
def test_direct_loop_fps_preserves_saved_value_until_explicit_selection(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    saved_fps: float,
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None
    sources.definition = replace(sources.definition, target_fps=saved_fps)
    sources.save()
    workspace.refresh()
    saved, geometry = sources.path.read_bytes(), workspace.draft.definition
    assert workspace.loop.isVisible() and workspace.fps.isVisible()
    assert workspace.fps.text() == str(saved_fps)
    assert not workspace.fps_decrease.isEnabled()
    assert not workspace.fps_increase.isEnabled()
    workspace.fps.editingFinished.emit()  # Untouched saved focus/blur is not an edit.
    assert not sources.dirty
    workspace.refresh()
    window.set_language("en")
    window.set_theme("light")
    assert not sources.dirty and sources.path.read_bytes() == saved
    workspace.loop.click()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert load_camera_sources(sources.path).target_fps == saved_fps
    assert not load_camera_sources(sources.path).loop
    for invalid in ("0", "11", "3.5", "1e1", "", "abc"):
        workspace.fps.setText(invalid)
        workspace.fps_changed()
        assert sources.definition.target_fps == saved_fps and not sources.dirty
    workspace.fps.setText("10")
    workspace.fps.editingFinished.emit()
    assert workspace.fps_decrease.isEnabled() and not workspace.fps_increase.isEnabled()
    workspace.fps_decrease.click()
    assert sources.definition.target_fps == 9
    workspace.fps_increase.click()
    assert sources.definition.target_fps == 10 and sources.dirty
    assert load_camera_sources(sources.path).target_fps == saved_fps
    workspace.source_modes["basler"].click()
    assert not workspace.fps.isVisible() and not workspace.fps.isEnabled()
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert load_camera_sources(sources.path).target_fps == 10
    assert (
        not sources.dirty
        and workspace.draft.definition == geometry
        and not engine.calls
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_source_status_uses_saved_mode_and_actual_matching_connections(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    window, workspace, engine = camera_screen
    window.set_language(language)
    sources = workspace.sources
    connection = window.bridge.source_connection
    assert sources is not None and connection is not None
    before = window.bridge.application.snapshot()
    window.event_frame.state.setToolTip("stale source status")
    window.event_frame.state_icon.setToolTip("stale source error")
    window.bridge.timer.timeout.emit()
    assert window.event_frame.state.accessibleName() == text(
        "source_folder_status", language
    )
    assert "/6" not in window.event_frame.state.accessibleName()
    assert not window.event_frame.state.toolTip()
    assert not window.event_frame.state_icon.toolTip()
    workspace.source_modes["basler"].click()
    assert sources.dirty
    assert window.event_frame.state.accessibleName() == text(
        "source_folder_status", language
    )
    sources.definition = replace(
        sources.definition,
        cameras=tuple(
            replace(c, basler_serial=f"100{i}")
            for i, c in enumerate(sources.definition.cameras)
        ),
    )
    sources.save()
    snapshot = connection.source_snapshot()
    observed = [snapshot]
    monkeypatch.setattr(connection, "source_snapshot", lambda: observed[0])
    window.bridge.timer.timeout.emit()
    assert window.event_frame.state.accessibleName() == "0/6"
    connected = tuple(
        ConnectedCamera(c.camera_id, c.basler_serial, 60, 20)
        for c in sources.saved.cameras
    )
    observed[0] = replace(snapshot, state=ConnectionState.CONNECTED, cameras=connected)
    window.bridge.timer.timeout.emit()
    assert window.event_frame.state.accessibleName() == "6/6"
    assert not window.event_frame.state.toolTip()
    assert observed[0].state is ConnectionState.CONNECTED
    # A stale device from another mapping is not counted as this configured source.
    observed[0] = replace(
        observed[0], cameras=(*connected[:-1], replace(connected[-1], serial="99999"))
    )
    window.bridge.timer.timeout.emit()
    assert window.event_frame.state.accessibleName() == "5/6"
    for state in (
        ApplicationState.UNCONFIGURED,
        ApplicationState.STARTING,
        ApplicationState.STOPPING,
        ApplicationState.FAULT,
    ):
        window.bridge.snapshot_changed.emit(replace(before, state=state))
        assert window.event_frame.state.accessibleName() == window.state_label.text()
    window.bridge.snapshot_changed.emit(before)
    window.bridge.command_failed.emit("sources", ValueError("source diagnostic"))
    assert window.event_frame.state.accessibleName() == window.state_label.text()
    assert window.event_label.accessibleName()
    assert window.bridge.application.snapshot() == before and not engine.calls
    # This test changes observations, never opens a camera or starts Production.
    observed[0] = snapshot


def test_fps_stepper_boundaries_and_keyboard_entry(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    sources = workspace.sources
    assert sources is not None
    saved = sources.path.read_bytes()
    workspace.fps.setFocus()
    workspace.fps.selectAll()
    QTest.keyClicks(workspace.fps, "1")
    QTest.keyClick(workspace.fps, Qt.Key.Key_Return)
    assert sources.definition.target_fps == 1
    assert not workspace.fps_decrease.isEnabled() and workspace.fps_increase.isEnabled()
    workspace.fps_increase.click()
    assert sources.definition.target_fps == 2
    workspace.fps_decrease.click()
    assert sources.definition.target_fps == 1
    assert sources.path.read_bytes() == saved and sources.dirty and not engine.calls
    assert workspace.status.text() == "" and workspace.status.pixmap() is not None
    assert workspace.status.accessibleName() == text("unsaved", window.language)
    assert workspace.buttons["vision_discard"].isEnabled()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_roi_toolbar_wraps_without_shrinking_fields(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    language: Language,
) -> None:
    window, workspace, engine = camera_screen
    window.set_language(language)
    QApplication.processEvents()
    restored_width = workspace.width()
    toolbar = workspace.geometry_toolbar
    numeric = workspace.buttons["roi_set_rect"]
    bulk = workspace.buttons["roi_common"]
    widths = [field.width() for field in workspace.coordinates.values()]
    before = workspace.draft.definition
    save = workspace.buttons["save_apply"]
    upper = workspace.reference_toolbar
    reference = workspace.buttons["vision_import"]
    assert numeric.mapTo(toolbar, QPoint()).y() == bulk.mapTo(toolbar, QPoint()).y()
    assert save.mapTo(toolbar, QPoint()).y() == numeric.mapTo(toolbar, QPoint()).y()
    assert upper.height() <= Style.touch + Style.small
    narrow = toolbar.minimumSizeHint().width() + Style.small
    workspace.setFixedWidth(narrow + workspace.width() - toolbar.width())
    QApplication.processEvents()
    assert bulk.mapTo(toolbar, QPoint()).y() > numeric.mapTo(toolbar, QPoint()).y()
    assert toolbar.height() >= numeric.height() + bulk.height()
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
    workspace.setFixedWidth(restored_width)
    QApplication.processEvents()
    assert numeric.mapTo(toolbar, QPoint()).y() == bulk.mapTo(toolbar, QPoint()).y()
    assert save.mapTo(toolbar, QPoint()).y() == numeric.mapTo(toolbar, QPoint()).y()
    assert upper.height() <= Style.touch + Style.small
    upper.setFixedWidth(upper.minimumSizeHint().width() + Style.small)
    QApplication.processEvents()
    fit, actual = workspace.buttons["roi_fit"], workspace.buttons["roi_actual"]
    assert actual.mapTo(upper, QPoint()).y() > reference.mapTo(upper, QPoint()).y()
    assert fit.mapTo(upper, QPoint()).y() == actual.mapTo(upper, QPoint()).y()
    assert actual.mapTo(upper, actual.rect().topRight()).x() == upper.width() - 1
    for control in upper_controls:
        assert upper.rect().contains(control.mapTo(upper, QPoint()))
        assert upper.rect().contains(control.mapTo(upper, control.rect().bottomRight()))
    upper.setFixedWidth(1000)
    QApplication.processEvents()
    assert upper.height() <= Style.touch + Style.small
    assert workspace.draft.definition == before and not engine.calls


def test_basler_mode_draft_reference_identity_and_explicit_save(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    device_inventory: tuple[BaslerDevice, ...],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    initial = source.path.read_bytes()
    geometry = workspace.draft.definition
    folders_before = tuple(c.folder for c in source.definition.cameras)
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(1), CameraId("CAM1"), 0),
            1,
            np.full((20, 60, 3), 99, dtype=np.uint8),
        ),
        generation=1,
        source_key="basler:1001",
    )
    window.bridge.refresh()
    assert not workspace.buttons["vision_runtime"].isEnabled()
    workspace.source_modes["basler"].click()
    workspace.discover.click()
    until(lambda: not window.recipe_page.busy)
    for i in range(1, 7):
        card = workspace.camera_cards[CameraId(f"CAM{i}")]
        index = card.device.findData(f"100{i}")
        card.device.setCurrentIndex(index)
        card.device.activated.emit(index)
    assert source.definition.mode == "basler"
    assert workspace.source_modes["basler"].isChecked()
    assert [c.detail for c in workspace.camera_cards.values()] == [
        f"100{i}" for i in range(1, 7)
    ]
    assert not any(c.toolTip() for c in workspace.camera_cards.values())
    assert (
        text("source_disconnected", window.language)
        in workspace.connection_status.accessibleName()
    )
    assert not workspace.loop.isEnabled() and not workspace.folder.isVisible()
    assert (
        workspace.frame is not None
        and int(workspace.frame.image[0, 0, 0]) == 99
        and not engine.calls
    )
    assert workspace.buttons["vision_runtime"].isEnabled()
    workspace.set_reference_mode("runtime")
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 99
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    assert source.definition.mode == "folder" and workspace.loop.isEnabled()
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 1
    assert not workspace.buttons["vision_runtime"].isEnabled()
    workspace.source_modes["basler"].click()
    assert tuple(c.folder for c in source.definition.cameras) == folders_before
    assert source.path.read_bytes() == initial and source.dirty
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not source.dirty and load_camera_sources(source.path).mode == "basler"
    assert source.saved.basler_serials(
        workspace.draft.definition.layout.camera_ids
    ) == tuple(f"100{i}" for i in range(1, 7))
    assert workspace.draft.definition == geometry
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )
    # Discard restores both configurations, not just the current source mode.
    workspace.select_device(CameraId("CAM1"), 0)
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    window.navigate(0)
    box = workspace.findChildren(QMessageBox)[0]
    next(
        b for b in box.buttons() if b.text() == text("discard", window.language)
    ).click()
    until(lambda: window.vision_workspace is None)
    assert source.definition == source.saved and source.saved.mode == "basler"
    assert source.saved.camera(CameraId("CAM1")).basler_serial == "1001"


def test_incomplete_basler_mapping_save_persists_without_applying(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None and workspace.draft.path is not None
    initial_source, initial_machine = (
        source.path.read_bytes(),
        workspace.draft.path.read_bytes(),
    )
    candidate = window.bridge.application.snapshot().candidate
    workspace.source_modes["basler"].click()
    assert source.definition.mode == "basler" and source.saved.mode == "folder"
    assert source.path.read_bytes() == initial_source
    assert workspace.draft.definition == workspace.draft.saved
    assert window.bridge.application.snapshot().candidate == candidate
    assert not window.buttons["operation"].isEnabled()
    assert all(
        card.detail == text("source_unconfigured", window.language)
        for card in workspace.camera_cards.values()
    )
    workspace.refresh()
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert source.path.read_bytes() != initial_source
    assert load_camera_sources(source.path).mode == "basler"
    assert workspace.draft.path.read_bytes() == initial_machine
    assert not source.dirty and workspace.status.accessibleName() == text(
        "setup_failed", window.language
    )
    assert "six distinct" in window.event_label.toolTip()
    assert window.bridge.application.snapshot().candidate == candidate
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


def test_inline_discovery_mapping_validation_and_explicit_save(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    device_inventory: tuple[BaslerDevice, ...],
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    original = source.path.read_bytes()
    geometry = workspace.draft.definition
    workspace.source_modes["basler"].click()
    assert workspace.frame is None
    workspace.discover.click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.camera == CameraId("CAM1")
    assert not engine.calls
    assert not workspace.feedback and not window.event_label.text()
    assert workspace.discover.toolTip() == text("source_discover", window.language)
    assert all(
        not c.basler_serial for c in source.definition.cameras
    )  # Discovery never auto-assigns.
    for i, (camera, card) in enumerate(workspace.camera_cards.items(), 1):
        assert not card.folder.isVisible() and card.device.isVisible()
        assert not card.device.isEditable() and card.device.count() == 7
        index = card.device.findData(f"100{i}")
        assert "acA1920-test" in card.device.itemText(index)
        card.device.setCurrentIndex(index)
        card.device.activated.emit(index)
        assert source.definition.camera(camera).basler_serial == f"100{i}"
    assert workspace.camera == CameraId("CAM1") and source.path.read_bytes() == original
    workspace.save_and_apply()
    until(lambda: not window.recipe_page.busy)
    assert not source.dirty and source.saved.mode == "basler"
    assert source.saved.basler_serials(
        workspace.draft.definition.layout.camera_ids
    ) == tuple(f"100{i}" for i in range(1, 7))
    assert workspace.draft.definition == geometry
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


@pytest.mark.parametrize("sdk_failure", [False, True])
def test_inline_discovery_retains_offline_assignments_and_errors(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
    sdk_failure: bool,
) -> None:
    window, workspace, engine = camera_screen
    source = workspace.sources
    assert source is not None
    for i in range(1, 7):
        source.set_serial(CameraId(f"CAM{i}"), f"100{i}")
    workspace.source_modes["basler"].click()
    source.save()
    before, payload = source.definition, source.path.read_bytes()

    def discover() -> tuple[BaslerDevice, ...]:
        if sdk_failure:
            raise ModuleNotFoundError("pypylon unavailable for test")
        return (BaslerDevice("1001", "test model"),)

    monkeypatch.setattr(vision_page, "discover_basler_devices", discover)
    workspace.discover.click()
    until(lambda: not window.recipe_page.busy)
    assert source.definition == before and source.path.read_bytes() == payload
    assert workspace.camera_cards[CameraId("CAM3")].device.currentData() == "1003"
    assert not source.dirty
    if sdk_failure:
        assert window.event_label.accessibleName() == text(
            "source_discovery_failed", window.language
        )
        assert "ModuleNotFoundError" in window.event_label.toolTip()
    else:
        card = workspace.camera_cards[CameraId("CAM3")]
        assert text("source_not_discovered", window.language) in card.device.itemText(
            card.device.findData("1003")
        )
    assert workspace.camera_cards[CameraId("CAM3")].device.currentData() == "1003"
    assert not engine.calls


def test_vision_cards_reflow_by_usable_width_without_changing_pixels_or_identity(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    order = tuple(workspace.camera_cards)
    frame = workspace.frame
    assert frame is not None and workspace.sources is not None
    before = workspace.sources.definition, workspace.draft.definition
    workspace.select_roi(3)
    workspace.canvas.zoom(2)
    heights: dict[int, int] = {}
    for size, columns in (
        ((1080, 1920), 2),
        ((1920, 1080), 3),
        ((1300, 1920), 3),
        ((1080, 1920), 2),
    ):
        window.resize(*size)
        QApplication.processEvents()
        assert workspace.source_cards.columns == columns
        heights[columns] = workspace.source_cards.height()
        assert tuple(workspace.camera_cards) == order
        for i, card in enumerate(workspace.camera_cards.values()):
            item = workspace.source_cards.grid.itemAtPosition(i // columns, i % columns)
            assert item is not None and item.widget() is card
        assert workspace.frame is frame and workspace.selected_index == 3
        assert workspace.canvas.model.scale == 2
        assert workspace.camera == CameraId("CAM1")
        assert (
            0
            <= workspace.geometry_toolbar.y() - workspace.canvas.geometry().bottom()
            <= Style.small
        )
    assert heights[3] < heights[2]
    # Actual widget width controls reflow even inside a wide/landscape shell.
    window.resize(1920, 1080)
    workspace.source_cards.setFixedWidth(1000)
    QApplication.processEvents()
    assert workspace.source_cards.columns == 2
    assert (workspace.sources.definition, workspace.draft.definition) == before
    assert not engine.calls


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_unified_source_thumb_and_right_aligned_context(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    language: Language,
    theme: Theme,
) -> None:
    window, workspace, engine = camera_screen
    assert workspace.sources is not None
    original = workspace.sources.path.read_bytes(), workspace.draft.definition
    window.set_language(language)
    window.set_theme(theme)
    for size in ((1080, 1920), (1920, 1080)):
        window.resize(*size)
        QApplication.processEvents()
        source, roi = workspace.source_area, workspace.roi_area
        assert source.geometry().bottom() < roi.y()
        assert workspace.canvas.parentWidget() is roi
        assert workspace.geometry_toolbar.parentWidget() is roi
        for mode in ("basler", "folder"):
            workspace.source_modes[mode].click()
            until(lambda: not window.recipe_page.busy)
            switch = workspace.mode_switch
            selected = workspace.source_modes[mode]
            other = workspace.source_modes["folder" if mode == "basler" else "basler"]
            assert selected.isChecked() and not other.isChecked()
            # The single parent paints the thumb; transparent child hit targets
            # retain distinct localized accessibility and keyboard activation.
            pixels = switch.grab().toImage()
            active = pixels.pixelColor(
                selected.x() + 8, selected.y() + selected.height() // 2
            )
            inactive = pixels.pixelColor(other.x() + 8, other.y() + other.height() // 2)
            assert active != inactive
            assert selected.accessibleName() and selected.toolTip()
            context = (
                workspace.playback_options if mode == "folder" else workspace.discover
            )
            assert (
                context.mapTo(source, context.rect().topRight()).x()
                >= source.width() - 12
            )
            assert (
                context.mapTo(source, QPoint()).x()
                > switch.mapTo(source, switch.rect().topRight()).x()
            )
            assert workspace.playback_options.isVisible() == (mode == "folder")
            assert workspace.discover.isVisible() == (mode == "basler")
            assert not workspace.feedback and not window.event_label.text()
    assert (workspace.sources.path.read_bytes(), workspace.draft.definition) == original
    assert not engine.calls


def test_no_image_schematic_is_not_camera_bounds_or_persisted_geometry(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, engine = camera_screen
    assert workspace.draft.path is not None
    saved = workspace.draft.path.read_bytes()
    geometry = workspace.draft.definition
    workspace.source_modes["basler"].click()
    until(lambda: not window.recipe_page.busy)
    canvas = workspace.canvas
    assert workspace.frame is None and canvas.model.size is None
    assert workspace.reference.isVisible()
    assert workspace.camera not in workspace.draft.bounds
    assert canvas.model.schematic_size is not None
    assert workspace.reference.currentText() == "ROI ≈"
    assert (
        text("vision_schematic", window.language)
        in workspace.reference.accessibleDescription()
    )
    assert workspace.reference.toolTip() == text("vision_schematic", window.language)
    assert canvas.model.transform(canvas.width(), canvas.height()) is not None
    assert not canvas.model.can_drag
    # A no-pixel schematic still supports selection/Fit/zoom, never ROI dragging.
    rect = canvas.rois[2].rect
    transform = canvas.model.transform(canvas.width(), canvas.height())
    assert transform is not None
    x, y, w, h = transform.rectangle(rect)
    QTest.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(round(x + w / 2), round(y + h / 2)),
    )
    assert workspace.selected_index == 2
    canvas.zoom(2)
    assert canvas.model.scale == 2 and not canvas.grab().isNull()
    workspace.buttons["roi_fit"].click()
    assert canvas.model.scale is None
    assert (
        workspace.draft.definition == geometry
        and workspace.draft.path.read_bytes() == saved
    )
    # Schematic edges are not input bounds: numeric editing may extend them.
    workspace.coordinates["X"].setText("999")
    workspace.apply_geometry()
    changed = workspace.draft.definition
    assert workspace.draft.camera(workspace.camera).slots[2].rect.x == 999
    assert canvas.model.size is None and workspace.camera not in workspace.draft.bounds
    assert workspace.draft.path.read_bytes() == saved
    workspace.source_modes["folder"].click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and canvas.model.schematic_size is None
    assert canvas.model.size == (60, 20)
    assert workspace.reference.isVisible()
    assert window.event_label.accessibleName() == text(
        "roi_geometry_error", window.language
    )
    assert workspace.draft.definition == changed  # Real bounds report, never repair.
    workspace.coordinates["X"].setText("998")
    workspace.apply_geometry()
    assert workspace.draft.definition == changed  # Atomic real-bounds rejection.
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


def test_vision_feedback_is_page_scoped_and_cannot_hide_application_fault(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
) -> None:
    window, workspace, _ = camera_screen
    workspace.coordinates["X"].setText("99999")
    workspace.apply_geometry()
    assert window.event_label.accessibleName() == text(
        "roi_geometry_error", window.language
    )
    assert "rejected" in window.event_label.toolTip()
    original = window.snapshot
    try:
        window.pages.setCurrentIndex(0)
        window.recipe_page.setup_changed.emit()
        assert window.event_label.accessibleName() != text(
            "roi_geometry_error", window.language
        )
        assert "rejected" not in window.event_label.toolTip()
        window.pages.setCurrentIndex(4)
        window.snapshot = replace(original, state=ApplicationState.FAULT)
        window.recipe_page.setup_changed.emit()
        assert window.event_label.accessibleName() == text(
            "runtime_error", window.language
        )
        assert "rejected" not in window.event_label.toolTip()
    finally:
        window.snapshot = original
        window.recipe_page.setup_changed.emit()
    assert window.event_label.accessibleName() == text(
        "roi_geometry_error", window.language
    )


def test_discovery_close_waits_for_actual_io_owner(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, _ = camera_screen
    entered, release = Event(), Event()
    before = {t.ident for t in threads()}

    def discover() -> tuple[BaslerDevice, ...]:
        entered.set()
        assert release.wait(WAIT)
        return ()

    monkeypatch.setattr(vision_page, "discover_basler_devices", discover)
    workspace.source_modes["basler"].click()
    workspace.discover.click()
    until(entered.is_set)
    try:
        assert not workspace.discover.isEnabled()
        window.close()
        assert window.recipe_page.busy and not window.bridge.closed
        # Discard the source-mode draft only; closing still waits for discovery.
    finally:
        release.set()
    until(lambda: not window.recipe_page.busy)
    box = workspace.findChildren(QMessageBox)
    if box:
        next(
            b for b in box[-1].buttons() if b.text() == text("discard", window.language)
        ).click()
    until(lambda: window.bridge.closed)
    assert window.bridge.application.join(0)
    assert not {t.ident for t in threads()} - before


def test_import_is_temporary_owned_bgr_and_preserves_geometry(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, engine = camera_screen
    path = tmp_path / "import.png"
    pixels = np.full((20, 60, 3), (11, 22, 33), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", pixels)
    assert ok
    encoded.tofile(path)
    geometry = workspace.draft.definition
    assert workspace.sources is not None
    source = workspace.sources.path.read_bytes()

    def choose(*_args: object) -> tuple[str, str]:
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)
    workspace.buttons["vision_import"].click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and workspace.reference_mode == "import"
    np.testing.assert_array_equal(workspace.frame.image, pixels)
    assert not workspace.frame.image.flags.writeable
    with pytest.raises(ValueError):
        workspace.frame.image.setflags(write=True)
    assert workspace.draft.definition == geometry and not workspace.draft.dirty
    assert workspace.sources.path.read_bytes() == source and not engine.calls
    assert workspace.reference.currentText().startswith(
        text("vision_imported", window.language)
    )
    assert workspace.reference.toolTip() == str(path)
    workspace.reference.activated.emit(1)
    until(lambda: not window.recipe_page.busy)
    assert workspace.reference_mode == "folder"


def _assert_folder_reference(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    language: Language,
    size: tuple[int, int],
    tmp_path: Path,
    runtime_cycle: int,
) -> None:
    window, workspace, engine = camera_screen
    window.resize(*size)
    window.set_language(language)
    QApplication.processEvents()
    assert workspace.isVisible() and workspace.parentWidget() is window.vision_page
    assert workspace.buttons["vision_import"].accessibleName() == text(
        "vision_import", language
    )
    assert workspace.canvas.height() > workspace.source_cards.height()
    window.grab()
    assert workspace.canvas.width() >= 600
    for control in (
        workspace.folder,
        workspace.browse,
        workspace.loop,
        *workspace.buttons.values(),
    ):
        assert workspace.rect().contains(control.mapTo(workspace, QPoint(0, 0)))
    assert list(workspace.camera_cards) == [CameraId(f"CAM{i}") for i in range(1, 7)]
    for index, card in enumerate(workspace.camera_cards.values()):
        name = text("camera_number", language).format(number=index + 1)
        assert card.title == str(index + 1) and name in card.accessibleName()
        assert f"CAM{index + 1}" in card.accessibleName()
        assert card.width() >= Style.touch and card.height() >= Style.touch
        assert card.isChecked() == (index == 0)
        assert not card.toolTip() and card.folder.toolTip() == card.detail
        if index % workspace.source_cards.columns:
            previous = list(workspace.camera_cards.values())[index - 1]
            assert card.y() == previous.y() and card.x() > previous.x()
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 1
    frozen = workspace.frame
    workspace.select_roi(2)
    workspace.canvas.zoom(2)
    workspace.camera_cards[workspace.camera].click()
    assert workspace.frame is frozen and workspace.selected_index == 2
    assert workspace.canvas.model.scale == 2
    workspace.camera_cards[CameraId("CAM3")].click()
    until(lambda: not window.recipe_page.busy)
    # The preceding language may have left an eligible Runtime frame. Restore
    # Folder viewing explicitly so this is the same 21-valued source scenario.
    workspace.set_reference_mode("folder")
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None and int(workspace.frame.image[0, 0, 0]) == 21
    old = workspace.frame
    source = workspace.sources
    assert source is not None
    key = source.definition.camera(workspace.camera).key(source.path.parent)
    assert window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(runtime_cycle), workspace.camera, 1),
            1,
            np.full((20, 60, 3), 88, dtype=np.uint8),
        ),
        generation=1,
        source_key=key,
    )
    window.bridge.refresh()
    QApplication.processEvents()
    assert workspace.frame is old
    workspace.buttons["vision_runtime"].click()
    assert workspace.frame is window.bridge.frames.snapshot().frames[0]
    assert int(workspace.frame.image[0, 0, 0]) == 88
    assert (
        not engine.calls
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )
    assert not source.dirty and not workspace.draft.dirty
    assert not workspace.frame.image.flags.writeable


def _reopen_camera_workspace(window: MainWindow) -> VisionPage:
    window.navigate(0)
    until(lambda: window.vision_workspace is None)
    window.navigate(4)
    until(lambda: not window.recipe_page.busy)
    workspace = window.vision_workspace
    assert workspace is not None
    return workspace


def _prepare_vision_image(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    mode: str,
    tmp_path: Path,
) -> None:
    window, workspace, _ = camera_screen
    sources = workspace.sources
    assert sources is not None
    for i in range(1, 7):
        sources.set_serial(CameraId(f"CAM{i}"), f"100{i}")
    sources.set_mode("basler" if mode == "basler" else "folder")
    sources.save()
    workspace.switch_camera()
    until(lambda: not window.recipe_page.busy)
    image = demo_images()[0]
    path = tmp_path / "canvas.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    encoded.tofile(path)
    workspace.load_image(path)
    until(lambda: not window.recipe_page.busy)
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


def _assert_source_layout(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    language: Language,
    size: tuple[int, int],
    mode: str,
    theme: Theme,
    tmp_path: Path,
) -> None:
    window, workspace, engine = camera_screen
    window.resize(*size)
    window.set_language(language)
    window.set_theme(theme)
    QApplication.processEvents()
    assert workspace.canvas.height() > workspace.source_cards.height()
    assert len(workspace.canvas.rois) == 6 and workspace.frame is not None
    assert workspace.source_modes[
        "basler" if mode == "basler" else "folder"
    ].isChecked()
    for control in (
        workspace.source_cards,
        *workspace.source_modes.values(),
        *workspace.coordinates.values(),
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
    assert not window.event_label.text()
    assert workspace.reference_toolbar.height() <= Style.touch + Style.small
    assert workspace.geometry_toolbar.height() == Style.touch
    save = workspace.buttons["save_apply"]
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
        if size[0] == 1920 and center_control.isVisible():
            center = (
                center_control
                if center_control is workspace.reference
                else center_control.parentWidget()
            )
            assert center is not None
            assert (
                abs(
                    center.mapTo(toolbar, center.rect().center()).x()
                    - toolbar.rect().center().x()
                )
                <= 1
            )
    assert (
        workspace.camera_cards[workspace.camera]
        .rect()
        .contains(workspace.folder.geometry())
    )
    assert (
        workspace.reference_toolbar.mapTo(workspace, QPoint()).y()
        > workspace.source_cards.geometry().bottom()
    )
    assert workspace.canvas.y() > workspace.reference_toolbar.geometry().bottom()
    window.grab()
    assert not engine.calls and not workspace.draft.dirty


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
@pytest.mark.parametrize("mode", ["folder", "basler"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_source_layout_reference_and_frozen_refresh(
    camera_screen: tuple[MainWindow, VisionPage, ControlledEngine],
    size: tuple[int, int],
    mode: str,
    theme: Theme,
    tmp_path: Path,
) -> None:
    window, workspace, engine = camera_screen
    languages: tuple[Language, Language] = ("zh", "en")
    # Preserve the original four Folder-reference rows once, before the imported
    # image/geometry stage, rather than repeating them for every theme/source.
    if mode == "folder" and theme == "dark":
        for runtime_cycle, language in enumerate(languages, 1):
            try:
                _assert_folder_reference(
                    (window, workspace, engine), language, size, tmp_path, runtime_cycle
                )
                workspace = _reopen_camera_workspace(window)
            except Exception as exc:
                exc.add_note(f"Folder reference: size={size}, language={language}")
                raise
    current = (window, workspace, engine)
    _prepare_vision_image(current, mode, tmp_path)
    frozen = workspace.frame
    assert frozen is not None
    sources = workspace.sources
    assert sources is not None and workspace.draft.path is not None
    saved = sources.path.read_bytes(), workspace.draft.path.read_bytes()
    before = window.bridge.application.snapshot()
    for language in languages:
        try:
            workspace.select_roi(0)
            workspace.canvas.zoom(None)
            _assert_source_layout(current, language, size, mode, theme, tmp_path)
            assert workspace.frame is frozen
            assert sources.path.read_bytes() == saved[0]
            assert workspace.draft.path.read_bytes() == saved[1]
            assert window.bridge.application.snapshot() == before
        except Exception as exc:
            exc.add_note(
                f"Vision layout: size={size}, mode={mode}, theme={theme}, language={language}"
            )
            raise
