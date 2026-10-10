"""Compact Recipe/preview presentation, with real draft math and fake runtime only."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from tests.integration.test_parameters_ui import Screen
from tests.integration.test_parameters_ui import configured_screen as configured_screen
from tests.integration.test_traditional_preview import setup
from tests.qt_support import until
from tests.unit.test_capsule import SETTINGS

from vimo_demo.configuration import ModelRegistry
from vimo_demo.inspection.production import ProductJudgment
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import Language, Style
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.traditional_preview import PreviewMode


def assert_inside(widget: QWidget, parent: QWidget) -> None:
    assert parent.rect().contains(widget.mapTo(parent, QPoint(0, 0)))
    assert parent.rect().contains(
        widget.mapTo(parent, QPoint(widget.width() - 1, widget.height() - 1))
    )


@pytest.mark.parametrize(
    "size_enabled,color_enabled",
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_one_entry_mode_selection_and_draft_synchronization(
    configured_screen: Screen, size_enabled: bool, color_enabled: bool
) -> None:
    window, session, runtimes = configured_screen
    setup(configured_screen).back.click()
    page = window.parameters
    session.draft.capsule = replace(
        SETTINGS,
        size=replace(SETTINGS.size, enabled=size_enabled),
        color=replace(SETTINGS.color, enabled=color_enabled),
    )
    page.show_draft()
    before = window.bridge.application.snapshot()
    assert [
        b
        for b in page.findChildren(QPushButton)
        if b.text() == page.tune_preview.text()
    ] == [page.tune_preview]
    assert page.capsule_enabled["size"].isChecked() is size_enabled
    assert page.capsule_enabled["color"].isChecked() is color_enabled
    page.tune_preview.click()
    preview = page.preview
    assert preview is not None
    until(lambda: not preview.busy)
    assert preview.mode == ("color" if color_enabled and not size_enabled else "size")
    assert preview.enabled.isChecked() is (
        color_enabled if preview.mode == "color" else size_enabled
    )
    preview.enabled.setChecked(not preview.enabled.isChecked())
    until(lambda: not preview.busy)
    preview.mode_tabs.setCurrentIndex(0)
    preview.spins["target_width"].setValue(21)
    preview.mode_tabs.setCurrentIndex(1)
    preview.tolerance.setValue(7)
    expected = session.draft.capsule
    preview.back.click()
    assert page.preview is None and session.draft.capsule == expected
    assert page.capsule_enabled["size"].isChecked() is expected.size.enabled
    assert page.capsule_enabled["color"].isChecked() is expected.color.enabled
    assert "21" in page.capsule_summary["size"].text()
    assert "7" in page.capsule_summary["color"].text()
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
    preview.mode_tabs.setCurrentIndex(0 if mode == "size" else 1)
    preview.show_settings()
    until(lambda: not preview.busy)
    assert not preview.enabled.isChecked()
    assert preview.enabled.text() == ("未启用" if language == "zh" else "Disabled")
    assert all(
        r and r.assessment.judgment is ProductJudgment.NG for r in preview.results
    )
    assert preview.canvas.results == preview.results
    measurements, results = preview.measurements, preview.results
    preview.mode_tabs.setCurrentIndex(1 if mode == "size" else 0)
    assert preview.enabled.isChecked()
    assert preview.measurements is measurements and preview.results == results
    assert window.bridge.application.snapshot() == before and not runtimes
    preview.back.click()


def test_whole_control_groups_wrap_without_shrinking_fields(
    configured_screen: Screen,
) -> None:
    window, _, _ = configured_screen
    page = window.parameters
    # Simulate wider readable fields; layout responds to their actual minimum hints.
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
    preview = setup(configured_screen)
    preview.mode_tabs.setMinimumWidth(400)
    window.resize(900, 1200)
    QApplication.processEvents()
    assert (
        preview.controls.mapTo(preview, QPoint(0, 0)).y()
        >= preview.enabled.mapTo(preview, QPoint(0, preview.enabled.height())).y()
    )
    assert_inside(preview.controls, preview)
    for spin in preview.spins.values():
        assert_inside(spin, preview)
        assert spin.height() >= Style.touch
    window.resize(1920, 1080)
    QApplication.processEvents()
    assert (
        preview.controls.mapTo(preview, QPoint(0, 0)).x()
        > preview.mode_tabs.mapTo(preview, QPoint(preview.mode_tabs.width(), 0)).x()
    )
    preview.back.click()


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


@pytest.mark.parametrize(
    "language,size,workspace",
    [
        ("zh", (1920, 1080), "recipe"),
        ("zh", (1080, 1920), "recipe"),
        ("en", (1920, 1080), "recipe"),
        ("en", (1920, 1080), "size"),
        ("zh", (1080, 1920), "color"),
    ],
)
def test_recipe_and_preview_screenshots(
    configured_screen: Screen,
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    workspace: str,
) -> None:
    from run_production_ui import compose, demo_images

    # Reuse the existing two-view demo images, model fixture and offscreen fonts.
    composition = compose(demo_images(), tmp_path / "demo", "normal", 0.001)
    if workspace == "recipe":
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
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=5)
    window = ProductionWindow(
        bridge,
        composition.choices,
        language=language,
        simulation=True,
        recipe_editor=composition.editor,
    )
    window.resize(*size)
    window.show()
    try:
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
        if workspace != "recipe":
            page.tune_preview.click()
            preview = page.preview
            assert preview is not None
            preview.mode_tabs.setCurrentIndex(0 if workspace == "size" else 1)
            until(lambda: not preview.busy)
            qapp.processEvents()
            assert preview.frame is not None and len(preview.results) == 6
            assert preview.mode_tabs.width() < preview.width() // 2
            assert preview.canvas.height() > window.height() * 0.6
            for control in (
                preview.back,
                preview.camera_tabs,
                preview.fit_button,
                preview.refresh_button,
                preview.mode_tabs,
                preview.enabled,
                preview.controls,
            ):
                assert_inside(control, preview)
            for spin in preview.spins.values():
                assert spin.width() < 200
                if workspace == "size":
                    assert_inside(spin, preview)
        else:
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
                name.mapTo(page, QPoint(0, 0)).y()
                == product.mapTo(page, QPoint(0, 0)).y()
            )
            assert name.width() < page.width() // 2
            assert page.model.width() < page.width() // 2
            assert page.identity.mapTo(page, QPoint(0, 0)).y() > page.height() * 0.8
        assert composition.application.snapshot() == before
        assert window.event_frame.height() == 33 and not window.event_label.text()
        assert window.grab().save(
            str(tmp_path / f"recipe-{language}-{size[0]}x{size[1]}-{workspace}.png")
        )
    finally:
        composition.editor.discard()
        window.close()
        until(lambda: bridge.closed)
        composition.source.close()
        assert composition.application.join(0)
        window.deleteLater()
