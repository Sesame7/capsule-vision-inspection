"""Independent source/inspection lifetimes using the real scheduler and fake SDK."""

import sys
from collections import deque
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event, Thread, get_ident
from threading import enumerate as threads
from time import perf_counter

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.application_support import WAIT
from tests.bootstrap_support import saved_data
from tests.cycle_support import image_result
from tests.production_support import inferred
from tests.qt_support import close_window, until
from tests.reporting_support import seed, server

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
    _basler_sdk,
)
from vimo_demo.application.basler import (
    AcquisitionMetadata,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
    CameraSettings,
)
from vimo_demo.application.basler_connection import (
    BaslerConnection,
    CameraSnapshot,
    ConnectionState,
    SourceConnectionCommands,
)
from vimo_demo.application.basler_control import BaslerControlSnapshot
from vimo_demo.application.image_archive import (
    ArchiveImage,
    ImageArchive,
    ImageArchiveCommands,
)
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ProductionReportingCommands,
    ReportingSelection,
    ReportStage,
)
from vimo_demo.configuration import (
    CameraFolder,
    ImageArchiveSettings,
    load_camera_sources,
    load_recipe,
    save_camera_sources,
    save_recipe,
)
from vimo_demo.inference import BoundingRect, ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId
from vimo_demo.ui import local_production
from vimo_demo.ui.bootstrap import create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import Language, Theme, text

# Provisional field mapping in configured CAM1..CAM6 order; explicitly includes
# the CAM5/CAM6 order that differs from sorting serials. This is test data only.
SERIALS = ("25334173", "25334174", "25334178", "25334180", "25334188", "25334184")


