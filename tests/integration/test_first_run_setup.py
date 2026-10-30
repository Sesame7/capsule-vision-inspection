"""Real Qt first-run editing against isolated definitions and a serial fake engine."""

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, get_ident

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
)
from tests.support.cycles import image_result
from tests.support.qt import close_window, until

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ModelPreparationRequest,
    ModelPreparationResult,
    ProductionCounters,
)
from capsule_vision.application.basler_connection import (
    ConnectedCamera,
    ConnectionState,
)
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.configuration import (
    ModelDefinition,
    RecipeDefinition,
    load_camera_sources,
    load_machine_layout,
    managed_models,
    save_model_definition,
    save_recipe,
)
from capsule_vision.inference import BoundingRect, ImageResult, SegmentationModuleInfo
from capsule_vision.inspection import CameraId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui import recipe_page
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.image_tools import image_icon
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.style import production_icon, theme_colors
from capsule_vision.ui.vision_page import VisionPage
from capsule_vision.web.publisher import SnapshotPublisher


class Engine:
    def __init__(self) -> None:
        self.threads: list[int] = []
        self.closed = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.threads.append(get_ident())
        return image_result(np.asarray(image_bgr, dtype=np.uint8))

    def close(self) -> None:
        self.threads.append(get_ident())
        self.closed = True


@pytest.fixture
def first_run(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[MainWindow, VisionPage, ApplicationData, list[Engine]]]:
    data = ApplicationData.at_entry(tmp_path)
    engines: list[Engine] = []

    def factory(_prepared: object) -> Engine:
        engine = Engine()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        window.navigate(4)
        until(lambda: not window.recipe_page.busy)
        workspace = window.vision_workspace
        assert workspace is not None
        yield window, workspace, data, engines
    finally:
        until(
            lambda: (
                not window.recipe_page.busy and not window.recipe_page.labels_loading
            )
        )
        if startup.editor is not None:
            startup.editor.machine_editor.discard()
            if startup.editor.camera_sources is not None:
                startup.editor.camera_sources.discard()
            startup.editor.discard()
        close_window(window)
        delivery.detach()


def write_image(path: Path, width: int = 61, height: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", np.full((height, width, 3), 40, dtype=np.uint8))
    assert ok
    encoded.tofile(path)


def choose_model_file(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    def choose(*_args: object, **_kwargs: object) -> tuple[str, str]:
        return str(source), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose)


def test_empty_vision_sources_save_independently_and_cam6_is_a_placeholder(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
) -> None:
    window, workspace, data, engines = first_run
    assert list(workspace.camera_cards) == [f"CAM{i}" for i in range(1, 7)]
    assert all(
        card.isCheckable() and card.folder.isEnabled()
        for card in workspace.camera_cards.values()
    )
    assert not workspace.canvas.rois and workspace.canvas.model.size is None
    assert workspace.canvas.model.rect is None
    assert workspace.sources is not None and not workspace.sources.persisted
    assert workspace.buttons["save"].text() == ""
    assert workspace.buttons["save"].toolTip() == text("save", window.language)
    assert "camera_setup" in window.configuration_messages
    workspace.buttons["save"].click()
    until(lambda: not window.recipe_page.busy)
    assert json.loads(data.sources.read_text(encoding="utf-8")) == {}
    assert (
        not data.machine.exists()
        and not data.recipes.exists()
        and not data.models.exists()
    )
    assert workspace.sources.persisted and not workspace.sources.dirty
    assert window.bridge.application.snapshot().state is ApplicationState.UNCONFIGURED
    assert window.bridge.application.snapshot().counters == ProductionCounters()
    assert not window.bridge.frames.cameras and not engines
    with pytest.raises(ApplicationStateError):
        window.bridge.application.start()
    workspace.camera_cards[CameraId("CAM6")].configuration.click()
    dialog = next(d for d in workspace.findChildren(QMessageBox) if d.isVisible())
    next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) is QMessageBox.ButtonRole.DestructiveRole
    ).click()
    assert len(workspace.draft.camera_ids) == 5
    assert not workspace.camera_cards[CameraId("CAM6")].isCheckable()
    workspace.camera_cards[CameraId("CAM6")].configuration.click()
    assert len(workspace.draft.camera_ids) == 6 and not workspace.draft.rois(
        CameraId("CAM6")
    )
    assert not data.machine.exists()


