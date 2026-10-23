from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLineEdit,
    QMessageBox,
    QScrollArea,
)
from tests.qt_support import assert_full_caption, until
from tests.recipe_ui_support import Screen, assert_inside, edit
from tests.recipe_ui_support import configured_screen as configured_screen

from vimo_demo.application import (
    ApplicationState,
    ModelPreparationState,
    PreparedConfiguration,
)
from vimo_demo.application.image_archive import ArchiveSnapshot
from vimo_demo.application.preferences import LANGUAGES
from vimo_demo.configuration import (
    ConfigurationError,
    ConfigurationIOError,
    ImageArchiveSettings,
    ModelRegistry,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)
from vimo_demo.ui.parameters_page import DirtyDecision
from vimo_demo.ui.production_style import Language, Style, Theme, text


def test_archive_draft_save_discard_browse_and_runtime_locks(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    window, session, runtimes = configured_screen
    page, app = window.parameters, window.bridge.application
    assert session.entry is not None
    before = app.snapshot()
    session.load(
        next(
            entry
            for entry in session.catalog.discover().entries
            if entry.recipe.channel_overrides
        )
    )
    page.show_draft()
    original = session.entry.recipe
    assert original.camera_overrides and original.channel_overrides
    assert not original.image_archive.enabled
    page.archive_checks["ng"].click()
    assert session.dirty
    with pytest.raises(ConfigurationError, match="absolute"):
        session.draft.to_recipe()
    directory = str(tmp_path / "archives")

    def choose_directory(*_args: object) -> str:
        return directory

    monkeypatch.setattr(
        "vimo_demo.ui.parameters_page.QFileDialog.getExistingDirectory",
        choose_directory,
    )
    page.buttons["archive_browse"].click()
    assert session.draft.image_archive.root_directory == directory
    page.archive_checks["error"].click()
    page.archive_checks["empty"].click()
    session.save()
    assert session.entry.recipe.image_archive == ImageArchiveSettings(
        directory, False, True, True, True
    )
    assert session.entry.recipe.defaults == original.defaults
    assert session.entry.recipe.camera_overrides == original.camera_overrides
    assert session.entry.recipe.channel_overrides == original.channel_overrides
    assert app.snapshot() == before and not runtimes
    page.archive_checks["ok"].click()
    page.archive_checks["empty"].click()
    edit(page.archive_directory, str(tmp_path / "discarded"))
    session.discard()
    page.show_draft()
    assert not session.dirty and page.archive_directory.text() == directory
    assert not page.archive_checks["ok"].isChecked()
    assert (
        page.archive_checks["ng"].isChecked()
        and page.archive_checks["error"].isChecked()
        and page.archive_checks["empty"].isChecked()
    )
    assert not Path(directory).exists()  # Draft/save never creates archive directories.
    directory_path = Path(directory)
    directory_path.mkdir()
    opened: list[str] = []

    def open_directory(url: object) -> bool:
        opened.append(str(url))
        return True

    monkeypatch.setattr(
        "vimo_demo.ui.parameters_page.QDesktopServices.openUrl", open_directory
    )
    page.buttons["archive_open"].click()
    assert len(opened) == 1 and not session.dirty
    app.start()
    window.bridge.refresh()
    assert not page.archive_directory.isEnabled()
    assert not any(check.isEnabled() for check in page.archive_checks.values())
    assert not page.buttons["archive_browse"].isEnabled()
    assert not page.buttons["archive_open"].isEnabled()
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    app.stop().result(3)
    window.bridge.refresh()
    assert page.archive_directory.isEnabled() and not session.dirty


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_archive_responsive_groups_label_hit_targets_and_locks(
    configured_screen: Screen, language: Language, theme: Theme
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.set_theme(theme)
    page = window.parameters
    assert tuple(page.archive_checks) == ("ok", "ng", "error", "empty")
    heights: list[int] = []
    for width, height in ((1080, 1920), (1920, 1080), (1080, 1920)):
        window.resize(width, height)
        QApplication.processEvents()
        conditions = page.archive_checks["ok"]
        directory_y = page.archive_directory.mapTo(page.archive_section, QPoint()).y()
        conditions_y = conditions.mapTo(page.archive_section, QPoint()).y()
        assert (directory_y > conditions_y) == (height > width)
        heights.append(page.archive_section.height())
        for widget in (
            page.archive_directory,
            *page.archive_checks.values(),
            page.buttons["archive_browse"],
            page.buttons["archive_open"],
        ):
            assert_inside(widget, page.archive_section)
        assert (
            page.buttons["archive_browse"].toolTip()
            != page.buttons["archive_open"].toolTip()
        )
        assert (
            page.buttons["archive_browse"].icon().pixmap(24, 24).toImage()
            != page.buttons["archive_open"].icon().pixmap(24, 24).toImage()
        )
    assert heights[0] == heights[2] and heights[0] > heights[1]
    previous_max = page.summary_scroll.maximumHeight()
    page.summary_scroll.setMaximumHeight(page.archive_section.sizeHint().height())
    QApplication.processEvents()
    assert page.summary_scroll.verticalScrollBar().maximum() > 0
    page.summary_scroll.ensureWidgetVisible(page.archive_directory)
    QApplication.processEvents()
    assert_inside(page.archive_directory, page)
    assert_inside(page.buttons["save_apply"], page)
    page.summary_scroll.setMaximumHeight(previous_max)
    QApplication.processEvents()
    assert page.summary_scroll.verticalScrollBar().maximum() == 0
    for check in page.archive_checks.values():
        # Click in the label/padded right edge, away from the indicator.
        point = QPoint(check.width() - 5, check.height() // 2)
        QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=point)
        assert check.isChecked()
        QTest.mouseClick(check, Qt.MouseButton.LeftButton, pos=point)
        assert not check.isChecked()
    assert not session.dirty
    app = window.bridge.application
    app.start()
    window.bridge.refresh()
    for check in page.archive_checks.values():
        QTest.mouseClick(
            check,
            Qt.MouseButton.LeftButton,
            pos=QPoint(check.width() - 5, check.height() // 2),
        )
        assert not check.isChecked()
    assert not session.dirty
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    app.stop().result(3)


def test_archive_failures_are_observable_without_changing_production(
    configured_screen: Screen,
) -> None:
    window, _session, runtimes = configured_screen
    app = window.bridge.application
    before = app.snapshot()

    class Observation:
        def archive_snapshot(self) -> ArchiveSnapshot:
            return ArchiveSnapshot(
                1, 0, 1, 1, 0, 0, 0, "PermissionError: denied", False
            )

    window.bridge.archive = Observation()
    window.bridge.refresh()
    assert text("archive_issue", window.language) == window.event_label.accessibleName()
    assert "PermissionError" in window.event_label.toolTip()
    assert app.snapshot() == before and not runtimes


@pytest.mark.parametrize("language", ["zh", "en"])
def test_model_cache_preparation_is_queued_identity_bound_and_draft_independent(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, language: Language
) -> None:
    from run_production_ui import DemoEngine

    window, session, runtimes = configured_screen
    app, page = window.bridge.application, window.parameters
    entered, release = Event(), Event()

    def factory(_request: object) -> DemoEngine:
        entered.set()
        assert release.wait(5)
        return DemoEngine("normal", 0, 0)

    monkeypatch.setattr(app, "_preparation_factory", factory)
    window.set_language(language)
    before = app.snapshot()
    assert session.entry is not None
    original_file = session.entry.path.read_bytes()
    try:
        assert page.cache_status.toolTip() == text("cache_unknown", language)
        page.buttons["cache_prepare"].click()
        until(entered.is_set)
        assert app.preparation_snapshot().state is ModelPreparationState.PREPARING
        assert page.cache_status.toolTip() == text("cache_preparing", language)
        assert not page.buttons["cache_prepare"].isEnabled()
        assert not window.buttons["operation"].isEnabled()
        assert not session.dirty and not runtimes and app.snapshot() == before
        # GUI dispatch stays live while native initialization would be blocked.
        dispatch = Event()
        QTimer.singleShot(0, dispatch.set)
        until(dispatch.is_set)
        index = page.model.currentIndex()
        page.model.setCurrentIndex(1 - index)
        page.model.activated.emit(1 - index)
        assert page.cache_status.toolTip() == text("cache_unknown", language)
        release.set()
        until(lambda: not app.preparation_snapshot().owns_resources)
        window.bridge.refresh()
        assert page.cache_status.toolTip() == text("cache_unknown", language)
        page.model.setCurrentIndex(index)
        page.model.activated.emit(index)
        assert page.cache_status.toolTip() == text("cache_initialized", language)
        assert page.buttons["cache_prepare"].isEnabled()
        assert window.buttons["operation"].isEnabled()
        assert not session.dirty and not runtimes and app.snapshot() == before
        assert session.entry.path.read_bytes() == original_file
    finally:
        release.set()


def test_model_cache_failure_is_presented_only_for_its_requested_model(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _runtimes = configured_screen
    page, app = window.parameters, window.bridge.application

    def broken(_request: object) -> None:
        raise ValueError("test initialization failed")

    monkeypatch.setattr(app, "_preparation_factory", broken)
    page.buttons["cache_prepare"].click()
    until(lambda: app.preparation_snapshot().state is ModelPreparationState.FAILED)
    window.bridge.refresh()
    assert "test initialization failed" in page.cache_status.toolTip()
    assert not session.dirty and app.snapshot().state is ApplicationState.READY
    assert page.buttons["cache_prepare"].isEnabled()
    page.model.setCurrentIndex(1)
    page.model.activated.emit(1)
    assert page.cache_status.toolTip() == text("cache_unknown", page.language)
    session.discard()


def test_recipe_cache_action_respects_production_editing_lock(
    configured_screen: Screen,
) -> None:
    window, _session, runtimes = configured_screen
    started = window.bridge.application.start()
    until(lambda: bool(runtimes))
    window.bridge.refresh()
    assert not window.parameters.buttons["cache_prepare"].isEnabled()
    runtimes[0].ready.set_result(None)
    until(started.done)
    window.bridge.refresh()
    assert not window.parameters.buttons["cache_prepare"].isEnabled()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_compact_recipe_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    # Qt honors the shell's effective minimum even if resize requests less.
    effective_width = window.width()
    assert effective_width == max(size[0], window.minimumWidth())
    page = window.parameters
    assert page.recipes.count() == 2 and not session.dirty and not runtimes
    assert set(page.metadata) == {"display_name", "product_code"}
    assert page.findChildren(QComboBox) == [page.recipes, page.model]
    assert page.findChildren(QScrollArea) == [page.summary_scroll]
    assert not page.findChildren(QGroupBox)
    assert len(page.findChildren(QLineEdit)) == 3
    assert set(page.buttons) == {
        "new_recipe",
        "duplicate_recipe",
        "delete_recipe",
        "reload",
        "recipe_discard",
        "save_apply",
        "cache_prepare",
        "archive_browse",
        "archive_open",
    }
    assert window.navigation[1].text() == text("parameters", language)
    for button in page.buttons.values():
        assert page.rect().contains(button.mapTo(page, QPoint(0, 0)))
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    assert session.entry is not None
    model = session.registry.get(session.default_model_id)
    assert (
        page.model.currentText()
        == f"{model.display_name} · {model.expected_sha256[:8]}"
    )
    assert (
        model.model_id in page.model.toolTip()
        and model.expected_sha256 in page.model.toolTip()
    )
    assert (
        "Configured SHA-256" in page.model.toolTip()
        if language == "en"
        else "配置 SHA-256" in page.model.toolTip()
    )
    assert not hasattr(page, "model_more") and not hasattr(page, "advanced")
    assert not hasattr(page, "label_filter")
    assert_inside(page.label_choices, page)
    color = session.draft.capsule.color
    assert (
        page.expand_tuning["color"]
        .text()
        .endswith(f" · H {color.target_hue} ± {color.hue_tolerance}")
    )
    assert page.roi_workspace is None

    sections = (page.recipe_section, page.model_section, page.inspection_section)
    for above, below in zip(sections, sections[1:], strict=False):
        assert above.geometry().bottom() < below.y()
    for control in (
        *sections,
        page.recipes,
        page.model,
        *page.metadata.values(),
        *page.buttons.values(),
    ):
        assert_inside(control, page)
    name, product = page.metadata.values()
    assert abs(name.width() - product.width()) <= 1
    assert page.model.width() > page.width() // 2
    assert (
        page.buttons["save_apply"].geometry().right() >= page.width() - 2 * Style.space
    )
    before = window.bridge.application.snapshot()
    page.expand_tuning["size"].click()
    preview = page.preview
    assert preview is not None and page.tuning_section.isVisible()
    assert (
        page.expand_tuning["size"].isChecked()
        and not page.expand_tuning["color"].isChecked()
    )
    assert_inside(preview, page)
    assert preview.canvas.height() >= 240
    assert window.width() == effective_width
    page.expand_tuning["color"].click()
    assert page.preview is preview and preview.mode == "color"
    assert (
        not session.dirty
        and not runtimes
        and window.bridge.application.snapshot() == before
    )
    page.expand_tuning["color"].click()
    until(lambda: page.preview is None)

    # Vision creates its editor lazily; keep navigation after structural/tuning checks.
    window.navigate(4)
    assert (
        page.roi_workspace is not None
        and page.roi_workspace.isVisible()
        and window.pages.currentWidget() is page.settings
    )
    window.navigate(1)
    assert (
        not session.dirty
        and not runtimes
        and window.bridge.application.snapshot() == before
    )


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_multilingual_recipe_vision_actions_and_tuning_keep_stable_slots(
    configured_screen: Screen, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    window.resize(*size)
    page = window.parameters
    page.open_preview("size")
    preview = page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    QApplication.processEvents()
    before = window.bridge.application.snapshot()
    draft = session.draft.to_recipe()
    fields = tuple(spin.geometry() for spin in preview.spins.values())
    save = page.buttons["save_apply"]
    save_size = save.size()
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert save.size() == save_size
        assert_full_caption(save, text("save_apply", language))
        assert save.accessibleName() == save.toolTip() == text("save_apply", language)
        for key, compact in (
            ("display_name", "short_name"),
            ("product_code", "short_code"),
        ):
            assert page.labels[key].text() == text(compact, language)
            assert (
                page.labels[key].toolTip()
                == page.labels[key].accessibleName()
                == text(key, language)
            )
        for mode in ("size", "color"):
            assert (
                page.expand_tuning[mode].text().startswith(text(mode, language) + " · ")
            )
            assert (
                text("preview_scope_tip", language)
                in page.expand_tuning[mode].toolTip()
            )
        assert tuple(spin.geometry() for spin in preview.spins.values()) == fields
        for key, label in preview.labels.items():
            assert label.text() in ("W (px)", "H (px)", "± W (px)", "± H (px)")
            assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
            assert text(key, language) in label.toolTip() == label.accessibleName()
        assert_inside(preview.controls, preview)
    page.open_preview("color")
    QApplication.processEvents()
    sample_size = preview.sample.size()
    tolerance_size = preview.tolerance_label.size()
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert preview.sample.size() == sample_size
        assert preview.sample.text() == "" and not preview.sample.icon().isNull()
        assert preview.sample.width() == preview.sample.height() == Style.touch
        assert preview.tolerance_label.size() == tolerance_size
        assert text("color_tolerance", language) in preview.tolerance_label.toolTip()
        assert preview.sample.accessibleName() == preview.sample.toolTip()
        assert_inside(preview.controls, preview)
    page.leave_preview(lambda: None)
    until(lambda: page.preview is None)
    window.navigate(4)
    workspace = page.roi_workspace
    assert workspace is not None
    QApplication.processEvents()
    assert workspace.geometry_toolbar.height() == Style.touch
    assert all(line.width() == 72 for line in workspace.coordinates.values())
    sizes = tuple(
        workspace.buttons[key].size() for key in ("roi_set_rect", "roi_save_apply")
    )
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        for key, expected in zip(
            ("roi_set_rect", "roi_save_apply"), sizes, strict=True
        ):
            button = workspace.buttons[key]
            assert button.size() == expected
            if key == "roi_set_rect":
                assert button.text() == "" and not button.icon().isNull()
                assert button.width() == button.height() == Style.touch
            else:
                assert_full_caption(button, text(key, language))
            assert button.accessibleName() == button.toolTip() == text(key, language)
            assert_inside(button, workspace)
    assert session.draft.to_recipe() == draft and not session.dirty and not runtimes
    assert window.bridge.application.snapshot() == before


def test_metadata_model_and_hidden_overrides_preserved(
    configured_screen: Screen,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    assert session.entry is not None
    original = session.entry.recipe
    edit(page.metadata["display_name"], "新产品")
    edit(page.metadata["product_code"], "SKU-中文")
    page.buttons["save_apply"].click()
    until(lambda: not page.busy)
    saved = load_recipe(session.entry.path)
    assert saved == replace(original, display_name="新产品", product_code="SKU-中文")
    assert window.bridge.application.snapshot().active is None
    assert window.choices[-1].prepared.configuration.recipe_id == original.recipe_id
    # Explicit default model IDs survive an unrelated edit; changing the selector
    # deliberately uses the registry/default mapping, never the display name as ID.
    default_index = next(
        i
        for i, m in enumerate(session.registry.models)
        if m.model_id == session.default_model_id
    )
    page.model.setCurrentIndex(default_index)
    page.model.activated.emit(default_index)
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path).model_override_id is None
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if not e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    assert session.draft.defaults == session.entry.recipe.defaults


@pytest.mark.parametrize("action", ["exclude", "all", "unknown"])
def test_default_model_labels_edit_only_defaults_and_persist(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    target = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(target)
    assert session.entry is not None
    catalog = ("Dent", "dent", "黑点", "压痕", "A&B")
    calls: list[int] = []

    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        calls.append(get_ident())
        assert module_id == "2" and len(expected_sha256) == 64
        return catalog

    monkeypatch.setattr("vimo_demo.ui.parameters_page.read_model_labels", metadata)
    disabled = (
        ("Retired",)
        if action == "unknown"
        else ("黑点", "Dent")
        if action == "all"
        else ()
    )
    session.draft.defaults = replace(
        session.draft.defaults,
        vimo=replace(
            session.draft.defaults.vimo,
            enabled=False,
            required=False,
            disabled_labels=disabled,
        ),
    )
    original = session.draft.to_recipe()
    before = window.bridge.application.snapshot()
    saved_bytes = session.entry.path.read_bytes()
    window.navigate(0)
    window.navigate(1)
    until(lambda: not page.labels_loading)
    assert page.findChild(QDialog) is None
    checks = page.label_checks
    assert tuple(checks) == catalog and calls and calls[0] != get_ident()
    assert all(
        check.isChecked() == (label not in disabled) for label, check in checks.items()
    )
    if action == "all":
        for check in checks.values():
            check.setChecked(True)
    else:
        checks["Dent"].setChecked(False)
        assert checks["dent"].isChecked()  # Exact, case-sensitive identity.
    QApplication.processEvents()
    expected = (
        ()
        if action == "all"
        else ("Dent", "Retired")
        if action == "unknown"
        else ("Dent",)
    )
    result = session.draft.to_recipe()
    assert result.defaults.vimo.disabled_labels == expected
    assert (
        result.defaults.vimo.enabled is False and result.defaults.vimo.required is False
    )
    assert (
        result.camera_overrides == original.camera_overrides
        and result.channel_overrides == original.channel_overrides
    )
    assert (
        result.model_override_id == original.model_override_id
        and result.capsule == original.capsule
    )
    assert (
        session.entry.path.read_bytes() == saved_bytes
        and window.bridge.application.snapshot() == before
        and not runtimes
    )
    resolved = session.resolve()
    assert resolved.channels[0].checks.vimo.disabled_labels == expected
    assert resolved.channels[12].checks.vimo.required is False
    assert resolved.channels[16].checks.vimo.required is True
    assert resolved.channels[16].checks.vimo.disabled_labels == ("黑点",)
    page.save_and_apply()
    until(lambda: not page.busy)
    saved = load_recipe(session.entry.path)
    assert saved == result
    session.draft.defaults = replace(
        saved.defaults,
        vimo=replace(saved.defaults.vimo, disabled_labels=("temporary",)),
    )
    page.show_draft()
    page.request_discard()
    confirm = page.findChild(QMessageBox)
    assert confirm is not None
    next(
        b
        for b in confirm.buttons()
        if b.text() == text("recipe_discard", page.language)
    ).click()
    assert session.draft.to_recipe() == saved


@pytest.mark.parametrize("failure", ["sdk", "empty", "stale"])
def test_label_catalog_failure_or_stale_model_keeps_draft(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    session.draft.defaults = replace(
        session.draft.defaults,
        vimo=replace(session.draft.defaults.vimo, disabled_labels=("Saved",)),
    )
    before = session.draft.to_recipe()
    calls: list[str] = []

    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        if failure == "sdk":
            raise RuntimeError("SDK metadata unavailable")
        calls.append("read")
        return () if failure == "empty" else ("Old" if len(calls) == 1 else "Current",)

    monkeypatch.setattr("vimo_demo.ui.parameters_page.read_model_labels", metadata)
    window.navigate(0)
    window.navigate(1)
    if failure == "stale":
        session.draft.model_override_id = "special"
    until(
        lambda: (
            not page.labels_loading
            and (failure != "stale" or "Current" in page.label_checks)
        )
    )
    if failure == "stale":
        assert tuple(page.label_checks) == ("Current",) and len(calls) == 2
    else:
        assert not page.label_checks
        assert page.status.text() == text("recipe_labels_error", page.language)
    assert session.draft.defaults == before.defaults
    assert window.bridge.application.snapshot().active is None


def test_inline_labels_wrap_and_cards_follow_content(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    labels = [tuple(f"Defect label {i:02d}" for i in range(40))]

    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        return labels[0]

    monkeypatch.setattr("vimo_demo.ui.parameters_page.read_model_labels", metadata)
    window.navigate(0)
    window.navigate(1)
    until(lambda: not page.labels_loading)
    heights: list[int] = []
    for width in (1920, 1080):
        window.resize(width, 1200)
        until(lambda: page.label_choices.width() == page.model_section.width() - 22)
        QApplication.processEvents()
        assert len({check.y() for check in page.label_checks.values()}) > 1
        for check in page.label_checks.values():
            assert_inside(check, page.label_choices)
        heights.append(page.model_section.height())
        for card in (page.recipe_section, page.model_section, page.inspection_section):
            assert card.minimumHeight() == 0 and card.maximumHeight() > card.height()
        assert page.summary_scroll.horizontalScrollBar().maximum() == 0
        assert page.summary_scroll.verticalScrollBar().maximum() == 0
    assert heights[1] > heights[0]
    before = session.draft.to_recipe()
    labels[0] = tuple(f"Defect label {i:03d}" for i in range(120))
    window.resize(1080, 800)
    window.navigate(0)
    window.navigate(1)
    until(lambda: not page.labels_loading)
    until(lambda: page.summary_scroll.verticalScrollBar().maximum() > 0)
    assert_inside(page.buttons["save_apply"], page)
    for language in ("zh", "en"):
        window.set_language(language)
        QApplication.processEvents()
        for check in page.label_checks.values():
            assert_inside(check, page.label_choices)
    # Replacing expanded content with a short catalog shrinks both card and area.
    labels[0] = ("Dent", "dent")
    window.resize(1080, 1920)
    window.navigate(0)
    window.navigate(1)
    until(lambda: not page.labels_loading)
    until(lambda: page.summary_scroll.verticalScrollBar().maximum() == 0)
    assert page.model_section.height() < heights[0]
    assert session.draft.to_recipe() == before


def test_inline_labels_recheck_runtime_lock_and_metadata_close_is_observed(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session, runtimes = configured_screen
    page, app = window.parameters, window.bridge.application
    before = session.draft.to_recipe()
    future = app.start()
    until(lambda: bool(runtimes))
    runtimes[0].ready.set_result(None)
    until(future.done)
    # An already delivered click is rechecked, even before the next UI refresh.
    page.label_checks["黑点"].setChecked(False)
    assert session.draft.to_recipe() == before
    window.bridge.refresh()
    assert all(not check.isEnabled() for check in page.label_checks.values())
    stopped = app.stop()
    until(stopped.done)
    window.bridge.refresh()
    entered, release = Event(), Event()
    closed: list[bool] = []

    def blocked(path: Path, *, module_id: str, expected_sha256: str) -> tuple[str, ...]:
        entered.set()
        assert release.wait(5)
        return ("黑点",)

    monkeypatch.setattr("vimo_demo.ui.parameters_page.read_model_labels", blocked)
    window.navigate(0)
    window.navigate(1)
    until(entered.is_set)
    # Catalog IO does not claim readiness or block the unrelated setup slot.
    assert not page.busy and page.editable()
    try:
        page.request_close(lambda: closed.append(True))
        assert not closed and page.labels_loading
    finally:
        release.set()
    until(lambda: bool(closed))
    assert not page.labels_loading and session.draft.to_recipe() == before


def test_save_apply_start_and_running_view_only(configured_screen: Screen) -> None:
    window, session, runtimes = configured_screen
    page, app = window.parameters, window.bridge.application
    assert session.entry is not None
    original = session.entry.recipe
    edit(page.metadata["display_name"], "Updated")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path).display_name == "Updated"
    assert app.snapshot().active is None
    assert "next successful Start" in text("roi_applied", "en")
    assert page.status.text() == text("roi_applied", page.language)
    model_identity = page.model.toolTip()
    window.bridge.start()
    until(lambda: len(runtimes) == 1)
    window.bridge.refresh()
    assert page.model.toolTip() == model_identity
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    window.bridge.refresh()
    assert page.model.toolTip() == model_identity
    assert all(not check.isEnabled() for check in page.label_checks.values())
    active = app.snapshot().active
    assert active is not None and active.recipe_id == original.recipe_id
    assert not page.buttons["save_apply"].isEnabled()
    assert page.recipes.isEnabled() and not page.metadata["display_name"].isEnabled()
    before = session.draft.to_recipe()
    page.save_and_apply()
    assert session.draft.to_recipe() == before
    window.bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)
    window.bridge.refresh()
    assert page.model.toolTip() == model_identity


@pytest.mark.parametrize(
    "outcome", ["cancel", "delete", "candidate_changed", "failure"]
)
def test_recipe_delete_confirmation_identity_and_live_protection(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    assert not page.buttons["delete_recipe"].isEnabled()  # Current candidate.
    index = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.recipe.channel_overrides
    )
    page.recipes.activated.emit(index)
    entry = session.entry
    assert entry is not None
    original = entry.path.read_bytes()
    before = window.bridge.application.snapshot()
    page.buttons["delete_recipe"].click()
    dialog = next(d for d in page.findChildren(QMessageBox) if d.isVisible())
    assert (
        entry.recipe.recipe_id in dialog.text()
        and entry.recipe.display_name in dialog.text()
    )
    assert dialog.defaultButton() is not None and dialog.defaultButton().text() == text(
        "cancel", page.language
    )
    if outcome == "candidate_changed":
        window.bridge.application.configure(session.prepare(entry.recipe))
    if outcome == "failure":

        def denied(*, protected_ids: frozenset[str]) -> None:
            raise ConfigurationIOError("Delete denied")

        monkeypatch.setattr(session, "delete", denied)
    caption = text("cancel" if outcome == "cancel" else "delete_recipe", page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    if outcome == "delete":
        assert not entry.path.exists() and session.entry is None
        assert page.recipes.currentIndex() == -1 and not session.dirty
        assert window.bridge.application.snapshot() == before
    else:
        assert entry.path.read_bytes() == original and session.entry == entry
        if outcome != "cancel":
            assert page.status.text() == text("recipe_delete_error", page.language)
    assert not runtimes


@pytest.mark.parametrize("outcome", ["cancel", "reload", "failure"])
def test_recipe_discard_reads_disk_and_preserves_other_domains(
    configured_screen: Screen, outcome: str
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    assert session.entry is not None
    entry = session.entry
    assert not page.buttons["recipe_discard"].isEnabled()
    edit(page.metadata["display_name"], "Unsaved")
    page.capsule_enabled["size"].click()
    draft = replace(session.draft)
    machine = session.machine_editor.definition
    before = window.bridge.application.snapshot()
    persisted = replace(
        entry.recipe, display_name="Changed on disk", product_code="disk-code"
    )
    save_recipe(entry.path, persisted)
    if outcome == "failure":
        entry.path.write_text("{", encoding="utf8")
    payload = entry.path.read_bytes()
    page.buttons["recipe_discard"].click()
    dialog = next(d for d in page.findChildren(QMessageBox) if d.isVisible())
    assert entry.recipe.recipe_id in dialog.text()
    caption = text("cancel" if outcome == "cancel" else "recipe_discard", page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    if outcome == "reload":
        assert session.entry.recipe == persisted and not session.dirty
        assert page.metadata["display_name"].text() == "Changed on disk"
    else:
        assert session.draft == draft and session.entry == entry
        if outcome == "failure":
            assert page.status.text() == text("recipe_discard_error", page.language)
            save_recipe(
                entry.path, entry.recipe
            )  # Restore this test's file for teardown.
    if outcome != "failure":
        assert entry.path.read_bytes() == payload
    assert session.machine_editor.definition == machine
    assert window.bridge.application.snapshot() == before and not runtimes


def test_active_recipe_stays_protected_without_candidate(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    snapshot = window.bridge.application.snapshot()
    assert snapshot.candidate is not None
    payload = session.entry.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(
            window.bridge.application,
            "snapshot",
            lambda: replace(snapshot, active=snapshot.candidate, candidate=None),
        )
        window.parameters.refresh_state()
        assert not window.parameters.buttons["delete_recipe"].isEnabled()
        window.parameters.request_delete()
        assert not any(
            d.isVisible() for d in window.parameters.findChildren(QMessageBox)
        )
    assert session.entry.path.read_bytes() == payload


def test_rule_rows_expand_and_collapse_without_toggling_checks(
    configured_screen: Screen,
) -> None:
    from dataclasses import replace

    window, session, _ = configured_screen
    page = window.parameters
    session.draft.capsule = replace(
        session.draft.capsule,
        size=replace(session.draft.capsule.size, enabled=True),
        color=replace(
            session.draft.capsule.color, enabled=True, target_hue=14, hue_tolerance=1
        ),
    )
    page.show_draft()
    assert page.expand_tuning["color"].text().endswith("H 14 ± 1")
    page.expand_tuning["size"].click()
    preview = page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    assert page.expand_tuning["size"].width() > page.width() // 2
    assert not hasattr(preview, "back")
    assert session.draft.capsule.size.enabled and session.draft.capsule.color.enabled
    page.expand_tuning["color"].click()
    assert page.preview is preview and preview.mode == "color"
    assert session.draft.capsule.size.enabled and session.draft.capsule.color.enabled
    page.expand_tuning["color"].click()
    until(lambda: page.preview is None)
    page.capsule_enabled["size"].click()
    assert page.preview is None and not session.draft.capsule.size.enabled
    page.expand_tuning["size"].click()
    assert page.preview is not None and not session.draft.capsule.size.enabled


@pytest.mark.parametrize("duplicate", [False, True])
def test_new_duplicate_dialog_and_save(
    configured_screen: Screen, duplicate: bool
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    if duplicate:
        page.recipes.activated.emit(
            next(
                i
                for i, e in enumerate(session.catalog.discover().entries)
                if e.recipe.channel_overrides
            )
        )
    assert session.entry is not None
    original = session.entry
    page.buttons["duplicate_recipe" if duplicate else "new_recipe"].click()
    dialog = page.findChildren(QDialog)[-1]
    lines = dialog.findChildren(QLineEdit)
    assert len(lines) == 1  # IDs are generated, never an operator form field.
    name = lines[0]
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    name.setText("")
    box.button(QDialogButtonBox.StandardButton.Ok).click()
    assert dialog.isVisible() and session.entry == original
    name.setText("新品种")
    box.button(QDialogButtonBox.StandardButton.Ok).click()
    assert not dialog.isVisible() and session.entry is None and session.dirty
    new = session.draft.to_recipe()
    assert new.recipe_id != original.recipe.recipe_id
    expected = (
        replace(original.recipe, recipe_id=new.recipe_id, display_name="新品种")
        if duplicate
        else RecipeDefinition(new.recipe_id, "新品种")
    )
    assert new == expected
    assert (
        len(session.catalog.discover().entries) == 2
    )  # Draft only until Save and apply.
    page.save_and_apply()
    until(lambda: not page.busy)
    assert session.entry is not None and load_recipe(session.entry.path) == expected
    assert load_recipe(original.path) == original.recipe
    assert len(session.catalog.discover().entries) == 3
    assert window.bridge.application.snapshot().active is None


@pytest.mark.parametrize(
    "action", ["select", "new_recipe", "duplicate_recipe", "close"]
)
@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_dirty_decisions(
    configured_screen: Screen,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    decision: DirtyDecision,
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "有修改")

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        callback(decision)

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    if action == "select":
        target = next(
            i
            for i, e in enumerate(session.catalog.discover().entries)
            if e.path != original.path
        )
        page.recipes.activated.emit(target)
    elif action == "close":
        window.close()
    else:
        page.buttons[action].click()
    until(lambda: not page.busy)
    if decision == "cancel":
        assert session.dirty and session.entry == original and window.isVisible()
    elif action == "select":
        assert session.entry is not None and session.entry.path != original.path
    elif action == "close":
        until(lambda: window.bridge.closed)
    else:
        dialogs = [d for d in page.findChildren(QDialog) if d.isVisible()]
        assert len(dialogs) == 1
        dialogs[0].reject()
    assert load_recipe(original.path).display_name == (
        "有修改" if decision == "save" else original.recipe.display_name
    )


def test_invalid_save_blocks_navigation(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    original = session.entry
    edit(page.metadata["display_name"], "")

    def confirm(callback: Callable[[DirtyDecision], None]) -> None:
        callback("save")

    monkeypatch.setattr(page, "confirm_dirty", confirm)
    page.buttons["new_recipe"].click()
    assert session.entry == original and session.dirty
    assert page.status.text() == text("recipe_validation_error", page.language)
    assert "display_name" in page.status.toolTip()
    assert not page.findChildren(QDialog)


def test_async_preparation_close_wait_and_no_late_apply(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    entered, release, heartbeat = Event(), Event(), Event()
    prepare = session.prepare
    thread_ids: list[int] = []
    before = window.bridge.application.snapshot().candidate

    def held(recipe: RecipeDefinition) -> PreparedConfiguration:
        thread_ids.append(get_ident())
        entered.set()
        assert release.wait(5)
        return prepare(recipe)

    monkeypatch.setattr(session, "prepare", held)
    try:
        page.save_and_apply()
        until(entered.is_set)
        QTimer.singleShot(0, heartbeat.set)
        until(heartbeat.is_set)
        assert page.busy and thread_ids != [get_ident()]
        window.close()
        assert window.isVisible() and not window.bridge.closed
        release.set()
        until(lambda: window.bridge.closed)
        assert window.bridge.application.snapshot().candidate == before
    finally:
        release.set()


def test_prepare_failure_preserves_saved_edits_but_not_promoted(
    configured_screen: Screen,
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    before = window.bridge.application.snapshot()
    assert session.entry is not None
    Path(session.registry.get(session.default_model_id).model_path).write_bytes(
        b"wrong SHA"
    )
    edit(page.metadata["display_name"], "Saved before preparation")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert page.status.text() == text("model_prepare_error", page.language)
    assert "SHA-256" in page.status.toolTip()
    assert window.bridge.application.snapshot() == before and not runtimes
    assert (
        not session.dirty
        and load_recipe(session.entry.path).display_name == "Saved before preparation"
    )
    assert page.metadata["display_name"].isEnabled()


@pytest.mark.parametrize(
    "state",
    [
        ApplicationState.STARTING,
        ApplicationState.RUNNING,
        ApplicationState.STOPPING,
        ApplicationState.FAULT,
        ApplicationState.CLOSED,
    ],
)
def test_locked_state_mapping(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch, state: ApplicationState
) -> None:
    window, _, _ = configured_screen
    app, page = window.bridge.application, window.parameters
    snapshot = replace(app.snapshot(), state=state)
    with monkeypatch.context() as patch:
        patch.setattr(app, "snapshot", lambda: snapshot)
        page.refresh_state()
        assert page.recipes.isEnabled()
        assert not page.metadata["display_name"].isEnabled()
        assert all(
            not page.buttons[k].isEnabled()
            for k in (
                "save_apply",
                "new_recipe",
                "duplicate_recipe",
                "recipe_discard",
                "delete_recipe",
            )
        )
        page.request_discard()
        page.request_delete()
        assert not any(box.isVisible() for box in page.findChildren(QMessageBox))
    page.refresh_state()


def test_unknown_model_preserved_on_unrelated_edit(configured_screen: Screen) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    save_recipe(
        session.entry.path,
        replace(session.entry.recipe, model_override_id="unknown-model"),
    )
    page = window.parameters
    page.reload()
    assert page.model.currentText() == text("unavailable_model", page.language)
    edit(page.metadata["display_name"], "名称")
    assert session.draft.model_override_id == "unknown-model"
    page.save_and_apply()
    assert "unknown-model" in page.status.toolTip() and session.dirty


@pytest.mark.parametrize("decision", ["save", "discard", "cancel"])
def test_actual_dirty_dialog(
    configured_screen: Screen, decision: DirtyDecision
) -> None:
    window, session, _ = configured_screen
    page = window.parameters
    assert session.entry is not None
    original = session.entry
    edit(page.metadata["display_name"], "未保存")
    page.buttons["reload"].click()
    dialog = page.findChildren(QMessageBox)[-1]
    caption = text("save_apply" if decision == "save" else decision, page.language)
    next(b for b in dialog.buttons() if b.text() == caption).click()
    until(lambda: not page.busy)
    assert not any(d.isVisible() for d in page.findChildren(QMessageBox))
    assert session.dirty == (decision == "cancel")
    assert load_recipe(original.path).display_name == (
        "未保存" if decision == "save" else original.recipe.display_name
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_single_model_and_unrelated_edit_preserve_model_semantics(
    configured_screen: Screen, explicit: bool
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    session.registry = ModelRegistry((session.registry.get(session.default_model_id),))
    baseline = replace(
        session.entry.recipe,
        model_override_id=session.default_model_id if explicit else None,
    )
    save_recipe(session.entry.path, baseline)
    page = window.parameters
    page.reload()
    assert page.model.count() == 1 and not page.model.isEnabled()
    assert (
        page.model.currentText()
        == f"{session.registry.models[0].display_name} · {session.registry.models[0].expected_sha256[:8]}"
    )
    edit(page.metadata["product_code"], "new-code")
    page.save_and_apply()
    until(lambda: not page.busy)
    assert load_recipe(session.entry.path) == replace(baseline, product_code="new-code")


def test_save_failure_keeps_draft_file_and_application(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    original = session.entry.path.read_bytes()
    before = window.bridge.application.snapshot()

    def fail(*, new_identity: tuple[str, str] | None = None) -> None:
        raise ConfigurationIOError("Recipe file is read-only")

    monkeypatch.setattr(session, "save", fail)
    page = window.parameters
    edit(page.metadata["display_name"], "Keep draft")
    page.save_and_apply()
    assert page.status.text() == text("recipe_save_error", page.language)
    assert "read-only" in page.status.toolTip() and session.dirty and not page.busy
    assert session.entry.path.read_bytes() == original
    assert window.bridge.application.snapshot() == before


def test_failed_preparation_blocks_dirty_switch(
    configured_screen: Screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session, _ = configured_screen
    assert session.entry is not None
    original = session.entry
    page = window.parameters
    before = window.bridge.application.snapshot()

    def fail(recipe: RecipeDefinition) -> PreparedConfiguration:
        raise ConfigurationIOError("model temporarily unavailable")

    def save(callback: Callable[[DirtyDecision], None]) -> None:
        callback("save")

    monkeypatch.setattr(session, "prepare", fail)
    monkeypatch.setattr(page, "confirm_dirty", save)
    edit(page.metadata["display_name"], "Persisted")
    target = next(
        i
        for i, e in enumerate(session.catalog.discover().entries)
        if e.path != original.path
    )
    page.recipes.activated.emit(target)
    until(lambda: not page.busy)
    assert session.entry.path == original.path
    assert load_recipe(original.path).display_name == "Persisted"
    assert page.status.text() == text("recipe_prepare_error", page.language)
    assert "unavailable" in page.status.toolTip()
    assert window.bridge.application.snapshot() == before
