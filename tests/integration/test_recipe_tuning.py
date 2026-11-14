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
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QLabel
from tests.support.capsule_images import SETTINGS, bodies
from tests.support.cycles import image_result
from tests.support.qt import until
from tests.support.recipe_ui import Screen, assert_inside, edit
from tests.support.recipe_ui import configured_screen as configured_screen

from capsule_vision.application import ConfiguredProductionPipeline
from capsule_vision.application.basler_connection import (
    CameraSnapshot,
    ConnectedCamera,
    ConnectionState,
    SourceConnectionSnapshot,
)
from capsule_vision.application.folder_replay import decode_image
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    MachineLayoutDefinition,
    load_recipe,
)
from capsule_vision.configuration.drafts import CameraSourceDraft, MachineLayoutDraft
from capsule_vision.display import DisplayFrame
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId, CycleId, Frame, FrameId
from capsule_vision.inspection.production import (
    CameraLayout,
    ChannelBusinessStatus,
    ChannelROI,
    InspectionLayout,
    ProductJudgment,
    TechnicalStatus,
)
from capsule_vision.inspection.production.capsule import CapsuleSettings, judge_capsule
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui.recipe_tuning import (
    PreviewMode,
    RecipeTuning,
)


def collapse(preview: RecipeTuning, screen: Screen) -> None:
    assert screen[0].recipe_page.preview is preview
    screen[0].recipe_page.expand_tuning[preview.mode].click()


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
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
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
    window.recipe_page.open_preview("color")
    preview = window.recipe_page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    before, recipe = window.bridge.application.snapshot(), session.draft.to_recipe()
    path = tmp_path / "CAM1/image10.png"
    window.set_language("de")

    def choose_file(
        parent: object, title: str, directory: str, name_filter: str
    ) -> tuple[str, str]:
        assert parent is preview and not directory
        assert title == text("vision_import", "de")
        assert name_filter == f"{title} (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)"
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
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
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
                    SETTINGS.size, target_length=SETTINGS.size.target_length + 50
                ),
            )
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
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
            for mode in ("size", "color"):
                preview.set_mode(mode)
                preview.select_view(0, 0)
                assert not hasattr(preview, "title")
                assert not preview.message.text() and preview.message.isHidden()
                assert "NG" in preview.selection_detail.text()
                assert "—" in preview.selection_detail.text()
                assert all(
                    r and r.assessment.judgment is ProductJudgment.NG
                    for r in preview.results
                )
            # Explicitly compare with the real synchronous Production fusion;
            # this test call is separate from the preview, which never runs Occupancy.
            production = ConfiguredProductionPipeline(
                session.resolve(session.draft.to_recipe())
            ).inspect_frame(
                Frame(preview.frame.frame_id, 1, preview.frame.image.copy()),
                image_result,
            )
            assert all(
                c.business_status is ChannelBusinessStatus.EMPTY
                for c in production.channels
            )
            assert window.bridge.application.snapshot() == before and not runtimes
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
            self,
            camera: CameraId,
            *,
            timeout: float | None = 10.0,
            revision: int | None = None,
        ) -> Future[CameraSnapshot]:
            pytest.fail("Preview must not acquire")

    connection = Connection()
    window.recipe_page.open_preview("color")
    preview = window.recipe_page.preview
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
    import capsule_vision.ui.recipe_tuning as module

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
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
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
            assert window.recipe_page.preview is preview
    finally:
        release.set()
    if close:
        until(lambda: window.recipe_page.preview is None)
        assert not any(t.name == "CapsulePreview" for t in threads())
    else:
        until(lambda: not preview.busy)
        assert preview.frame and preview.frame.frame_id.camera_id == "CAM3"
        assert len(preview.results) == 6 and "image2.png" in preview.reference.toolTip()


def setup(screen: Screen, *, pixel_scale: int = 1) -> RecipeTuning:
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
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    return preview