@pytest.mark.parametrize("available,count", [(True, 5), (True, 6), (False, 6)])
def test_setup_apply_and_start_keep_the_same_owners(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
    available: bool,
    count: int,
) -> None:
    window, workspace, data, engines = first_run
    app, frames, bridge = window.bridge.application, window.bridge.frames, window.bridge
    publisher = SnapshotPublisher(app, frames)
    publisher.publish()
    assert not publisher.cameras
    if count == 5:
        workspace.camera_cards[CameraId("CAM6")].configuration.click()
        dialog = next(d for d in workspace.findChildren(QMessageBox) if d.isVisible())
        next(
            b
            for b in dialog.buttons()
            if dialog.buttonRole(b) is QMessageBox.ButtonRole.DestructiveRole
        ).click()
    for i in range(1, count + 1):
        camera = CameraId(f"CAM{i}")
        path = tmp_path / "frames" / f"cam{i}" / "first.png"
        write_image(path)
        workspace.camera_cards[camera].click()
        until(lambda: not window.recipe_page.busy)
        field = workspace.camera_cards[camera].folder
        field.setText(f"../frames/cam{i}")
        field.textEdited.emit(field.text())
        workspace.refresh_frame()
        until(lambda: not window.recipe_page.busy)
        assert workspace.frame is not None and len(workspace.draft.rois(camera)) == 6
    workspace.loop.click()  # Save one bounded Folder cycle, not a loop.
    if not available:
        for camera in workspace.draft.camera_ids:
            workspace.source_edited("../unavailable", camera)
        until(lambda: not window.recipe_page.busy)
    workspace.buttons["save"].click()
    until(lambda: not window.recipe_page.busy)
    assert data.machine.is_file() and data.sources.is_file()
    assert "camera_setup" not in window.configuration_messages
    assert not workspace.vision_dirty and not engines
    assert (
        load_machine_layout(data.machine).layout.camera_ids
        == workspace.draft.camera_ids
    )
    assert load_camera_sources(data.sources).camera(CameraId("CAM1")).folder == (
        "../frames/cam1" if available else "../unavailable"
    )
    assert app.snapshot().state is ApplicationState.UNCONFIGURED
    assert (
        app.snapshot().counters == ProductionCounters()
        and app.snapshot().active is None
    )
    # Provision definitions after startup; existing explicit Refresh discovers them.
    owner = data.models / "general"
    owner.mkdir(parents=True)
    artifact = owner / "model.vimosln"
    artifact.write_bytes(b"synthetic first-run model")
    save_model_definition(
        owner / "model.json",
        ModelDefinition(
            "general",
            "General",
            str(artifact),
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "2",
        ),
    )
    data.recipes.mkdir()
    save_recipe(
        data.recipes / "selected.json",
        RecipeDefinition("selected", "Selected explicitly", model_id="general"),
    )
    window.navigate(1)
    until(lambda: window.vision_workspace is None)
    page = window.recipe_page
    page.buttons["reload"].click()
    assert page.session is not None and page.session.entry is None
    assert page.session.draft.model_id is None
    page.recipes.setCurrentIndex(0)
    page.recipes.activated.emit(0)
    until(lambda: not page.labels_loading)
    page.buttons["save_apply"].click()
    until(lambda: not page.busy and app.snapshot().state is ApplicationState.READY)
    assert (
        bridge is window.bridge
        and app is bridge.application
        and frames is bridge.frames
    )
    assert len(frames.cameras) == count and len(window.cards) == count
    assert (
        app.snapshot().active is None
        and app.snapshot().counters == ProductionCounters()
    )
    publisher.publish()
    assert len(publisher.cameras) == count and not engines
    if not available:
        started = app.start()
        until(started.done)
        with pytest.raises(ApplicationError):
            started.result()
        assert not engines and app.snapshot().counters == ProductionCounters()
        assert (
            app.snapshot().state is ApplicationState.READY
            and app.snapshot().active is None
        )
    else:
        window.navigate(0)
        window.buttons["operation"].click()
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert len(engines) == 1 and engines[0].closed
        assert len(set(engines[0].threads)) == 1
        assert app.snapshot().candidate == app.snapshot().active


