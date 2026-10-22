"""Real SQLite + queued Qt browsing, independent of Production and source actions."""

import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from time import time
from uuid import uuid4

import pytest
from PySide6.QtCore import QDate, QLocale, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QLineEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QToolButton,
)
from tests.application_support import WAIT, production
from tests.bootstrap_support import saved_data
from tests.business_support import channel as business_channel
from tests.business_support import mixed_result
from tests.qt_support import assert_full_caption, close_window, ensure_test_fonts, until

from vimo_demo.application import ConfigurationIdentity
from vimo_demo.application.history import (
    HistoryChannel,
    HistoryContext,
    HistoryPageData,
    HistoryQuery,
    ProductionHistory,
)
from vimo_demo.application.preferences import LANGUAGES
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CycleStatus
from vimo_demo.inspection.production import (
    ChannelId,
    FindingSource,
    OccupancyState,
    ProductJudgment,
    Reason,
    TechnicalStatus,
    ViMoFinding,
)
from vimo_demo.ui.bootstrap import create_window, load_startup
from vimo_demo.ui.history_page import HistoryPage
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_style import (
    Language,
    Style,
    Theme,
    apply_theme,
    text,
    theme_colors,
)


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


def cycle_tip(page: HistoryPage, row: int = 0) -> str:
    item = page.cycles.item(row, 1)
    assert item is not None
    return item.toolTip()