def test_length_controls_and_complete_crossing_axis(configured_screen: Screen) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    window.set_language("en")
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    image[22:62, 20:30] = (0, 180, 180)
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.concatenate([image] * 6, axis=1),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    preview.select_view(0, 0)
    assert set(preview.spins) == {"target_length", "length_tolerance"}
    assert "Length 39.00" in preview.selection_detail.text()
    measured = preview.measurements[0].measured
    assert measured is not None and measured.views[0].body is not None
    assert measured.views[1].body is None
    body = measured.views[0].body
    assert max(y for _, y in body.endpoints) == 61
    transform = preview.canvas.model.transform(
        preview.canvas.width(), preview.canvas.height()
    )
    assert transform is not None
    pixels = preview.canvas.grab().toImage()
    for x, y in body.endpoints:
        point = QPoint(
            round(transform.x + x * transform.scale),
            round(transform.y + y * transform.scale),
        )
        assert pixels.pixelColor(point).name() == "#ffff00"
    original = session.draft.capsule
    for language in LANGUAGES:
        window.set_language(language)
        assert preview.canvas.selection == (0, 0)
        assert preview.measurements[0].measured is measured
        assert text("length", language) in preview.selection_detail.text()
        for key, spin in preview.spins.items():
            assert text(key, language) in spin.accessibleName()
            assert_inside(spin, preview)
    assert session.draft.capsule == original
    collapse(preview, configured_screen)