def test_partial_geometry_save_and_discard_keep_sources_and_view_separate(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
) -> None:
    window, workspace, data, engines = first_run
    path = tmp_path / "import.png"
    write_image(path)
    workspace.load_image(path)
    until(lambda: not window.recipe_page.busy)
    assert workspace.frame is not None
    frame = workspace.frame
    assert len(workspace.draft.rois(CameraId("CAM1"))) == 6
    workspace.commit_geometry(BoundingRect(1, 0, 9, 20))
    assert workspace.sources is not None
    workspace.source_edited("../not-yet-available", CameraId("CAM3"))
    workspace.loop.setChecked(False)
    workspace.fps.setText("3")
    workspace.fps_changed()
    workspace.buttons["save"].click()
    until(lambda: not window.recipe_page.busy)
    saved = load_camera_sources(data.sources)
    assert not saved.loop and saved.target_fps == 3
    assert saved.camera(CameraId("CAM3")).folder == "../not-yet-available"
    assert not data.machine.exists() and workspace.draft.dirty
    assert not workspace.sources.dirty and workspace.frame is frame
    workspace.loop.setChecked(True)
    workspace.buttons["vision_discard"].click()
    dialog = next(d for d in workspace.findChildren(QMessageBox) if d.isVisible())
    next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) is QMessageBox.ButtonRole.DestructiveRole
    ).click()
    until(lambda: not window.recipe_page.busy)
    assert workspace.sources.definition == saved
    assert not workspace.vision_dirty and not workspace.draft.rois(CameraId("CAM1"))
    assert workspace.frame is frame and not data.machine.exists()
    assert (
        not engines
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


def test_first_machine_save_uses_connected_dimensions_without_source_actions(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, data, engines = first_run
    connection = workspace.connection
    assert connection is not None and workspace.sources is not None
    workspace.set_source_mode("basler")
    until(lambda: not window.recipe_page.busy)
    for i, camera in enumerate(workspace.draft.camera_ids, 1):
        workspace.sources.set_serial(camera, str(1000 + i))
    workspace.sources.save()
    observed = replace(
        connection.source_snapshot(),
        state=ConnectionState.CONNECTED,
        cameras=tuple(
            ConnectedCamera(c, str(1000 + i), 61, 20)
            for i, c in enumerate(workspace.draft.camera_ids, 1)
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(connection, "source_snapshot", lambda: observed)
        workspace.refresh()
        assert workspace.draft.complete and workspace.frame is None
        assert workspace.canvas.model.size == (61, 20)
        assert workspace.coordinates["H"].text() == "20"
        workspace.buttons["save"].click()
        until(lambda: not window.recipe_page.busy)
        assert workspace.draft.persisted and not workspace.vision_dirty
        assert load_machine_layout(data.machine) == workspace.draft.saved
        assert connection.source_snapshot() is observed
        assert not workspace.source_editable()
    assert (
        not engines
        and window.bridge.application.snapshot().state is ApplicationState.UNCONFIGURED
    )


def test_empty_gui_import_create_recipe_and_folder_start_without_json_edits(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, workspace, data, engines = first_run
    for i in range(1, 7):
        camera = CameraId(f"CAM{i}")
        write_image(tmp_path / "frames" / f"cam{i}" / "first.png")
        workspace.camera_cards[camera].click()
        until(lambda: not window.recipe_page.busy)
        workspace.source_edited(f"../frames/cam{i}", camera)
        workspace.refresh_frame()
        until(lambda: not window.recipe_page.busy)
    workspace.loop.click()
    workspace.buttons["save"].click()
    until(lambda: not window.recipe_page.busy)
    window.navigate(1)
    page = window.recipe_page
    assert page.session is not None and page.session.entry is None
    source = tmp_path / "external.vimosln"
    source.write_bytes(b"GUI import fake model")
    gui = get_ident()

    def inspect(
        *_args: object, **_kwargs: object
    ) -> tuple[SegmentationModuleInfo, ...]:
        assert get_ident() != gui
        return (SegmentationModuleInfo("2", "Segmentation", ("defect",)),)

    monkeypatch.setattr(managed_models, "read_model_modules", inspect)

    def labels(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("defect",)

    monkeypatch.setattr(recipe_page, "read_model_labels", labels)
    choose_model_file(monkeypatch, source)
    page.buttons["model_import"].click()
    until(
        lambda: (
            not page.busy
            and page.session is not None
            and bool(page.session.registry.models)
        )
    )
    model = page.session.registry.models[0]
    assert model.model_id == hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    assert page.session.entry is None and page.session.draft.model_id is None
    assert not data.recipes.exists() and not engines
    page.buttons["new_recipe"].click()
    dialog = next(d for d in page.findChildren(QDialog) if d.isVisible())
    dialog.findChildren(QLineEdit)[0].setText("New product")
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    box.button(QDialogButtonBox.StandardButton.Ok).click()
    assert page.session.draft.recipe_id.startswith(datetime.now().strftime("%Y%m%d-"))
    index = next(
        i
        for i in range(page.model.count())
        if model.model_id in page.model.itemText(i)
        or model.expected_sha256[:8] in page.model.itemText(i)
    )
    page.model.setCurrentIndex(index)
    page.model.activated.emit(index)
    until(lambda: bool(page.label_checks) and not page.labels_loading)
    assert set(page.label_checks) == {"defect"}
    page.buttons["save_apply"].click()
    until(
        lambda: (
            not page.busy
            and window.bridge.application.snapshot().state is ApplicationState.READY
        )
    )
    assert page.session.entry is not None
    assert not engines and window.bridge.application.snapshot().active is None
    selected = page.session.prepare(page.session.entry.recipe)
    assert selected.managed_model and selected.model_path == Path(model.model_path)
    assert not (Path(model.model_path).parent / "cache").exists()
    requests: list[ModelPreparationRequest] = []

    def capture(request: ModelPreparationRequest) -> Future[ModelPreparationResult]:
        requests.append(request)
        future: Future[ModelPreparationResult] = Future()
        future.set_result(ModelPreparationResult(request, True, True))
        return future

    with monkeypatch.context() as patch:
        patch.setattr(window.bridge.application, "prepare_model_cache", capture)
        page.buttons["cache_prepare"].click()
        assert len(requests) == 1 and requests[0].managed_model
        assert requests[0].model_path == selected.model_path
    assert window.bridge.application.snapshot().counters == ProductionCounters()
    window.navigate(0)
    window.buttons["operation"].click()
    until(lambda: window.bridge.application.snapshot().counters.cycles_completed == 1)
    until(lambda: window.bridge.application.snapshot().state is ApplicationState.READY)
    assert len(engines) == 1 and engines[0].closed
    assert source.read_bytes() == b"GUI import fake model"


@pytest.mark.parametrize("cancel", [True, False])
@pytest.mark.parametrize("language", ["en", "ti"])
def test_multiple_module_import_is_explicit_and_preserves_dirty_recipe(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    language: Language,
) -> None:
    window, _workspace, data, engines = first_run
    window.set_language(language)
    window.navigate(1)
    page = window.recipe_page
    assert page.session is not None
    page.session.draft.display_name = "Unsaved draft"
    page.changed()
    draft = replace(page.session.draft)
    source = tmp_path / "multi.vimosln"
    source.write_bytes(b"multiple modules")

    def inspect(
        *_args: object, **_kwargs: object
    ) -> tuple[SegmentationModuleInfo, ...]:
        return tuple(
            SegmentationModuleInfo(str(i), f"Segmentation {i}", ("defect",))
            for i in (2, 3)
        )

    monkeypatch.setattr(managed_models, "read_model_modules", inspect)
    choose_model_file(monkeypatch, source)
    page.buttons["model_import"].click()
    until(lambda: not page.busy and page.model_controls.dialog is not None)
    dialog = page.model_controls.dialog
    assert dialog is not None
    choice = dialog.findChild(QComboBox)
    box = dialog.findChild(QDialogButtonBox)
    assert choice is not None and box is not None
    QApplication.processEvents()
    form = dialog.layout()
    assert isinstance(form, QFormLayout)
    assert len(dialog.findChildren(QComboBox)) == 1
    assert choice.isVisible() and form.indexOf(choice) >= 0
    assert dialog.rect().contains(choice.geometry())
    label = form.labelForField(choice)
    assert isinstance(label, QLabel) and label.text() == text("model_module", language)
    field = dialog.findChild(QLineEdit)
    assert field is not None
    assert field.geometry().bottom() < choice.geometry().top()
    assert [choice.itemData(i) for i in range(choice.count())] == ["2", "3"]
    assert box.button(QDialogButtonBox.StandardButton.Ok).text() == text(
        "model_import", language
    )
    assert box.button(QDialogButtonBox.StandardButton.Cancel).text() == text(
        "cancel", language
    )
    assert choice.currentIndex() == -1
    assert not box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert not data.models.exists()
    if cancel:
        dialog.reject()
        until(lambda: not page.busy and page.model_controls.pending is None)
        assert not data.models.exists()
    else:
        choice.setCurrentIndex(1)
        box.button(QDialogButtonBox.StandardButton.Ok).click()
        until(lambda: not page.busy and page.model_controls.pending is None)
        assert page.session.registry.models[0].module_id == "3"
    assert page.session.draft == draft and page.session.dirty
    assert not tuple(data.root.glob(".model-import-*"))
    assert (
        not engines
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )


def test_closing_waits_for_import_metadata_and_removes_unpublished_copy(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _workspace, data, engines = first_run
    window.navigate(1)
    page = window.recipe_page
    entered, release, closed = Event(), Event(), Event()
    source = tmp_path / "slow.vimosln"
    source.write_bytes(b"slow SDK metadata")

    def inspect(
        *_args: object, **_kwargs: object
    ) -> tuple[SegmentationModuleInfo, ...]:
        entered.set()
        assert release.wait(5)
        return (SegmentationModuleInfo("2", "Segmentation", ("defect",)),)

    monkeypatch.setattr(managed_models, "read_model_modules", inspect)
    choose_model_file(monkeypatch, source)
    page.buttons["model_import"].click()
    until(entered.is_set)
    try:
        window.request_editing_close(closed.set)
        assert not closed.is_set() and page.busy
    finally:
        release.set()
    until(closed.is_set)
    assert not page.busy and page.model_controls.pending is None
    assert not data.models.exists() and not tuple(data.root.glob(".model-import-*"))
    assert not engines


@pytest.mark.parametrize("language", ["en", "ti"])
def test_model_actions_preserve_recipe_draft_and_protect_saved_references(
    first_run: tuple[MainWindow, VisionPage, ApplicationData, list[Engine]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
) -> None:
    from capsule_vision.configuration.catalog import RecipeCatalog

    window, _workspace, data, engines = first_run
    window.set_language(language)
    window.navigate(1)
    page = window.recipe_page
    assert page.session is not None
    source = tmp_path / "owned.vimosln"
    source.write_bytes(b"managed actions")

    def inspect(
        *_args: object, **_kwargs: object
    ) -> tuple[SegmentationModuleInfo, ...]:
        return (SegmentationModuleInfo("2", "Segmentation", ("defect",)),)

    monkeypatch.setattr(managed_models, "read_model_modules", inspect)
    choose_model_file(monkeypatch, source)
    page.buttons["model_import"].click()
    until(lambda: not page.busy and bool(page.session and page.session.registry.models))
    model = page.session.registry.models[0]
    page.model.setCurrentIndex(1)
    page.model.activated.emit(1)
    until(lambda: bool(page.label_checks) and not page.labels_loading)
    draft = replace(page.session.draft)
    original = Path(model.model_path).read_bytes()
    dark = page.buttons["model_rename"].icon().pixmap(24, 24).toImage()
    window.set_theme("light")
    assert page.buttons["model_rename"].icon().pixmap(24, 24).toImage() != dark
    window.set_theme("dark")
    assert page.buttons["model_rename"].icon().pixmap(24, 24).toImage() == dark
    cache = Path(model.model_path).parent / "vimo_trt_cache"
    cache.mkdir(parents=True)
    (cache / "engine.trt").write_bytes(b"opaque")
    assert (
        not page.cache_status.toolTip()
        and page.cache_status.accessibleName().endswith(
            text("unknown", window.language)
        )
    )
    assert {"cache_info", "cache_clear", "model_open"}.isdisjoint(page.buttons)
    color = theme_colors(page).primary
    assert page.buttons["model_import"].icon().pixmap(24, 24).toImage() != (
        image_icon("folder", color).pixmap(24, 24).toImage()
    )
    assert page.buttons["cache_prepare"].icon().pixmap(24, 24).toImage() != (
        production_icon("cycles", color).pixmap(24, 24).toImage()
    )
    assert (cache / "engine.trt").read_bytes() == b"opaque"
    assert page.session.draft == draft
    assert (
        not page.cache_status.toolTip()
        and page.cache_status.accessibleName().endswith(
            text("unknown", window.language)
        )
    )
    assert page.buttons["model_rename"].isEnabled()
    page.buttons["model_rename"].click()
    form = page.model_controls.dialog
    assert form is not None
    QApplication.processEvents()
    assert not form.findChildren(QComboBox)
    layout = form.layout()
    assert isinstance(layout, QFormLayout)
    field = form.findChildren(QLineEdit)[0]
    label = layout.labelForField(field)
    assert isinstance(label, QLabel) and label.text() == text("model_name", language)
    assert form.rect().contains(field.geometry())
    assert form.rect().contains(label.geometry())
    assert label.geometry().right() < field.geometry().left()
    box = form.findChild(QDialogButtonBox)
    assert box is not None
    assert box.button(QDialogButtonBox.StandardButton.Ok).text() == text(
        "save", language
    )
    assert box.button(QDialogButtonBox.StandardButton.Cancel).text() == text(
        "cancel", language
    )
    field.clear()
    assert not box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    field.setText("Renamed model")
    assert box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    box.button(QDialogButtonBox.StandardButton.Ok).click()

    def renamed() -> bool:
        selected = page.selected_model()
        return (
            not page.busy
            and selected is not None
            and selected.display_name == "Renamed model"
        )

    until(renamed)
    until(lambda: bool(page.label_checks) and not page.labels_loading)
    assert page.session.draft == draft
    selected = page.selected_model()
    assert selected is not None and selected.model_id == model.model_id
    data.recipes.mkdir()
    save_recipe(
        data.recipes / "dependent.json",
        RecipeDefinition("dependent", "Other Recipe", model_id=model.model_id),
    )

    def confirm(key: str) -> None:
        page.buttons[key].click()
        confirmation = page.model_controls.dialog
        assert isinstance(confirmation, QMessageBox)
        next(
            b
            for b in confirmation.buttons()
            if confirmation.buttonRole(b) is QMessageBox.ButtonRole.DestructiveRole
        ).click()
        until(lambda: not page.busy)

    confirm("model_delete")
    assert "dependent" in page.status.toolTip()
    assert Path(model.model_path).is_file() and cache.is_dir()
    catalog = RecipeCatalog(data.recipes)
    catalog.delete(catalog.discover().entries[0], protected_ids=frozenset())
    assert Path(model.model_path).is_file()  # Recipe deletion never deletes its Model.
    assert cache.is_dir() and Path(model.model_path).read_bytes() == original
    confirm("model_delete")
    assert not Path(model.model_path).parent.exists() and not cache.exists()
    assert page.session.draft == draft and page.session.dirty
    assert not page.session.registry.models and page.selected_model() is None
    assert source.read_bytes() == b"managed actions"
    assert (
        not engines
        and window.bridge.application.snapshot().counters == ProductionCounters()
    )
