"""Real SQLite + queued Qt browsing, independent of Production and source actions."""

import json
import sqlite3
import zlib
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from time import time
from uuid import uuid4

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
)
from tests.support.application import WAIT, production
from tests.support.business import channel as business_channel
from tests.support.business import mixed_result
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.startup import saved_data

from capsule_vision.application import ConfigurationIdentity
from capsule_vision.application.archive_links import PublishedImage
from capsule_vision.application.history import (
    HistoryChannel,
    HistoryContext,
    HistoryPageData,
    HistoryQuery,
    HistoryRecord,
    ProductionHistory,
)
from capsule_vision.application.startup import load_startup
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CycleStatus
from capsule_vision.inspection.production import (
    ChannelId,
    FindingSource,
    OccupancyState,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.history_page import HistoryPage
from capsule_vision.ui.records_filters import RecordsFilters
from capsule_vision.ui.style import Style, Theme, apply_theme, theme_colors


def content(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def channel_at(page: HistoryPage, row: int, position: int) -> HistoryChannel:
    item = page.channels.item(row, position)
    assert item is not None
    value: object = item.data(Qt.ItemDataRole.UserRole)
    assert isinstance(value, HistoryChannel)
    return value


def cycle_record(page: HistoryPage, row: int = 0) -> HistoryRecord:
    item = page.cycles.item(row, 5)
    assert item is not None
    record = item.data(Qt.ItemDataRole.UserRole)
    assert isinstance(record, HistoryRecord)
    return record


def test_open_image_button_opens_only_the_selected_cycle_and_camera_png(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = ProductionHistory(tmp_path / "history.sqlite3")
    ctx = HistoryContext(
        time(), "segment", None, uuid4().hex, ConfigurationIdentity("r", "m", "c" * 64)
    )
    page = HistoryPage(history)
    photo = tmp_path / "归档 图片" / "saved_1.png"
    photo.parent.mkdir()
    pixels = QImage(4, 3, QImage.Format.Format_RGB32)
    pixels.fill(Qt.GlobalColor.green)
    assert pixels.save(str(photo))
    target = QUrl.fromLocalFile(str(photo))
    opened: list[QUrl] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url)
        return len(opened) == 1

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    try:
        history.start_run(None, ctx.run_id, ctx.active, None).result(WAIT)
        assert history.offer(ctx, mixed_result())
        assert history.offer(ctx, mixed_result(2))
        page.resize(1080, 900)
        page.show()
        page.activate()
        until(lambda: not page.busy)
        button = page.image_button
        assert button.text() == button.accessibleName() == "打开图片"
        assert button.menu() is None
        assert page.cycles.columnCount() == 6 and not button.isEnabled()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert not opened
        history.offer_image(
            PublishedImage(ctx.run_id, 1, "CAM2", photo, ctx.completed_at)
        )
        until(lambda: history.snapshot().images_linked == 1)
        page.refresh()
        until(lambda: not page.busy)
        page.cycles.selectRow(1)
        assert cycle_record(page, 1).cycle_id == 1 and not button.isEnabled()
        page.channels.setCurrentCell(
            1, 0
        )  # Camera name as well as any of its channel cells.
        assert button.isEnabled() and button.toolTip() == str(photo)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert opened == [target]
        page.set_language("en")
        assert button.text() == button.accessibleName() == "Open Image"
        page.channels.setCurrentCell(1, 3)
        button.setFocus()
        QTest.keyClick(button, Qt.Key.Key_Space)  # Desktop handler refuses this open.
        assert opened == [target, target]
        assert (
            text("history_image_open_failed", "en") in page.channel_detail.toPlainText()
        )
        photo.rename(photo.with_name("moved.png"))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert (
            opened == [target, target]
            and text("history_image_missing", "en") in page.channel_detail.toPlainText()
        )
        page.channels.setCurrentCell(0, 2)
        assert not button.isEnabled()
        page.cycles.selectRow(0)
        page.channels.setCurrentCell(1, 3)
        assert not button.isEnabled()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert opened == [target, target]
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)


def test_history_filters_pagination_selection_and_presentation(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    ensure_test_fonts()
    startup = load_startup(saved_data(tmp_path))
    history = startup.application.history
    assert history is not None
    first = HistoryContext(
        time(),
        uuid4().hex,
        "LOT-20261020-003",
        uuid4().hex,
        ConfigurationIdentity("recipe-a", "model", "c" * 64),
        "黄色胶囊-规格B-灯检方案",
    )
    second = replace(
        first,
        completed_at=first.completed_at + 1,
        batch_segment=uuid4().hex,
        run_id=uuid4().hex,
    )
    old = replace(
        first,
        completed_at=first.completed_at - 2 * 86400,
        batch_segment=uuid4().hex,
        run_id=uuid4().hex,
    )
    for i in range(1, 52):
        assert history.offer(first, production(i) if i == 49 else mixed_result(i))
    assert history.offer(second, mixed_result()) and history.offer(old, mixed_result())
    assert len(history.query().result(WAIT).records) == 50
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.set_language("zh")
    window.resize(1080, 1600)
    window.show()
    before = startup.application.snapshot()
    try:
        window.navigate(3)
        window.history_tabs.setCurrentWidget(window.history)
        page = window.history
        until(lambda: not page.busy)
        assert window.pages.currentWidget() is window.history_tabs
        assert window.history_tabs.currentWidget() is page
        assert page.cycles.rowCount() == 50 and page.channels.rowCount() == 6
        assert page.channels.columnCount() == 7
        assert (
            page.cycles.editTriggers() is QAbstractItemView.EditTrigger.NoEditTriggers
        )
        assert page.older.isEnabled() and not page.newer.isEnabled()
        assert channel_at(page, 0, 2).business == "EMPTY"
        assert channel_at(page, 0, 4).technical == "ERROR"
        item = page.channels.item(0, 4)
        assert item is not None
        QTest.mouseClick(
            page.channels.viewport(),
            Qt.MouseButton.LeftButton,
            pos=page.channels.visualItemRect(item).center(),
        )
        assert page.channel_detail.toPlainText().splitlines() == [
            text("records_model_defect", "zh"),
            text("execution_error", "zh"),
        ]
        assert "NG · ERR" in item.text()
        assert (
            cycle_record(page).context.active.model_sha256 == first.active.model_sha256
        )
        page.older.click()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 3 and page.newer.isEnabled()
        page.newer.click()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50 and not page.newer.isEnabled()
        filters = window.records_filters
        assert filters.start.value is filters.end.value is None
        filters.select_batch(old.batch_segment, old.batch_label or "")
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 1 and cycle_record(page).context == old
        assert filters.scope.batch == old.batch_segment
        filters.select_batch(second.batch_segment, second.batch_label or "")
        until(lambda: not page.busy)
        assert (
            page.cycles.rowCount() == 1
            and cycle_record(page).context.run_id == second.run_id
        )
        filters.select_batch(None)
        page.result.showPopup()
        view = page.result.view()
        for index in (1, 4):
            item = page.result.model().index(index, 0)
            QTest.mouseClick(
                view.viewport(),
                Qt.MouseButton.LeftButton,
                pos=view.visualRect(item).center(),
            )
            until(lambda: not page.busy)
            assert view.isVisible()
        assert page.cycles.rowCount() == 50
        page.result.hidePopup()
        page.result.set_selected(frozenset(("OK",)))
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50
        assert {c.business for c in cycle_record(page).channels} >= {"OK", "NG"}
        page.result.set_selected(frozenset())
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 0 and page.channels.rowCount() == 0
        assert page.channel_detail.isHidden() and not page.channel_detail.toPlainText()
        page.result.set_selected(frozenset(("EMPTY",)))
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50 and startup.application.snapshot() == before
        assert history.snapshot().accepted == history.snapshot().persisted == 53
        assert not history.snapshot().dropped and not history.snapshot().failed
    finally:
        close_window(window)
        delivery.detach()
    assert not history.snapshot().thread_alive


@pytest.mark.parametrize(
    "language,theme,size",
    [
        ("zh", "dark", (1080, 1500)),
        ("zh", "dark", (1920, 1017)),
        ("zh", "dark", (3440, 1200)),
        ("en", "light", (1920, 1017)),
    ],
)
def test_viewport_allocation_real_identifiers_and_numeric_fields(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
    theme: Theme,
    size: tuple[int, int],
) -> None:
    ensure_test_fonts()
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    apply_theme(page, theme)
    page.set_language(language)
    page.resize(*size)
    page.show()
    context = HistoryContext(
        time(),
        "segment",
        "20261020-01",
        "run",
        ConfigurationIdentity("recipe-a", "model", "c" * 64),
        "胶囊A",
    )
    try:
        for cycle_id, recipe, ok, ng in (
            (1, "胶囊A", 20, 2),
            (2, "阿莫西林胶囊", 6, 2),
            (3, "黄色胶囊-规格B-灯检方案", 0, 0),
        ):
            result = mixed_result(cycle_id)
            result = replace(
                result,
                cameras=tuple(
                    replace(
                        camera,
                        channels=tuple(
                            business_channel(
                                channel,
                                OccupancyState.PRESENT
                                if i < ok + ng
                                else OccupancyState.EMPTY,
                                TechnicalStatus.SUCCESS,
                                ProductJudgment.NG
                                if ok <= i < ok + ng
                                else ProductJudgment.OK,
                            )
                            for channel in camera.channels
                            for i in (int(channel.channel_id) - 1,)
                        ),
                    )
                    for camera in result.cameras
                ),
            )
            assert history.offer(
                replace(
                    context,
                    batch_label="LOT-20261020-003"
                    if cycle_id == 2
                    else context.batch_label,
                    recipe_name=recipe,
                ),
                result,
            )
        page.activate()
        until(lambda: not page.busy)
        qapp.processEvents()
        assert page.width() == size[0]
        layout = page.layout()
        assert layout is not None
        margins = layout.contentsMargins()
        assert margins.left() == margins.right() == 0
        assert page.workspace.width() == page.width() - margins.left() - margins.right()
        assert page.cycles.width() == page.channels.width() == page.workspace.width()
        assert content(page.cycles, 2, 5) == "OK 20 · NG 2 · EMPTY 14"
        assert content(page.cycles, 1, 5) == "OK 6 · NG 2 · EMPTY 28"
        widths = [page.cycles.columnWidth(i) for i in range(6)]
        viewport = page.cycles.viewport().width()
        print(
            f"History {language}/{theme}, page {page.width()}, viewport {viewport}, columns {widths}"
        )
        assert sum(widths) == viewport
        assert not page.cycles.horizontalScrollBar().isVisible()
        assert all(
            page.cycles.horizontalHeader().sectionResizeMode(i)
            == QHeaderView.ResizeMode.Fixed
            for i in range(6)
        )
        metrics = page.cycles.fontMetrics()
        for row in range(3):
            for col in range(6):
                item = page.cycles.item(row, col)
                assert item is not None
                assert metrics.horizontalAdvance(item.text()) + 12 <= widths[col]
                assert item.toolTip() == item.data(
                    Qt.ItemDataRole.AccessibleDescriptionRole
                )
                assert item.toolTip()
                if col == 0:
                    full = datetime.fromisoformat(item.toolTip())
                    assert full.utcoffset() is not None
                    assert full.strftime("%m-%d %H:%M:%S.%f")[:-3] == item.text()
        assert max(widths[1:3]) < widths[0]
        assert max(widths[1:3]) < min(widths[3:])
        camera_width = page.channels.columnWidth(0)
        if size[0] == 1080:
            assert 80 <= camera_width <= 100
        elif size[0] == 1920:
            assert 130 <= camera_width <= 160
        else:
            assert 160 < camera_width <= 260
        cells = [page.channels.columnWidth(i) for i in range(1, 7)]
        assert max(cells) - min(cells) <= 1
        assert camera_width + sum(cells) == page.channels.viewport().width()
        assert not page.channels.verticalScrollBar().isVisible()
        assert page.channels.verticalScrollBar().maximum() == 0
        assert not page.channels.horizontalScrollBar().isVisible()
        header = page.channels.horizontalHeader()
        pixels = header.grab().toImage()
        dpr = pixels.devicePixelRatio()
        assert pixels.pixelColor(
            int((header.width() - 3) * dpr), int(header.height() // 2 * dpr)
        ) == QColor(theme_colors(page).raised)
        for table in (page.cycles, page.channels):
            # The existing table frame is the card: children must not paint
            # square corners over it, or require an extra frame/padding.
            assert table.frameWidth() == 1
            header_mask = table.horizontalHeader().mask()
            viewport_mask = table.viewport().mask()
            assert not header_mask.contains(QPoint(0, 0))
            assert header_mask.contains(table.horizontalHeader().rect().center())
            assert not viewport_mask.contains(table.viewport().rect().bottomLeft())
            # The reserved scroll gutter owns the outer right corner of the Cycle list.
            corner = (
                table.verticalScrollBar().parentWidget()
                if table.verticalScrollBar().isVisible()
                else table.viewport()
            )
            assert corner and not corner.mask().contains(corner.rect().bottomRight())
            assert viewport_mask.contains(table.viewport().rect().center())
            frame = table.grab().toImage()
            scale = frame.devicePixelRatio()
            assert frame.pixelColor(int(table.width() // 2 * scale), 0) == QColor(
                theme_colors(page).border
            )
        # Exercise the real delegate with a painter: count fields have equal
        # pixel widths/right edges even when values change from one to two digits.
        drawn: list[tuple[QRect, Qt.AlignmentFlag, str]] = []
        original = QPainter.drawText

        def draw_text(
            painter: QPainter, rect: QRect, flags: Qt.AlignmentFlag, value: str
        ) -> None:
            drawn.append((QRect(rect), flags, value))
            original(painter, rect, flags, value)

        delegate = page.cycles.itemDelegateForColumn(5)
        assert isinstance(delegate, QStyledItemDelegate)
        model = page.cycles.model()
        assert model is not None
        option = QStyleOptionViewItem()
        option.initFrom(page.cycles)
        option.widget = page.cycles
        option.rect = QRect(0, 0, widths[5], 36)
        pixmap = QPixmap(widths[5], 36)
        right_edges: list[list[int]] = []
        with monkeypatch.context() as patch:
            patch.setattr(QPainter, "drawText", draw_text)
            for row, expected in ((2, ["20", "2", "14"]), (1, ["6", "2", "28"])):
                drawn.clear()
                painter = QPainter(pixmap)
                try:
                    delegate.paint(painter, option, model.index(row, 5))
                finally:
                    painter.end()
                numbers = [
                    (rect, value)
                    for rect, flags, value in drawn
                    if flags & Qt.AlignmentFlag.AlignRight
                ]
                assert [value for _, value in numbers] == expected
                assert len({rect.width() for rect, _ in numbers}) == 1
                assert all(
                    rect.width() >= metrics.horizontalAdvance("99")
                    for rect, _ in numbers
                )
                right_edges.append([rect.right() for rect, _ in numbers])
        assert right_edges[0] == right_edges[1]
        page.grab()

        page.resize(1920 if size[0] == 1080 else 1080, size[1])
        page.set_language("en" if language == "zh" else "zh")
        qapp.processEvents()
        resized = [page.cycles.columnWidth(i) for i in range(6)]
        assert sum(resized) == page.cycles.viewport().width() and resized != widths
        assert not page.cycles.horizontalScrollBar().isVisible()
        assert (
            page.channels.viewport()
            .mask()
            .contains(page.channels.viewport().rect().center())
        )
        assert (
            not page.channels.viewport()
            .mask()
            .contains(page.channels.viewport().rect().bottomRight())
        )
        # Secondary edge case: long identifiers elide without consuming all
        # surplus, losing their full tooltip or moving the columns.
        long_context = replace(
            context,
            completed_at=context.completed_at + 1,
            run_id="long-run",
            batch_label="LOT-20261020-003 · " + "long batch " * 8,
            recipe_name="long recipe " * 10,
        )
        assert history.offer(long_context, result)
        page.refresh()
        until(lambda: not page.busy)
        assert [page.cycles.columnWidth(i) for i in range(6)] == resized
        for col, value in (
            (3, long_context.batch_label),
            (4, long_context.recipe_name),
        ):
            item = page.cycles.item(0, col)
            assert item is not None and item.text() == value
            assert item.toolTip() == item.text() + (
                "\nID: recipe-a" if col == 4 else ""
            )
            assert metrics.horizontalAdvance(item.text()) > page.cycles.columnWidth(col)
        assert (
            sum(page.cycles.columnWidth(i) for i in range(6))
            == page.cycles.viewport().width()
        )
        assert history.snapshot().accepted == history.snapshot().persisted == 4
        if size[0] == 1080:
            # A deliberately constrained matrix still scrolls real overflow;
            # removing the trailing header artifact must not hide columns.
            page.channels.setMaximumWidth(5 * Style.touch)
            until(page.channels.horizontalScrollBar().isVisible)
            assert page.channels.horizontalScrollBar().maximum() > 0
            assert page.channels.verticalScrollBar().maximum() == 0
            scrollbar = page.channels.horizontalScrollBar()
            container = scrollbar.parentWidget()
            assert container is not None
            assert container.mask().contains(container.rect().center())
            assert not container.mask().contains(container.rect().bottomRight())
            last = page.channels.item(0, 6)
            assert last is not None
            page.channels.scrollToItem(last)
            assert (
                page.channels.viewport()
                .rect()
                .contains(page.channels.visualItemRect(last))
            )
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


@pytest.mark.parametrize(
    "language,theme,size",
    [
        ("zh", "dark", (1080, 1500)),
        ("en", "light", (1920, 1017)),
    ],
)
def test_stored_recipe_names_missing_names_and_content_sized_channel_details(
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    theme: Theme,
    size: tuple[int, int],
) -> None:
    ensure_test_fonts()
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    apply_theme(page, theme)
    page.set_language(language)
    page.resize(*size)
    page.show()
    context = HistoryContext(
        time(),
        "segment",
        "LOT-20261020-003",
        "run",
        ConfigurationIdentity("recipe-internal-id", "model", "c" * 64),
        "阿莫西林胶囊-规格B-灯检方案",
    )
    try:
        assert history.offer(context, mixed_result())
        assert history.offer(
            replace(context, completed_at=context.completed_at - 1, recipe_name=None),
            mixed_result(2),
        )
        page.activate()
        until(lambda: not page.busy)
        assert content(page.cycles, 0, 4) == context.recipe_name
        name_item = page.cycles.item(0, 4)
        missing_name_item = page.cycles.item(1, 4)
        assert (
            name_item is not None
            and name_item.toolTip() == f"{context.recipe_name}\nID: recipe-internal-id"
        )
        assert (
            missing_name_item is not None
            and missing_name_item.text() == "—"
            and "recipe-internal-id" in missing_name_item.toolTip()
            and missing_name_item.data(Qt.ItemDataRole.AccessibleDescriptionRole)
            == missing_name_item.toolTip()
        )
        assert (
            text("history_recipe_name_missing", language) in missing_name_item.toolTip()
        )
        qapp.processEvents()
        assert page.channel_detail.isHidden() and not page.channel_detail.toPlainText()
        page.channels.setCurrentCell(0, 2)
        assert page.channel_detail.isHidden() and not page.channel_detail.toPlainText()
        defect, failure = (
            text("records_model_defect", language),
            text("execution_error", language),
        )
        for col, expected in (
            (6, [defect]),
            (5, [failure]),
            (4, [defect, failure]),
        ):
            page.channels.setCurrentCell(0, col)
            assert page.channel_detail.isVisible()
            assert page.channel_detail.toPlainText().splitlines() == expected
            assert (
                24
                <= page.channel_detail.height()
                <= 4 * page.channel_detail.fontMetrics().lineSpacing()
            )
        qapp.processEvents()
        assert not page.channels.horizontalScrollBar().isVisible()
        page.grab()
        page.channels.setCurrentCell(-1, -1)
        assert (
            not page.channel_detail.isVisible()
            and not page.channel_detail.toPlainText()
        )
        page.cycles.selectRow(1)
        assert cycle_record(page, 1).context.active.recipe_id == "recipe-internal-id"
        assert cycle_record(page, 1).context.recipe_name is None
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("mixed", "OK 6 · NG 12 · EMPTY 6 · UNKNOWN 12 · ERR 12"),
        ("ok", "OK 36 · NG 0"),
        ("empty", "OK 0 · NG 0 · EMPTY 36"),
        ("unknown", "OK 0 · NG 0 · UNKNOWN 36"),
        ("unassigned", "OK 36 · NG 0"),
        ("cycle_error", "OK 36 · NG 0 · 周期 ERR · 周期 UNKNOWN"),
        ("timed_out", "OK 36 · NG 0 · 周期超时 · 周期 ERR · 周期 UNKNOWN"),
    ],
)
def test_business_counts_independent_errors_and_cycle_anomalies(
    qapp: QApplication,
    tmp_path: Path,
    scenario: str,
    expected: str,
) -> None:
    ensure_test_fonts()
    result = mixed_result()
    if scenario != "mixed":
        occupancy = {
            "empty": OccupancyState.EMPTY,
            "unknown": OccupancyState.UNKNOWN,
        }.get(scenario, OccupancyState.PRESENT)
        result = replace(
            result,
            cameras=tuple(
                replace(
                    camera,
                    channels=tuple(
                        business_channel(
                            c, occupancy, TechnicalStatus.SUCCESS, ProductJudgment.OK
                        )
                        for c in camera.channels
                    ),
                )
                for camera in result.cameras
            ),
        )
    if scenario == "unassigned":
        camera = result.cameras[0]
        assert camera.frame_id is not None
        finding = ViMoFinding(
            camera.frame_id,
            0,
            "unassigned_defect",
            1,
            BoundingRect(0, 0, 1, 1),
            "module",
        )
        result = replace(
            result,
            cameras=(
                replace(camera, unassigned_findings=(finding,)),
                *result.cameras[1:],
            ),
        )
    elif scenario == "cycle_error":
        camera = result.cameras[0]
        result = replace(
            result,
            cameras=(
                replace(
                    camera,
                    collection_issue=Reason(
                        FindingSource.COLLECTION, "camera_collection_error"
                    ),
                ),
                *result.cameras[1:],
            ),
        )
    elif scenario == "timed_out":
        result = replace(result, cycle_status=CycleStatus.TIMED_OUT)
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    apply_theme(page, "dark")
    page.resize(1080, 1500)
    page.show()
    try:
        assert history.offer(
            HistoryContext(
                time(),
                "segment",
                "batch",
                "run",
                ConfigurationIdentity("recipe", "model", "c" * 64),
            ),
            result,
        )
        page.activate()
        until(lambda: not page.busy)
        assert content(page.cycles, 0, 5) == expected
        qapp.processEvents()
        result_item = page.cycles.item(0, 5)
        assert result_item is not None
        assert (
            page.cycles.viewport()
            .rect()
            .contains(page.cycles.visualItemRect(result_item))
        )
        # Exceptional warnings can elide at narrow widths; the full summary
        # remains accessible, independently of the fixed-width count rendering.
        assert (
            result_item.toolTip()
            == result_item.data(Qt.ItemDataRole.AccessibleTextRole)
            == expected
        )
        counts = [
            part for part in expected.split(" · ") if part.rpartition(" ")[2].isdigit()
        ]
        assert (
            page.cycles.columnWidth(5)
            >= page.cycles.fontMetrics().horizontalAdvance(" · ".join(counts)) + 12
        )
        stored = history.query().result(WAIT).records[0]
        assert (
            sum(
                sum(c.business == state for c in stored.channels)
                for state in ("OK", "NG", "EMPTY", "UNKNOWN")
            )
            == 36
        )
        assert stored.technical == result.assessment.technical_status.name
        texts = [
            content(page.cycles, 0, 5),
            page.channels.toolTip(),
            page.channel_detail.toPlainText(),
        ]
        for row in range(6):
            for column in range(7):
                item = page.channels.item(row, column)
                assert item is not None
                texts.extend((item.text(), item.toolTip()))
        assert all(
            "SUCCESS" not in value and "/ S" not in value and "COMPLETE" not in value
            for value in texts
        )
        if scenario == "mixed":
            errors = [c for c in stored.channels if c.technical == "ERROR"]
            assert len(errors) == 12 and sum(c.business == "NG" for c in errors) == 6
            page.channels.setCurrentCell(0, 4)
            assert page.channel_detail.toPlainText().splitlines() == [
                text("records_model_defect", "zh"),
                text("execution_error", "zh"),
            ]
            assert "NG · ERR" in content(page.channels, 0, 4)
        elif scenario in ("ok", "empty"):
            assert (
                page.channel_detail.isHidden() and not page.channel_detail.toPlainText()
            )
        elif scenario == "unassigned":
            assert stored.judgment == stored.cameras[0].judgment == "OK"
            assert "unassigned_defect" in stored.cameras[0].unassigned_labels
            assert all(not c.reasons for c in stored.channels)
            page.channels.setCurrentCell(0, 0)
            assert (
                text("records_unassigned", "zh").format(labels="unassigned_defect")
                in page.channel_detail.toPlainText()
            )
            page.channels.setCurrentCell(0, 1)
            assert page.channel_detail.isHidden()
        elif scenario in ("cycle_error", "timed_out"):
            assert "周期 ERR" in content(page.cycles, 0, 5)
        if scenario == "cycle_error":
            page.channels.setCurrentCell(0, 0)
            assert text("collection_short", "zh") in page.channel_detail.toPlainText()
            item = page.channels.item(0, 0)
            assert (
                item is not None
                and "camera_collection_error" in stored.cameras[0].reasons
            )
        if scenario in ("cycle_error", "timed_out"):
            page.set_language("en")
            assert content(page.cycles, 0, 5) == expected.replace(
                "周期超时", text("cycle_timeout", "en")
            ).replace("周期", "Cycle")
            assert "Cycle" in content(page.cycles, 0, 5)
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


def test_historical_camera_ng_keeps_its_stored_judgment(
    qapp: QApplication, tmp_path: Path
) -> None:
    path = tmp_path / "history.sqlite3"
    history = ProductionHistory(path)
    history.initial_selection.result(WAIT)
    history.request_stop()
    assert history.join(WAIT)
    # A recorded pre-change Cycle: every channel was OK, while the unmatched
    # finding on CAM1 was recorded as camera- and Cycle-level NG.
    channels = [
        [f"CAM{i // 6 + 1}", i % 6, i + 1, "OK", "SUCCESS", "OK", ()] for i in range(36)
    ]
    cameras = [
        [
            f"CAM{i + 1}",
            "SUCCESS",
            "NG" if i == 0 else "OK",
            [],
            ["background"] if i == 0 else [],
        ]
        for i in range(6)
    ]
    detail = zlib.compress(json.dumps([channels, cameras, "Old Recipe"]).encode())
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO cycles(completed_at,batch_segment,run_id,cycle_id,recipe_id,model_id,model_sha,"
            "status,technical,judgment,empty_count,unknown_count,detail) VALUES(?,?,?,?,?,?,?,"
            "'COMPLETE','SUCCESS','NG',0,0,?)",
            (
                time(),
                "segment",
                "historical-run",
                "1",
                "recipe",
                "model",
                "c" * 64,
                detail,
            ),
        )
    history = ProductionHistory(path)
    page = HistoryPage(history)
    try:
        page.activate()
        until(lambda: not page.busy)
        assert content(page.cycles, 0, 5) == "OK 36 · NG 0 · 周期 NG"
        stored = cycle_record(page)
        assert stored.judgment == stored.cameras[0].judgment == "NG"
        assert stored.cameras[0].unassigned_labels == ("background",)
        page.set_language("en")
        assert content(page.cycles, 0, 5).endswith("Cycle NG")
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


@pytest.mark.parametrize("camera_count", [5, 6])
def test_matrix_preserves_stored_camera_order_and_slot_mapping_at_minimum_width(
    qapp: QApplication,
    tmp_path: Path,
    camera_count: int,
) -> None:
    ensure_test_fonts()
    result = mixed_result()
    cameras = tuple(
        replace(
            camera,
            layout=replace(
                camera.layout,
                slots=tuple(
                    replace(
                        slot,
                        channel_id=ChannelId(
                            camera_count * 6 + 1 - int(slot.channel_id)
                        ),
                    )
                    for slot in camera.layout.slots
                ),
            ),
            channels=tuple(
                replace(
                    channel,
                    channel_id=ChannelId(
                        camera_count * 6 + 1 - int(channel.channel_id)
                    ),
                )
                for channel in camera.channels
            ),
        )
        for camera in reversed(result.cameras[:camera_count])
    )
    result = replace(
        result,
        layout=replace(result.layout, cameras=tuple(c.layout for c in cameras)),
        cameras=cameras,
    )
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    apply_theme(page, "dark")
    page.resize(1080, 1500)
    page.show()
    try:
        assert history.offer(
            HistoryContext(
                time(),
                "segment",
                "batch",
                "run",
                ConfigurationIdentity("recipe", "model", "c" * 64),
            ),
            result,
        )
        page.activate()
        until(lambda: not page.busy)
        qapp.processEvents()
        assert page.width() == 1080
        assert (
            page.channels.rowCount() == camera_count
            and page.channels.columnCount() == 7
        )
        assert not page.channels.horizontalScrollBar().isVisible()
        for row, camera in enumerate(cameras):
            assert content(page.channels, row, 0) == camera.camera_id
            for position, source in enumerate(camera.channels, 1):
                stored = channel_at(page, row, position)
                assert (stored.camera, stored.channel, stored.slot) == (
                    source.camera_id,
                    source.channel_id,
                    source.slot,
                )
                assert (stored.business, stored.technical, stored.judgment) == (
                    source.business_status.name,
                    source.assessment.technical_status.name,
                    source.assessment.judgment.name,
                )
                cell = page.channels.item(row, position)
                assert cell is not None
                assert not cell.toolTip()
                assert (
                    page.channels.viewport()
                    .rect()
                    .contains(page.channels.visualItemRect(cell))
                )
        assert channel_at(page, 0, 1).channel == 6
        assert channel_at(page, 0, 6).channel == 1
        page.channels.setCurrentCell(0, 4)
        assert "NG · ERR" in content(page.channels, 0, 4)
        assert page.channel_detail.toPlainText().splitlines() == [
            text("records_model_defect", page.language),
            text("execution_error", page.language),
        ]
        page.grab()
        page.cycles.clearSelection()
        page.cycles.setCurrentCell(-1, -1)
        page.cycles.clearSelection()
        # A no-result filter must clear every old detail, not reuse the previous cycle.
        page.result.set_selected(frozenset())
        until(lambda: not page.busy)
        assert page.channels.rowCount() == 0 and not page.channel_detail.toPlainText()
    finally:
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


@pytest.mark.parametrize("size", [(1080, 1500), (1920, 1017)])
def test_bilingual_geometry_segments_top_pagination_and_calendar(
    qapp: QApplication,
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    ensure_test_fonts()
    stamp = (
        datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    )
    context = HistoryContext(
        stamp,
        "abcdef1-first",
        "LOT-20261020-003",
        "run",
        ConfigurationIdentity("recipe", "model", "c" * 64),
        "阿莫西林胶囊",
    )
    history = ProductionHistory(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    shared = RecordsFilters(history)
    page.filter_bar.insertWidget(0, shared)

    def update_scope() -> None:
        page.scope = shared.scope
        page.refresh()

    shared.changed.connect(update_scope)
    apply_theme(page, "dark")
    page.resize(*size)
    page.show()
    page.activateWindow()
    assert QTest.qWaitForWindowActive(page)
    try:
        for cycle in range(1, 52):
            assert history.offer(context, mixed_result(cycle))
        assert history.offer(
            replace(context, batch_segment="abcdef2-second"), mixed_result(52)
        )
        assert history.offer(
            replace(context, completed_at=stamp + 5, batch_segment="other-segment"),
            replace(production(53), cycle_status=CycleStatus.TIMED_OUT),
        )
        page.activate()
        until(lambda: not page.busy)
        qapp.processEvents()
        assert page.width() == size[0]
        assert shared.start.value is shared.end.value is None
        assert shared.scope.batch is None
        assert shared.batch.text() == text("records_all_batches", "zh")
        page.cycles.selectRow(1)
        page.channels.setCurrentCell(0, 4)
        selected = channel_at(page, 0, 4)
        assert selected.business == "NG" and selected.technical == "ERROR"
        qapp.processEvents()  # Settle the normal-to-abnormal detail height change.
        widgets = (
            shared.start,
            shared.end,
            shared.batch,
            page.result,
            page.newer,
            page.older,
            page.refresh_button,
            page.cycles,
            page.channels,
            page.channel_detail,
        )
        geometry = tuple(
            QRect(widget.mapTo(page, QPoint()), widget.size()) for widget in widgets
        )
        matrix_body_height = (
            page.channels.height() - page.channels.horizontalHeader().height()
        )
        columns = tuple(page.cycles.columnWidth(i) for i in range(6))
        matrix_columns = tuple(page.channels.columnWidth(i) for i in range(7))
        assert shared.batch.width() == 208 and page.result.width() == 176
        assert (
            len(
                {
                    button.size().toTuple()
                    for button in (page.newer, page.older, page.refresh_button)
                }
            )
            == 1
        )
        assert not page.refresh_button.icon().isNull()
        filters = (
            shared.start.value,
            shared.scope.batch,
            page.result.selected,
        )
        assert not page.status.isVisible() and not page.status.text()
        assert not hasattr(page, "identity")
        assert page.channels.y() == page.cycles.y() + page.cycles.height() + Style.small
        assert geometry[6].bottom() < geometry[7].top()
        assert geometry[4].right() < geometry[5].left() < geometry[6].left()
        assert geometry[2].right() < geometry[3].left()
        page.grab()
        assert all(page.rect().contains(rect) for rect in geometry), (
            page.rect(),
            geometry,
        )
        assert page.older.isEnabled() and not page.newer.isEnabled()
        page.grab()
        page.set_language("en")
        shared.set_language("en")
        qapp.processEvents()
        assert not page.busy
        localized_geometry = tuple(
            QRect(widget.mapTo(page, QPoint()), widget.size()) for widget in widgets
        )
        assert geometry[:7] == localized_geometry[:7]
        assert geometry[9] == localized_geometry[9]
        # Native header glyph extents may differ between scripts. Preserve the
        # exact row body and contiguous list/matrix allocation, not that font metric.
        assert (
            page.channels.height() - page.channels.horizontalHeader().height()
            == matrix_body_height
        )
        assert page.cycles.y() == geometry[7].y()
        assert (
            page.cycles.height() + page.channels.height()
            == geometry[7].height() + geometry[8].height()
        )
        assert page.channels.y() == page.cycles.y() + page.cycles.height() + Style.small
        assert all(page.rect().contains(rect) for rect in localized_geometry)
        assert columns == tuple(page.cycles.columnWidth(i) for i in range(6))
        assert matrix_columns == tuple(page.channels.columnWidth(i) for i in range(7))
        assert filters == (
            shared.start.value,
            shared.scope.batch,
            page.result.selected,
        )
        assert channel_at(page, 0, 4) == selected
        assert (
            text("execution_error", page.language) in page.channel_detail.toPlainText()
        )
        page.grab()
        toolbar_geometry = tuple(
            QRect(widget.mapTo(page, QPoint()), widget.size()) for widget in widgets[:7]
        )
        for language in LANGUAGES:
            page.set_language(language)
            shared.set_language(language)
            assert columns == tuple(page.cycles.columnWidth(i) for i in range(6))
            result_header = page.cycles.horizontalHeaderItem(5)
            assert result_header and result_header.data(
                Qt.ItemDataRole.AccessibleTextRole
            ) == text("history_result", language)
            time_header = page.cycles.horizontalHeaderItem(0)
            cycle_header = page.cycles.horizontalHeaderItem(1)
            assert time_header is not None and cycle_header is not None
            assert time_header.text() == text("short_time", language)
            assert text("history_clock", language) in time_header.toolTip()
            assert shared.start.toolTip() and not page.channels.toolTip()
            assert cycle_header.text() == text(
                "records_cycle", language
            ) and cycle_header.toolTip() == text("history_cycle", language)
            assert tuple(page.result.itemText(i) for i in range(5)) == (
                "OK",
                "NG",
                "EMPTY",
                "UNKNOWN",
                "ERR",
            )
            assert page.result.itemData(2, Qt.ItemDataRole.ToolTipRole) == text(
                "history_empty", language
            )
            for button, key in (
                (page.newer, "history_newer"),
                (page.older, "history_older"),
                (page.refresh_button, "history_refresh"),
            ):
                assert button.text() == "" and not button.icon().isNull()
                assert button.width() == button.height() == Style.touch
                assert (
                    button.accessibleName() == button.toolTip() == text(key, language)
                )
            qapp.processEvents()
            assert shared.start.accessibleName() == text("records_from", language)
            assert shared.end.accessibleName() == text("records_to", language)
            assert toolbar_geometry == tuple(
                QRect(widget.mapTo(page, QPoint()), widget.size())
                for widget in widgets[:7]
            ), language
            assert (
                page.result.width() == 176
            )  # Rare captions elide; full descriptions remain.
            assert (
                page.refresh_button.accessibleName()
                == page.refresh_button.toolTip()
                == text("history_refresh", language)
            )
            assert filters == (
                shared.start.value,
                shared.scope.batch,
                page.result.selected,
            )
            assert channel_at(page, 0, 4) == selected and not page.busy
        page.set_language("en")
        shared.set_language("en")
        shared.select_batch("abcdef2-second", "LOT-20261020-003")
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 1 and content(page.cycles, 0, 1) == "52"
        page.set_language("zh")
        assert shared.scope.batch == "abcdef2-second"
        shared.select_batch(None)
        until(lambda: not page.busy)
        page.older.click()
        until(lambda: not page.busy)
        assert (
            page.cycles.rowCount() == 3
            and page.newer.isEnabled()
            and not page.older.isEnabled()
        )
        page.newer.click()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50 and not page.newer.isEnabled()

        # Calendar keyboard and clear semantics are covered by the shared-filter test.
    finally:
        shared.start.popup.hide()
        shared.popup.hide()
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()


def test_loading_stale_delivery_empty_unavailable_and_warning(
    qapp: QApplication, tmp_path: Path
) -> None:
    entered, release = Event(), Event()
    queries: list[HistoryQuery] = []

    class SlowRead(ProductionHistory):
        def _read(
            self, connection: sqlite3.Connection, query: HistoryQuery
        ) -> HistoryPageData:
            queries.append(query)
            entered.set()
            assert release.wait(WAIT)
            return super()._read(connection, query)

    history = SlowRead(tmp_path / "history.sqlite3")
    page = HistoryPage(history)
    apply_theme(page, "dark")
    page.show()
    try:
        page.activate()
        assert entered.wait(WAIT) and page.busy
        assert page.status.text() == text("history_loading", "zh")
        responsive = Event()
        QTimer.singleShot(0, responsive.set)
        until(responsive.is_set)
        page.result.set_selected(frozenset(("NG",)))
        page.result.set_selected(frozenset(("EMPTY",)))
        release.set()
        until(lambda: not page.busy)
        assert queries[-1].result == frozenset(("EMPTY",))
        assert page.status.text() == text("history_none", "zh")
        assert page.cycles.rowCount() == page.channels.rowCount() == 0
        history.request_stop()
        assert history.join(WAIT)
        assert not history.offer(
            HistoryContext(
                time(), "batch", None, "run", ConfigurationIdentity("r", "m", "a" * 64)
            ),
            mixed_result(),
        )
        page.refresh()
        until(lambda: not page.busy)
        assert page.status.text() == text("history_unavailable", "zh")
        until(page.warning.isVisible)
        assert "1" in page.warning.text()
    finally:
        release.set()
        page.dispose()
        page.close()
        history.request_stop()
        assert history.join(WAIT)
        page.deleteLater()
    unavailable = HistoryPage(None)
    try:
        unavailable.activate()
        assert unavailable.status.text() == text("history_unavailable", "zh")
        assert not unavailable.refresh_button.isEnabled()
    finally:
        unavailable.dispose()
        unavailable.deleteLater()


def test_result_popup_keeps_multi_selection_open_and_keyboard_accessible(
    qapp: QApplication,
) -> None:
    from capsule_vision.application.report_scope import (
        ALL_RESULTS,
        RESULT_STATES,
        ResultFilter,
    )
    from capsule_vision.ui.history_result_filter import HistoryResultFilter

    selector = HistoryResultFilter()
    selector.resize(176, 44)
    selector.show()
    changes: list[object] = []
    selector.changed.connect(lambda: changes.append(selector.selected))
    try:
        assert selector.selected == ALL_RESULTS
        selector.showPopup()
        view = selector.view()
        for row in (1, 4):
            index = selector.model().index(row, 0)
            assert index.flags() & Qt.ItemFlag.ItemIsUserCheckable
            QTest.mouseClick(
                view.viewport(),
                Qt.MouseButton.LeftButton,
                pos=view.visualRect(index).center(),
            )
            assert view.isVisible()
            assert (
                selector.itemData(row, Qt.ItemDataRole.CheckStateRole)
                == Qt.CheckState.Unchecked
            )
        assert selector.selected == frozenset(("OK", "EMPTY", "UNKNOWN"))
        assert len(changes) == 2
        view.setCurrentIndex(selector.model().index(0, 0))
        QTest.keyClick(view, Qt.Key.Key_Space)
        assert "OK" not in selector.selected and view.isVisible()
        QTest.keyClick(view, Qt.Key.Key_Escape)
        assert not view.isVisible()
        for language in ("zh", "en", "de"):
            selector.set_language(language)
            assert selector.accessibleDescription() == selector.toolTip()
            assert selector.selected == frozenset(("EMPTY", "UNKNOWN"))
        selections: tuple[ResultFilter, ...] = (frozenset(), ALL_RESULTS)
        for states in selections:
            selector.set_selected(states)
            assert selector.selected == states
            assert all(
                selector.itemData(i, Qt.ItemDataRole.CheckStateRole)
                == (Qt.CheckState.Checked if s in states else Qt.CheckState.Unchecked)
                for i, s in enumerate(RESULT_STATES)
            )
    finally:
        selector.hidePopup()
        selector.close()
        selector.deleteLater()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_result_popup_draws_plain_checkmarks_without_selection_boxes(
    qapp: QApplication, theme: Theme
) -> None:
    from capsule_vision.ui.history_result_filter import HistoryResultFilter

    ensure_test_fonts()
    selector = HistoryResultFilter()
    apply_theme(selector, theme)
    colors = theme_colors(selector)
    selector.set_selected(frozenset(("OK",)))
    view = selector.view()
    option = QStyleOptionViewItem()
    option.widget = view
    option.rect = QRect(0, 0, 176, 44)
    option.font = view.font()
    option.state = QStyle.StateFlag.State_Enabled

    def rendered(row: int) -> QImage:
        image = QImage(176, 44, QImage.Format.Format_ARGB32)
        image.fill(QColor("magenta"))
        painter = QPainter(image)
        selector.itemDelegate().paint(painter, option, selector.model().index(row, 0))
        painter.end()
        return image

    try:
        unchecked = rendered(1)
        assert all(
            unchecked.pixelColor(x, y) == QColor(colors.surface)
            for x in range(140, 176)
            for y in range(44)
        )
        checked = rendered(0)
        option.state |= QStyle.StateFlag.State_Selected
        assert rendered(0) == checked  # Current row adds no persistent fill/box.
        assert any(
            checked.pixelColor(x, y) != QColor(colors.surface)
            for x in range(146, 166)
            for y in range(10, 34)
        )
        # The empty border around the glyph excludes a native checkbox square.
        assert all(
            checked.pixelColor(x, y) == QColor(colors.surface)
            for x in range(140, 176)
            for y in (1, 2, 41, 42)
        )
        option.state |= QStyle.StateFlag.State_MouseOver
        assert rendered(0).pixelColor(80, 4) == QColor(colors.raised)
    finally:
        selector.close()
        selector.deleteLater()
