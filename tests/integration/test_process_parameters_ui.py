"""Normal Recipe process metadata, native layout constraints and no equipment I/O."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from tests.support.application import WAIT
from tests.support.qt import close_window, delete_widget, ensure_test_fonts, until
from tests.support.recipe_ui import assert_inside, edit
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)

from capsule_vision.application import ProductionCounters
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.startup import Startup
from capsule_vision.configuration import load_recipe, save_recipe
from capsule_vision.configuration.process_parameters import load_process_catalog
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.process_parameters import ProcessParametersCard
from capsule_vision.ui.style import Style, Theme, apply_theme, theme_colors


@contextmanager
def process_window(
    root: Path, *, custom: bool = False
) -> Generator[tuple[Startup, MainWindow]]:
    data = saved_data(root)
    if custom:
        data.process_catalog.write_text(
            json.dumps(
                {
                    "overrides": [{"id": "running_speed", "minimum": -20}],
                    "groups": {"feeder": {"en": "Feeder", "zh": "供料"}},
                    "parameters": [
                        {
                            "id": "feeder_speed",
                            "group": "feeder",
                            "labels": {"en": "Feeder speed"},
                            "address": 180,
                            "minimum": -100,
                            "maximum": 100,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    catalog = load_process_catalog(data.process_catalog)
    recipe_path = data.recipes / "local.json"
    save_recipe(
        recipe_path,
        replace(
            load_recipe(recipe_path),
            process_parameters=(("running_speed", 0), ("photo_phase", 120)),
        ),
        process_catalog=catalog,
    )

    def no_engine(*_args: object) -> Never:
        raise AssertionError("Recipe editing/preparation must not construct an engine")

    startup = authenticated_startup(data, engine_factory=no_engine)
    delivery = FutureDelivery()
    ensure_test_fonts()
    window = create_window(startup, delivery)
    window.resize(1080, 1920)
    window.show()
    window.navigate(1)
    try:
        until(lambda: not window.recipe_page.busy)
        yield startup, window
    finally:
        until(lambda: not window.recipe_page.busy)
        if startup.editor is not None:
            startup.editor.discard()
        close_window(window)
        delivery.detach()


def test_process_save_preserves_raw_errors_and_missing_target_blocks_application(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> Never:
        calls.append("PLC I/O")
        raise AssertionError("No PLC operations during Recipe workflow")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.sendto", forbidden)
    with process_window(tmp_path, custom=True) as (startup, window):
        page, session = window.recipe_page, startup.editor
        assert session is not None and session.entry is not None
        card = page.process_parameters
        candidate = startup.application.snapshot().candidate_prepared
        original = session.entry.path.read_bytes()
        card.expand.click()
        edit(card.fields["running_speed"], "-12")
        edit(card.fields["feeder_speed"], "0")
        edit(card.fields["photo_phase"], "-")
        edit(card.fields["reject_phase"], "1.5")
        page.save()
        until(lambda: not page.busy)
        assert page.status.text() == text("recipe_validation_error", page.language)
        assert card.fields["reject_phase"].text() == "1.5" and session.dirty
        assert session.entry.path.read_bytes() == original
        window.set_language("zh")
        assert card.fields["reject_phase"].text() == "1.5" and card.expand.isChecked()
        edit(card.fields["reject_phase"], "")
        page.save()
        until(lambda: not page.busy)
        expected = (("feeder_speed", 0), ("running_speed", -12))
        assert session.entry.recipe.process_parameters == expected and not session.dirty
        assert startup.application.snapshot().candidate_prepared is not candidate
        snapshot = startup.application.snapshot()
        assert snapshot.candidate_prepared is not None
        assert snapshot.candidate_prepared.configuration.process_parameters == expected
        assert snapshot.active is None and snapshot.counters == ProductionCounters()
        assert not snapshot.runtime_owned and not calls
        assert snapshot.process_required and snapshot.last_issue
        assert page.status.text() == "已保存 · 工艺参数应用失败"
        assert not startup.data.communication.exists()


@pytest.mark.parametrize("role", list(Role))
def test_process_values_are_visible_to_all_roles_but_only_engineers_can_edit(
    qapp: QApplication, tmp_path: Path, role: Role
) -> None:
    with process_window(tmp_path) as (startup, window):
        owner = startup.application.identity
        assert owner is not None and startup.editor is not None
        if role is Role.GUEST:
            owner.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(owner, role.name, role, TEST_PASSWORD)
            owner.login(role.name, TEST_PASSWORD).result(WAIT)
        window.identity_controls.refresh()
        page = window.recipe_page
        card = page.process_parameters
        card.expand.click()
        field = card.fields["running_speed"]
        assert field.text() == "0" and card.fields["photo_phase"].text() == "120"
        assert field.isReadOnly() is (role < Role.ENGINEER)
        field.setFocus()
        field.selectAll()
        QTest.keyClicks(field, "12")
        assert field.text() == ("12" if role >= Role.ENGINEER else "0")
        if role < Role.ENGINEER:
            assert not page.buttons["save"].isEnabled()
            with pytest.raises(AuthorizationError):
                startup.editor.save()
        else:
            page.save()
            until(lambda: not page.busy)
            assert startup.editor.entry is not None
            assert (
                dict(startup.editor.entry.recipe.process_parameters)["running_speed"]
                == 12
            )


@pytest.mark.parametrize("duplicate", (False, True))
def test_process_new_duplicate_and_discard_use_existing_recipe_workflow(
    qapp: QApplication, tmp_path: Path, duplicate: bool
) -> None:
    with process_window(tmp_path) as (startup, window):
        page, session = window.recipe_page, startup.editor
        assert session is not None and session.entry is not None
        original = session.entry
        card = page.process_parameters
        edit(card.fields["running_speed"], "99")
        page.request_discard()
        confirm = page.findChild(QMessageBox)
        assert confirm is not None
        next(
            b
            for b in confirm.buttons()
            if b.text() == text("recipe_discard", page.language)
        ).click()
        assert card.fields["running_speed"].text() == "0" and not session.dirty
        page.buttons["duplicate_recipe" if duplicate else "new_recipe"].click()
        dialog = next(d for d in page.findChildren(QDialog) if d.isVisible())
        names = dialog.findChildren(QLineEdit)
        assert len(names) == 1
        names[0].setText("New process product")
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        expected = original.recipe.process_parameters if duplicate else ()
        assert session.draft.to_recipe().process_parameters == expected
        page.save()
        until(lambda: not page.busy)
        assert session.entry is not None and session.entry.path != original.path
        assert load_recipe(session.entry.path).process_parameters == expected
        assert load_recipe(original.path) == original.recipe


def test_process_card_fits_localized_fields_at_both_layout_widths(
    qapp: QApplication,
) -> None:
    ensure_test_fonts()
    card = ProcessParametersCard()
    card.set_editable(True)
    card.show_values(card.catalog, {"running_speed": "0", "photo_phase": "120"})
    card.show()
    card.expand.click()
    before = card.values()
    try:
        for theme in ("dark", "light"):
            apply_theme(card, theme)
            card.refresh_theme()
            for language in LANGUAGES:
                card.set_language(language)
                for width, columns in ((1032, 2), (1872, 3)):
                    card.resize(width, 1200)
                    qapp.processEvents()
                    qapp.processEvents()
                    assert len({group.x() for group in card.groups.values()}) == columns
                    assert card.values() == before and card.expand.isChecked()
                    for parameter in card.catalog.parameters:
                        field = card.fields[parameter.id]
                        assert (
                            field.height() >= Style.touch
                            and field.focusPolicy() == Qt.FocusPolicy.StrongFocus
                        )
                        assert parameter.label(language) in field.accessibleName()
                        assert "D" in field.toolTip()
                        assert_inside(field, card.groups[parameter.group])
    finally:
        card.close()
        delete_widget(card)


def test_recipe_places_process_card_and_reflows_when_expanded(
    qapp: QApplication, tmp_path: Path
) -> None:
    languages: tuple[Language, ...] = ("zh", "en", "de", "ti")
    with process_window(tmp_path) as (_, window):
        page = window.recipe_page
        card = page.process_parameters
        assert not card.details.isVisible()
        assert card.override_count.text() == text(
            "process_set_count", card.language
        ).format(count=2)
        card.expand.click()
        before = card.values()
        for language in languages:
            window.set_language(language)
            for width, height, columns in (
                (1080, 1920, 2),
                (1920, 1080, 3),
                (1080, 1920, 2),
            ):
                window.resize(width, height)
                qapp.processEvents()
                assert len({group.x() for group in card.groups.values()}) == columns
                assert card.values() == before
                assert card.expand.isChecked()
                assert page.archive_section.y() < card.y() < page.model_section.y()
                assert (
                    page.summary_scroll.geometry().bottom()
                    < page.inspection_section.geometry().top()
                )
                assert page.summary_scroll.horizontalScrollBar().maximum() == 0
        card.expand.click()
        qapp.processEvents()
        assert not card.details.isVisible() and "2" in card.override_count.text()
        assert card.height() <= 70


def test_mechanism_order_preserves_catalog_order_inside_groups_and_extensions(
    qapp: QApplication, tmp_path: Path
) -> None:
    path = tmp_path / "process_parameters.json"
    path.write_text(
        json.dumps(
            {
                "groups": {"alpha": {"en": "Alpha"}, "zeta": {"en": "Zeta"}},
                "parameters": [
                    {
                        "id": key,
                        "group": group,
                        "labels": {"en": key},
                        "address": address,
                        "minimum": 0,
                        "maximum": 100,
                    }
                    for key, group, address in (
                        ("alpha_speed", "alpha", 184),
                        ("lift_aux", "lift_valve", 158),
                        ("zeta_speed", "zeta", 180),
                        ("zeta_phase", "zeta", 182),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = load_process_catalog(path)
    card = ProcessParametersCard()
    card.show_values(catalog, {})
    card.resize(1056, card.sizeHint().height())
    card.expand.click()
    card.show()
    try:
        qapp.processEvents()
        expected = (
            "lift_valve",
            "drop_valve",
            "extend_valve",
            "speed",
            "reject",
            "photo",
            "zeta",
            "alpha",
        )
        visual_order = sorted(
            card.groups, key=lambda key: (card.groups[key].y(), card.groups[key].x())
        )
        assert tuple(visual_order) == expected
        assert catalog.groups[:3] == ("speed", "photo", "reject")
        for group in expected:
            ids = tuple(p.id for p in catalog.parameters if p.group == group)
            visible = sorted(
                ids,
                key=lambda key: (
                    card.fields[key].mapTo(card.groups[group], QPoint()).y(),
                    card.fields[key].mapTo(card.groups[group], QPoint()).x(),
                ),
            )
            assert tuple(visible) == ids
            assert (
                len(
                    {
                        card.fields[key].mapTo(card.groups[group], QPoint()).x()
                        for key in ids
                    }
                )
                <= 2
            )
    finally:
        card.close()


def test_recipe_draft_and_field_focus_survive_presentation_changes(
    qapp: QApplication, tmp_path: Path
) -> None:
    with process_window(tmp_path, custom=True) as (startup, window):
        page, session = window.recipe_page, startup.editor
        assert session is not None and session.entry is not None
        original = session.entry.path.read_bytes()
        candidate = startup.application.snapshot().candidate_prepared
        card = page.process_parameters
        card.expand.click()
        edit(card.fields["running_speed"], "-12")
        edit(page.metadata["product_code"], "QA-17")
        page.capsule_enabled["size"].click()
        edit(page.archive_directory, str(tmp_path / "archive"))
        page.archive_checks["ng"].click()
        expected = session.draft.to_recipe(session.process_catalog)
        fields = dict(card.fields)
        layouts: tuple[tuple[int, Theme], ...] = ((1080, "dark"), (1920, "light"))
        for language in ("zh", "en", "pt-PT", "sr-Latn", "bn", "si", "ti"):
            for width, theme in layouts:
                card.fields["running_speed"].setFocus()
                window.set_language(language)
                window.set_theme(theme)
                window.resize(width, 1920 if width == 1080 else 1080)
                qapp.processEvents()
                assert card.fields == fields and card.fields["running_speed"].hasFocus()
                assert card.expand.isChecked() and card.details.isVisible()
                for mode in ("size", "color"):
                    page.expand_tuning[mode].click()
                    until(lambda: page.preview is not None and not page.preview.busy)
                    assert page.preview is not None and page.preview.mode == mode
                    assert card.expand.isChecked()
                    assert (
                        session.draft.to_recipe(session.process_catalog) == expected
                        and session.dirty
                    )
                page.expand_tuning["color"].click()
                until(lambda: page.preview is None)
                card.expand.click()
                card.expand.click()
                assert (
                    session.draft.to_recipe(session.process_catalog) == expected
                    and session.dirty
                )
                assert page.buttons["save"].isEnabled()
                assert page.summary_scroll.horizontalScrollBar().maximum() == 0
        assert session.entry.path.read_bytes() == original
        assert startup.application.snapshot().candidate_prepared is candidate
        assert not startup.application.snapshot().runtime_owned


@pytest.mark.parametrize("theme", ("dark", "light"))
def test_process_disclosure_is_neutral_after_pointer_but_keeps_keyboard_focus(
    qapp: QApplication, theme: Theme
) -> None:
    panel = QWidget()
    layout = QVBoxLayout(panel)
    previous = QPushButton("Previous")
    card = ProcessParametersCard()
    layout.addWidget(previous)
    layout.addWidget(card)
    apply_theme(panel, theme)
    card.refresh_theme()
    panel.resize(1056, 700)
    panel.show()
    try:
        panel.activateWindow()
        previous.setFocus()
        qapp.processEvents()
        QCursor.setPos(panel.mapToGlobal(QPoint(-20, -20)))
        QTest.mouseClick(card.expand, Qt.MouseButton.LeftButton)
        QTest.mouseMove(previous)
        qapp.processEvents()
        pointer = card.expand.grab().toImage()
        assert card.expand.width() == card.expand.height() == Style.touch
        assert card.expand.isChecked() and card.expand.hasFocus()
        background = card.expand.mapTo(card, QPoint(3, 3))
        assert card.grab().toImage().pixelColor(background) == QColor(
            theme_colors(card).surface
        )
        assert not panel.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
        previous.setFocus()
        QTest.keyClick(previous, Qt.Key.Key_Tab)
        assert card.expand.hasFocus()
        assert panel.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
        assert card.expand.grab().toImage() != pointer
        QTest.keyClick(card.expand, Qt.Key.Key_Space)
        assert not card.expand.isChecked() and not card.details.isVisible()
    finally:
        panel.close()


def test_extension_group_label_wraps_without_growing_the_window(
    qapp: QApplication, tmp_path: Path
) -> None:
    with process_window(tmp_path, custom=True) as (startup, window):
        label = (
            "Additional feeder speed and capsule transfer process settings "
            "for the secondary mechanism"
        )
        catalog = json.loads(startup.data.process_catalog.read_text(encoding="utf-8"))
        catalog["groups"]["feeder"] = {"en": label}
        startup.data.process_catalog.write_text(json.dumps(catalog), encoding="utf-8")
        page = window.recipe_page
        page.reload()
        window.set_language("ti")  # Explicit English fallback for this extension.
        card = page.process_parameters
        card.expand.click()
        title = card.group_titles["feeder"]
        until(lambda: title.height() > title.fontMetrics().lineSpacing())
        assert title.text() == label and window.width() == 1080
        assert page.summary_scroll.horizontalScrollBar().maximum() == 0
        assert_inside(title, card.groups["feeder"])
        assert label in card.fields["feeder_speed"].accessibleName()
        # Reload rebuilt the catalog fields; their Tab sequence still follows
        # the cards rather than appending the new fields after Save.
        field = card.fields["feeder_speed"]
        field.setFocus()
        QTest.keyClick(field, Qt.Key.Key_Tab)
        assert page.model.hasFocus()


def test_compact_process_captions_keep_full_meaning_and_custom_device_names(
    qapp: QApplication, tmp_path: Path
) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "overrides": [
                    {"id": "running_speed", "labels": {"en": "Custom line speed"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    card = ProcessParametersCard()
    card.show_values(load_process_catalog(path), {"running_speed": "125"})
    card.set_language("en")
    card.set_plc_available(True)
    card.resize(1056, 600)
    card.show()
    card.expand.click()
    try:
        assert card.title.text() == "Process"
        assert (
            card.title.accessibleName() == card.title.toolTip() == "Process Parameters"
        )
        assert card.override_count.text() == "Overrides 1"
        assert card.override_count.accessibleName() == "Recipe overrides: 1"
        assert card.group_titles["lift_valve"].text() == "Lift"
        assert card.group_titles["lift_valve"].toolTip() == "Lift valve"
        assert card.group_titles["drop_valve"].text() == "Drop"
        assert card.group_titles["drop_valve"].accessibleName() == "Drop valve"
        assert card.labels["running_speed"].text() == "Custom line speed"
        assert card.labels["rotation_speed"].text() == "Rotation"
        assert "Capsule rotation" in card.labels["rotation_speed"].accessibleName()
        assert (
            "Close compensation"
            in card.fields["drop_valve_close_compensation"].toolTip()
        )
        before = card.values()
        QTest.mousePress(card.view_plc, Qt.MouseButton.LeftButton)
        card.show_plc_values({"running_speed": 99})
        qapp.processEvents()
        assert not card.plc_status.isVisible() and card.view_plc.isDown()
        assert "Read-only" in card.view_plc.accessibleDescription()
        assert card.readings["running_speed"].text() == "99"
        card.set_language("zh")
        assert (
            card.title.text() == "工艺参数" and card.override_count.text() == "覆盖项 1"
        )
        assert card.plc_previewing and not card.plc_status.isVisible()
        assert card.values() == before
        for language in ("ja", "pt-PT", "sw", "bn", "si", "am", "ti"):
            card.set_language(language)
            assert card.title.accessibleName() == text("process_parameters", language)
            assert (
                text("process_plc_values", language)
                in card.view_plc.accessibleDescription()
            )
            assert card.labels["running_speed"].text() == "Custom line speed"
            assert card.readings["running_speed"].text() == "99"
            assert card.values() == before and card.plc_previewing
        QTest.mouseRelease(card.view_plc, Qt.MouseButton.LeftButton)
        assert not card.plc_previewing and card.values() == before
        assert card.fields["running_speed"].isVisible() and card.expand.isChecked()
    finally:
        card.close()


@pytest.mark.parametrize("theme", ("dark", "light"))
def test_read_only_recipe_choices_retain_distinguishable_saved_selections(
    qapp: QApplication, tmp_path: Path, theme: Theme
) -> None:
    with process_window(tmp_path) as (startup, window):
        page, session = window.recipe_page, startup.editor
        identity = startup.application.identity
        assert session is not None and identity is not None
        until(lambda: bool(page.label_checks) and not page.labels_loading)
        included, excluded = tuple(page.label_checks.values())[:2]
        excluded.click()
        page.save()
        until(lambda: not page.busy)
        identity.logout()
        window.identity_controls.refresh()
        window.set_theme(theme)
        qapp.processEvents()
        assert included.isChecked() and not excluded.isChecked()
        assert not included.isEnabled() and not excluded.isEnabled()
        assert included.grab().toImage().pixelColor(
            8, 8
        ) != excluded.grab().toImage().pixelColor(8, 8)
        QTest.keyClick(excluded, Qt.Key.Key_Space)
        assert not excluded.isChecked() and not session.dirty