def test_cached_judgment_and_isolation_and_save(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    window, session, runtimes = configured_screen
    before = window.bridge.application.snapshot()
    preview = setup(configured_screen)
    assert all(
        result and result.assessment.judgment is ProductJudgment.OK
        for result in preview.results
    )
    for row, result in zip(preview.measurements, preview.results, strict=True):
        assert row.measured is not None
        assert result == judge_capsule(
            row.measured,
            replace(
                session.draft.capsule,
                color=replace(session.draft.capsule.color, enabled=False),
            ),
        )

    def forbidden(*args: object) -> None:
        pytest.fail("Threshold edit must not segment again")

    monkeypatch.setattr(module, "measure_capsule", forbidden)
    preview.spins["target_length"].setValue(25)
    assert all(
        result and result.assessment.judgment is ProductJudgment.NG
        for result in preview.results
    )
    preview.spins["length_tolerance"].setValue(5)
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
    assert window.recipe_page.preview is None
    window.recipe_page.save()
    until(lambda: not window.recipe_page.busy)
    assert session.entry and load_recipe(session.entry.path).capsule == expected
    candidate = window.bridge.application.snapshot().candidate_prepared
    assert candidate is not None
    assert candidate.configuration.capsule == expected
    assert window.bridge.application.snapshot() == replace(
        before, candidate_prepared=candidate
    )
    assert not runtimes and window.bridge.application.snapshot().active is before.active


@pytest.mark.parametrize("language", ("zh", "en"))
def test_manual_hue_wrap_sampling_and_save_discard_reuse_evidence(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, language: Language
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    preview.set_mode("color")
    window.set_language(language)
    page = window.recipe_page
    page.save()
    until(lambda: not page.busy)
    assert session.entry is not None and not session.dirty
    original = session.draft.capsule
    persisted = session.entry.path.read_bytes()
    before = window.bridge.application.snapshot()
    frame, evidence, camera = preview.frame, preview.measurements, preview.camera
    preview.select_view(0, 0)
    changes: list[bool] = []
    changed = preview.changed

    def observed_change() -> None:
        changes.append(True)
        changed()

    def forbidden(*args: object) -> None:
        pytest.fail("Hue edits and sampling must reuse cached measurements")

    preview.changed = observed_change
    monkeypatch.setattr(module, "measure_reference", forbidden)
    assert (preview.hue.minimum(), preview.hue.maximum(), preview.hue.singleStep()) == (
        0,
        179,
        1,
    )
    assert text("target_color", language) in preview.hue.toolTip()
    assert preview.hue.accessibleName() == preview.hue.lineEdit().accessibleName()

    for theme in ("dark", "light"):
        window.set_theme(theme)
        for mode in ("size", "color"):
            preview.set_mode(mode)
            preview.show_settings()
    assert not changes and not session.dirty
    preview.hue.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(preview.hue, Qt.Key.Key_Tab)
    assert preview.sample.hasFocus()
    QTest.keyClick(preview.sample, Qt.Key.Key_Tab)
    assert preview.tolerance.hasFocus()
    field = preview.hue.lineEdit()
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, "70")
    QTest.keyClick(field, Qt.Key.Key_Return)
    assert session.draft.capsule == replace(
        original, color=replace(original.color, target_hue=70)
    )
    assert session.dirty and session.entry.path.read_bytes() == persisted
    assert QColor.fromHsv(140, 220, 220).name() in preview.swatch.styleSheet()
    assert preview.swatch.toolTip().endswith("H 70")
    assert "70 ±" in preview.selection_detail.text()
    assert (
        page.expand_tuning["color"].text() == f"H 70 ± {original.color.hue_tolerance}"
    )
    assert all(
        r and r.assessment.judgment is ProductJudgment.NG for r in preview.results
    )
    assert preview.canvas.results == preview.results

    for start, key, expected in (
        (179, Qt.Key.Key_Up, 0),
        (0, Qt.Key.Key_Down, 179),
        (73, Qt.Key.Key_Up, 74),
        (73, Qt.Key.Key_Down, 72),
    ):
        field.selectAll()
        QTest.keyClicks(field, str(start))
        assert preview.hue.value() == start
        count = len(changes)
        QTest.keyClick(preview.hue, key)
        assert preview.hue.value() == session.draft.capsule.color.target_hue == expected
        assert len(changes) == count + 1
        assert session.draft.capsule == replace(
            original, color=replace(original.color, target_hue=expected)
        )
        assert (
            QColor.fromHsv(expected * 2, 220, 220).name() in preview.swatch.styleSheet()
        )

    page.save()
    until(lambda: not page.busy)
    assert (
        not session.dirty
        and load_recipe(session.entry.path).capsule.color.target_hue == 72
    )
    preview.sample.click()
    assert preview.hue.value() == session.draft.capsule.color.target_hue == 30
    assert preview.swatch.toolTip().endswith("H 30")
    assert all(
        r and r.assessment.judgment is ProductJudgment.OK for r in preview.results
    )
    assert session.draft.capsule == original and session.dirty
    count = len(changes)
    session.discard()
    page.show_draft()
    assert not session.dirty and preview.hue.value() == 72
    assert len(changes) == count
    for mode in ("size", "color", "size", "color"):
        preview.set_mode(mode)
        preview.show_settings()
    assert len(changes) == count and not session.dirty
    assert preview.frame is frame and preview.measurements is evidence
    assert preview.camera == camera and preview.canvas.selection == (0, 0)
    assert not preview.busy and not runtimes
    candidate = window.bridge.application.snapshot().candidate_prepared
    assert candidate is not None
    assert candidate.configuration.capsule == session.entry.recipe.capsule
    assert window.bridge.application.snapshot() == replace(
        before, candidate_prepared=candidate
    )


def test_manual_hue_permissions_disabled_color_and_close(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    window, session, _ = configured_screen
    preview = setup(configured_screen)
    preview.set_mode("color")
    original = session.draft.capsule
    preview.allowed = lambda: False
    preview.refresh_state()
    assert not preview.hue.isEnabled()
    preview.hue.setValue(67)
    assert session.draft.capsule == original
    preview.show_settings()
    assert preview.hue.value() == original.color.target_hue
    preview.allowed = window.recipe_page.editable
    window.recipe_page.capsule_enabled["color"].click()
    until(lambda: not preview.busy)
    disabled = session.draft.capsule
    assert not disabled.color.enabled and preview.hue.isEnabled()
    assert not preview.sample.isEnabled()

    def forbidden(*args: object) -> None:
        pytest.fail("Editing a disabled Color target must not measure or enable it")

    monkeypatch.setattr(module, "measure_reference", forbidden)
    preview.hue.setValue(41)
    assert session.draft.capsule == replace(
        disabled, color=replace(disabled.color, target_hue=41)
    )
    assert (
        not preview.results[0]
        or preview.results[0].assessment.judgment is not ProductJudgment.NG
    )
    closing = session.draft.capsule
    preview.request_close(lambda: None)
    assert preview.closing and not preview.hue.isEnabled()
    preview.hue.setValue(179)
    assert session.draft.capsule == closing


@pytest.mark.parametrize("partial", (False, True))
def test_length_sample_uses_cached_valid_axes_and_changes_only_target(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    session.draft.capsule = replace(
        SETTINGS, size=replace(SETTINGS.size, target_length=292, length_tolerance=10)
    )
    preview.show_settings()
    if partial:
        row = preview.measurements[0]
        assert row.measured is not None
        upper, lower = row.measured.views
        assert upper.body is not None and lower.body is not None
        first = replace(
            row.measured,
            views=(
                replace(upper, body=replace(upper.body, length=291.25)),
                replace(lower, body=replace(lower.body, length=292.75)),
            ),
        )
        second = replace(
            row.measured,
            views=(
                replace(upper, body=None, invalid_reason="missing_body"),
                replace(lower, body=replace(lower.body, length=310.0)),
            ),
        )
        preview.measurements = (
            replace(row, measured=first),
            replace(preview.measurements[1], measured=second),
            *(
                replace(r, measured=None, error="unavailable")
                for r in preview.measurements[2:]
            ),
        )
    frame, evidence = preview.frame, preview.measurements
    original = session.draft.capsule
    before = window.bridge.application.snapshot()

    def forbidden(*args: object) -> None:
        pytest.fail("Sampling/threshold changes must reuse the frozen measurements")

    monkeypatch.setattr(module, "measure_reference", forbidden)
    preview.length_sample.click()
    expected = 293 if partial else 29  # Major-axis scalars, not 80×120 ROIs.
    assert session.draft.capsule == replace(
        original, size=replace(original.size, target_length=expected)
    )
    assert preview.frame is frame and preview.measurements is evidence
    assert not preview.busy and session.dirty
    window.recipe_page.save()
    until(lambda: not window.recipe_page.busy)
    assert session.entry is not None and not session.dirty
    assert load_recipe(session.entry.path).capsule == session.draft.capsule
    preview.spins["target_length"].setValue(expected + 5)
    session.discard()
    window.recipe_page.show_draft()
    assert preview.spins["target_length"].value() == expected and not session.dirty
    candidate = window.bridge.application.snapshot().candidate_prepared
    assert (
        candidate is not None
        and candidate.configuration.capsule == session.entry.recipe.capsule
    )
    assert window.bridge.application.snapshot() == replace(
        before, candidate_prepared=candidate
    )
    assert not runtimes


def test_length_sample_unavailable_disabled_and_denied_preserve_target(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    before = session.draft.capsule
    preview.allowed = lambda: False
    preview.refresh_state()
    assert (
        not preview.length_sample.isEnabled() and not preview.length_slider.isEnabled()
    )
    preview.sample_length()
    assert session.draft.capsule == before
    preview.allowed = window.recipe_page.editable
    window.bridge.frames.publish(
        Frame(
            FrameId(CycleId(2), CameraId("CAM1"), 2),
            2,
            np.zeros((120, 480, 3), dtype=np.uint8),
        ),
        generation=0,
    )
    preview.refresh_reference()
    until(lambda: not preview.busy)
    dirty = session.dirty
    preview.length_sample.click()
    assert session.draft.capsule == before and session.dirty == dirty
    assert preview.message.text() == text("length_sample_missing", preview.language)
    window.recipe_page.capsule_enabled["size"].click()
    until(lambda: not preview.busy)
    disabled = session.draft.capsule
    preview.sample_length()
    assert not preview.length_sample.isEnabled() and session.draft.capsule == disabled


@pytest.mark.parametrize("tolerance", (0, 1_500_001, 2**40))
def test_length_numeric_and_slider_preserve_unbounded_saved_values(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, tolerance: int
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    window, session, _ = configured_screen
    preview = setup(configured_screen)
    original = replace(
        SETTINGS,
        size=replace(SETTINGS.size, target_length=2**40, length_tolerance=tolerance),
    )
    session.draft.capsule = original
    window.recipe_page.save()
    until(lambda: not window.recipe_page.busy)
    assert not session.dirty

    def forbidden(*args: object) -> None:
        pytest.fail("Numeric/slider changes must not remeasure")

    monkeypatch.setattr(module, "measure_reference", forbidden)
    for mode in ("size", "color", "size"):
        preview.set_mode(mode)
        assert session.draft.capsule == original and not session.dirty
    assert preview.spins["target_length"].value() == 2**40
    assert preview.spins["length_tolerance"].value() == tolerance
    target = preview.spins["target_length"].lineEdit()
    target.setFocus()
    target.selectAll()
    QTest.keyClicks(target, str(2**40 + 17))
    assert session.draft.capsule.size.target_length == 2**40 + 17
    assert session.draft.capsule.size.length_tolerance == tolerance
    QTest.keyClick(preview.spins["target_length"], Qt.Key.Key_Up)
    assert session.draft.capsule.size.target_length == 2**40 + 18
    if tolerance <= 2**31 - 1:
        assert preview.length_slider.value() == tolerance
        assert preview.length_slider.isEnabled()
        preview.length_slider.setValue(12)
        assert preview.spins["length_tolerance"].value() == 12
        assert session.draft.capsule.size.length_tolerance == 12
    else:
        assert not preview.length_slider.isEnabled()
        preview.length_slider.setValue(12)
        assert session.draft.capsule.size.length_tolerance == tolerance
    # Dragging does not rescale beneath the pointer; release/numeric entry can
    # recover a useful range after an exceptionally large saved tolerance.
    edit(preview.spins["length_tolerance"].lineEdit(), "1000")
    preview.length_slider.setSliderDown(True)
    preview.length_slider.setValue(250)
    assert preview.length_slider.maximum() == 1000
    assert session.draft.capsule.size.length_tolerance == 250
    preview.length_slider.setSliderDown(False)
    assert preview.length_slider.maximum() == 250
    edit(preview.spins["length_tolerance"].lineEdit(), "37")
    assert preview.length_slider.isEnabled() and preview.length_slider.value() == 37
    assert preview.length_slider.maximum() == 100
    target.setFocus()
    target.selectAll()
    QTest.keyClick(target, Qt.Key.Key_Backspace)
    QTest.keyClicks(target, "-x")
    assert target.text() == ""  # Invalid text never becomes a draft value.
    QTest.keyClick(target, Qt.Key.Key_Tab)
    assert target.text() == str(2**40 + 18)
    assert session.draft.capsule.size == replace(
        original.size, target_length=2**40 + 18, length_tolerance=37
    )
    assert session.draft.capsule.color == original.color
    session.discard()
    window.recipe_page.show_draft()
    assert session.draft.capsule == original and not session.dirty


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
    # The new collapsed card may consume the former spare height; preserve the
    # canvas's existing 240-pixel usable minimum and scroll the summary if needed.
    assert preview.canvas.width() > 500 and preview.canvas.height() >= 240
    assert preview.frame is not None and len(preview.results) == 6
    for widget in (
        preview.camera_tabs,
        preview.fit_button,
        preview.refresh_button,
        preview.controls,
    ):
        assert_inside(widget, preview)
    for spin in preview.spins.values():
        assert spin.width() < 200
        assert_inside(spin, preview)
    assert window.event_frame.height() == 44 and not window.event_label.text()
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
    window.recipe_page.open_preview("color")
    preview = window.recipe_page.preview
    assert preview is not None and preview.frame is None
    assert preview.measurements == () and preview.results == () and not preview.busy
    assert not preview.sample.isEnabled()
    preview.select_view(0, 0)
    assert (
        preview.canvas.selection is None
        and "Length " not in preview.selection_detail.text()
    )
    collapse(preview, configured_screen)


def test_stale_job_and_observable_close(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import capsule_vision.ui.recipe_tuning as module

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
    draft = configured_screen[1].draft.capsule
    assert not preview.length_sample.isEnabled()
    preview.sample_length()
    assert configured_screen[1].draft.capsule == draft
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
    preview.sample_length()
    assert configured_screen[1].draft.capsule == draft
    release.set()
    until(lambda: bool(closed))
    assert not any(t.name == "CapsulePreview" for t in threads())


def test_preview_read_only_during_runtime_and_navigation_releases(
    configured_screen: Screen,
) -> None:
    from capsule_vision.application import ApplicationState

    window, session, runtimes = configured_screen
    preview = setup(configured_screen)
    before = session.draft.capsule
    future = window.bridge.application.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(future.done)
    window.bridge.refresh()
    assert (
        not configured_screen[0].recipe_page.capsule_enabled[preview.mode].isEnabled()
        and not preview.controls.isEnabled()
    )
    preview.sample_color()
    preview.sample_length()
    preview.set_mode("color")
    assert not preview.hue.isEnabled()
    preview.hue.setValue((before.color.target_hue + 1) % 180)
    assert session.draft.capsule == before
    window.navigate(4)
    until(lambda: window.recipe_page.preview is None)
    assert window.pages.currentWidget() is window.vision_page
    stop = window.bridge.application.stop()
    until(stop.done)
    assert window.bridge.application.snapshot().state is ApplicationState.READY


def test_disabled_preview_never_segments(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import capsule_vision.ui.recipe_tuning as module
    from capsule_vision.inspection.production.capsule import CapsuleSettings

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
    window.recipe_page.open_preview("size")
    preview = window.recipe_page.preview
    assert preview is not None and not preview.busy and not calls
    configured_screen[0].recipe_page.capsule_enabled[preview.mode].setChecked(True)
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
    assert preview.message.isVisible() and not preview.message.toolTip()
    assert preview.message.text() == text("preview_measurement_error", language).format(
        count=6
    )
    assert all("ValueError" in row.error for row in preview.measurements)
    assert preview.message.text() == text("preview_measurement_error", language).format(
        count=6
    )
    collapse(preview, configured_screen)


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
        and preview.results[0].assessment.judgment is ProductJudgment.OK
    )
    assert preview.selection_detail.text() == ""
    assert not preview.selection_detail.toolTip()
    assert text("preview_scope_tip", "en") in preview.canvas.toolTip()
    preview.select_view(0, 1)
    assert "Size OK" in preview.selection_detail.text()
    preview.set_mode("color")
    assert (
        "Color NG" in preview.selection_detail.text()
        and "Hue 45.00" in preview.selection_detail.text()
    )
    assert not preview.selection_detail.toolTip()
    assert preview.results[0] is not None
    assert any(
        r.code == "capsule.color.lower.tolerance" for r in preview.results[0].reasons
    )
    preview.select_view(0, 0)
    assert (
        "Color OK" in preview.selection_detail.text()
        and "Hue 30.00" in preview.selection_detail.text()
    )
    preview.set_mode("size")
    preview.spins["target_length"].setValue(21)
    assert (
        "Size NG" in preview.selection_detail.text()
        and "Length 29.00 [21, 21]" in preview.selection_detail.text()
    )
    window.set_language("zh")
    assert preview.canvas.toolTip() == (
        text("preview_select", "zh") + "\n" + text("preview_scope_tip", "zh")
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
    assert (
        "Length —" if mode == "size" else "Hue —"
    ) in preview.selection_detail.text()
    assert (
        "No measurable body" if mode == "size" else "Insufficient usable color pixels"
    ) in preview.selection_detail.text()
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
        and not preview.selection_detail.toolTip()
        and "ValueError" in preview.measurements[0].error
    )
    window.recipe_page.capsule_enabled[mode].click()
    until(lambda: not preview.busy)
    assert "DISABLED" in preview.selection_detail.text()
    assert (
        "Length —" if mode == "size" else "Hue —"
    ) in preview.selection_detail.text()
    assert not preview.canvas.check_enabled and not preview.message.isVisible()
    preview.canvas.selection = None
    preview.refresh_state()
    assert preview.selection_detail.text() == text("preview_disabled_short", "en")


def test_selection_clears_on_image_camera_change_and_while_evidence_pending(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import capsule_vision.ui.recipe_tuning as module

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
        assert "Length —" in preview.selection_detail.text()
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
    from tests.support.application import production

    from capsule_vision.application import ApplicationState
    from capsule_vision.inspection.production import TechnicalStatus
    from capsule_vision.localization.catalog import text

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
        r and not any(m.name.endswith((".length",)) for m in r.measurements)
        for r in preview.results
    )
    assert window.bridge.application.snapshot() == before and not runtimes
    collapse(preview, configured_screen)


def test_preview_capability_changes_recompute_but_thresholds_rejudge(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    import capsule_vision.ui.recipe_tuning as module

    _, session, _ = configured_screen
    preview = setup(configured_screen)
    original = module.measure_reference
    calls: list[tuple[bool, bool]] = []

    def observed(frame: DisplayFrame, camera: CameraLayout, settings: CapsuleSettings):
        calls.append((settings.size.enabled, settings.color.enabled))
        return original(frame, camera, settings)

    monkeypatch.setattr(module, "measure_reference", observed)
    configured_screen[0].recipe_page.capsule_enabled[preview.mode].setChecked(
        False
    )  # Color-only
    until(lambda: not preview.busy)
    assert calls == [(False, True)]
    preview.set_mode("color")
    preview.tolerance.setValue(12)
    preview.sample.click()
    assert calls == [(False, True)]  # Targets and tolerances only rejudge.
    configured_screen[0].recipe_page.capsule_enabled[preview.mode].setChecked(
        False
    )  # Neither: clear stale evidence, no job.
    assert not preview.busy and not preview.measurements and not preview.results
    assert calls == [(False, True)]
    preview.set_mode("size")
    configured_screen[0].recipe_page.capsule_enabled[preview.mode].setChecked(
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
    import capsule_vision.ui.recipe_tuning as module

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
    page = window.recipe_page
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
    assert configured_screen[0].recipe_page.capsule_enabled[
        preview.mode
    ].isChecked() is (color_enabled if preview.mode == "color" else size_enabled)
    page.capsule_enabled[preview.mode].click()
    until(lambda: not preview.busy)
    preview.set_mode("size")
    preview.spins["target_length"].setValue(21)
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
        size=replace(SETTINGS.size, enabled=mode != "size", target_length=25),
        color=replace(SETTINGS.color, enabled=mode != "color", target_hue=50),
    )
    preview.set_mode("size" if (0 if mode == "size" else 1) == 0 else "color")
    preview.show_settings()
    until(lambda: not preview.busy)
    assert (
        not configured_screen[0].recipe_page.capsule_enabled[preview.mode].isChecked()
    )
    assert configured_screen[0].recipe_page.capsule_enabled[preview.mode].isVisible()
    assert all(
        r and r.assessment.technical_status is TechnicalStatus.DISABLED
        for r in preview.results
    )
    assert all(
        row.measured
        and judge_capsule(row.measured, session.draft.capsule).assessment.judgment
        is ProductJudgment.NG
        for row in preview.measurements
    )
    assert preview.canvas.results == preview.results
    measurements, results = preview.measurements, preview.results
    preview.set_mode("size" if (1 if mode == "size" else 0) == 0 else "color")
    assert configured_screen[0].recipe_page.capsule_enabled[preview.mode].isChecked()
    assert preview.measurements is measurements and preview.results != results
    assert all(
        r and r.assessment.judgment is ProductJudgment.NG for r in preview.results
    )
    assert window.bridge.application.snapshot() == before and not runtimes
    collapse(preview, configured_screen)


@pytest.mark.parametrize("size", ((1080, 1920), (1920, 1080)))
@pytest.mark.parametrize("language", ("zh", "en"))
def test_integrated_controls_precede_canvas_and_enablement_is_separate(
    configured_screen: Screen, size: tuple[int, int], language: Language
) -> None:
    window, session, _ = configured_screen
    preview = setup(configured_screen)
    window.resize(*size)
    window.set_language(language)
    session.draft.capsule = replace(
        session.draft.capsule, color=replace(session.draft.capsule.color, target_hue=60)
    )
    preview.show_settings()
    page = window.recipe_page
    assert not preview.findChildren(QCheckBox)
    assert preview.controls.y() + preview.controls.height() <= preview.canvas.y()
    before = session.draft.capsule
    frozen = preview.frame
    page.expand_tuning["color"].click()
    assert (
        page.preview is preview and preview.frame is frozen and preview.mode == "color"
    )
    assert session.draft.capsule == before
    for theme in ("dark", "light"):
        window.set_theme(theme)
        preview.select_view(0, 0)  # Size OK has two lines; Color NG adds a reason.
        QApplication.processEvents()
        viewport = preview.canvas.geometry()
        for mode in ("size", "color", "size", "color"):
            preview.set_mode(mode)
            QApplication.processEvents()
            assert preview.canvas.geometry() == viewport
            assert preview.frame is frozen and page.preview is preview
    page.capsule_enabled["color"].click()
    until(lambda: not preview.busy)
    assert (
        session.draft.capsule.size.enabled and not session.draft.capsule.color.enabled
    )
    assert page.expand_tuning["color"].isChecked()
    collapse(preview, configured_screen)


def _assert_preview_zoom_selection(
    screen: Screen, preview: RecipeTuning, scale: float | None
) -> None:
    window, session, runtimes = screen
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
            assert "Length 89.00 [29, 29]" in detail
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


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_preview_zoom_selection_by_orientation(
    configured_screen: Screen, size: tuple[int, int]
) -> None:
    window, _, _ = configured_screen
    preview = setup(configured_screen, pixel_scale=3)
    window.set_language("en")
    window.resize(*size)
    QApplication.processEvents()
    frozen = preview.frame
    for scale in (None, 1.0, 1.5):
        try:
            preview.set_mode("size")
            preview.canvas.selection = None
            preview.evaluate()
            assert preview.canvas.selection is None
            # The helper's zoom resets pan and computes a new transform each row.
            _assert_preview_zoom_selection(configured_screen, preview, scale)
            assert preview.frame is frozen
        except Exception as exc:
            exc.add_note(f"Tune selection: size={size}, scale={scale}")
            raise
