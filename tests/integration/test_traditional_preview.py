"""Preview shares production math but has no application/runtime side effects."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QLabel
from tests.capsule_support import SETTINGS, bodies
from tests.qt_support import until
from tests.recipe_ui_support import Screen, assert_inside, demo_recipe_window
from tests.recipe_ui_support import configured_screen as configured_screen

from vimo_demo.application.basler_connection import (
    CameraSnapshot,
    ConnectedCamera,
    ConnectionState,
    SourceConnectionSnapshot,
)
from vimo_demo.application.folder_replay import decode_image
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    MachineLayoutDefinition,
    load_recipe,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelROI,
    InspectionLayout,
    ProductJudgment,
)
from vimo_demo.inspection.production.capsule import CapsuleSettings, judge_capsule
from vimo_demo.ui.camera_source_editor import CameraSourceDraft
from vimo_demo.ui.display_frames import DisplayFrame
from vimo_demo.ui.machine_editor import MachineLayoutDraft
from vimo_demo.ui.production_style import Language
from vimo_demo.ui.traditional_preview import (
    PreviewMode,
    TraditionalPreview,
)


def collapse(preview: TraditionalPreview, screen: Screen) -> None:
    assert screen[0].parameters.preview is preview
    screen[0].parameters.expand_tuning[preview.mode].click()


def source_preview(
    screen: Screen, root: Path, *, basler: bool = False
) -> CameraSourceDraft:
    preview = setup(screen)
    collapse(preview, screen)
    session = screen[1]
    mappings: list[CameraFolder] = []
    for index, camera in enumerate(session.machine.layout.camera_ids):
        folder = root / str(camera)
        folder.mkdir()
        for number in (2, 10):
            image = np.concatenate(
                [bodies(30 + index + number) for _ in range(6)], axis=1
            )
            ok, encoded = cv2.imencode(".png", image)
            assert ok
            encoded.tofile(folder / f"image{number}.png")
        mappings.append(
            CameraFolder(camera, str(folder), basler_serial=str(1000 + index))
        )
    sources = CameraSourceDraft(
        CameraSources(tuple(mappings), mode="basler" if basler else "folder"),
        root / "sources.json",
    )
    session.camera_sources = sources
    return sources


def test_folder_first_valid_navigation_camera_identity_and_no_side_effects(
    configured_screen: Screen,
    tmp_path: Path,
) -> None:
    window, session, runtimes = configured_screen
    sources = source_preview(configured_screen, tmp_path)
    (tmp_path / "CAM1/image1.png").write_bytes(b"not an image")
    before, recipe, machine = (
        window.bridge.application.snapshot(),
        session.draft.to_recipe(),
        session.machine,
    )
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    original = preview.frame
    assert original is not None and original.frame_id.camera_id == "CAM1"
    assert original.frame_id.sequence == 1 and "2/3" in preview.reference.currentText()
    assert (
        "480×120" in preview.reference.currentText()
        and "schematic" not in preview.reference.currentText()
    )
    assert not original.image.flags.writeable
    with pytest.raises(ValueError):
        original.image.setflags(write=True)
    preview.next_button.click()
    until(lambda: not preview.busy)
    second = preview.frame
    assert (
        second is not None
        and second.frame_id.sequence == 2
        and not preview.next_button.isEnabled()
    )
    preview.camera_tabs.setCurrentIndex(1)
    until(lambda: not preview.busy)
    assert preview.frame and preview.frame.frame_id.camera_id == "CAM2"
    preview.camera_tabs.setCurrentIndex(0)
    until(lambda: not preview.busy)
    assert (
        preview.frame is second
    )  # Per-camera frozen selection, not first-image reload.
    preview.refresh_button.click()
    until(lambda: not preview.busy)
    assert (
        preview.frame and preview.frame.frame_id.sequence == 2
    )  # Refresh is not Next.
    preview.actual_button.click()
    assert preview.canvas.model.scale == 1.0
    preview.fit_button.click()
    assert preview.canvas.model.scale is None
    assert session.draft.to_recipe() == recipe and session.machine == machine
    assert sources.saved == sources.definition and not sources.dirty
    assert window.bridge.application.snapshot() == before and not runtimes


@pytest.mark.parametrize("basler", [False, True])
def test_temporary_import_stays_frozen_and_failure_keeps_pixels(
    configured_screen: Screen,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    basler: bool,
) -> None:
    window, session, _ = configured_screen
    sources = source_preview(configured_screen, tmp_path, basler=basler)
    window.parameters.open_preview("color")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    before, recipe = window.bridge.application.snapshot(), session.draft.to_recipe()
    path = tmp_path / "CAM1/image10.png"

    def choose_file(*args: object, **kwargs: object) -> tuple[str, str]:
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)
    preview.import_button.click()
    until(lambda: not preview.busy)
    imported = preview.frame
    assert imported is not None and imported.source_key == str(path)
    assert (
        imported.frame_id.camera_id == "CAM1"
        and "480×120" in preview.reference.currentText()
    )
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(7), CameraId("CAM1"), 7),
            7,
            np.zeros((120, 480, 3), np.uint8),
        ),
        generation=0,
        source_key="basler:1000",
    )
    window.bridge.refresh()
    preview.set_mode("size")
    assert preview.frame is imported
    preview.camera_tabs.setCurrentIndex(1)
    until(lambda: not preview.busy)
    assert preview.frame is None or preview.frame.frame_id.camera_id == "CAM2"
    preview.camera_tabs.setCurrentIndex(0)
    until(lambda: not preview.busy)
    assert preview.frame is imported
    preview.refresh_button.click()
    until(lambda: not preview.busy)
    refreshed = preview.frame
    assert refreshed is not None and refreshed.source_key == str(path)
    assert refreshed is not imported and refreshed.frame_id.camera_id == "CAM1"
    preview.load_image(tmp_path / "missing.png")
    until(lambda: not preview.busy)
    assert preview.frame is refreshed and "missing.png" in preview.message.text()
    assert session.draft.to_recipe() == recipe and not sources.dirty
    assert window.bridge.application.snapshot() == before


def test_basler_reference_filters_mapping_and_requires_explicit_refresh(
    configured_screen: Screen,
    tmp_path: Path,
) -> None:
    window, _, _ = configured_screen
    source_preview(configured_screen, tmp_path, basler=True)
    store = window.bridge.frames
    cam1, cam2 = CameraId("CAM1"), CameraId("CAM2")
    image = np.concatenate([bodies(42) for _ in range(6)], axis=1)
    store.publish(
        Frame(FrameId(CycleId(2), cam1, 2), 2, image),
        generation=3,
        source_key="basler:1000",
    )
    store.publish(
        Frame(FrameId(CycleId(2), cam2, 2), 2, image),
        generation=3,
        source_key="basler:wrong",
    )
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    frozen = preview.frame
    assert (
        frozen is not None and frozen.generation == 3 and frozen.frame_id.sequence == 2
    )
    store.publish(
        Frame(FrameId(CycleId(3), cam1, 3), 3, image),
        generation=3,
        source_key="basler:1000",
    )
    window.bridge.refresh()
    assert preview.frame is frozen
    preview.camera_tabs.setCurrentIndex(1)
    until(lambda: not preview.busy)
    assert (
        preview.frame is None and preview.measurements == () and preview.results == ()
    )
    assert (
        preview.canvas.model.size is None
        and preview.canvas.model.schematic_size is not None
    )
    assert not preview.runtime_button.isEnabled() and not preview.sample.isEnabled()
    preview.camera_tabs.setCurrentIndex(0)
    until(lambda: not preview.busy)
    assert preview.frame is frozen
    preview.refresh_button.click()
    until(lambda: not preview.busy)
    assert preview.frame and preview.frame.frame_id.sequence == 3


@pytest.mark.parametrize("reference", ["none", "black", "ng"])
def test_runtime_preview_never_invents_pixels_or_results_but_preserves_real_pixel_judgment(
    configured_screen: Screen, tmp_path: Path, reference: str
) -> None:
    window, session, runtimes = configured_screen
    source_preview(configured_screen, tmp_path, basler=True)
    before = window.bridge.application.snapshot()
    if reference != "none":
        image = (
            np.zeros((120, 480, 3), dtype=np.uint8)
            if reference == "black"
            else np.concatenate([bodies(30) for _ in range(6)], axis=1)
        )
        window.bridge.frames.publish(
            Frame(FrameId(CycleId(10), CameraId("CAM1"), 12), 1, image),
            generation=3,
            source_key="basler:1000",
        )
        if reference == "ng":
            session.draft.capsule = replace(
                SETTINGS,
                size=replace(
                    SETTINGS.size, target_width=SETTINGS.size.target_width + 50
                ),
            )
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    if reference == "none":
        assert (
            preview.frame is None and not preview.measurements and not preview.results
        )
        assert not preview.canvas.rows and not preview.canvas.results
        assert not preview.runtime_button.isEnabled()
    else:
        assert preview.frame is not None and preview.frame.frame_id == FrameId(
            CycleId(10), CameraId("CAM1"), 12
        )
        assert (
            preview.frame.source_key == "basler:1000" and preview.frame.generation == 3
        )
        assert len(preview.measurements) == len(preview.results) == 6
        assert all(
            result and result.assessment.judgment is ProductJudgment.NG
            for result in preview.results
        )
        if reference == "black":
            assert not np.any(preview.frame.image)
            assert all(
                row.measured and all(view.body is None for view in row.measured.views)
                for row in preview.measurements
            )
        else:
            assert all(
                row.measured
                and all(view.body is not None for view in row.measured.views)
                for row in preview.measurements
            )
    preview.camera_tabs.setCurrentIndex(1)
    until(lambda: not preview.busy)
    assert preview.frame is None and not preview.results and not preview.measurements
    assert not preview.canvas.results and not preview.canvas.rows
    preview.select_view(0, 0)
    assert preview.canvas.selection is None
    assert window.bridge.application.snapshot() == before and not runtimes


def test_blank_preview_uses_only_authoritative_connected_dimensions(
    configured_screen: Screen,
    tmp_path: Path,
) -> None:
    window, _, _ = configured_screen
    source_preview(configured_screen, tmp_path, basler=True)

    class Connection:
        state = ConnectionState.CONNECTED

        def source_snapshot(self) -> SourceConnectionSnapshot:
            return SourceConnectionSnapshot(
                self.state,
                (ConnectedCamera(CameraId("CAM1"), "1000", 480, 120),),
                False,
                True,
                "",
                None,
                0,
                0,
                None,
                False,
            )

        def connect_source(self) -> None:
            pytest.fail("Preview must not connect")

        def disconnect_source(self) -> None:
            pytest.fail("Preview must not disconnect")

        def capture_image(
            self, camera: CameraId, *, timeout: float = 10.0
        ) -> Future[CameraSnapshot]:
            pytest.fail("Preview must not acquire")

    connection = Connection()
    window.parameters.open_preview("color")
    preview = window.parameters.preview
    assert preview is not None and preview.frame is None and not preview.busy
    preview.connection = connection
    preview.refresh_state()
    assert preview.canvas.model.size == (480, 120)
    assert (
        "480" in preview.reference.currentText()
        and "120" in preview.reference.currentText()
    )
    assert (
        not preview.measurements
        and not preview.results
        and not preview.sample.isEnabled()
    )
    connection.state = ConnectionState.DISCONNECTED
    preview.refresh_state()
    assert (
        preview.canvas.model.size is None
        and preview.canvas.model.schematic_size is not None
    )
    assert "480" not in preview.reference.currentText()


@pytest.mark.parametrize("close", [False, True])
def test_async_decode_camera_change_and_close_discard_stale_work(
    configured_screen: Screen,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close: bool,
) -> None:
    import vimo_demo.ui.traditional_preview as module

    window, _, _ = configured_screen
    source_preview(configured_screen, tmp_path)
    entered, release = Event(), Event()
    original = decode_image

    def blocked(path: Path):
        if path.parent.name == "CAM1":
            entered.set()
            assert release.wait(5)
        return original(path)

    monkeypatch.setattr(module, "decode_image", blocked)
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(entered.is_set)
    try:
        preview.camera_tabs.setCurrentIndex(1)
        preview.camera_tabs.setCurrentIndex(2)
        assert (
            preview.frame is None and not preview.measurements and not preview.results
        )
        assert sum(t.name == "CapsulePreview" for t in threads()) == 1
        if close:
            collapse(preview, configured_screen)
            assert window.parameters.preview is preview
    finally:
        release.set()
    if close:
        until(lambda: window.parameters.preview is None)
        assert not any(t.name == "CapsulePreview" for t in threads())
    else:
        until(lambda: not preview.busy)
        assert preview.frame and preview.frame.frame_id.camera_id == "CAM3"
        assert len(preview.results) == 6 and "image2.png" in preview.reference.toolTip()


def setup(screen: Screen, *, pixel_scale: int = 1) -> TraditionalPreview:
    window, session, _ = screen
    cameras = tuple(
        replace(
            camera,
            slots=tuple(
                ChannelROI(
                    slot.slot,
                    slot.channel_id,
                    BoundingRect(
                        slot.slot * 80 * pixel_scale,
                        0,
                        80 * pixel_scale,
                        120 * pixel_scale,
                    ),
                )
                for slot in camera.slots
            ),
        )
        for camera in session.machine.layout.cameras
    )
    session.machine_editor = MachineLayoutDraft(
        MachineLayoutDefinition(InspectionLayout(cameras)), session.machine_path
    )
    for index, camera in enumerate(cameras):
        image = np.concatenate([bodies(30 + index) for _ in range(6)], axis=1)
        image = image.repeat(pixel_scale, axis=0).repeat(pixel_scale, axis=1)
        window.bridge.frames.publish(
            Frame(FrameId(CycleId(1), camera.camera_id, 1), 1.0, image), generation=0
        )
    session.draft.capsule = SETTINGS
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None
    until(lambda: not preview.busy)
    return preview


def test_cached_judgment_and_isolation_and_save(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    window, session, runtimes = configured_screen
    before = window.bridge.application.snapshot()
    preview = setup(configured_screen)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    for row, result in zip(preview.measurements, preview.results, strict=True):
        assert row.measured is not None
        assert result == judge_capsule(row.measured, session.draft.capsule)

    def forbidden(*args: object) -> None:
        pytest.fail("Threshold edit must not segment again")

    monkeypatch.setattr(module, "measure_capsule", forbidden)
    preview.spins["target_width"].setValue(25)
    assert all(
        result and result.assessment.judgment is ProductJudgment.NG
        for result in preview.results
    )
    preview.spins["width_tolerance"].setValue(5)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    preview.set_mode("color")
    session.draft.capsule = replace(
        session.draft.capsule, color=replace(session.draft.capsule.color, target_hue=50)
    )
    preview.show_settings()
    assert all(
        result and result.assessment.judgment is ProductJudgment.NG
        for result in preview.results
    )
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 30
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    preview.tolerance.setValue(10)
    after = window.bridge.application.snapshot()
    assert (
        before.counters == after.counters
        and before.active == after.active
        and before.candidate == after.candidate
    )
    assert not runtimes and after.latest_production_result is None
    assert (
        session.entry
        and load_recipe(session.entry.path).capsule != session.draft.capsule
    )
    expected = session.draft.capsule
    collapse(preview, configured_screen)
    assert window.parameters.preview is None
    window.parameters.save_and_apply()
    until(lambda: not window.parameters.busy)
    assert session.entry and load_recipe(session.entry.path).capsule == expected
    assert window.choices[-1].prepared.configuration.capsule == expected
    assert not runtimes and window.bridge.application.snapshot().active is before.active


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_camera_refresh_and_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    assert [preview.camera_tabs.tabText(i) for i in range(6)] == [
        str(c).removeprefix("CAM") for c in session.machine.layout.camera_ids
    ]
    for index, camera in enumerate(session.machine.layout.camera_ids):
        assert not preview.camera_tabs.tabIcon(index).isNull()
        assert str(camera) in preview.camera_tabs.tabToolTip(index)
        assert preview.camera_tabs.accessibleTabName(
            index
        ) == preview.camera_tabs.tabToolTip(index)
        assert preview.camera_tabs.rect().contains(preview.camera_tabs.tabRect(index))
    assert preview.canvas.width() > 500 and preview.canvas.height() > 240
    for widget in (
        preview.refresh_button,
        preview.controls,
    ):
        assert preview.rect().contains(widget.mapTo(preview, QPoint(0, 0)))
        assert preview.rect().contains(
            widget.mapTo(preview, QPoint(widget.width() - 1, widget.height() - 1))
        )
    assert not preview.message.isVisible()
    original = preview.frame
    preview.camera_tabs.setCurrentIndex(2)
    until(lambda: not preview.busy)
    assert preview.frame is not None and preview.frame is not original
    assert preview.frame.frame_id.camera_id == "CAM3"
    preview.set_mode("color")
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 32
    camera = CameraId("CAM3")
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), camera, 2), 2, np.zeros((120, 480, 3), dtype=np.uint8)
        ),
        generation=0,
    )
    assert preview.frame.frame_id.cycle_id == 1
    preview.refresh_button.click()
    until(lambda: not preview.busy)
    assert preview.frame is not None and preview.frame.frame_id.cycle_id == 2
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 32
    assert "unchanged" in preview.message.text() or "未更改" in preview.message.text()
    collapse(preview, configured_screen)


def test_unavailable_reference_no_invented_measurement(
    configured_screen: Screen,
) -> None:
    window, _, _ = configured_screen
    window.parameters.open_preview("color")
    preview = window.parameters.preview
    assert preview is not None and preview.frame is None
    assert preview.measurements == () and preview.results == () and not preview.busy
    assert not preview.sample.isEnabled()
    preview.select_view(0, 0)
    assert (
        preview.canvas.selection is None and "W " not in preview.selection_detail.text()
    )
    collapse(preview, configured_screen)


def test_stale_job_and_observable_close(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    preview = setup(configured_screen)
    original = module.measure_reference
    entered, release = Event(), Event()

    def blocked(
        frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
    ) -> tuple[module.PreviewMeasurement, ...]:
        entered.set()
        assert release.wait(5)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", blocked)
    preview.refresh_reference()
    until(entered.is_set)
    preview.camera_tabs.setCurrentIndex(1)
    assert not preview.measurements
    release.set()
    until(lambda: not preview.busy)
    assert preview.frame and preview.frame.frame_id.camera_id == "CAM2"
    assert preview.measurements[0].measured
    assert preview.measurements[0].measured.views[0].body
    assert preview.measurements[0].measured.views[0].color
    assert preview.measurements[0].measured.views[0].color.hue == 31
    entered.clear()
    release.clear()
    preview.refresh_reference()
    until(entered.is_set)
    closed: list[bool] = []

    def finished() -> None:
        closed.append(True)
        preview.leave()

    preview.request_close(finished)
    assert not closed and preview.busy
    release.set()
    until(lambda: bool(closed))
    assert not any(t.name == "CapsulePreview" for t in threads())


def test_preview_read_only_during_runtime_and_navigation_releases(
    configured_screen: Screen,
) -> None:
    from vimo_demo.application import ApplicationState

    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    before = session.draft.capsule
    future = window.bridge.application.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(future.done)
    window.bridge.refresh()
    assert (
        not configured_screen[0].parameters.capsule_enabled[preview.mode].isEnabled()
        and not preview.controls.isEnabled()
    )
    preview.sample_color()
    assert session.draft.capsule == before
    window.navigate(4)
    until(lambda: window.parameters.preview is None)
    assert window.pages.currentWidget() is window.parameters.settings
    stop = window.bridge.application.stop()
    until(stop.done)
    assert window.bridge.application.snapshot().state is ApplicationState.READY


def test_disabled_preview_never_segments(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module
    from vimo_demo.inspection.production.capsule import CapsuleSettings

    window, session, _ = configured_screen
    preview = setup(configured_screen)
    collapse(preview, configured_screen)
    session.draft.capsule = CapsuleSettings()
    calls: list[bool] = []
    original = module.measure_reference

    def observed(
        frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
    ) -> tuple[module.PreviewMeasurement, ...]:
        calls.append(True)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", observed)
    window.parameters.open_preview("size")
    preview = window.parameters.preview
    assert preview is not None and not preview.busy and not calls
    configured_screen[0].parameters.capsule_enabled[preview.mode].setChecked(True)
    until(lambda: not preview.busy)
    assert calls == [True]
    collapse(preview, configured_screen)


@pytest.mark.parametrize("language", ["zh", "en"])
def test_no_repeated_text_but_errors_remain(
    configured_screen: Screen, language: Language
) -> None:
    window, _, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language(language)
    for mode in (0, 1):
        preview.set_mode("size" if (mode) == 0 else "color")
        assert not preview.message.isVisible() and preview.message.text() == ""
        labels = " ".join(label.text() for label in preview.findChildren(QLabel))
        assert "CH01: OK" not in labels
        assert "未标定" not in labels and "Uncalibrated demo" not in labels
    assert preview.frame is not None
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.zeros((1, 1, 3), dtype=np.uint8),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert preview.message.isVisible() and "ValueError" in preview.message.text()
    collapse(preview, configured_screen)


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
@pytest.mark.parametrize("scale", [None, 1.0, 1.5])
def test_per_view_selection_at_fit_and_zoom(
    configured_screen: Screen,
    size: tuple[int, int],
    scale: float | None,
) -> None:
    window, session, runtimes = configured_screen
    preview = setup(configured_screen, pixel_scale=3)
    window.set_language("en")
    window.resize(*size)
    QApplication.processEvents()
    before = window.bridge.application.snapshot()
    preview.canvas.zoom(scale)
    transform = preview.canvas.model.transform(
        preview.canvas.width(), preview.canvas.height()
    )
    assert transform is not None
    count = 0
    for index, row in enumerate(preview.measurements):
        assert row.measured is not None
        for view_index, view in enumerate(row.measured.views):
            assert view.body is not None
            r = view.body.bounding_rect
            x, y, w, h = transform.rectangle(r)
            point = QPoint(round(x + w / 2), round(y + h / 2))
            if not preview.canvas.rect().contains(point):
                continue
            QTest.mouseClick(preview.canvas, Qt.MouseButton.LeftButton, pos=point)
            assert preview.canvas.selection == (index, view_index)
            detail = preview.selection_detail.text()
            assert f"CH{row.slot.channel_id:02d}" in detail and view.name in detail
            assert "W 60 [20, 20]" in detail and "H 90 [30, 30]" in detail
            assert "Size NG" in detail and "Occupancy" not in detail
            count += 1
    assert count > 0
    selected = preview.canvas.selection
    preview.set_mode("color")
    assert (
        preview.canvas.selection == selected
        and "Color OK" in preview.selection_detail.text()
    )
    assert "Hue 30.00" in preview.selection_detail.text()
    assert window.bridge.application.snapshot() == before and not runtimes
    assert session.draft.capsule == SETTINGS


def test_view_judgment_is_separate_from_other_view_and_check(
    configured_screen: Screen,
) -> None:
    window, _, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language("en")
    image = bodies(30)
    image[60:] = bodies(45)[60:]
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.concatenate([image for _ in range(6)], axis=1),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert (
        preview.results[0]
        and preview.results[0].assessment.judgment is ProductJudgment.NG
    )
    preview.select_view(0, 1)
    assert "Size OK" in preview.selection_detail.text()
    preview.set_mode("color")
    assert (
        "Color NG" in preview.selection_detail.text()
        and "Hue 45.00" in preview.selection_detail.text()
    )
    assert "capsule.color.lower.tolerance" in preview.selection_detail.toolTip()
    preview.select_view(0, 0)
    assert (
        "Color OK" in preview.selection_detail.text()
        and "Hue 30.00" in preview.selection_detail.text()
    )
    preview.set_mode("size")
    preview.spins["target_width"].setValue(21)
    assert (
        "Size NG" in preview.selection_detail.text()
        and "W 20 [21, 21]" in preview.selection_detail.text()
    )


@pytest.mark.parametrize("mode", ["size", "color"])
def test_missing_error_and_disabled_measurements_are_not_invented(
    configured_screen: Screen,
    mode: PreviewMode,
) -> None:
    window, _, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language("en")
    preview.set_mode(mode)
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.zeros((120, 480, 3), np.uint8),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    preview.select_view(0, 0)
    assert f"{mode.title()} NG" in preview.selection_detail.text()
    assert ("W —" if mode == "size" else "Hue —") in preview.selection_detail.text()
    assert "Occupancy" not in preview.selection_detail.text()
    assert (
        "EMPTY" not in preview.selection_detail.text()
    )  # Missing body is not occupancy.
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(3), CameraId("CAM1"), 3), 3, np.zeros((1, 1, 3), np.uint8)
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert (
        preview.canvas.selection is None
        and "CH01" not in preview.selection_detail.text()
    )
    preview.select_view(0, 0)
    assert (
        "ERROR" in preview.selection_detail.text()
        and "ValueError" in preview.selection_detail.toolTip()
    )
    window.parameters.capsule_enabled[mode].click()
    until(lambda: not preview.busy)
    assert "DISABLED" in preview.selection_detail.text()
    assert ("W —" if mode == "size" else "Hue —") in preview.selection_detail.text()


def test_selection_clears_on_image_camera_change_and_while_evidence_pending(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vimo_demo.ui.traditional_preview as module

    window, _, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language("en")
    preview.select_view(0, 0)
    entered, release = Event(), Event()
    original = module.measure_reference

    def blocked(
        frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings
    ) -> tuple[module.PreviewMeasurement, ...]:
        entered.set()
        assert release.wait(5)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", blocked)
    preview.camera_tabs.setCurrentIndex(1)
    until(entered.is_set)
    try:
        assert (
            preview.canvas.selection is None
            and "CAM1" not in preview.selection_detail.text()
        )
        preview.select_view(0, 1)
        assert (
            "CAM2" in preview.selection_detail.text()
            and "Unavailable" in preview.selection_detail.text()
        )
        assert "W —" in preview.selection_detail.text()
    finally:
        release.set()
    until(lambda: not preview.busy)
    preview.set_mode("color")
    assert (
        "Hue 31.00" in preview.selection_detail.text()
        and "lower" in preview.selection_detail.text()
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_preview_scopes_ng_event_but_preserves_errors(
    configured_screen: Screen, language: Language
) -> None:
    from tests.application_support import production

    from vimo_demo.application import ApplicationState
    from vimo_demo.inspection.production import TechnicalStatus
    from vimo_demo.ui.production_style import text

    window, _, runtimes = configured_screen
    window.set_language(language)
    app = window.bridge.application
    start = app.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(start.done)
    receipt = app.begin_cycle(CycleId(1))
    runtimes[0].cycles[0].accepted.set_result(None)
    runtimes[0].cycles[0].result.set_result(production(judgment=ProductJudgment.NG))
    until(receipt.result.done)
    stop = app.stop()
    until(stop.done)
    window.bridge.refresh()
    window.navigate(0)
    assert window.event_frame.isVisible() and not window.event_label.text()
    before = app.snapshot()
    window.navigate(1)
    preview = setup(configured_screen)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    assert window.event_frame.isVisible() and not window.event_label.text()
    assert app.snapshot() == before
    # Snapshot notification only: exercise real rendering branches without changing app state.
    window.bridge.snapshot_changed.emit(
        replace(
            before,
            latest_production_result=production(
                status=TechnicalStatus.ERROR, judgment=ProductJudgment.UNKNOWN
            ),
        )
    )
    assert (
        window.event_frame.isVisible()
        and text("inspection_issue", language) in window.event_label.text()
    )
    window.bridge.snapshot_changed.emit(replace(before, state=ApplicationState.FAULT))
    assert (
        window.event_frame.isVisible()
        and text("runtime_error", language) in window.event_label.text()
    )
    window.bridge.snapshot_changed.emit(before)
    window.bridge.command_failed.emit(
        "start", RuntimeError("simulated command failure")
    )
    assert (
        window.event_frame.isVisible()
        and text("operation_error", language) in window.event_label.text()
    )
    window.bridge.command_finished.emit("start")
    assert window.event_frame.isVisible() and not window.event_label.text()
    collapse(preview, configured_screen)
    window.navigate(0)
    assert window.event_frame.isVisible() and not window.event_label.text()
    assert app.snapshot() == before


def test_color_only_sampling_without_body_geometry(configured_screen: Screen) -> None:
    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    preview.set_mode("color")
    session.draft.capsule = replace(
        SETTINGS,
        size=replace(SETTINGS.size, enabled=False),
        color=replace(SETTINGS.color, target_hue=80),
    )
    preview.show_settings()
    until(lambda: not preview.busy)
    # Entire colored views touch all edges: Size would reject each body.
    image = np.tile(bodies()[10, 20], (120, 480, 1)).copy()
    # A channel with only one usable view must not contribute a target sample.
    image[:60, :80] = bodies(80)[10, 20]
    image[60:, :80] = 0
    before = window.bridge.application.snapshot()
    window.bridge.frames.publish(
        Frame(FrameId(CycleId(2), CameraId("CAM1"), 2), 2, image), generation=0
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    assert all(
        row.measured
        and row.measured.color_measured
        and not row.measured.size_measured
        and all(v.body is None for v in row.measured.views)
        for row in preview.measurements
    )
    preview.sample.click()
    assert session.draft.capsule.color.target_hue == 30
    assert (
        preview.results[0]
        and preview.results[0].assessment.judgment is ProductJudgment.NG
    )
    assert all(
        r and r.assessment.judgment is ProductJudgment.OK for r in preview.results[1:]
    )
    assert all(
        r and not any(m.name.endswith((".width", ".height")) for m in r.measurements)
        for r in preview.results
    )
    assert window.bridge.application.snapshot() == before and not runtimes
    collapse(preview, configured_screen)


def test_preview_capability_changes_recompute_but_thresholds_rejudge(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    _, session, _ = configured_screen
    preview = setup(configured_screen)
    original = module.measure_reference
    calls: list[tuple[bool, bool]] = []

    def observed(frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings):
        calls.append((settings.size.enabled, settings.color.enabled))
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", observed)
    configured_screen[0].parameters.capsule_enabled[preview.mode].setChecked(
        False
    )  # Color-only
    until(lambda: not preview.busy)
    assert calls == [(False, True)]
    preview.set_mode("color")
    preview.tolerance.setValue(12)
    preview.sample.click()
    assert calls == [(False, True)]  # Targets and tolerances only rejudge.
    configured_screen[0].parameters.capsule_enabled[preview.mode].setChecked(
        False
    )  # Neither: clear stale evidence, no job.
    assert not preview.busy and not preview.measurements and not preview.results
    assert calls == [(False, True)]
    preview.set_mode("size")
    configured_screen[0].parameters.capsule_enabled[preview.mode].setChecked(
        True
    )  # Size-only
    until(lambda: not preview.busy)
    assert calls[-1] == (True, False)
    assert all(
        row.measured
        and row.measured.size_measured
        and not row.measured.color_measured
        and all(v.color is None for v in row.measured.views)
        for row in preview.measurements
    )
    session.draft.capsule = replace(
        session.draft.capsule, color=replace(session.draft.capsule.color, enabled=True)
    )
    preview.show_settings()
    until(lambda: not preview.busy)
    assert calls == [(False, True), (True, False), (True, True)]
    collapse(preview, configured_screen)


def test_capability_change_discards_inflight_job_and_uses_latest_snapshot(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vimo_demo.ui.traditional_preview as module

    _, session, _ = configured_screen
    preview = setup(configured_screen)
    original = module.measure_reference
    entered, release = Event(), Event()
    calls: list[tuple[bool, bool]] = []

    def blocked(frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings):
        calls.append((settings.size.enabled, settings.color.enabled))
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", blocked)
    preview.refresh_reference()
    until(entered.is_set)
    try:
        session.draft.capsule = replace(
            SETTINGS, size=replace(SETTINGS.size, enabled=False)
        )
        preview.show_settings()
        assert preview.busy and not preview.measurements and len(calls) == 1
    finally:
        release.set()
    until(lambda: not preview.busy)
    assert calls == [(True, True), (False, True)]
    assert all(
        row.measured and not row.measured.size_measured for row in preview.measurements
    )
    assert not any(t.name == "CapsulePreview" for t in threads())
    collapse(preview, configured_screen)


@pytest.mark.parametrize(
    "size_enabled,color_enabled",
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_one_entry_mode_selection_and_draft_synchronization(
    configured_screen: Screen, size_enabled: bool, color_enabled: bool
) -> None:
    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    collapse(preview, configured_screen)
    page = window.parameters
    session.draft.capsule = replace(
        SETTINGS,
        size=replace(SETTINGS.size, enabled=size_enabled),
        color=replace(SETTINGS.color, enabled=color_enabled),
    )
    page.show_draft()
    before = window.bridge.application.snapshot()
    assert page.capsule_enabled["size"].isChecked() is size_enabled
    assert page.capsule_enabled["color"].isChecked() is color_enabled
    initial = "color" if color_enabled and not size_enabled else "size"
    page.expand_tuning[initial].click()
    preview = page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    assert preview.mode == ("color" if color_enabled and not size_enabled else "size")
    assert configured_screen[0].parameters.capsule_enabled[
        preview.mode
    ].isChecked() is (color_enabled if preview.mode == "color" else size_enabled)
    page.capsule_enabled[preview.mode].click()
    until(lambda: not preview.busy)
    preview.set_mode("size")
    preview.spins["target_width"].setValue(21)
    preview.set_mode("color")
    preview.tolerance.setValue(7)
    expected = session.draft.capsule
    collapse(preview, configured_screen)
    assert page.preview is None and session.draft.capsule == expected
    assert page.capsule_enabled["size"].isChecked() is expected.size.enabled
    assert page.capsule_enabled["color"].isChecked() is expected.color.enabled
    assert "21" in page.expand_tuning["size"].text()
    assert "7" in page.expand_tuning["color"].text()
    assert window.bridge.application.snapshot() == before and not runtimes


@pytest.mark.parametrize("mode", ["size", "color"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_disabled_selected_mode_keeps_other_checks_ng(
    configured_screen: Screen, mode: PreviewMode, language: Language
) -> None:
    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    window.set_language(language)
    before = window.bridge.application.snapshot()
    session.draft.capsule = replace(
        SETTINGS,
        size=replace(SETTINGS.size, enabled=mode != "size", target_width=25),
        color=replace(SETTINGS.color, enabled=mode != "color", target_hue=50),
    )
    preview.set_mode("size" if (0 if mode == "size" else 1) == 0 else "color")
    preview.show_settings()
    until(lambda: not preview.busy)
    assert not configured_screen[0].parameters.capsule_enabled[preview.mode].isChecked()
    assert configured_screen[0].parameters.capsule_enabled[preview.mode].isVisible()
    assert all(
        r and r.assessment.judgment is ProductJudgment.NG for r in preview.results
    )
    assert preview.canvas.results == preview.results
    measurements, results = preview.measurements, preview.results
    preview.set_mode("size" if (1 if mode == "size" else 0) == 0 else "color")
    assert configured_screen[0].parameters.capsule_enabled[preview.mode].isChecked()
    assert preview.measurements is measurements and preview.results == results
    assert window.bridge.application.snapshot() == before and not runtimes
    collapse(preview, configured_screen)


def test_integrated_controls_precede_canvas_and_enablement_is_separate(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    page = window.parameters
    assert not preview.findChildren(QCheckBox)
    assert preview.controls.y() + preview.controls.height() <= preview.canvas.y()
    before = session.draft.capsule
    frozen = preview.frame
    page.expand_tuning["color"].click()
    assert (
        page.preview is preview and preview.frame is frozen and preview.mode == "color"
    )
    assert session.draft.capsule == before
    page.capsule_enabled["color"].click()
    until(lambda: not preview.busy)
    assert (
        session.draft.capsule.size.enabled and not session.draft.capsule.color.enabled
    )
    assert page.expand_tuning["color"].isChecked()
    collapse(preview, configured_screen)


@pytest.mark.parametrize(
    "language,size,workspace",
    [("en", (1920, 1080), "size"), ("zh", (1080, 1920), "color")],
)
def test_tuning_preview_screenshots(
    configured_screen: Screen,
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    workspace: str,
) -> None:
    from run_production_ui import compose, demo_images

    composition = compose(demo_images(), tmp_path / "demo", "normal", 0.001)
    with demo_recipe_window(composition, language=language, size=size) as window:
        window.navigate(1)
        page = window.parameters
        composition.editor.draft.capsule = replace(
            SETTINGS,
            size=replace(
                SETTINGS.size,
                target_width=65,
                width_tolerance=10,
                target_height=181,
                height_tolerance=3,
            ),
            color=replace(SETTINGS.color, target_hue=27, hue_tolerance=8),
        )
        page.show_draft()
        before = composition.application.snapshot()
        page.expand_tuning["size" if workspace == "size" else "color"].click()
        preview = page.preview
        assert preview is not None
        preview.set_mode("size" if (0 if workspace == "size" else 1) == 0 else "color")
        until(lambda: not preview.busy)
        qapp.processEvents()
        assert preview.frame is not None and len(preview.results) == 6
        assert preview.canvas.height() >= 240
        for control in (
            preview.camera_tabs,
            preview.fit_button,
            preview.refresh_button,
            preview.controls,
        ):
            assert_inside(control, preview)
        for spin in preview.spins.values():
            assert spin.width() < 200
            if workspace == "size":
                assert_inside(spin, preview)
        assert composition.application.snapshot() == before
        assert window.event_frame.height() == 44 and not window.event_label.text()
        assert window.grab().save(
            str(tmp_path / f"recipe-{language}-{size[0]}x{size[1]}-{workspace}.png")
        )
