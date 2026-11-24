"""Qt gallery selection, native pixel crops, path confinement and Records admission."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QDate, QPoint, QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from tests.support.application import WAIT
from tests.support.ng_capsules import (
    MEMBERS,
    RECT,
    RUN,
    add_group,
    add_measured_groups,
    sample,
)
from tests.support.qt import close_window, delete_widget, ensure_test_fonts, until
from tests.support.startup import authenticated_startup, saved_data

from capsule_vision.application.history_review import open_history_review
from capsule_vision.application.identity_models import AuthorizationError
from capsule_vision.application.ng_capsules import (
    CapsuleExposure,
    CapsuleFilter,
    CapsuleKey,
    FrozenROI,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui import ng_capsule_images
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_review_window import HistoryReviewWindow
from capsule_vision.ui.ng_capsule_images import ExposureImage, exposure_image
from capsule_vision.ui.ng_capsules_page import NgCapsulesPage
from capsule_vision.ui.production_presentation import image_transform
from capsule_vision.ui.style import Style


def test_sort_menu_navigation_latest_choice_and_scope_reset(
    qapp: QApplication, tmp_path: Path
) -> None:
    saved = sample(tmp_path, with_filtered=True)
    expected = add_measured_groups(saved)
    with open_history_review(tmp_path) as reader:
        win = HistoryReviewWindow(reader)
        try:
            win.resize(1080, 1920)
            win.set_language("de")
            win.show()
            win.tabs.setCurrentWidget(win.ng_capsules)
            page = win.ng_capsules
            until(lambda: page.selected_capsule is not None)
            original = page.selected_capsule

            def open_with(label: str) -> None:
                QTest.keyClick(page.sort_button, Qt.Key.Key_Space)
                until(
                    lambda: (
                        page.sort_menu.isVisible()
                        and any(a.data() == label for a in page.sort_menu.actions())
                    )
                )

            open_with("Spot")
            choices = page.sort_menu.actions()
            assert [a.data() for a in choices] == [None, "Dent", "Spot"]
            assert (
                choices[0].text() == text("short_time", "de") and choices[0].isChecked()
            )
            assert page.selected_capsule == original
            QTest.mouseClick(
                page.sort_menu,
                Qt.MouseButton.LeftButton,
                pos=page.sort_menu.actionGeometry(choices[1]).center(),
            )
            until(
                lambda: (
                    page.selected_capsule is not None
                    and page.selected_capsule.sort_area == 5
                )
            )
            assert not page.newer.isEnabled() and page.older.isEnabled()
            assert (
                page.selected_capsule
                and page.selected_capsule.key.first_cycle == expected[0][0]
            )
            assert (
                text("records_sort", "de") in page.sort_button.accessibleName()
                and "Dent" in page.sort_button.toolTip()
            )
            page.table.setCurrentCell(49, 0)
            page.gallery.next.click()
            until(lambda: page.table.rowCount() == 13 and page.table.currentRow() == 0)
            assert (
                page.selected_capsule
                and page.selected_capsule.key.first_cycle == expected[50][0]
            )
            page.gallery.previous.click()
            until(lambda: page.table.rowCount() == 50 and page.table.currentRow() == 49)
            open_with("Spot")
            choices = page.sort_menu.actions()
            assert [a.data() for a in choices if a.isChecked()] == ["Dent"]
            # Three admissions in one Qt turn exercise cancellation and stale-page delivery.
            choices[2].trigger()
            choices[0].trigger()
            choices[2].trigger()
            page.sort_menu.hide()
            until(
                lambda: (
                    page.selected_capsule is not None
                    and page.selected_capsule.sort_area == 12
                    and page.area_label == "Spot"
                )
            )
            assert page.table.currentRow() == 0
            page.kind.setCurrentIndex(page.kind.findData("filtered"))
            until(lambda: page.message.text() == text("history_none", "de"))
            assert page.table.rowCount() == 0 and page.selected_capsule is None
            open_with("Hair")
            assert [a.data() for a in page.sort_menu.actions()] == [None, "Hair"]
            page.sort_menu.actions()[1].trigger()
            page.sort_menu.hide()
            until(
                lambda: (
                    page.selected_capsule is not None and page.selected_capsule.filtered
                )
            )
            assert page.selected_capsule and page.selected_capsule.sort_area == 17
            page.kind.setCurrentIndex(page.kind.findData("ng"))
            until(lambda: page.message.text() == text("history_none", "de"))
            open_with("Dent")
            page.sort_menu.actions()[0].trigger()
            page.sort_menu.hide()
            until(lambda: page.selected_capsule == original)
            for dimensions in ((1080, 1920), (1920, 1080)):
                win.resize(*dimensions)
                positions: set[tuple[int, int]] = set()
                for tab in (win.batches, page, win.inspection, win.alarms):
                    win.tabs.setCurrentWidget(tab)
                    qapp.processEvents()
                    point = tab.newer.mapTo(win, QPoint())
                    positions.add((point.x(), point.y()))
                    assert page.sort_button.isVisible() == (tab is page)
                    if tab is page:
                        offset = point - page.sort_button.mapTo(win, QPoint())
                        assert offset == QPoint(
                            page.sort_button.width() + Style.small, 0
                        )
                assert len(positions) == 1
        finally:
            win.close()
            delete_widget(win)


def test_portable_native_pixels_boxes_and_unavailable_members(
    qapp: QApplication, tmp_path: Path
) -> None:
    recipe = "Historische verkapselte Rezeptur · " + "Lichtprüfung und Freigabe " * 6
    saved = sample(tmp_path / "original", recipe_name=recipe, edge_area=41)
    package = tmp_path / "relocated"
    copytree(saved.root, package)
    files = [package / "history.sqlite3", *package.rglob("*.png")]
    before = {p: sha256(p.read_bytes()).digest() for p in files}
    with open_history_review(package) as owner:
        window = HistoryReviewWindow(owner)
        try:
            window.set_language("en")
            window.resize(1080, 1920)
            window.show()
            window.tabs.setCurrentIndex(1)
            page = window.ng_capsules
            cells = page.gallery.cells
            until(lambda: not cells[0].image.isNull())
            assert (
                window.tabs.currentWidget() is page
                and window.tabs.tabText(1) == "NG"
                and window.tabs.tabToolTip(1)
                == window.tabs.tabBar().accessibleTabName(1)
                == "NG Capsules"
            )
            assert len(cells) == 4
            assert [label.text() for label in page.gallery.findChildren(QLabel)] == [
                "1",
                "2",
                "3",
                "4",
            ]
            for cycle, status, cell, header in zip(
                MEMBERS,
                ("NG", "OK", "EMPTY", "UNKNOWN · ERROR"),
                cells,
                page.gallery.headers,
                strict=True,
            ):
                assert f"Cycle {cycle}" in cell.accessibleName()
                assert f"Result · {status}" in cell.accessibleDescription()
                assert header.toolTip() == cell.toolTip()
                assert cell.toolTip() in page.table.accessibleDescription()
            assert "execution failed" in cells[3].toolTip()
            for i, caption in enumerate(
                ("Time", "Batch", "Recipe", "Channel", "Group")
            ):
                heading = page.table.horizontalHeaderItem(i)
                assert heading is not None and heading.text() == caption
            for i, value in enumerate(("Training lot", recipe, "CH29", "101"), 1):
                item = page.table.item(0, i)
                assert item is not None and item.text() == value
            assert page.table.columnCount() == 5
            recipe_item, channel_item = page.table.item(0, 2), page.table.item(0, 3)
            assert (
                recipe_item
                and recipe_item.toolTip()
                == recipe_item.data(Qt.ItemDataRole.AccessibleTextRole)
                == recipe
            )
            assert page.table.fontMetrics().horizontalAdvance(
                recipe
            ) > page.table.columnWidth(2)
            assert (
                channel_item
                and channel_item.toolTip()
                == channel_item.data(Qt.ItemDataRole.AccessibleTextRole)
                == f"CH29 · {saved.camera}"
            )
            group = page.table.item(0, 4)
            assert group is not None and RUN in group.toolTip()
            source = QImage(str(package / "2026-01-15" / saved.camera / "shot-101.png"))
            original_crop = source.copy(RECT.x, RECT.y, RECT.width, RECT.height)
            assert cells[0].image == original_crop
            for slot in (1, 3):
                member = QImage(
                    str(
                        package
                        / "2026-01-15"
                        / saved.camera
                        / f"shot-{MEMBERS[slot]}.png"
                    )
                )
                expected = member.copy(RECT.x, RECT.y, RECT.width, RECT.height)
                assert cells[slot].image == expected and expected != original_crop
            assert cells[0].overlays == (
                (BoundingRect(134, 28, 19, 17), "Spot"),
                (BoundingRect(134, 110, 19, 17), "Spot"),
                (BoundingRect(0, 80, 7, 5), "Edge · 41 px²"),
            )
            assert cells[0].toolTip().count("Spot") == 1
            assert cells[2].image.isNull() and cells[2].empty_text == "Unavailable"
            assert (
                "EMPTY" in cells[2].toolTip() and "image" in cells[2].toolTip().lower()
            )
            assert all(not cell.overlays for cell in cells[1:])
            window.set_language("de")
            assert (
                window.tabs.tabText(1) == "NG"
                and window.tabs.tabToolTip(1)
                == window.tabs.tabBar().accessibleTabName(1)
                == "NG-Kapseln"
                and cells[0].image == original_crop
            )
            assert "Zyklus 101" in cells[0].toolTip()
            assert page.gallery.previous.accessibleName() == "Vorherige Kapsel"
            assert page.gallery.next.toolTip() == "Nächste Kapsel"
            detail = owner.ng_capsule_detail(
                CapsuleKey(RUN, "101", saved.channel)
            ).result(WAIT)
            assert detail and detail.roi
            shot = detail.exposures[0]
            assert shot.path
            assert (
                exposure_image(
                    replace(shot, size=(1519, 196)), detail.roi, package
                ).issue
                == "history_image_size_mismatch"
            )
            assert (
                exposure_image(
                    shot, replace(detail.roi, rect=BoundingRect(1519, 0, 2, 2)), package
                ).issue
                == "history_image_size_mismatch"
            )
            assert (
                exposure_image(
                    replace(
                        shot, path=shot.path.parent.parent / "CAM1" / shot.path.name
                    ),
                    detail.roi,
                    package,
                ).issue
                == "history_image_missing"
            )
            # An original PNG still exists, while the identical suffix is absent from this other package.
            assert (
                shot.path.is_file()
                and exposure_image(shot, detail.roi, tmp_path).image.isNull()
            )
            assert exposure_image(shot, None, package).image.isNull()
        finally:
            window.close()
            delete_widget(window)
    assert before == {p: sha256(p.read_bytes()).digest() for p in files}


def test_portable_tabs_center_captions_and_preserve_localized_names(
    qapp: QApplication, tmp_path: Path
) -> None:
    ensure_test_fonts()
    with open_history_review(sample(tmp_path).root) as owner:
        window = HistoryReviewWindow(owner)
        try:
            window.set_language("en")
            window.show()
            tabs = window.tabs
            bar = tabs.tabBar()
            QTest.mouseClick(
                bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(1).center()
            )
            for index in range(4):
                rect = bar.tabRect(index)
                assert rect.size() == QSize(132, 44)
                # Measure the visible strokes inside the tab face, excluding its gutter and border.
                image = bar.grab(rect.adjusted(0, 0, -4, 0)).toImage()
                inset = round(7 * image.devicePixelRatio())
                background = image.pixel(inset, inset)
                ink = [
                    x
                    for x in range(inset, image.width() - inset)
                    if any(
                        image.pixel(x, y) != background
                        for y in range(inset, image.height() - inset)
                    )
                ]
                assert ink
                assert (
                    abs(ink[0] - (image.width() - 1 - ink[-1]))
                    <= 4 * image.devicePixelRatio()
                )

            window.set_language("az-Latn")
            assert (
                tabs.tabText(3)
                == bar.accessibleTabName(3)
                == text("records_alarms", "az-Latn")
            )
            assert tabs.tabToolTip(3) == text("alarm_history", "az-Latn")
            assert tabs.currentIndex() == 1 and bar.tabText(1) == "NG"
        finally:
            window.close()
            delete_widget(window)


def test_guest_normal_records_reads_only_capsule_projection(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    saved = sample(data.history.parent, 6, with_filtered=True)
    start = authenticated_startup(data, select_basler_batch=False)
    delivery = FutureDelivery()
    window = create_window(start, delivery)
    try:
        identity = start.application.identity
        history = start.application.history
        assert identity and history
        identity.logout()
        window.identity_controls.refresh()
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(1)
        page = window.ng_capsules
        until(lambda: not page.gallery.cells[0].image.isNull())
        assert (
            window.history_tabs.currentWidget() is page
            and window.records_export.isVisible()
        )
        assert not window.records_export.isEnabled()
        assert not window.history_tabs.isTabEnabled(
            3
        ) and not window.history_tabs.isTabEnabled(4)
        key = CapsuleKey(RUN, "101", saved.channel)
        detail = history.ng_capsule_detail(key).result(WAIT)
        assert detail and detail.roi == FrozenROI("CAM2", 4, RECT)
        assert not hasattr(detail, "effective") and not hasattr(detail, "resources")
        page.kind.setCurrentIndex(page.kind.findData(CapsuleFilter.FILTERED.value))
        until(lambda: bool(page.gallery.cells[0].filtered_boxes))
        assert page.selected_capsule and page.selected_capsule.filtered
        assert not window.records_export.isEnabled()
        with pytest.raises(AuthorizationError):
            history.run_configuration(RUN).result(WAIT)
        window.history_tabs.setCurrentWidget(window.history)
        assert (
            window.records_export.isHidden() and not window.records_export.isEnabled()
        )
    finally:
        close_window(window)
        delivery.detach()


def test_filtered_capsule_selection_overlay_and_export_in_both_workspaces(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    saved = sample(data.history.parent, with_filtered=True)

    def select_filtered(page: NgCapsulesPage) -> None:
        page.set_language("en")
        assert [page.kind.itemText(i) for i in range(3)] == ["NG", "Filtered", "All"]
        page.kind.setCurrentIndex(page.kind.findData(CapsuleFilter.FILTERED.value))
        cell, _, missing, _ = page.gallery.cells
        until(lambda: bool(cell.filtered_boxes))
        assert page.table.rowCount() == 1
        assert (
            page.selected_capsule
            and page.selected_capsule.filtered
            and page.selected_capsule.batch_uuid == saved.batch
        )
        rect = BoundingRect(15, 18, 12, 6)
        assert cell.filtered_boxes == {rect} and cell.overlays == (
            (rect, "Hair · 17 px²"),
        )
        assert missing.image.isNull() and not missing.overlays
        assert "Filtered · Hair · 17 px²" in missing.accessibleDescription()
        assert "Result · OK" in cell.toolTip()
        assert not page.gallery.next.isEnabled()

    with open_history_review(data.history.parent) as reader:
        portable = HistoryReviewWindow(reader)
        try:
            portable.show()
            portable.tabs.setCurrentIndex(1)
            select_filtered(portable.ng_capsules)
        finally:
            portable.close()
            delete_widget(portable)
    start = authenticated_startup(data, select_basler_batch=False)
    delivery = FutureDelivery()
    window = create_window(start, delivery)
    try:
        window.show()
        window.navigate(3)
        window.history_tabs.setCurrentIndex(1)
        page = window.ng_capsules
        select_filtered(page)
        assert (
            window.records_export.isVisible() and not window.records_export.isEnabled()
        )
        assert window.records_export.request is None
        page.kind.setCurrentIndex(page.kind.findData(CapsuleFilter.ALL.value))
        until(lambda: page.table.rowCount() == 3 and page.gallery.next.isEnabled())
        page.gallery.next.click()
        until(
            lambda: (
                window.records_export.isEnabled()
                and bool(page.gallery.cells[0].overlays)
            )
        )
        assert page.selected_capsule and not page.selected_capsule.filtered
        assert not page.gallery.cells[0].filtered_boxes
        page.gallery.previous.click()
        until(
            lambda: (
                not window.records_export.isEnabled()
                and bool(page.gallery.cells[0].filtered_boxes)
            )
        )
        assert page.selected_capsule and page.selected_capsule.filtered
    finally:
        close_window(window)
        delivery.detach()


def test_rapid_selection_coalesces_image_reads_and_dispose_is_nonblocking(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = sample(tmp_path, edge_area=41)
    started, release = Event(), Event()
    threads: list[int] = []
    slots: list[int] = []
    original = exposure_image

    def slow(
        exposure: CapsuleExposure, roi: FrozenROI | None, root: Path | None
    ) -> ExposureImage:
        threads.append(get_ident())
        if roi is not None:
            slots.append(roi.slot)
        if not started.is_set():
            started.set()
            assert release.wait(WAIT)
        return original(exposure, roi, root)

    monkeypatch.setattr(ng_capsule_images, "exposure_image", slow)
    with open_history_review(saved.root) as reader:
        page = NgCapsulesPage(reader, image_root=saved.root)
        try:
            page.show()
            page.refresh()
            until(started.is_set)
            for button in (page.gallery.next, page.gallery.previous) * 2:
                button.click()
            page.gallery.next.click()
            assert all(cell.image.isNull() for cell in page.gallery.cells)
            release.set()
            until(lambda: not page.gallery.cells[0].image.isNull())
            assert slots == [4, 5, 5, 5, 5] and all(t != get_ident() for t in threads)
            assert (
                "Edge · 41 px², Neighbour"
                in page.gallery.cells[0].accessibleDescription()
            )
            assert "Spot" not in page.gallery.cells[0].toolTip()
            started.clear()
            release.clear()
            page.table.setCurrentCell(0, 0)
            until(started.is_set)
            page.dispose()
            assert not release.is_set()
        finally:
            release.set()
            page.dispose()
            page.close()
            delete_widget(page)


@pytest.mark.parametrize("roi_width", (30, 230))
def test_gallery_packs_fitted_images_and_allocates_remaining_space(
    qapp: QApplication, tmp_path: Path, roi_width: int
) -> None:
    saved = sample(tmp_path, roi_width=roi_width)
    with open_history_review(saved.root) as reader:
        window = HistoryReviewWindow(reader)
        try:
            window.show()
            page = window.ng_capsules
            window.tabs.setCurrentWidget(page)
            gallery = page.gallery
            cells = gallery.cells
            until(lambda: not cells[0].image.isNull())
            images = tuple(cell.image for cell in cells)
            overlays = cells[0].overlays
            assert cells[2].image.isNull()
            viewports: tuple[tuple[int, int, Language], ...] = (
                (1080, 1920, "zh"),
                (1920, 1080, "en"),
                (1080, 1920, "de"),
            )
            for width, height, language in viewports:
                window.set_language(language)
                window.setFixedSize(width, height)
                qapp.processEvents()
                group, table = gallery.geometry(), page.table.geometry()
                header = page.table.horizontalHeader()
                for index in (0, 3, 4):
                    assert page.table.columnWidth(index) >= header.sectionSizeHint(
                        index
                    )
                assert page.table.horizontalScrollBar().maximum() == 0
                pane = gallery.parentWidget()
                assert pane is not None
                if width < height:
                    assert (
                        group.top() == 0
                        and table.top() == group.bottom() + 1 + Style.small
                    )
                    assert (
                        table.bottom() == pane.rect().bottom()
                        and table.width() == pane.width()
                    )
                else:
                    assert (
                        group.left() == 0
                        and table.left() == group.right() + 1 + Style.small
                    )
                    assert (
                        table.right() == pane.rect().right()
                        and table.height() == pane.height()
                    )
                assert len({c.x() for c in cells}) == 4
                assert (
                    len({c.y() for c in cells})
                    == len({h.y() for h in gallery.headers})
                    == 1
                )
                assert [(c.y(), c.x()) for c in cells] == sorted(
                    (c.y(), c.x()) for c in cells
                )
                assert all(gallery.rect().contains(c.geometry()) for c in cells)
                for i, cell in enumerate(cells):
                    assert cell.size() == cells[0].size() and cell.image == images[i]
                    assert gallery.headers[i].x() == cell.x()
                    assert gallery.headers[i].width() == cell.width()
                    assert (
                        gallery.headers[i].y()
                        == cell.geometry().bottom() + 1 + Style.icon_text_gap
                    )
                    assert (
                        gallery.headers[i].alignment() == Qt.AlignmentFlag.AlignCenter
                    )
                assert cells[0].overlays == overlays
                fit = image_transform(
                    roi_width, RECT.height, cells[0].width(), cells[0].height()
                )
                assert abs(fit.width - cells[0].width()) < 1
                assert abs(fit.height - cells[0].height()) < max(
                    1, RECT.height / roi_width
                )
                gutter = cells[1].x() - cells[0].x() - fit.width
                assert 3 <= gutter <= 6
                center = (
                    cells[0].geometry().top() + cells[-1].geometry().bottom()
                ) // 2
                for arrow in (gallery.previous, gallery.next):
                    assert arrow.size() == QSize(Style.touch, Style.touch)
                    assert abs(arrow.geometry().center().y() - center) <= 1
            # Even the transparent inner edge of each portrait control takes a tap.
            assert gallery.childAt(gallery.next.pos() + QPoint(2, 22)) is gallery.next
            QTest.mouseClick(gallery.next, Qt.MouseButton.LeftButton, pos=QPoint(2, 22))
            assert page.table.currentRow() == 1
            QTest.mouseClick(
                gallery.previous, Qt.MouseButton.LeftButton, pos=QPoint(41, 22)
            )
            assert page.table.currentRow() == 0
        finally:
            window.close()
            delete_widget(window)


def test_gallery_paging_and_shared_filter_reset(
    qapp: QApplication, tmp_path: Path
) -> None:
    saved = sample(tmp_path)
    with closing(sqlite3.connect(tmp_path / "history.sqlite3")) as db, db:
        for first in (301, 401):
            add_group(
                db,
                RUN,
                (first, first + 8, first + 2, first + 11),
                saved.observed + first,
                tuple(range(1, 31)),
                30,
            )
        db.execute(
            """INSERT INTO runs(uuid,started,outcome,recipe,reporting_mode)
               VALUES('unbatched',?,'stopped','{}','four_cycle')""",
            (saved.observed - 10,),
        )
        add_group(db, "unbatched", MEMBERS, saved.observed - 10, (1,), 30)
    with open_history_review(tmp_path) as owner:
        window = HistoryReviewWindow(owner)
        try:
            window.show()
            window.tabs.setCurrentWidget(window.ng_capsules)
            page = window.ng_capsules
            until(lambda: page.table.rowCount() == 50 and page.older.isEnabled())
            previous, following = page.gallery.previous, page.gallery.next
            assert not previous.isEnabled() and following.isEnabled()
            page.table.setFocus()
            QTest.keyClick(page.table, Qt.Key.Key_Down)
            until(lambda: "CH02" in page.gallery.cells[0].accessibleName())
            assert page.table.currentRow() == 1 and previous.isEnabled()
            previous.setFocus()
            QTest.keyClick(previous, Qt.Key.Key_Space)
            assert page.table.currentRow() == 0 and not previous.isEnabled()
            page.table.setCurrentCell(49, 0)
            following.setFocus()
            QTest.keyClick(following, Qt.Key.Key_Space)
            until(lambda: page.table.rowCount() == 13 and page.table.currentRow() == 0)
            assert following.hasFocus()
            assert page.table.selectedItems()[3].text() == "CH21"
            assert page.table.selectedItems()[4].text() == "301"
            previous.click()
            until(lambda: page.table.rowCount() == 50 and page.table.currentRow() == 49)
            assert page.table.selectedItems()[3].text() == "CH20"
            assert page.table.selectedItems()[4].text() == "301"
            last = page.table.currentItem()
            assert last is not None and page.table.viewport().rect().contains(
                page.table.visualItemRect(last).center()
            )
            page.older.click()
            until(lambda: page.table.rowCount() == 13 and page.newer.isEnabled())
            assert not page.older.isEnabled()
            page.table.setCurrentCell(12, 0)
            assert previous.isEnabled() and not following.isEnabled()
            batch = page.table.item(12, 1)
            recipe, channel, group = (page.table.item(12, i) for i in (2, 3, 4))
            assert recipe and recipe.text() == recipe.toolTip() == "—"
            assert channel and channel.text() == channel.toolTip() == "CH01"
            assert batch is not None and batch.text() == batch.toolTip() == "—"
            assert group is not None and group.text() == "101"
            page.newer.click()
            until(lambda: page.table.rowCount() == 50 and not page.newer.isEnabled())
            assert page.table.currentRow() == 0 and not previous.isEnabled()
            # Changing the scope supersedes the in-flight step back to row 49.
            page.older.click()
            until(lambda: page.table.rowCount() == 13)
            previous.click()
            window.filters.select_batch(None, unbatched=True)
            until(
                lambda: (
                    page.table.rowCount() == 1
                    and page.gallery.cells[0].empty_text == "不可用"
                )
            )
            assert not previous.isEnabled() and not following.isEnabled()
            assert all(
                cell.image.isNull() and not cell.overlays for cell in page.gallery.cells
            )
            assert page.table.currentRow() == 0
            window.filters.select_batch(saved.batch, "Training lot")
            until(
                lambda: page.scope.batch == saved.batch and page.table.rowCount() == 50
            )
            window.filters.end.set_date(QDate.currentDate().addDays(-3))
            until(
                lambda: (
                    page.message.isVisible()
                    and page.table.rowCount() == 0
                    and not page.gallery.isVisible()
                )
            )
            window.filters.end.clear()
            page.refresh_button.click()
            until(lambda: page.table.rowCount() == 50 and page.older.isEnabled())
            assert not previous.isEnabled() and following.isEnabled()
        finally:
            window.close()
            delete_widget(window)