@pytest.mark.parametrize(
    "language,theme,size",
    [
        ("zh", "dark", (1080, 1600)),
        ("zh", "dark", (1920, 1017)),
        ("zh", "dark", (3440, 1200)),
        ("en", "light", (1920, 1017)),
    ],
)
def test_history_filters_pagination_selection_and_presentation(
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    theme: Theme,
    size: tuple[int, int],
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
    window.set_language(language)
    window.set_theme(theme)
    window.resize(*size)
    window.show()
    before = startup.application.snapshot()
    try:
        window.navigate(3)
        page = window.history
        until(lambda: not page.busy)
        assert window.pages.currentWidget() is page
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
        assert "execution_error" in page.channel_detail.toPlainText()
        assert (
            "NG" in page.channel_detail.toPlainText()
            and "ERROR" in page.channel_detail.toPlainText()
        )
        assert (
            content(page.cycles, 0, 4) == "OK 6 · NG 12 · EMPTY 6 · UNKNOWN 12 · ERR 12"
        )
        assert "COMPLETE" not in content(page.cycles, 0, 4)
        assert first.active.model_sha256 in cycle_tip(page)
        assert page.batch.count() == 3 and page.batch.itemData(
            1
        ) != page.batch.itemData(2)
        page.older.click()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 2 and page.newer.isEnabled()
        page.newer.click()
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50 and not page.newer.isEnabled()
        page.all_dates.click()
        until(lambda: not page.busy)
        assert not page.date.isEnabled() and page.batch.count() == 4
        page.batch.setCurrentIndex(page.batch.findData(second.batch_segment))
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 1 and second.run_id in cycle_tip(page)
        page.batch.setCurrentIndex(0)
        page.result.setCurrentIndex(page.result.findData("OK"))
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 1 and channel_at(page, 0, 1).judgment == "OK"
        assert "execution_error" not in page.channel_detail.toPlainText()
        page.result.setCurrentIndex(page.result.findData("EMPTY"))
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 50 and startup.application.snapshot() == before
        assert history.snapshot().accepted == history.snapshot().persisted == 53
        assert not history.snapshot().dropped and not history.snapshot().failed
        qapp.processEvents()
        layout = page.layout()
        assert layout is not None
        margins = layout.contentsMargins()
        assert margins.left() == margins.right() == 0
        assert page.workspace.width() == page.width() - margins.left() - margins.right()
        assert page.cycles.width() == page.channels.width() == page.workspace.width()
        assert window.grab().save(
            str(tmp_path / f"history-{language}-{theme}-{size[0]}.png")
        )
        assert not page.channels.horizontalScrollBar().isVisible(), (
            window.width(),
            page.workspace.width(),
            page.channels.viewport().width(),
            page.channels.horizontalHeader().length(),
            [page.channels.columnWidth(i) for i in range(7)],
        )
        for row, camera in enumerate(history.query().result(WAIT).records[0].cameras):
            assert content(page.channels, row, 0) == camera.camera
            for slot in range(6):
                stored = channel_at(page, row, slot + 1)
                assert stored.camera == camera.camera and stored.slot == slot
                cell = page.channels.item(row, slot + 1)
                assert cell is not None
                rect = page.channels.visualItemRect(cell)
                assert page.channels.viewport().rect().contains(rect)
        assert (
            page.cycles.columnWidth(0)
            >= page.cycles.fontMetrics().horizontalAdvance(content(page.cycles, 0, 0))
            + 12
        )
        assert page.cycles.columnWidth(1) < page.cycles.columnWidth(0)
        for col in (2, 3):
            value = page.cycles.item(0, col)
            assert value is not None
            assert value.toolTip() == value.text() + (
                "\nID: recipe-a" if col == 3 else ""
            )
            if size[0] >= 1920:
                assert page.cycles.fontMetrics().horizontalAdvance(
                    value.text()
                ) + 12 <= page.cycles.columnWidth(col)
        assert (
            page.cycles.columnWidth(4)
            >= page.cycles.fontMetrics().horizontalAdvance(content(page.cycles, 0, 4))
            + 12
        )
        assert page.rect().contains(
            page.refresh_button.mapTo(page, page.refresh_button.rect().topLeft())
        )
        assert page.rect().contains(
            page.older.mapTo(page, page.older.rect().bottomRight())
        )
        page.channels.setCurrentCell(0, 4)
        window.set_language("en" if language == "zh" else "zh")
        window.set_theme("light" if theme == "dark" else "dark")
        assert page.refresh_button.text() == text("history_refresh", window.language)
        assert (
            page.channels.rowCount() == 6 and startup.application.snapshot() == before
        )
        assert "execution_error" in page.channel_detail.toPlainText()
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
        assert content(page.cycles, 2, 4) == "OK 20 · NG 2 · EMPTY 14"
        assert content(page.cycles, 1, 4) == "OK 6 · NG 2 · EMPTY 28"
        widths = [page.cycles.columnWidth(i) for i in range(5)]
        viewport = page.cycles.viewport().width()
        print(
            f"History {language}/{theme}, page {page.width()}, viewport {viewport}, columns {widths}"
        )
        assert sum(widths) == viewport
        assert not page.cycles.horizontalScrollBar().isVisible()
        assert all(
            page.cycles.horizontalHeader().sectionResizeMode(i)
            == QHeaderView.ResizeMode.Fixed
            for i in range(5)
        )
        metrics = page.cycles.fontMetrics()
        for row in range(3):
            for col in range(5):
                item = page.cycles.item(row, col)
                assert item is not None
                assert metrics.horizontalAdvance(item.text()) + 12 <= widths[col]
                assert item.toolTip()
        assert widths[1] < widths[0] < min(widths[2:4])
        assert max(widths[2:4]) < 1.25 * min(widths[2:4])
        assert widths[4] >= max(widths[2:4])
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
            assert not viewport_mask.contains(table.viewport().rect().bottomRight())
            assert viewport_mask.contains(table.viewport().rect().center())
            frame = table.grab().toImage()
            scale = frame.devicePixelRatio()
            assert frame.pixelColor(int(table.width() // 2 * scale), 0) == QColor(
                theme_colors(page).border
            )
        if viewport >= 1800:
            assert widths[0] > 0.12 * viewport and widths[1] > 0.05 * viewport
            assert widths[4] >= 0.25 * viewport

        # Exercise the real delegate with a painter: count fields have equal
        # pixel widths/right edges even when values change from one to two digits.
        drawn: list[tuple[QRect, Qt.AlignmentFlag, str]] = []
        original = QPainter.drawText

        def draw_text(
            painter: QPainter, rect: QRect, flags: Qt.AlignmentFlag, value: str
        ) -> None:
            drawn.append((QRect(rect), flags, value))
            original(painter, rect, flags, value)

        delegate = page.cycles.itemDelegateForColumn(4)
        assert isinstance(delegate, QStyledItemDelegate)
        model = page.cycles.model()
        assert model is not None
        option = QStyleOptionViewItem()
        option.initFrom(page.cycles)
        option.widget = page.cycles
        option.rect = QRect(0, 0, widths[4], 36)
        pixmap = QPixmap(widths[4], 36)
        right_edges: list[list[int]] = []
        with monkeypatch.context() as patch:
            patch.setattr(QPainter, "drawText", draw_text)
            for row, expected in ((2, ["20", "2", "14"]), (1, ["6", "2", "28"])):
                drawn.clear()
                painter = QPainter(pixmap)
                try:
                    delegate.paint(painter, option, model.index(row, 4))
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
        assert page.grab().save(
            str(tmp_path / f"history-{language}-{theme}-{size[0]}-balanced.png")
        )

        page.resize(1920 if size[0] == 1080 else 1080, size[1])
        page.set_language("en" if language == "zh" else "zh")
        qapp.processEvents()
        resized = [page.cycles.columnWidth(i) for i in range(5)]
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
        # surplus or losing their full tooltip. New data triggers allocation.
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
        for col, value in (
            (2, long_context.batch_label),
            (3, long_context.recipe_name),
        ):
            item = page.cycles.item(0, col)
            assert item is not None and item.text() == value
            assert item.toolTip() == item.text() + (
                "\nID: recipe-a" if col == 3 else ""
            )
            assert metrics.horizontalAdvance(item.text()) > page.cycles.columnWidth(col)
        assert (
            sum(page.cycles.columnWidth(i) for i in range(5))
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
def test_stored_recipe_names_legacy_ids_and_content_sized_channel_details(
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
        assert content(page.cycles, 0, 3) == context.recipe_name
        name_item = page.cycles.item(0, 3)
        legacy_item = page.cycles.item(1, 3)
        assert (
            name_item is not None
            and name_item.toolTip() == f"{context.recipe_name}\nID: recipe-internal-id"
        )
        assert (
            legacy_item is not None and legacy_item.text() == "ID: recipe-internal-id"
        )
        assert text("history_recipe_legacy", language) in legacy_item.toolTip()
        qapp.processEvents()
        normal_height = page.channel_detail.height()
        assert 28 <= normal_height <= 36
        assert page.channel_detail.toPlainText() == "CAM1 · CH01 · OK"
        page.channels.setCurrentCell(0, 2)
        assert page.channel_detail.toPlainText() == "CAM1 · CH02 · EMPTY"
        assert page.channel_detail.height() == normal_height
        for col, expected in (
            (6, "CAM1 · CH06 · NG\ndefect_region"),
            (5, "CAM1 · CH05 · UNKNOWN · ERROR\nexecution_error"),
            (4, "CAM1 · CH04 · NG · ERROR\ndefect_region, execution_error"),
        ):
            page.channels.setCurrentCell(0, col)
            assert page.channel_detail.toPlainText() == expected
            assert (
                normal_height
                < page.channel_detail.height()
                <= normal_height + 3 * page.channel_detail.fontMetrics().lineSpacing()
            )
        qapp.processEvents()
        assert not page.channels.horizontalScrollBar().isVisible()
        assert page.grab().save(
            str(tmp_path / f"history-{language}-{theme}-{size[0]}-names-details.png")
        )
        print(
            f"History {language}, page {page.width()}, camera column {page.channels.columnWidth(0)}, detail normal/abnormal {normal_height}/{page.channel_detail.height()}"
        )
        page.channels.setCurrentCell(-1, -1)
        assert (
            not page.channel_detail.isVisible()
            and not page.channel_detail.toPlainText()
        )
        page.cycles.selectRow(1)
        assert "ID: recipe-internal-id" in cycle_tip(page, 1)
        assert "未记录配方名称" in cycle_tip(
            page, 1
        ) or "name not recorded" in cycle_tip(page, 1)
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
        ("ok", "OK 36 · NG 0 · EMPTY 0"),
        ("empty", "OK 0 · NG 0 · EMPTY 36"),
        ("unknown", "OK 0 · NG 0 · EMPTY 0 · UNKNOWN 36"),
        ("cycle_ng", "OK 36 · NG 0 · EMPTY 0 · 周期 NG"),
        ("cycle_error", "OK 36 · NG 0 · EMPTY 0 · 周期 ERR · 周期 UNKNOWN"),
        ("timed_out", "OK 36 · NG 0 · EMPTY 0 · TIMED_OUT · 周期 ERR · 周期 UNKNOWN"),
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
    if scenario == "cycle_ng":
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
        assert content(page.cycles, 0, 4) == expected
        qapp.processEvents()
        result_item = page.cycles.item(0, 4)
        assert result_item is not None
        assert (
            page.cycles.viewport()
            .rect()
            .contains(page.cycles.visualItemRect(result_item))
        )
        # Exceptional warnings can elide at narrow widths; the full summary
        # remains accessible, independently of the fixed-width count rendering.
        assert result_item.toolTip() == expected
        counts = expected.split(" · ")[
            : 5 if scenario == "mixed" else 4 if scenario == "unknown" else 3
        ]
        assert (
            page.cycles.columnWidth(4)
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
            content(page.cycles, 0, 4),
            cycle_tip(page),
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
            assert (
                page.channel_detail.toPlainText()
                == "CAM1 · CH04 · NG · ERROR\ndefect_region, execution_error"
            )
            assert "NG · ERR" in content(page.channels, 0, 4)
        elif scenario in ("ok", "empty"):
            assert (
                page.channel_detail.toPlainText()
                == f"CAM1 · CH01 · {'OK' if scenario == 'ok' else 'EMPTY'}"
            )
        elif scenario == "cycle_ng":
            item = page.channels.item(0, 0)
            assert item is not None
            assert (
                "周期 NG" in content(page.cycles, 0, 4)
                and "unassigned_defect" in item.toolTip()
            )
        elif scenario in ("cycle_error", "timed_out"):
            assert "周期 ERR" in content(page.cycles, 0, 4)
        if scenario == "cycle_error":
            item = page.channels.item(0, 0)
            assert item is not None and "camera_collection_error" in item.toolTip()
        if scenario in ("cycle_error", "timed_out", "cycle_ng"):
            page.set_language("en")
            assert content(page.cycles, 0, 4) == expected.replace("周期", "Cycle")
            assert "Cycle" in content(page.cycles, 0, 4)
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
                assert (
                    page.channels.viewport()
                    .rect()
                    .contains(page.channels.visualItemRect(cell))
                )
        assert channel_at(page, 0, 1).channel == 6
        assert channel_at(page, 0, 6).channel == 1
        page.channels.setCurrentCell(0, 4)
        assert (
            f"CAM{camera_count}" in page.channel_detail.toPlainText()
            and "CH03" in page.channel_detail.toPlainText()
        )
        assert (
            "NG" in page.channel_detail.toPlainText()
            and "ERROR" in page.channel_detail.toPlainText()
        )
        assert "execution_error" in page.channel_detail.toPlainText()
        assert page.grab().save(str(tmp_path / "history-zh-dark-1080-page.png"))
        page.cycles.clearSelection()
        page.cycles.setCurrentCell(-1, -1)
        page.cycles.clearSelection()
        # A no-result filter must clear every old detail, not reuse the previous cycle.
        page.result.setCurrentIndex(page.result.findData("OK"))
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
        assert page.batch.count() == 4
        labels = [page.batch.itemText(i) for i in range(1, 4)]
        assert len(set(labels)) == 3 and all("LOT-" not in label for label in labels)
        first_index = page.batch.findData(context.batch_segment)
        assert datetime.fromtimestamp(stamp).strftime(
            "%m-%d %H:%M:%S"
        ) in page.batch.itemText(first_index)
        assert "abcdef1" in page.batch.itemText(first_index)
        tooltip = page.batch.itemData(first_index, Qt.ItemDataRole.ToolTipRole)
        assert context.batch_label is not None
        assert (
            isinstance(tooltip, str)
            and context.batch_segment in tooltip
            and context.batch_label in tooltip
        )
        assert text("history_segment_observed", "zh") in tooltip
        page.cycles.selectRow(1)
        page.channels.setCurrentCell(0, 4)
        selected = channel_at(page, 0, 4)
        assert selected.business == "NG" and selected.technical == "ERROR"
        qapp.processEvents()  # Settle the normal-to-abnormal detail height change.
        widgets = (
            page.date,
            page.all_dates,
            page.batch,
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
        columns = tuple(page.cycles.columnWidth(i) for i in range(5))
        matrix_columns = tuple(page.channels.columnWidth(i) for i in range(7))
        assert page.batch.width() == page.result.width()
        assert (
            len(
                {
                    button.size().toTuple()
                    for button in (page.newer, page.older, page.refresh_button)
                }
            )
            == 1
        )
        assert page.refresh_button.icon().isNull()
        filters = (
            page.date.date(),
            page.batch.currentData(),
            page.result.currentData(),
        )
        assert not page.status.isVisible() and not page.status.text()
        assert not hasattr(page, "identity")
        assert page.channels.y() == page.cycles.y() + page.cycles.height() + Style.small
        assert geometry[6].bottom() < geometry[7].top()
        assert geometry[4].right() < geometry[5].left() < geometry[6].left()
        assert geometry[2].right() < geometry[3].left()
        assert page.grab().save(str(tmp_path / f"layout-{size[0]}.png"))
        assert all(page.rect().contains(rect) for rect in geometry), (
            page.rect(),
            geometry,
        )
        assert page.older.isEnabled() and not page.newer.isEnabled()
        assert page.grab().save(str(tmp_path / f"history-zh-dark-{size[0]}-final.png"))
        page.set_language("en")
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
        assert columns == tuple(page.cycles.columnWidth(i) for i in range(5))
        assert matrix_columns == tuple(page.channels.columnWidth(i) for i in range(7))
        assert filters == (
            page.date.date(),
            page.batch.currentData(),
            page.result.currentData(),
        )
        assert channel_at(page, 0, 4) == selected
        assert "execution_error" in page.channel_detail.toPlainText()
        assert page.grab().save(str(tmp_path / f"history-en-dark-{size[0]}-final.png"))
        toolbar_geometry = tuple(
            QRect(widget.mapTo(page, QPoint()), widget.size()) for widget in widgets[:7]
        )
        for language in LANGUAGES:
            page.set_language(language)
            for button, key in (
                (page.newer, "history_newer"),
                (page.older, "history_older"),
                (page.refresh_button, "history_refresh"),
            ):
                assert_full_caption(button, text(key, language))
            qapp.processEvents()
            date_text = page.date.findChild(QLineEdit)
            assert date_text is not None
            assert (
                date_text.fontMetrics().horizontalAdvance(date_text.text())
                <= date_text.contentsRect().width() - 4
            )

            assert toolbar_geometry == tuple(
                QRect(widget.mapTo(page, QPoint()), widget.size())
                for widget in widgets[:7]
            ), language
            if size[0] == 1920:
                assert page.all_dates.text() == text("history_all_dates", language)
            assert (
                page.all_dates.accessibleName()
                == page.all_dates.toolTip()
                == text("history_all_dates", language)
            )
            assert (
                page.refresh_button.accessibleName()
                == page.refresh_button.toolTip()
                == text("history_refresh", language)
            )
            assert filters == (
                page.date.date(),
                page.batch.currentData(),
                page.result.currentData(),
            )
            assert channel_at(page, 0, 4) == selected and not page.busy
        page.set_language("en")
        page.batch.showPopup()
        until(page.batch.view().isVisible)
        assert (
            page.batch.view()
            .window()
            .grab()
            .save(str(tmp_path / f"segments-{size[0]}.png"))
        )
        page.batch.hidePopup()
        second_index = page.batch.findData("abcdef2-second")
        page.batch.setCurrentIndex(second_index)
        until(lambda: not page.busy)
        assert page.cycles.rowCount() == 1 and content(page.cycles, 0, 1) == "52"
        page.set_language("zh")
        assert page.batch.currentData() == "abcdef2-second"
        page.batch.setCurrentIndex(0)
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

        if size[0] == 1920:
            cases: tuple[tuple[Theme, Language], ...] = (
                ("dark", "zh"),
                ("light", "zh"),
                ("light", "en"),
            )
            for theme, language in cases:
                apply_theme(page, theme)
                page.set_language(language)
                original_date = page.date.date()
                calendar = page.date.calendarWidget()
                assert calendar.locale().language() == (
                    QLocale.Language.Chinese
                    if language == "zh"
                    else QLocale.Language.English
                )
                assert calendar.firstDayOfWeek() == calendar.locale().firstDayOfWeek()
                QTest.mouseClick(
                    page.date,
                    Qt.MouseButton.LeftButton,
                    pos=QPoint(page.date.width() - 8, page.date.height() // 2),
                )
                until(calendar.isVisible)
                qapp.processEvents()
                view = calendar.findChild(QTableView, "qt_calendar_calendarview")
                month = calendar.findChild(QToolButton, "qt_calendar_monthbutton")
                assert view is not None and month is not None and month.text()

                def luminance(color: QColor) -> float:
                    values = [
                        v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                        for v in (color.redF(), color.greenF(), color.blueF())
                    ]
                    return sum(
                        v * weight
                        for v, weight in zip(
                            values, (0.2126, 0.7152, 0.0722), strict=True
                        )
                    )

                palette = view.palette()
                for foreground, background in (
                    (QPalette.ColorRole.Text, QPalette.ColorRole.Base),
                    (QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Highlight),
                ):
                    a, b = sorted(
                        (
                            luminance(palette.color(foreground)),
                            luminance(palette.color(background)),
                        )
                    )
                    assert (b + 0.05) / (a + 0.05) >= 4.5
                assert palette.color(
                    QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
                ) != palette.color(QPalette.ColorRole.Base)
                assert calendar.grab().save(
                    str(tmp_path / f"calendar-{language}-{theme}.png")
                )
                # Click a mid-month date via the actual calendar view, then
                # use keyboard navigation/Enter on the reopened popup.
                model = view.model()
                assert model is not None
                day = next(
                    model.index(row, col)
                    for row in range(1, model.rowCount())
                    for col in range(1, model.columnCount())
                    if str(model.index(row, col).data()) == "15"
                )
                QTest.mouseClick(
                    view.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=view.visualRect(day).center(),
                )
                until(
                    lambda current=calendar: not current.isVisible() and not page.busy
                )
                assert page.date.date() == QDate(
                    original_date.year(), original_date.month(), 15
                )
                QTest.mouseClick(
                    page.date,
                    Qt.MouseButton.LeftButton,
                    pos=QPoint(page.date.width() - 8, page.date.height() // 2),
                )
                until(calendar.isVisible)
                view.setFocus()
                QTest.keyClick(view, Qt.Key.Key_Right)
                assert calendar.selectedDate().day() == 16
                QTest.keyClick(view, Qt.Key.Key_Return)
                until(
                    lambda current=calendar: not current.isVisible() and not page.busy
                )
                assert page.date.date().day() == 16
                page.date.setDate(original_date)
                until(lambda: not page.busy)
    finally:
        page.date.calendarWidget().hide()
        page.batch.hidePopup()
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
        page.result.setCurrentIndex(page.result.findData("NG"))
        page.result.setCurrentIndex(page.result.findData("EMPTY"))
        release.set()
        until(lambda: not page.busy)
        assert queries[-1].result == "EMPTY"
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
