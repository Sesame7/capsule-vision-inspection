from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
)
from tests.capsule_support import SETTINGS
from tests.qt_support import until
from tests.recipe_ui_support import Screen, assert_inside, demo_recipe_window, edit
from tests.recipe_ui_support import configured_screen as configured_screen

from vimo_demo.application import (
    ApplicationState,
    PreparedConfiguration,
)
from vimo_demo.configuration import (
    ConfigurationIOError,
    ModelRegistry,
    RecipeDefinition,
    load_recipe,
    save_recipe,
)
from vimo_demo.ui.parameters_page import DirtyDecision
from vimo_demo.ui.production_style import Language, Style, text


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (900, 1200)])
def test_compact_recipe_layout(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    page = window.parameters
    assert page.recipes.count() == 2 and not session.dirty and not runtimes
    assert set(page.metadata) == {"display_name", "product_code"}
    assert page.findChildren(QComboBox) == [page.recipes, page.model]
    assert not page.findChildren(QScrollArea) and not page.findChildren(QGroupBox)
    assert len(page.findChildren(QLineEdit)) == 2
    assert set(page.buttons) == {
        "new_recipe",
        "duplicate_recipe",
        "reload",
        "save_apply",
    }
    assert window.navigation[1].text() == text("parameters", language)
    for button in page.buttons.values():
        assert page.rect().contains(button.mapTo(page, QPoint(0, 0)))
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    assert session.entry is not None
    model = session.registry.get(session.default_model_id)
    visible_text = " ".join(label.text() for label in page.findChildren(QLabel))
    for technical in (
        model.model_path,
        model.expected_sha256,
        "SHA-256",
        "module_id",
        "required",
        session.entry.recipe.recipe_id,
    ):
        assert technical not in visible_text
    assert page.roi_workspace is None
    window.navigate(4)
    assert (
        page.roi_workspace is not None
        and page.roi_workspace.isVisible()
        and window.pages.currentWidget() is page.settings
    )
    window.navigate(1)


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
    assert page.notice.isVisible()
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
    assert not page.notice.isVisible()


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
    window.bridge.start()
    until(lambda: len(runtimes) == 1)
    runtimes[0].ready.set_result(None)
    until(lambda: app.snapshot().state is ApplicationState.RUNNING)
    window.bridge.refresh()
    active = app.snapshot().active
    assert active is not None and active.recipe_id == original.recipe_id
    assert not page.buttons["save_apply"].isEnabled()
    assert page.recipes.isEnabled() and not page.metadata["display_name"].isEnabled()
    before = session.draft.to_recipe()
    page.save_and_apply()
    assert session.draft.to_recipe() == before
    window.bridge.stop()
    until(lambda: app.snapshot().state is ApplicationState.READY)


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
            for k in ("save_apply", "new_recipe", "duplicate_recipe")
        )
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
    assert page.model.currentText() == session.registry.models[0].display_name
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


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920), (900, 1200)])
def test_full_width_recipe_columns_and_long_text(
    configured_screen: Screen, language: Language, size: tuple[int, int]
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    basic, inspection = (
        page.basic_title.parentWidget(),
        page.inspection_title.parentWidget(),
    )
    assert basic is not None and inspection is not None
    assert abs(basic.width() - inspection.width()) <= 1
    if (
        size[0] >= 1080
        or inspection.mapTo(page, QPoint(0, 0)).y()
        == basic.mapTo(page, QPoint(0, 0)).y()
    ):
        assert (
            inspection.mapTo(page, QPoint(0, 0)).x()
            > basic.mapTo(page, QPoint(0, 0)).x()
        )
        assert (
            basic.mapTo(page, QPoint(0, 0)).y()
            == inspection.mapTo(page, QPoint(0, 0)).y()
        )
    else:
        assert (
            inspection.mapTo(page, QPoint(0, 0)).y()
            >= basic.mapTo(page, QPoint(0, basic.height())).y()
        )
    save = page.buttons["save_apply"]
    assert (
        inspection.mapTo(page, QPoint(inspection.width(), 0)).x()
        == save.mapTo(page, QPoint(save.width(), 0)).x()
    )
    assert_inside(basic, page)
    assert_inside(inspection, page)
    before = window.bridge.application.snapshot()
    # Long translated summaries wrap within their section; identifiers stay intact
    # in the existing line edits without enlarging the window or other controls.
    identity = "长配方名称-Long-Recipe-Identity-" * 8
    page.metadata["display_name"].setText(identity)
    page.rule.setText(page.rule.text() * 12)
    page.capsule_summary["size"].setText("W 1000000 ± 1000000 · H 1000000 ± 1000000 px")
    QApplication.processEvents()
    assert window.size().width() == size[0]
    assert page.metadata["display_name"].text() == identity
    for control in (
        *page.metadata.values(),
        page.model,
        page.rule,
        page.tune_preview,
        *page.capsule_summary.values(),
        save,
    ):
        assert_inside(control, page)
    assert page.model.mapTo(basic, QPoint(page.model.width(), 0)).x() == basic.width()
    assert window.event_frame.height() == 33
    assert (
        window.bridge.application.snapshot() == before
        and not session.dirty
        and not runtimes
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_fields_follow_available_width_without_stretching_actions(
    configured_screen: Screen, language: Language
) -> None:
    window, session, runtimes = configured_screen
    page = window.parameters
    display_name = (
        "胶囊分割检测 — 本地验证模型"
        if language == "zh"
        else "Capsule segmentation — local reference"
    )
    session.registry = ModelRegistry(
        (
            replace(
                session.registry.get(session.default_model_id),
                display_name=display_name,
            ),
        )
    )
    window.set_language(language)
    page.show_draft()
    QApplication.processEvents()
    basic = page.basic_title.parentWidget()
    assert basic is not None
    name, product = page.metadata.values()
    actions = [page.buttons[k] for k in ("new_recipe", "duplicate_recipe", "reload")]
    action_sizes = [b.size() for b in actions + [page.tune_preview]]
    heights = [c.height() for c in (page.recipes, name, product, page.model)]
    before = window.bridge.application.snapshot()
    widths: list[tuple[int, ...]] = []
    for size in ((1920, 1080), (1080, 1920), (900, 1200), (1920, 1080)):
        window.resize(*size)
        QApplication.processEvents()
        assert abs(name.width() - product.width()) <= 1
        assert name.mapTo(basic, QPoint(0, 0)).x() == 0
        assert product.mapTo(basic, QPoint(product.width(), 0)).x() == basic.width()
        assert (
            product.mapTo(basic, QPoint(0, 0)).x()
            - name.mapTo(basic, QPoint(name.width(), 0)).x()
            == Style.space
        )
        assert (
            page.model.mapTo(basic, QPoint(page.model.width(), 0)).x() == basic.width()
        )
        assert page.model.currentText() == display_name and not page.model.isEnabled()
        assert (
            page.model.fontMetrics().horizontalAdvance(display_name)
            + 2 * Style.space
            + Style.icon
            <= page.model.width()
        )
        assert (
            actions[0].mapTo(page, QPoint(0, 0)).x()
            - page.recipes.mapTo(page, QPoint(page.recipes.width(), 0)).x()
            == Style.small
        )
        assert actions[-1].mapTo(page, QPoint(actions[-1].width(), 0)).x() == (
            page.buttons["save_apply"]
            .mapTo(page, QPoint(page.buttons["save_apply"].width(), 0))
            .x()
        )
        assert [b.size() for b in actions + [page.tune_preview]] == action_sizes
        assert [
            c.height() for c in (page.recipes, name, product, page.model)
        ] == heights
        for control in (page.recipes, name, product, page.model, *actions):
            assert_inside(control, page)
        widths.append(
            tuple(c.width() for c in (page.recipes, name, product, page.model))
        )
    assert all(wide > narrow for wide, narrow in zip(widths[0], widths[1], strict=True))
    assert widths[-1] == widths[0]
    assert (
        window.bridge.application.snapshot() == before
        and not session.dirty
        and not runtimes
    )


def test_recipe_metadata_groups_wrap(configured_screen: Screen) -> None:
    window, _, _ = configured_screen
    page = window.parameters
    for line in page.metadata.values():
        line.setMinimumWidth(450)
    window.resize(900, 1200)
    QApplication.processEvents()
    name, product = page.metadata.values()
    assert product.mapTo(page, QPoint(0, 0)).y() > name.mapTo(page, QPoint(0, 0)).y()
    assert_inside(name, page)
    assert_inside(product, page)
    window.resize(1920, 1080)
    QApplication.processEvents()
    assert product.mapTo(page, QPoint(0, 0)).y() == name.mapTo(page, QPoint(0, 0)).y()


@pytest.mark.parametrize(
    "language,size,workspace",
    [
        ("zh", (1920, 1080), "recipe"),
        ("zh", (1080, 1920), "recipe"),
        ("en", (1920, 1080), "recipe"),
    ],
)
def test_recipe_page_screenshots(
    configured_screen: Screen,
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    workspace: str,
) -> None:
    from run_production_ui import compose, demo_images

    composition = compose(demo_images(), tmp_path / "demo", "normal", 0.001)
    composition.editor.registry = ModelRegistry(
        tuple(
            replace(
                m,
                display_name=(
                    "胶囊分割检测 — 本地验证模型"
                    if language == "zh"
                    else "Capsule segmentation — local reference"
                ),
            )
            for m in composition.editor.registry.models
        )
    )
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
        qapp.processEvents()
        for control in (
            page.recipes,
            *page.buttons.values(),
            *page.metadata.values(),
            page.model,
            page.tune_preview,
            page.identity,
        ):
            assert_inside(control, page)
        name, product = page.metadata.values()
        assert (
            name.mapTo(page, QPoint(0, 0)).y() == product.mapTo(page, QPoint(0, 0)).y()
        )
        assert name.width() < page.width() // 2
        assert page.model.width() < page.width() // 2
        assert page.identity.mapTo(page, QPoint(0, 0)).y() > page.height() * 0.8
        assert composition.application.snapshot() == before
        assert window.event_frame.height() == 33 and not window.event_label.text()
        assert window.grab().save(
            str(tmp_path / f"recipe-{language}-{size[0]}x{size[1]}-{workspace}.png")
        )