@pytest.mark.parametrize("camera_count", [5, 6])
def test_normal_folder_configuration_cycles_and_six_vision_positions(
    qapp: QApplication, tmp_path: Path, camera_count: int
) -> None:
    data = saved_data(tmp_path, camera_count=camera_count)
    source = load_camera_sources(data.sources)
    rows: list[CameraFolder] = []
    for index, row in enumerate(source.cameras):
        folder = tmp_path / str(row.camera_id)
        folder.mkdir()
        ok, encoded = cv2.imencode(".png", np.full((20, 60, 3), 10 + index, np.uint8))
        assert ok
        encoded.tofile(folder / "only.png")
        rows.append(replace(row, folder=str(folder)))
    save_camera_sources(data.sources, replace(source, cameras=tuple(rows)))
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.resize(1080, 1600)
    window.show()
    try:
        assert app.snapshot().state is ApplicationState.READY
        assert len(window.cards) == camera_count
        window.navigate(4)
        until(lambda: not window.parameters.busy)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and len(workspace.camera_cards) == 6
        assert workspace.camera_cards[CameraId("CAM6")].isCheckable() == (
            camera_count == 6
        )
        window.navigate(0)
        app.start().result(WAIT)
        until(lambda: app.snapshot().state is ApplicationState.READY)
        result = app.snapshot().latest_production_result
        assert result is not None
        assert (
            len(result.cameras) == camera_count
            and len(result.channels) == camera_count * 6
        )
        assert [c.channel_id for c in result.channels] == list(
            range(1, camera_count * 6 + 1)
        )
        assert [value for value, _ in engine.calls][2:] == list(
            range(10, 10 + camera_count)
        )
        assert app.snapshot().counters.cycles_completed == 1
        assert app.snapshot().counters.business_empty == camera_count * 6
        assert (
            tuple(f.frame_id.camera_id for f in startup.frames.snapshot().frames)
            == result.layout.camera_ids
        )
        assert engine.closed and len({owner for _, owner in engine.calls}) == 1
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("save_empty", [False, True])
@pytest.mark.parametrize("camera_count", [5, 6])
def test_empty_archiving_uses_active_recipe_and_original_basler_frame(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    save_empty: bool,
    camera_count: int,
) -> None:
    data = saved_data(tmp_path, camera_count=camera_count)
    passive.serials = SERIALS[:camera_count]
    save_basler(data.sources)
    recipe_file = data.recipes / "local.recipe.json"
    root = tmp_path / "empty-archive"
    save_recipe(
        recipe_file,
        replace(
            load_recipe(recipe_file),
            image_archive=ImageArchiveSettings(
                str(root), save_ng=True, save_empty=save_empty
            ),
        ),
    )
    saved = recipe_file.read_bytes()
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands) and isinstance(
        app, ImageArchiveCommands
    )
    try:
        app.connect_source()
        app.start().result(WAIT)
        assert startup.editor is not None
        startup.editor.draft.image_archive.save_empty = not save_empty
        passive.expose(1, tuple(i for i in (4, 2, 5, 1, 3, 0) if i < camera_count))
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        app.stop().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        result = app.snapshot().latest_production_result
        assert result is not None
        assert (
            len(result.cameras) == camera_count
            and len(result.channels) == camera_count * 6
        )
        assert tuple(c.camera_id for c in result.cameras) == tuple(
            f"CAM{i}" for i in range(1, camera_count + 1)
        )
        assert all(
            c.frame_id is not None and c.frame_id.camera_id == c.camera_id
            for c in result.cameras
        )
        assert len(startup.frames.snapshot().frames) == camera_count
        assert app.snapshot().counters.business_empty == camera_count * 6
        observation = app.archive_snapshot()
        assert observation is not None and not observation.alive
        assert (
            observation.written == (camera_count if save_empty else 0)
            and observation.failed == 0
        )
        assert observation.pending == observation.retained_bytes == 0
        files = sorted(root.rglob("*.png"))
        assert len(files) == (camera_count if save_empty else 0)
        for path in files:
            assert path.name.startswith("3_") and path.name.endswith("_1.png")
            index = int(path.parent.name.removeprefix("CAM")) - 1
            decoded = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            np.testing.assert_array_equal(
                decoded, np.full((20, 60, 3), 10 + index, dtype=np.uint8)
            )
        assert recipe_file.read_bytes() == saved
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("scenario", ["idle", "startup_failure", "unfinished"])
def test_archive_zero_or_unfinished_frames_never_fabricate_images(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend, scenario: str
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    recipe_file = data.recipes / "local.recipe.json"
    root = tmp_path / "archive"
    save_recipe(
        recipe_file,
        replace(
            load_recipe(recipe_file),
            image_archive=ImageArchiveSettings(str(root), True, True, True),
        ),
    )
    engine = Engine()
    engine.release_warm.set()
    engine.fail_warmup = scenario == "startup_failure"
    if scenario == "unfinished":
        engine.release_active.clear()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert isinstance(app, ImageArchiveCommands)
    try:
        app.connect_source()
        if scenario == "startup_failure":
            with pytest.raises(ApplicationError):
                app.start().result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
        else:
            app.start().result(WAIT)
            if scenario == "unfinished":
                passive.expose(1, (0,))
                assert engine.active.wait(WAIT)
                stopped = app.stop()
                until(lambda: app.snapshot().counters.cycles_completed == 1)
                assert not stopped.done()
                engine.release_active.set()
                stopped.result(WAIT)
            else:
                app.stop().result(WAIT)
        observed = app.archive_snapshot()
        assert observed is not None and not observed.alive
        assert observed.retained_bytes == observed.pending == observed.written == 0
        assert not root.exists()
        assert app.snapshot().counters.cycles_completed == (
            1 if scenario == "unfinished" else 0
        )
    finally:
        engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("ending", ["stop", "close"])
@pytest.mark.parametrize("disk_failure", [False, True])
def test_archive_is_independent_of_accounting_and_reports_and_joined_on_shutdown(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    disk_failure: bool,
) -> None:
    from vimo_demo.application.image_archive import write_png

    data = saved_data(tmp_path)
    save_basler(data.sources)
    recipe_file = data.recipes / "local.recipe.json"
    root = tmp_path / "archive"
    recipe = load_recipe(recipe_file)
    save_recipe(
        recipe_file,
        replace(
            recipe, image_archive=ImageArchiveSettings(str(root), True, True, True)
        ),
    )
    entered, release = Event(), Event()
    owners: list[int] = []
    requests: list[ArchiveImage] = []

    def writer(directory: Path, item: ArchiveImage) -> None:
        owners.append(get_ident())
        requests.append(item)
        entered.set()
        assert release.wait(WAIT)
        if disk_failure:
            raise OSError("Simulated full disk")
        write_png(directory, item)

    def archiver(
        settings: ImageArchiveSettings, generation: int, cameras: tuple[CameraId, ...]
    ) -> ImageArchive:
        return ImageArchive(settings, generation, cameras, writer=writer)

    monkeypatch.setattr(local_production, "ImageArchive", archiver)

    class DefectEngine(Engine):
        def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
            base = super().infer(image_bgr)
            return replace(base, regions=inferred(BoundingRect(0, 0, 8, 10)).regions)

    engine = DefectEngine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert isinstance(app, ProductionReportingCommands)
    assert isinstance(app, ImageArchiveCommands)
    original = (
        recipe_file.read_bytes(),
        data.sources.read_bytes(),
        data.machine.read_bytes(),
    )
    initial_threads = {t.ident for t in threads()}
    with server() as peer:
        seed(peer, 0)
        app.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            app.connect_source()
            app.start().result(WAIT)
            assert app.snapshot().counters == ProductionCounters()
            assert not root.exists() and app.archive_snapshot() is not None
            # Source draft edits after successful activation cannot redirect recording.
            assert startup.editor is not None
            startup.editor.draft.image_archive.root_directory = str(
                tmp_path / "draft-only"
            )
            passive.expose(1, (5, 3, 1, 4, 2, 0))
            assert entered.wait(WAIT)
            until(lambda: app.snapshot().counters.cycles_completed == 1)
            until(
                lambda: (
                    (s := app.reporting_snapshot()) is not None
                    and s.acknowledged_count == 1
                )
            )
            result = app.snapshot().latest_production_result
            assert result is not None and len(result.channels) == 36
            accounting = app.snapshot().counters
            pending = app.stop() if ending == "stop" else app.close()
            assert not pending.done()  # Actual archive owner still writing.
            assert engine.closed or app.snapshot().state is ApplicationState.STOPPING
            release.set()
            pending.result(WAIT)
            observed = app.archive_snapshot()
            assert (
                observed is not None
                and not observed.alive
                and observed.retained_bytes == 0
            )
            assert observed.written + observed.failed == 6
            assert observed.failed == (6 if disk_failure else 0)
            assert app.snapshot().counters == accounting
            assert all(
                item.generation == 1
                and item.frame.id
                == result.cameras[
                    result.layout.camera_ids.index(item.frame.id.camera_id)
                ].frame_id
                for item in requests
            )
            assert len({item.frame.id for item in requests}) == 6
            assert (
                len(set(owners)) == 1
                and owners[0] != get_ident()
                and owners[0] not in {owner for _, owner in engine.calls}
            )
            for item in requests:
                index = result.layout.camera_ids.index(item.frame.id.camera_id)
                assert (
                    np.all(item.frame.image == 10 + index)
                    and not item.frame.image.flags.writeable
                )
            if not disk_failure:
                files = list(root.rglob("*.png"))
                assert len(files) == 6
                for file in files:
                    image = cv2.imdecode(
                        np.fromfile(file, dtype=np.uint8), cv2.IMREAD_COLOR
                    )
                    assert image is not None and image.shape == (20, 60, 3)
            assert not (tmp_path / "draft-only").exists()
            assert original == (
                recipe_file.read_bytes(),
                data.sources.read_bytes(),
                data.machine.read_bytes(),
            )
            assert not any(
                t.ident not in initial_threads and t.name.startswith("ImageArchive-")
                for t in threads()
            )
        finally:
            release.set()
            app.close().result(WAIT)
            assert app.join(WAIT)


class Engine:
    def __init__(self) -> None:
        self.warm, self.release_warm = Event(), Event()
        self.active, self.release_active = Event(), Event()
        self.release_active.set()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup = False
        self.created_on = get_ident()
        self.closed_on: int | None = None
        self.shapes: list[tuple[int, ...]] = []
        self.zeros: list[bool] = []

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        image = uint8_array(image_bgr)
        self.calls.append((int(image[0, 0, 0]), get_ident()))
        self.shapes.append(image.shape)
        self.zeros.append(not bool(np.any(image)))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("Warm-up failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
        assert not image.flags.writeable
        return image_result(image)

    def close(self) -> None:
        self.closed = True
        self.closed_on = get_ident()


def save_basler(path: Path) -> None:
    source = load_camera_sources(path)
    save_camera_sources(
        path,
        replace(
            source,
            mode="basler",
            cameras=tuple(
                replace(c, basler_serial=s)
                for c, s in zip(
                    source.cameras, SERIALS[: len(source.cameras)], strict=True
                )
            ),
        ),
    )


def test_normal_saved_source_switch_updates_controls_only_after_save_apply(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.bridge.controls_enabled and window.buttons["secondary"].isHidden()
        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and workspace.sources is not None
        until(lambda: not window.parameters.busy)
        for i in range(1, 7):
            workspace.sources.set_serial(CameraId(f"CAM{i}"), SERIALS[i - 1])
        workspace.source_modes["basler"].click()
        assert workspace.sources.saved.mode == "folder"  # Draft is not active.
        assert window.bridge.controls_enabled
        assert not window.buttons["operation"].isEnabled()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert workspace.sources.saved.mode == "basler"
        assert window.bridge.controls_enabled and window.buttons["secondary"].isHidden()
        assert (
            not engine.calls and not startup.application.source_snapshot().thread_alive
        )
        workspace.source_modes["folder"].click()
        until(lambda: not window.parameters.busy)
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert workspace.sources.saved.mode == "folder"
        assert window.bridge.controls_enabled and window.buttons["secondary"].isHidden()
        assert (
            workspace.sources.saved.camera(CameraId("CAM5")).basler_serial == SERIALS[4]
        )
        assert (
            not engine.calls and not startup.application.source_snapshot().thread_alive
        )
        window.navigate(0)
        assert window.buttons["secondary"].isHidden()
    finally:
        close_window(window)
        assert startup.application.join(0)
        delivery.detach()


def test_normal_folder_runtime_needs_no_pypylon(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pypylon", None)

    def no_hardware(
        serials: Sequence[str], *, timeout: float = 30
    ) -> BaslerAcquisitionSource:
        raise AssertionError("Folder runtime must not construct a hardware source")

    monkeypatch.setattr(_basler_sdk, "PylonBackend", no_hardware)
    data = saved_data(tmp_path)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    original = data.sources.read_bytes()
    try:
        startup.application.start().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        assert startup.application.snapshot().counters.cycles_completed > 0
        assert engine.closed and data.sources.read_bytes() == original
        assert startup.application.history is not None
        records = startup.application.history.query().result(WAIT).records
        assert len(records) == startup.application.snapshot().counters.cycles_completed
        assert all(len(record.channels) == 36 for record in records)
        assert "pypylon.pylon" not in sys.modules
    finally:
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_fixed_input_composition_overrides_saved_basler_without_false_status(
    qapp: QApplication, tmp_path: Path
) -> None:
    from vimo_demo.application.folder_replay import folder_images
    from vimo_demo.ui.production_window import ProductionWindow

    data = saved_data(tmp_path)
    save_basler(data.sources)
    original = data.sources.read_bytes()
    engine = Engine()
    engine.release_active.clear()
    composition = local_production.compose(
        data.models / "general.json",
        data.machine,
        data.recipes / "local.recipe.json",
        folder_images(tmp_path / "frames")[:6],
        sources_file=data.sources,
        engine_factory=lambda _: engine,
        replay_controls=True,
    )
    bridge = QtProductionBridge(
        composition.application,
        composition.store,
        poll_ms=5,
    )
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.show()
    try:
        bridge.start()
        until(engine.warm.is_set)
        assert "Basler" not in window.event_label.accessibleName()
        engine.release_warm.set()
        until(engine.active.is_set)
        assert (
            bridge.controls_enabled
            and not composition.application.source_snapshot().thread_alive
        )
        assert "Basler" not in window.event_label.accessibleName()
        assert data.sources.read_bytes() == original
    finally:
        engine.release_warm.set()
        engine.release_active.set()
        close_window(window)
        assert composition.application.join(0)


class PassiveBackend:
    """SDK FIFO fake; exercises the real source/control/session implementation."""

    def __init__(self) -> None:
        self.serials: tuple[str, ...] = SERIALS
        self.condition = Condition()
        self.queues = {
            s: deque[tuple[AcquisitionMetadata, NDArray[np.uint8]]]() for s in SERIALS
        }
        self.armed = Event()
        self.closed = False
        self.prepared: list[str] = []
        self.dimensions = {serial: (60, 20) for serial in SERIALS}
        self.fail_prepare = ""
        self.failure = self.unsafe = False
        self.owner_ids: list[int] = []
        self.cleanup_calls = 0
        self.cleanup_entered, self.release_cleanup = Event(), Event()
        self.release_cleanup.set()
        self.prepare_entered, self.release_prepare = Event(), Event()
        self.release_prepare.set()

    def inventory(self, serials: tuple[str, ...]) -> None:
        self.owner_ids.append(get_ident())
        assert serials == self.serials

    def prepare(self, serial: str) -> CameraSettings:
        self.prepared.append(serial)
        self.owner_ids.append(get_ident())
        self.prepare_entered.set()
        assert self.release_prepare.wait(WAIT)
        if serial == self.fail_prepare:
            raise RuntimeError("Injected partial Open failure")
        width, height = self.dimensions[serial]
        return CameraSettings(serial, {"Width": width, "Height": height}, {})

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.owner_ids.append(get_ident())
        self.armed.set()
        return tuple(
            CameraReadiness(s, True, True, perf_counter()) for s in self.serials
        )

    def available(self, serial: str, timeout_ms: int) -> bool:
        if self.failure:
            raise RuntimeError("Injected source loss")
        with self.condition:
            if timeout_ms:
                self.condition.wait_for(
                    lambda: bool(self.queues[serial]), timeout_ms / 1000
                )
            return bool(self.queues[serial])

    def retrieve(self, serial: str) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        self.owner_ids.append(get_ident())
        with self.condition:
            return self.queues[serial].popleft()

    def expose(self, ordinal: int, cameras: tuple[int, ...] = tuple(range(6))) -> None:
        with self.condition:
            for index in cameras:
                width, height = self.dimensions[SERIALS[index]]
                pixels = np.full(
                    (height, width, 3), ordinal * 10 + index, dtype=np.uint8
                )
                pixels.setflags(write=False)
                self.queues[SERIALS[index]].append(
                    (
                        AcquisitionMetadata(
                            SERIALS[index],
                            perf_counter(),
                            True,
                            0,
                            "",
                            width,
                            height,
                            1,
                            3600,
                            ordinal - 1,
                            ordinal,
                            ordinal * 1000,
                            0,
                        ),
                        pixels,
                    )
                )
            self.condition.notify_all()

    def cleanup(self) -> tuple[CameraCleanup, ...]:
        self.owner_ids.append(get_ident())
        self.cleanup_calls += 1
        self.cleanup_entered.set()
        assert self.release_cleanup.wait(WAIT)
        self.closed = not self.unsafe
        return tuple(
            CameraCleanup(s, True, not self.unsafe, not self.unsafe, not self.unsafe)
            for s in self.prepared
        )


@pytest.fixture
def passive(monkeypatch: pytest.MonkeyPatch) -> PassiveBackend:
    backend = PassiveBackend()

    def sdk(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", sdk)
    return backend


def without_folders(path: Path) -> None:
    source = load_camera_sources(path)
    save_camera_sources(
        path,
        replace(source, cameras=tuple(replace(c, folder="") for c in source.cameras)),
    )


def test_idle_capture_is_one_shot_owned_and_never_enters_production(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    engines: list[Engine] = []
    producer_pixels: list[NDArray[np.uint8]] = []
    retrieve = passive.retrieve

    def observe(serial: str) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        metadata, pixels = retrieve(serial)
        if serial == SERIALS[4]:
            producer_pixels.append(pixels)
        return metadata, pixels

    monkeypatch.setattr(passive, "retrieve", observe)

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    original = data.sources.read_bytes(), data.machine.read_bytes()
    assert isinstance(app, ProductionReportingCommands)
    try:
        app.connect_source()
        with pytest.raises(ValueError, match="Unknown"):
            app.capture_image(CameraId("unknown"))
        capture = app.capture_image(CameraId("CAM5"))
        with pytest.raises(RuntimeError, match="pending"):
            app.capture_image(CameraId("CAM1"))
        until(lambda: app.source_snapshot().capture_waiting)
        passive.expose(1, (2, 0))
        until(lambda: app.source_snapshot().received_frames == 2)
        assert not capture.done()
        passive.expose(1, (4,))
        snapshot = capture.result(WAIT)
        assert snapshot.camera_id == CameraId("CAM5")
        assert (
            snapshot.metadata.serial == SERIALS[4]
            and snapshot.metadata.image_number == 1
        )
        assert (
            snapshot.pixels.shape == (20, 60, 3) and snapshot.pixels.dtype == np.uint8
        )
        assert int(snapshot.pixels[0, 0, 0]) == 14
        assert not snapshot.pixels.flags.writeable
        with pytest.raises(ValueError):
            snapshot.pixels.setflags(write=True)
        passive.expose(1, (1, 3, 5))
        until(lambda: app.source_snapshot().discarded_frames == 6)
        # Idle source storage has no Frame/inference lease. Reusing that producer
        # storage after retrieval cannot change the independently owned snapshot.
        producer_pixels[0].setflags(write=True)
        producer_pixels[0].fill(255)
        assert int(snapshot.pixels[0, 0, 0]) == 14
        passive.expose(2)
        until(lambda: app.source_snapshot().discarded_frames == 12)
        assert int(snapshot.pixels[0, 0, 0]) == 14 and capture.result() is snapshot
        assert not engines and not startup.frames.snapshot().frames
        assert app.snapshot().counters == ProductionCounters()
        assert (
            app.snapshot().latest_production_result is None
            and app.reporting_snapshot() is None
        )
        assert (data.sources.read_bytes(), data.machine.read_bytes()) == original
        # Start cancels the idle request before warm-up and preserves later inspection.
        pending = app.capture_image(CameraId("CAM1"))
        app.start().result(WAIT)
        assert pending.cancelled()
        with pytest.raises(ApplicationStateError):
            app.capture_image(CameraId("CAM1"))
        passive.expose(3)
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        app.stop().result(WAIT)
        assert app.source_snapshot().state is ConnectionState.CONNECTED
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)
    assert passive.closed and not any(t.name == "BaslerSourceOwner" for t in threads())


def test_capture_timeout_cancel_and_busy_boundary_are_deterministic(
    qapp: QApplication,
    passive: PassiveBackend,
) -> None:
    now = [1.0]
    source = BaslerConnection(clock=lambda: now[0])
    source.begin_connect(
        tuple(CameraId(f"CAM{i}") for i in range(1, 7)), SERIALS, lambda _: None
    )
    source.wait_connected()
    try:
        for timeout in (0, -1, float("inf"), float("nan"), 121):
            with pytest.raises(ValueError):
                source.capture_image(CameraId("CAM1"), timeout=timeout)
        future = source.capture_image(CameraId("CAM1"), timeout=2)
        until(lambda: source.snapshot().capture_waiting)
        now[0] = 3.0
        with pytest.raises(TimeoutError):
            future.result(WAIT)
        cancelled = source.capture_image(CameraId("CAM2"))
        assert cancelled.cancel()
        pending = source.capture_image(CameraId("CAM3"))
        source.cancel_capture()
        assert pending.cancelled()
        # A partial old group is never reused as the "next" capture.
        passive.expose(1, (0,))
        until(lambda: source.snapshot().received_frames == 1)
        unsafe = source.capture_image(CameraId("CAM2"))
        with pytest.raises(ApplicationStateError, match="boundary"):
            unsafe.result(WAIT)
        passive.expose(1, (1, 2, 3, 4, 5))
        until(lambda: source.snapshot().received_frames == 6)
        pending = source.capture_image(CameraId("CAM1"))
        source.disconnect()
        assert pending.cancelled()
    finally:
        source.disconnect()
    assert passive.closed and not source.snapshot().thread_alive


@pytest.mark.parametrize(
    "action",
    ["success", "timeout", "cancel", "switch", "disconnect", "close", "start", "late"],
)
def test_vision_capture_cancellation_and_stale_delivery(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    engine = Engine()
    engine.release_warm.set()
    now = [1.0]
    connection = BaslerConnection(clock=lambda: now[0])
    monkeypatch.setattr(local_production, "BaslerConnection", lambda: connection)
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    captures: list[Future[CameraSnapshot]] = []
    original_capture = app.capture_image

    def capture(camera: CameraId, *, timeout: float = 10) -> Future[CameraSnapshot]:
        future = original_capture(camera, timeout=timeout)
        captures.append(future)
        return future

    monkeypatch.setattr(app, "capture_image", capture)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    window.navigate(4)
    workspace = window.parameters.roi_workspace
    assert workspace is not None
    try:
        workspace.connect_source.click()
        until(lambda: not window.parameters.busy)
        assert not workspace.discover.isEnabled()
        assert all(
            not c.device.isEnabled() and not c.folder.isVisible()
            for c in workspace.camera_cards.values()
        )
        workspace.capture.click()
        pending = captures[-1]
        assert pending is not None and not workspace.capture.isEnabled()
        assert workspace.cancel_capture_button.isVisible()
        assert (
            text("vision_capture_waiting", window.language)
            in workspace.capture.toolTip()
        )
        until(lambda: app.source_snapshot().capture_waiting)
        geometry = workspace.draft.definition
        if action == "success":
            passive.expose(1)
            until(lambda: workspace.frame is not None)
            assert workspace.frame is not None
            assert workspace.frame.frame_id.camera_id == CameraId("CAM1")
            assert int(workspace.frame.image[0, 0, 0]) == 10
            assert (
                text("vision_capture_success", window.language)
                in workspace.capture.toolTip()
            )
        elif action == "timeout":
            now[0] = 11.0
            until(
                lambda: (
                    window.event_label.accessibleName()
                    == text("vision_capture_timeout", window.language)
                )
            )
            assert window.event_label.accessibleName() == text(
                "vision_capture_timeout", window.language
            )
        elif action == "cancel":
            workspace.cancel_capture_button.click()
            assert pending.cancelled()
            assert (
                text("vision_capture_cancelled", window.language)
                in workspace.capture.toolTip()
            )
        elif action == "late":
            # Complete without pumping Qt, then change selection before queued delivery.
            passive.expose(1)
            pending.result(WAIT)
            workspace.camera_cards[CameraId("CAM3")].click()
        elif action == "switch":
            workspace.camera_cards[CameraId("CAM3")].click()
            assert pending.cancelled()
            passive.expose(1)
        elif action == "disconnect":
            workspace.disconnect_source.click()
            until(lambda: not window.parameters.busy)
            assert pending.cancelled() and passive.closed
        elif action == "start":
            app.start().result(WAIT)
            assert pending.cancelled()
            app.stop().result(WAIT)
        else:
            window.close()
            until(lambda: window.bridge.closed)
            assert pending.cancelled() and passive.closed
        QApplication.processEvents()
        assert (workspace.frame is not None) == (action == "success")
        assert workspace.draft.definition == geometry
        assert (
            app.snapshot().counters == ProductionCounters()
            and not startup.frames.snapshot().frames
        )
        if action == "success":
            workspace.disconnect_source.click()
            until(lambda: not window.parameters.busy)
            workspace.source_modes["folder"].click()
            until(lambda: not window.parameters.busy)
            assert (
                text("vision_capture_success", window.language)
                not in workspace.capture.toolTip()
            )
            assert not workspace.capture.isVisible()
            assert workspace.sources is not None
            workspace.sources.discard()
        assert app.history is not None
        assert app.history.snapshot().accepted == app.history.snapshot().persisted == 0
    finally:
        close_window(window)
        assert app.join(WAIT)
        delivery.detach()


def test_synthetic_warmup_uses_actual_distinct_shapes_on_single_engine_owner(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    passive.dimensions[SERIALS[4]] = 64, 24
    engines: list[Engine] = []

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        app.start().result(WAIT)
        engine = engines[0]
        assert engine.shapes == [(20, 60, 3)] * 2 + [(24, 64, 3)] * 2
        assert all(engine.zeros)
        assert app.history is not None and not app.history.query().result(WAIT).records
        assert (
            app.snapshot().counters == ProductionCounters()
            and not startup.frames.snapshot().frames
        )
        app.stop().result(WAIT)
        assert {
            engine.created_on,
            engine.closed_on,
            *[owner for _, owner in engine.calls],
        } == {engine.created_on}
        assert engine.created_on != get_ident() and not passive.closed
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_source_lifetime_idle_discard_restart_recipe_and_batch(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    engines: list[Engine] = []

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    original = data.sources.read_bytes(), data.machine.read_bytes()
    try:
        assert not startup.issues and not passive.prepared and not engines
        with pytest.raises(ApplicationStateError, match="Connect"):
            app.start()
        app.connect_source()
        connected = app.source_snapshot()
        assert connected.state is ConnectionState.CONNECTED and connected.thread_alive
        assert [
            (c.camera_id, c.serial, c.width, c.height) for c in connected.cameras
        ] == [(f"CAM{i + 1}", serial, 60, 20) for i, serial in enumerate(SERIALS)]
        with pytest.raises(ApplicationStateError, match="Disconnect"):
            app.connect_source()
        passive.expose(1)
        until(lambda: app.source_snapshot().discarded_frames == 6)
        assert (
            app.snapshot().counters == ProductionCounters()
            and not startup.frames.snapshot().frames
        )
        assert not engines
        for ordinal in (2, 4):
            app.start().result(WAIT)
            assert app.source_snapshot().state is ConnectionState.CONNECTED
            assert [value for value, _ in engines[-1].calls] == [0, 0]
            passive.expose(ordinal, (5, 2, 4, 0, 3, 1))
            until(
                lambda ordinal=ordinal: (
                    app.snapshot().counters.cycles_completed == ordinal // 2
                )
            )
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == ordinal
                and len(result.channels) == 36
            )
            assert [c.camera_id for c in result.cameras] == [
                f"CAM{i}" for i in range(1, 7)
            ]
            assert all(
                c.frame_id is not None and c.frame_id.sequence == ordinal
                for c in result.cameras
            )
            assert {value for value, _ in engines[-1].calls[2:]} == {
                ordinal * 10 + i for i in range(6)
            }
            with pytest.raises(ApplicationStateError):
                app.disconnect_source()
            app.stop().result(WAIT)
            assert engines[-1].closed and not passive.closed
            assert app.source_snapshot().inspection_generation is None
            if ordinal == 2:
                passive.expose(3)
                until(lambda: app.source_snapshot().discarded_frames == 12)
                assert app.snapshot().counters.cycles_completed == 1
                assert startup.choice is not None
                app.configure(
                    replace(
                        startup.choice.prepared,
                        configuration=replace(
                            startup.choice.prepared.configuration, recipe_id="next"
                        ),
                    )
                )
                assert app.source_snapshot().state is ConnectionState.CONNECTED
        app.start_batch("new-batch")
        assert app.history is not None
        records = app.history.query().result(WAIT).records
        assert [r.cycle_id for r in records] == [
            4,
            2,
        ]  # Idle captures never become records.
        assert all(len(r.channels) == 36 for r in records)
        assert len({r.context.run_id for r in records}) == 2
        assert len({r.context.batch_segment for r in records}) == 1
        assert records[0].context.active.recipe_id == "next"
        assert app.snapshot().counters == ProductionCounters() and not passive.closed
        assert len(passive.prepared) == 6
        assert all(len({owner for _, owner in engine.calls}) == 1 for engine in engines)
        assert len(set(passive.owner_ids)) == 1 and passive.owner_ids[0] != get_ident()
        assert not set(passive.owner_ids) & {
            owner for engine in engines for _, owner in engine.calls
        }
        app.disconnect_source()
        assert passive.closed and not app.source_snapshot().thread_alive
        assert app.source_snapshot().state is ConnectionState.DISCONNECTED
        assert original == (data.sources.read_bytes(), data.machine.read_bytes())
    finally:
        for engine in engines:
            engine.release_warm.set()
            engine.release_active.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
        assert not any(t.name == "BaslerSourceOwner" for t in threads())


@pytest.mark.parametrize("problem", ["partial_open", "dimensions", "sdk"])
def test_failed_connect_is_explicit_and_never_creates_an_engine(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    problem: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    if problem == "partial_open":
        passive.fail_prepare = SERIALS[2]
    elif problem == "dimensions":
        passive.dimensions[SERIALS[1]] = 0, 20
    else:
        monkeypatch.setitem(sys.modules, "pypylon", None)

        def unavailable(*, continuous: bool = False) -> PassiveBackend:
            raise ImportError("pypylon unavailable")

        monkeypatch.setattr(_basler_sdk, "PylonBackend", unavailable)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        with pytest.raises((RuntimeError, ValueError, ImportError)):
            app.connect_source()
        state = app.source_snapshot()
        assert (
            state.state is ConnectionState.FAULT
            and not state.thread_alive
            and state.cleanup_safe
        )
        assert state.error and not engine.calls
        with pytest.raises(ApplicationStateError):
            app.start()
        app.disconnect_source()
        assert app.source_snapshot().state is ConnectionState.DISCONNECTED
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_failed_cleanup_keeps_original_sdk_owner_until_explicit_retry(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    startup = load_startup(data)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        passive.unsafe = True
        with pytest.raises(ApplicationStateError, match="cleanup"):
            app.disconnect_source()
        state = app.source_snapshot()
        assert (
            state.state is ConnectionState.FAULT
            and state.thread_alive
            and not state.cleanup_safe
        )
        attempts = passive.cleanup_calls
        with pytest.raises(ApplicationStateError):
            app.connect_source()
        assert passive.cleanup_calls == attempts  # No automatic reconnect/cleanup loop.
        with pytest.raises(ApplicationError, match="Close failed"):
            app.close().result(WAIT)
        assert app.snapshot().state is ApplicationState.FAULT and not app.join(0)
        passive.unsafe = False
        app.close().result(WAIT)
        assert app.join(WAIT) and passive.closed
        assert len(set(passive.owner_ids)) == 1
    finally:
        passive.unsafe = False
        app.close().result(WAIT)
        assert app.join(WAIT)


@pytest.mark.parametrize("problem", ["roi", "warmup", "loss_during_warmup"])
def test_start_failure_retains_connected_source_and_last_active_configuration(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend, problem: str
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    if problem == "roi":
        passive.dimensions[SERIALS[3]] = 57, 20
    engine = Engine()
    engine.fail_warmup = problem == "warmup"
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        started = app.start()
        if problem != "roi":
            assert engine.warm.wait(WAIT)
            if problem == "loss_during_warmup":
                passive.failure = True
                until(lambda: app.source_snapshot().state is ConnectionState.FAULT)
            engine.release_warm.set()
        with pytest.raises(ApplicationError):
            started.result(WAIT)
        assert (
            app.snapshot().state is ApplicationState.READY
            and app.snapshot().active is None
        )
        assert app.snapshot().counters == ProductionCounters()
        if problem != "loss_during_warmup":
            assert (
                app.source_snapshot().state is ConnectionState.CONNECTED
                and not passive.closed
            )
        if problem == "roi":
            assert not engine.calls
        else:
            assert engine.closed
    finally:
        engine.release_warm.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_source_loss_during_inspection_stops_session_without_reconnect(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        app.start().result(WAIT)
        passive.failure = True
        until(lambda: app.snapshot().state is ApplicationState.READY)
        state = app.source_snapshot()
        assert state.state is ConnectionState.FAULT and state.error
        assert engine.closed and app.snapshot().counters == ProductionCounters()
        assert len(passive.prepared) == 6
    finally:
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_stop_during_native_warmup_keeps_source_and_discards_arrivals(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    try:
        app.connect_source()
        started = app.start()
        assert engine.warm.wait(WAIT)
        passive.expose(1)
        until(lambda: app.source_snapshot().discarded_frames == 6)
        stopped = app.stop()
        assert not stopped.done() and not engine.closed and not passive.closed
        engine.release_warm.set()
        stopped.result(WAIT)
        with pytest.raises(ApplicationError):
            started.result(WAIT)
        assert app.snapshot().counters == ProductionCounters()
        assert (
            app.source_snapshot().state is ConnectionState.CONNECTED and engine.closed
        )
    finally:
        engine.release_warm.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_close_during_connect_cancels_before_arm_and_joins_actual_owner(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    passive.release_prepare.clear()
    startup = load_startup(data)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    connected: Future[None] = Future()

    def connect() -> None:
        try:
            app.connect_source()
            connected.set_result(None)
        except BaseException as exc:
            connected.set_exception(exc)

    caller = Thread(target=connect, name="TestConnectCaller")
    caller.start()
    try:
        assert passive.prepare_entered.wait(WAIT)
        assert app.source_snapshot().state is ConnectionState.CONNECTING
        with pytest.raises(ApplicationStateError):
            app.start()
        closed = app.close()
        assert not closed.done() and not app.join(0)
        # Close is asynchronous: let its owner register disconnect before the
        # blocked native prepare returns, rather than racing the two threads.
        until(lambda: app.source_snapshot().state is ConnectionState.DISCONNECTING)
        passive.release_prepare.set()
        closed.result(WAIT)
        assert app.join(WAIT)
        caller.join(WAIT)
        assert not caller.is_alive() and not passive.armed.is_set()
        with pytest.raises(ApplicationStateError, match="cancelled"):
            connected.result(WAIT)
        assert not app.source_snapshot().thread_alive and passive.closed
    finally:
        passive.release_prepare.set()
        app.close().result(WAIT)
        assert app.join(WAIT)
        caller.join(WAIT)


def test_disconnect_marks_unready_before_waiting_for_native_cleanup(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    startup = load_startup(data)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    app.connect_source()
    passive.release_cleanup.clear()
    caller = Thread(target=app.disconnect_source, name="TestDisconnectCaller")
    caller.start()
    try:
        assert passive.cleanup_entered.wait(WAIT)
        assert app.source_snapshot().state is ConnectionState.DISCONNECTING
        with pytest.raises(ApplicationStateError):
            app.start()
        assert app.source_snapshot().thread_alive
        passive.release_cleanup.set()
        caller.join(WAIT)
        assert (
            not caller.is_alive()
            and app.source_snapshot().state is ConnectionState.DISCONNECTED
        )
    finally:
        passive.release_cleanup.set()
        caller.join(WAIT)
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_vision_connect_locks_sources_but_allows_reference_free_roi_save(
    qapp: QApplication, tmp_path: Path, passive: PassiveBackend
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    startup = load_startup(data)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and workspace.sources is not None
        until(lambda: not window.parameters.busy)
        assert workspace.frame is None and workspace.editable()
        workspace.coordinates["X"].setText("1")
        workspace.apply_geometry()
        assert workspace.draft.dirty
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        assert not workspace.draft.dirty and not window.event_label.text()
        workspace.connect_source.click()
        until(lambda: not window.parameters.busy)
        assert startup.application.source_snapshot().state is ConnectionState.CONNECTED
        assert all(
            not card.device.isEnabled() for card in workspace.camera_cards.values()
        )
        assert not workspace.discover.isEnabled() and workspace.editable()
        assert not workspace.camera_cards[CameraId("CAM6")].configuration.isEnabled()
        geometry = workspace.draft.definition
        workspace.change_camera_configuration()
        assert workspace.draft.definition == geometry
        presentations: tuple[tuple[Language, Theme, tuple[int, int]], ...] = (
            ("zh", "dark", (1080, 1920)),
            ("zh", "light", (1080, 1920)),
            ("en", "dark", (1920, 1080)),
        )
        for language, theme, size in presentations:
            window.set_language(language)
            window.set_theme(theme)
            window.resize(*size)
            QApplication.processEvents()
            assert workspace.reference.currentText() == text(
                "vision_dimensions_only", language
            ).format(width=60, height=20)
            assert workspace.canvas.model.size == (60, 20)
            assert workspace.canvas.model.schematic_size is None
            assert not workspace.canvas.model.can_drag
            assert workspace.reference.isVisible()
            assert (
                workspace.connect_source.isVisible()
                and workspace.disconnect_source.isEnabled()
            )
            assert not workspace.connect_source.isEnabled()
            assert window.grab().save(
                str(tmp_path / f"connected-{language}-{theme}-{size[0]}.png")
            )
        saved = workspace.sources.definition
        source_before = startup.application.source_snapshot()
        application_before = startup.application.snapshot()
        workspace.coordinates["X"].setText("2")
        workspace.apply_geometry()
        workspace.buttons["vision_discard"].click()
        box = next(b for b in workspace.findChildren(QMessageBox) if b.isVisible())
        next(
            b for b in box.buttons() if b.text() == text("discard", window.language)
        ).click()
        until(lambda: not window.parameters.busy)
        assert startup.application.source_snapshot() == source_before
        assert startup.application.snapshot() == application_before
        assert not passive.closed and workspace.frame is None
        assert not workspace.draft.dirty and workspace.coordinates["X"].text() == "1"
        workspace.set_source_mode("folder")
        workspace.select_device(CameraId("CAM1"), 0)
        assert workspace.sources.definition == saved
        workspace.coordinates["W"].setText("61")
        workspace.apply_geometry()
        assert (
            not workspace.draft.dirty
            and window.event_label.accessibleName()
            == text("roi_geometry_error", window.language)
        )
        window.navigate(0)
        window.navigate(4)
        until(lambda: not window.parameters.busy)
        workspace = window.parameters.roi_workspace
        assert workspace is not None
        assert startup.application.source_snapshot().state is ConnectionState.CONNECTED
        workspace.disconnect_source.click()
        until(lambda: not window.parameters.busy)
        assert (
            startup.application.source_snapshot().state is ConnectionState.DISCONNECTED
        )
        assert workspace.source_modes["folder"].isEnabled()
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize(
    "kind", ["complete", "partial", "native", "report", "ambiguous"]
)
@pytest.mark.parametrize("camera_count", [5, 6])
def test_persistent_source_stop_settles_admitted_results_and_reports(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    kind: str,
    camera_count: int,
) -> None:
    data = saved_data(tmp_path, camera_count=camera_count)
    passive.serials = SERIALS[:camera_count]
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    if kind == "native":
        engine.release_active.clear()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert isinstance(app, ProductionReportingCommands)
    with server() as peer:
        seed(peer, 199)
        app.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            app.connect_source()
            app.start().result(WAIT)
            if kind == "report":
                peer.hold_next(ReportStage.BITMAP)
            passive.expose(
                2 if kind == "ambiguous" else 1,
                (0,) if kind in ("partial", "native") else tuple(range(camera_count)),
            )
            if kind == "ambiguous":
                until(lambda: app.snapshot().state is ApplicationState.READY)
                observed = app.reporting_snapshot()
                assert (
                    observed is not None
                    and observed.queued_count == 0
                    and observed.acknowledged_count == 0
                )
                assert app.snapshot().counters == ProductionCounters()
                assert app.source_snapshot().state is ConnectionState.FAULT
                return
            until(lambda: app.snapshot().counters.cycles_started == 1)
            if kind == "native":
                assert engine.active.wait(WAIT)
            if kind == "report":
                assert peer.entered.wait(WAIT)
            stopped = app.stop()
            if kind in ("report", "native"):
                assert not stopped.done()
            peer.release.set()
            engine.release_active.set()
            stopped.result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == 1
                and len(result.channels) == camera_count * 6
            )
            assert app.snapshot().counters.cycles_completed == 1
            if kind in ("partial", "native"):
                assert all(camera.frame_id is None for camera in result.cameras[1:])
            observed = app.reporting_snapshot()
            assert (
                observed is not None
                and observed.acknowledged_count == 1
                and not observed.thread_alive
            )
            assert engine.closed and not passive.closed
            assert app.source_snapshot().state is ConnectionState.CONNECTED
        finally:
            peer.release.set()
            engine.release_active.set()
            app.close().result(WAIT)
            assert app.join(WAIT) and passive.closed


def test_normal_basler_player_drains_paused_frames_and_settles_reports(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backends: list[PassiveBackend] = []
    engines: list[Engine] = []

    def backend(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        value = PassiveBackend()
        backends.append(value)
        return value

    def factory(_prepared: object) -> Engine:
        value = Engine()
        value.release_warm.set()
        engines.append(value)
        return value

    monkeypatch.setattr(_basler_sdk, "PylonBackend", backend)
    delivery = FutureDelivery()
    startup = load_startup(data, engine_factory=factory)
    window = create_window(startup, delivery)
    window.show()
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    assert window.bridge.source_control is startup.application

    def control() -> BaslerControlSnapshot:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value

    original_threads = {t.ident for t in threads()}
    with server() as peer:
        seed(peer, 99)
        panel = window.communication.reporting_panel
        assert panel is not None
        panel.controller.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            app.connect_source()
            window.buttons["operation"].click()
            until(
                lambda: (
                    bool(backends)
                    and backends[0].armed.is_set()
                    and app.source_snapshot().inspection_generation is not None
                    and app.snapshot().state is ApplicationState.RUNNING
                )
            )
            window.bridge.refresh()
            middle = window.buttons["pause"]
            assert text("basler_armed", window.language) in window.state_label.toolTip()
            assert middle.isEnabled()
            window.buttons["pause"].click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            middle_x, middle_size = middle.x(), middle.size()
            assert (
                middle.text() == text("replay_step", window.language)
                and not window.buttons["secondary"].isVisible()
            )
            for ordinal in (1, 2, 3):
                backends[0].expose(ordinal)
                until(lambda ordinal=ordinal: control().received_frames == ordinal * 6)
            assert app.snapshot().counters == ProductionCounters()
            assert len(engines[0].calls) == 2 and not engines[0].closed
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.queued_count == 0
            peer.hold_next(ReportStage.BITMAP)
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(4)
            until(lambda: app.snapshot().counters.cycles_completed == 1)
            assert peer.entered.wait(WAIT)
            assert control().state is ReplayState.STEPPING
            assert not engines[0].closed and not backends[0].closed
            backends[0].expose(5)
            until(lambda: control().received_frames == 30)
            assert app.snapshot().counters.cycles_completed == 1
            peer.release.set()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            assert (middle.x(), middle.size()) == (middle_x, middle_size)
            # Qt may recenter the row vertically as its header settles; player
            # slots must keep their size, alignment, order and containment.
            player = tuple(
                window.buttons[key] for key in ("operation", "pause", "stop")
            )
            for button in player:
                assert button.parentWidget() is window.operation_group
                assert window.operation_group.rect().contains(button.geometry())
                assert button.y() == middle.y() and button.height() == middle.height()
            for left, right in zip(player, player[1:], strict=False):
                assert left.geometry().right() < right.x()
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 1
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == 4
                and len(result.channels) == 36
            )
            assert all(
                c.frame_id is not None and c.frame_id.sequence == 4
                for c in result.cameras
            )
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(6)
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 2
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(lambda: control().state is ReplayState.AUTOMATIC)
            for ordinal in (7, 8):
                backends[0].expose(ordinal)
                until(
                    lambda ordinal=ordinal: (
                        app.snapshot().counters.cycles_completed == ordinal - 4
                    )
                )
            backends[0].expose(9, (0,))
            until(lambda: app.snapshot().counters.cycles_started == 5)
            window.bridge.refresh()
            middle.click()
            assert control().state is ReplayState.PAUSING
            backends[0].expose(9, (5, 3, 4, 1, 2))
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 5
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 5
            window.bridge.refresh()
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert not backends[0].closed and engines[0].closed
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(
                lambda: (
                    len(engines) == 2
                    and app.source_snapshot().inspection_generation is not None
                    and app.snapshot().state is ApplicationState.RUNNING
                )
            )
            assert len(backends) == 1 and backends[0].armed.is_set()
            window.bridge.refresh()
            assert middle.isEnabled()
            middle.click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            middle.click()  # An idle Step needs no fabricated cycle or timeout.
            until(lambda: control().admission_armed)
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert app.snapshot().counters.cycles_completed == 5
            assert app.snapshot().last_issue is None
            assert all(e.closed for e in engines) and all(
                not b.closed for b in backends
            )
        finally:
            peer.release.set()
            for engine in engines:
                engine.release_active.set()
            close_window(window)
            assert app.join(WAIT)
            delivery.detach()
    assert not any(
        t.name.startswith(
            (
                "EarlyCycleOwner-",
                "EarlyCameraInspector-",
                "VimoInferenceWorker-",
                "LocalProductionSource-",
                "FirstStationReporting-",
            )
        )
        for t in threads()
        if t.ident not in original_threads
    )


def test_basler_partial_step_waits_for_late_native_return_before_paused(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backend = PassiveBackend()
    engine = Engine()
    engine.release_warm.set()
    engine.release_active.clear()

    def sdk(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", sdk)
    startup = load_startup(data, engine_factory=lambda _: engine)
    assert isinstance(startup.application, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)

    def state() -> ReplayState:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value.state

    try:
        app.connect_source()
        window.buttons["operation"].click()
        until(
            lambda: (
                backend.armed.is_set()
                and app.source_snapshot().inspection_generation is not None
            )
        )
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(lambda: state() is ReplayState.PAUSED)
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(
            lambda: (
                (value := window.bridge.control_snapshot()) is not None
                and isinstance(value, BaslerControlSnapshot)
                and value.admission_armed
            )
        )
        backend.expose(1, (0,))
        assert engine.active.wait(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        result = app.snapshot().latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.cameras[0].frame_id is not None
        assert all(c.frame_id is None for c in result.cameras[1:])
        assert (
            state() is ReplayState.STEPPING
        )  # A deadline result is not native return.
        engine.release_active.set()
        until(lambda: state() is ReplayState.PAUSED)
        assert len(engine.calls) == 3 and app.snapshot().counters.cycles_started == 1
        window.bridge.refresh()
        window.buttons["stop"].click()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert (
            app.snapshot().counters.cycles_completed == 1
            and engine.closed
            and not backend.closed
        )
    finally:
        engine.release_active.set()
        close_window(window)
        assert app.join(WAIT)
        delivery.detach()


@pytest.mark.parametrize("ending", ["disconnect", "source_loss"])
def test_vision_reentry_bilingual_metadata_without_discovery(
    qapp: QApplication,
    tmp_path: Path,
    passive: PassiveBackend,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    from vimo_demo.ui import roi_editor

    original_prepare = passive.prepare

    def prepare(serial: str) -> CameraSettings:
        return replace(original_prepare(serial), model=f"Connected-{serial}")

    def no_discovery() -> None:
        raise AssertionError("Navigation/connection presentation must not scan")

    monkeypatch.setattr(passive, "prepare", prepare)
    monkeypatch.setattr(roi_editor, "discover_basler_devices", no_discovery)
    passive.dimensions = {s: (60 + i, 20 + i) for i, s in enumerate(SERIALS)}
    data = saved_data(tmp_path)
    save_basler(data.sources)
    without_folders(data.sources)
    saved = data.sources.read_bytes(), data.machine.read_bytes()
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceConnectionCommands)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.set_language("zh")
    window.show()
    try:
        window.navigate(4)
        until(lambda: not window.parameters.busy)
        workspace = window.parameters.roi_workspace
        assert workspace is not None
        languages: tuple[Language, Language] = ("zh", "en")
        for language in languages:
            window.set_language(language)
            for card in workspace.camera_cards.values():
                assert (
                    text("source_not_discovered", language) in card.device.currentText()
                )
        workspace.connect_source.click()
        until(lambda: not window.parameters.busy)
        connected = app.source_snapshot()
        assert connected.state is ConnectionState.CONNECTED
        assert tuple(c.serial for c in connected.cameras) == SERIALS
        assert tuple(c.model for c in connected.cameras) == tuple(
            f"Connected-{s}" for s in SERIALS
        )
        before = app.snapshot()
        for language in languages:
            window.set_language(language)
            for _ in range(2):
                window.navigate(0)
                assert window.parameters.roi_workspace is None
                window.navigate(4)
                until(lambda: not window.parameters.busy)
                current = window.parameters.roi_workspace
                assert current is not None and current is not workspace
                workspace = current
                assert workspace.connection_status.text() == "6/6"
                assert not workspace.discover.isEnabled()
                assert tuple(workspace.camera_cards) == tuple(
                    c.camera_id for c in connected.cameras
                )
                for actual in connected.cameras:
                    card = workspace.camera_cards[actual.camera_id]
                    assert card.device.currentData() == actual.serial
                    assert (
                        card.device.currentText() == f"{actual.serial} · {actual.model}"
                    )
                    assert card.summary == f"{actual.width}×{actual.height}"
                    assert text("source_connected", language) in card.toolTip()
                    assert (
                        text("source_not_discovered", language)
                        not in card.device.currentText()
                    )
                    assert not card.device.isEnabled()
                assert app.source_snapshot() == connected
                assert app.snapshot() == before and not engine.calls
                assert passive.prepared == list(SERIALS) and not passive.closed

        if ending == "disconnect":
            workspace.disconnect_source.click()
            until(lambda: not window.parameters.busy)
            state = ConnectionState.DISCONNECTED
        else:
            passive.failure = True
            until(lambda: app.source_snapshot().state is ConnectionState.FAULT)
            state = ConnectionState.FAULT
        connection_status = workspace.connection_status
        until(lambda: connection_status.text() == "0/6")
        assert app.source_snapshot().state is state
        assert passive.closed
        for language in languages:
            window.set_language(language)
            window.navigate(0)
            window.navigate(4)
            until(lambda: not window.parameters.busy)
            workspace = window.parameters.roi_workspace
            assert workspace is not None and workspace.connection_status.text() == "0/6"
            for card in workspace.camera_cards.values():
                assert not card.summary
                assert text("source_" + state.name.lower(), language) in card.toolTip()
                assert (
                    text("source_not_discovered", language) in card.device.currentText()
                )
            if ending == "source_loss":
                assert "Injected source loss" in window.event_label.toolTip()
            assert (data.sources.read_bytes(), data.machine.read_bytes()) == saved
            assert app.snapshot() == before and not engine.calls
            assert app.snapshot().counters == ProductionCounters()
    except Exception as exc:
        exc.add_note(f"Vision metadata: ending={ending}, language={window.language}")
        raise
    finally:
        close_window(window)
        assert app.join(WAIT)
        delivery.detach()
    assert passive.closed and not app.source_snapshot().thread_alive
    assert not any(t.name == "BaslerSourceOwner" for t in threads())
