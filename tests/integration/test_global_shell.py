"""Shared navigation/status contracts, with existing controlled sessions and fake engines."""

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QPalette, QScreen
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOption,
    QStyleOptionComboBox,
    QStylePainter,
)
from tests.application_support import FakeSession, production
from tests.qt_support import screen as screen
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.application.preferences import LANGUAGES
from vimo_demo.communication.reporting import (
    ReportIdentity,
    ReportingSnapshot,
    ReportObservation,
    ReportOutcome,
)
from vimo_demo.inspection import CycleId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.ui.production_style import Language, Style, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.shell import LANGUAGE_LABELS
from vimo_demo.ui.translations import LANGUAGE_NAMES


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_navigation_language_and_themes_preserve_presentation_contract(
    screen: tuple[ProductionWindow, FakeSession],
    size: tuple[int, int],
) -> None:
    window, _ = screen
    window.resize(*size)
    window.set_language("zh")
    QApplication.processEvents()
    baseline = window.bridge.application.snapshot()
    timers = window.findChildren(QTimer)
    width = window.language_selector.width()
    navigation = tuple(button.geometry() for button in window.navigation)
    row = window.navbar.layout()
    assert row is not None
    assert window.navigation[0].x() == (
        window.navbar.contentsRect().left() + row.contentsMargins().left()
    )
    anchored = (
        window.buttons["operation"],
        window.event_frame.state_icon,
        window.event_frame.state,
        window.event_frame.plc_icon,
        window.event_frame.plc,
        window.event_frame.web_icon,
        window.event_frame.web,
        window.event_label,
    )
    geometry = tuple(
        QRect(widget.mapTo(window, QPoint()), widget.size()) for widget in anchored
    )
    for theme in ("light", "dark"):
        window.theme_button.click()
        assert window.theme == theme, (size, theme)
        for _ in range(2):
            current = window.language
            case = f"size={size}, theme={theme}, language={current}"
            assert len({button.width() for button in window.navigation}) == 1, case
            for key, button in zip(
                ("production", "parameters", "nav_diagnostics", "history", "settings"),
                window.navigation,
                strict=True,
            ):
                full = text(key, current)
                visible = button.text()
                assert visible == full or (
                    visible.endswith("…") and full.startswith(visible.removesuffix("…"))
                ), case
            for page, button in enumerate(window.navigation):
                page_case = f"{case}, page={page}"
                button.click()
                QApplication.processEvents()
                assert window.pages.currentIndex() == page, page_case
                assert tuple(b.isChecked() for b in window.navigation) == tuple(
                    i == page for i in range(5)
                ), page_case
                assert (
                    button.toolTip()
                    == button.accessibleName()
                    == text(
                        (
                            "production",
                            "parameters",
                            "diagnostics",
                            "history",
                            "settings",
                        )[page],
                        current,
                    )
                ), page_case
                assert not button.icon().isNull(), page_case
                assert button.height() >= Style.touch, page_case
                assert window.navbar.rect().contains(button.geometry()), page_case
                assert window.event_frame.isVisible(), page_case
                assert window.event_frame.state.text() == window.state_label.text(), (
                    page_case
                )
                assert "PLC" in window.event_frame.plc.text(), page_case
                assert "Web" in window.event_frame.web.text(), page_case
                assert not window.event_label.text(), page_case
                assert not window.event_label.wordWrap(), page_case
                assert window.bridge.application.snapshot() == baseline, page_case
                if page == 2:
                    assert window.pages.currentWidget() is window.communication, (
                        page_case
                    )
                    assert window.communication.write_button.text() == text(
                        "comm_write", current
                    ), page_case
            assert window.pages.currentWidget() is window.parameters.settings, case
            palette = window.parameters.metadata["display_name"].palette()
            assert palette.color(
                QPalette.ColorGroup.Active, QPalette.ColorRole.Text
            ) != palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base), (
                case
            )
            window.language_selector.activated.emit(
                1 - window.language_selector.currentIndex()
            )
            QApplication.processEvents()
            assert window.language != current, case
            assert window.navigation[4].isChecked(), case
            assert window.language_selector.width() == width, case
            assert (
                tuple(button.geometry() for button in window.navigation) == navigation
            ), case
            assert (
                tuple(
                    QRect(widget.mapTo(window, QPoint()), widget.size())
                    for widget in anchored
                )
                == geometry
            ), case
            assert window.columns == (2 if size[0] < size[1] else 3), case
    assert set(window.findChildren(QTimer)) == set(timers), size


def test_web_status_uses_owner_observation_without_a_new_timer(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, _ = screen
    before = window.bridge.application.snapshot()
    observed = [True]
    window.observe_web(lambda: (observed[0], "http://127.0.0.1:8765/"), interval_ms=500)
    assert window.event_frame.web.text() == "Web · 500 ms"
    observed[0] = False
    window.bridge.timer.timeout.emit()
    assert "500 ms" not in window.event_frame.web.text()
    assert window.event_frame.web.toolTip() == (
        f"{window.event_frame.web.accessibleName()}\nhttp://127.0.0.1:8765/"
    )
    window.resize(1080, 720)
    QApplication.processEvents()
    assert (
        window.event_frame.web.fontMetrics().horizontalAdvance(
            window.event_frame.web.text()
        )
        <= window.event_frame.web.width()
    )
    assert window.bridge.application.snapshot() == before


@pytest.mark.parametrize("size", [(1080, 1857), (1920, 1017)])
def test_all_languages_fit_shell_and_preserve_active_inspection(
    screen: tuple[ProductionWindow, FakeSession],
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    window.resize(*size)
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    complete_cycle(window, session, 1, TechnicalStatus.SUCCESS, ProductJudgment.NG)
    before = window.bridge.application.snapshot()
    geometry = tuple(button.geometry() for button in window.navigation)
    icon_strips = tuple(
        button.grab().toImage().copy(0, 0, Style.icon + Style.small, button.height())
        for button in window.navigation
    )
    player_geometry = tuple(
        QRect(button.mapTo(window, QPoint()), button.size())
        for button in (window.buttons[key] for key in ("operation", "pause", "stop"))
    )
    painted: list[str] = []
    original = QStylePainter.drawControl

    def draw_control(
        painter: QStylePainter, element: QStyle.ControlElement, option: QStyleOption
    ) -> None:
        if isinstance(option, QStyleOptionComboBox):
            painted.append(option.currentText)
        original(painter, element, option)

    monkeypatch.setattr(QStylePainter, "drawControl", draw_control)
    for language in LANGUAGES:
        window.language_selector.activated.emit(
            window.language_selector.findData(language)
        )
        QApplication.processEvents()
        window.language_selector.repaint()
        assert painted[-1] == LANGUAGE_LABELS[language]
        assert window.language_selector.currentText() == LANGUAGE_NAMES[language]
        assert LANGUAGE_NAMES[language] in window.language_selector.toolTip()
        assert window.width() == size[0], (language, window.minimumSizeHint())
        assert len({button.width() for button in window.navigation}) == 1, language
        assert tuple(button.geometry() for button in window.navigation) == geometry, (
            language
        )
        assert (
            tuple(
                button.grab()
                .toImage()
                .copy(0, 0, Style.icon + Style.small, button.height())
                for button in window.navigation
            )
            == icon_strips
        ), language
        for key, button in zip(
            ("production", "parameters", "diagnostics", "history", "settings"),
            window.navigation,
            strict=True,
        ):
            assert window.navbar.rect().contains(button.geometry()), language
            assert (
                button.fontMetrics().horizontalAdvance(button.text()) + 40
                <= button.width()
            ), language
            assert button.accessibleName() == button.toolTip() == text(key, language)
            assert button.property("iconAlignment") == "leading"
        assert window.navbar.rect().contains(window.language_selector.geometry()), (
            language
        )
        assert all(
            a.geometry().right() < b.geometry().left()
            for a, b in zip(window.navigation[:-1], window.navigation[1:], strict=True)
        )
        assert window.navigation[-1].geometry().right() < window.theme_button.x()
        assert window.theme_button.geometry().right() < window.language_selector.x()
        assert (
            window.language_selector.fontMetrics().horizontalAdvance(
                LANGUAGE_LABELS[language]
            )
            <= window.language_selector.width() - 60
        ), language
        option = QStyleOptionComboBox()
        window.language_selector.initStyleOption(option)
        field = window.language_selector.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            window.language_selector,
        )
        assert (
            window.language_selector.fontMetrics().horizontalAdvance(
                LANGUAGE_LABELS[language]
            )
            + option.iconSize.width()
            + Style.small
            <= field.width()
        ), language
        for key in ("operation", "pause", "stop"):
            button = window.buttons[key]
            assert window.rect().contains(
                button.mapTo(window, button.rect().bottomRight())
            ), language
        assert (
            tuple(
                QRect(button.mapTo(window, QPoint()), button.size())
                for button in (
                    window.buttons[key] for key in ("operation", "pause", "stop")
                )
            )
            == player_geometry
        ), language
        window.navigate(2)
        page = window.communication
        QApplication.processEvents()
        assert page.body.width() <= page.scroll_area.viewport().width(), language
        assert page.write_button.text() == text("comm_write", language)
        window.navigate(3)
        QApplication.processEvents()
        assert window.history.workspace.width() <= window.history.width(), language
        for control in (
            window.history.batch,
            window.history.result,
            window.history.refresh_button,
        ):
            assert window.history.rect().contains(
                control.mapTo(window.history, control.rect().bottomRight())
            ), language
        window.navigate(0)
        assert window.bridge.application.snapshot() == before, language

    # A previous wide layout must not raise the window's minimum width and
    # prevent the same navigation from returning to the supported portrait size.
    for width, height in ((1920, 1017), (1080, 1857), (1920, 1017)):
        window.resize(width, height)
        QApplication.processEvents()
        assert window.width() == width
        assert len({button.width() for button in window.navigation}) == 1
        assert window.navigation[-1].geometry().right() < window.theme_button.x()
        assert window.navbar.rect().contains(window.language_selector.geometry())
        assert window.bridge.application.snapshot() == before


def test_language_popup_retains_all_native_names_within_available_screen(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, _ = screen
    selector = window.language_selector
    selected = selector.currentData()
    application = window.bridge.application.snapshot()
    assert selector.count() == selector.maxVisibleItems() == 29
    assert [(selector.itemText(i), selector.itemData(i)) for i in range(29)] == [
        (LANGUAGE_NAMES[language], language) for language in LANGUAGES
    ]
    selector.showPopup()
    try:
        QApplication.processEvents()
        view = selector.view()
        popup = view.window()
        assert popup.isVisible()
        assert popup.screen().availableGeometry().contains(popup.frameGeometry())
        for row in range(29):
            index = selector.model().index(row, 0)
            view.scrollTo(index)
            QApplication.processEvents()
            rect = view.visualRect(index)
            assert 0 <= rect.top() <= rect.bottom() < view.viewport().height()
            # Native menu delegates can reserve extra blank trailing space in
            # their row rect; require the actual name/icon to fit the viewport.
            assert (
                view.fontMetrics().horizontalAdvance(selector.itemText(row))
                + selector.iconSize().width()
                + 2 * Style.space
                <= view.viewport().width()
            )
        rows_height = sum(view.sizeHintForRow(i) for i in range(selector.count()))
        assert (view.verticalScrollBar().maximum() == 0) == (
            rows_height <= view.viewport().height()
        )
        assert selector.currentData() == selected
    finally:
        selector.hidePopup()
    selector.showPopup()
    QTest.keyClick(selector.view(), Qt.Key.Key_End)
    QTest.keyClick(selector.view(), Qt.Key.Key_Return)
    QApplication.processEvents()
    assert selector.currentData() == "pt-PT" and window.language == "pt-PT"
    assert window.bridge.application.snapshot() == application


@pytest.mark.parametrize("height", [500, 1040])
def test_language_popup_uses_available_work_area_before_scrolling(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    height: int,
) -> None:
    window, _ = screen
    selector = window.language_selector
    bounds = QRect(0, 0, selector.screen().availableGeometry().width(), height)

    def work_area(_screen: QScreen) -> QRect:
        return bounds

    with monkeypatch.context() as patch:
        patch.setattr(QScreen, "availableGeometry", work_area)
        selector.showPopup()
        QApplication.processEvents()
        try:
            view = selector.view()
            assert bounds.contains(view.window().frameGeometry())
            rows_height = sum(view.sizeHintForRow(i) for i in range(selector.count()))
            if height == 1040:
                assert view.viewport().height() >= rows_height
                assert view.verticalScrollBar().maximum() == 0
            else:
                assert view.verticalScrollBar().maximum() > 0
            for row in range(selector.count()):
                index = selector.model().index(row, 0)
                view.scrollTo(index)
                QApplication.processEvents()
                assert view.viewport().rect().contains(view.visualRect(index)), row
        finally:
            selector.hidePopup()


@pytest.mark.parametrize("outcome", [ReportOutcome.ACKNOWLEDGED, ReportOutcome.FAILED])
def test_reporting_observation_is_not_a_connectivity_or_application_state_claim(
    screen: tuple[ProductionWindow, FakeSession], outcome: ReportOutcome
) -> None:
    window, _ = screen
    before = window.bridge.application.snapshot()
    observation = ReportObservation(ReportIdentity(1, CycleId(1)), outcome, sequence=42)
    window.bridge.reporting_snapshot = ReportingSnapshot(
        generation=1,
        accepting=False,
        thread_alive=False,
        pending=0,
        peak_pending=1,
        active=None,
        queued_count=1,
        attempted_count=1,
        acknowledged_count=int(outcome is ReportOutcome.ACKNOWLEDGED),
        failed_count=int(outcome is ReportOutcome.FAILED),
        skipped_count=0,
        rejected_count=0,
        next_sequence=43,
        last_queued=observation.identity,
        last_attempted=observation,
        last_acknowledged=observation
        if outcome is ReportOutcome.ACKNOWLEDGED
        else None,
        last_outcome=observation,
        last_failed=observation if outcome is ReportOutcome.FAILED else None,
        last_skipped=None,
        last_rejected=None,
        failure_issue=None,
        cleanup_issue=None,
    )
    for language in ("zh", "en"):
        window.set_language(language)
        message = window.event_frame.plc.text()
        assert ("42" in message) == (outcome is ReportOutcome.ACKNOWLEDGED)
        assert "connected" not in message.lower() and "已连接" not in message
        assert window.event_frame.plc.toolTip() == (
            f"{window.event_frame.plc.accessibleName()}\n{text('plc_ack_hint', language)}"
        )
        assert window.snapshot.state is ApplicationState.READY
        assert window.bridge.application.snapshot() == before


def complete_cycle(
    window: ProductionWindow,
    session: FakeSession,
    number: int,
    status: TechnicalStatus,
    judgment: ProductJudgment,
) -> None:
    receipt = window.bridge.application.begin_cycle(CycleId(number))
    session.cycles[-1].accepted.set_result(None)
    session.cycles[-1].result.set_result(production(number, status, judgment))
    until(
        lambda: (
            receipt.result.done()
            and window.snapshot.counters.cycles_completed == number
        )
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_latest_technical_evidence_is_global_and_historical_counts_do_not_warn(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
) -> None:
    window, session = screen
    window.set_language(language)
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    complete_cycle(window, session, 1, TechnicalStatus.ERROR, ProductJudgment.NG)
    error = window.event_label.toolTip()
    assert "CAM1" in error and text("inspection_issue", language) in error
    before = window.bridge.application.snapshot()
    for page in range(5):
        window.navigate(page)
        assert window.event_label.toolTip() == error
        assert window.event_icon.isVisible()
        assert Style.warning in window.event_label.styleSheet()
        assert window.bridge.application.snapshot() == before
    complete_cycle(window, session, 2, TechnicalStatus.SUCCESS, ProductJudgment.NG)
    healthy = window.bridge.application.snapshot()
    assert healthy.counters.technical_error_cycles == 1
    assert healthy.counters.channel_ng > 0
    assert healthy.latest_production_result is not None
    for page in range(5):
        window.navigate(page)
        assert window.event_frame.isVisible()
        assert not window.event_label.text() and not window.event_label.toolTip()
        assert not window.event_icon.isVisible()
        assert Style.secondary in window.event_label.styleSheet()
        assert window.bridge.application.snapshot() == healthy


@pytest.mark.parametrize("language", ["zh", "en"])
def test_fault_and_authoritative_reset_across_pages(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
) -> None:
    window, session = screen
    window.set_language(language)
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    session.failure = RuntimeError("private SDK details C:/internal/model.dll")
    session.finished.set_result(None)
    until(
        lambda: (
            window.snapshot.state is ApplicationState.FAULT
            and not window.snapshot.runtime_owned
        )
    )
    window.bridge.command_failed.emit("stop", RuntimeError("old command details"))
    before = window.bridge.application.snapshot()
    assert before.last_issue is not None and before.last_issue.stage == "shutdown"
    for page in range(5):
        window.navigate(page)
        assert text("shutdown_error", language) in window.event_label.text()
        assert before.last_issue.exception_type in window.event_label.toolTip()
        assert "private" not in window.event_label.toolTip()
        assert window.event_icon.isVisible()
        assert Style.fault in window.event_label.styleSheet()
        assert window.bridge.application.snapshot() == before
    # Reset through the application, not a synthetic successful command signal.
    window.bridge.application.reset_fault()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    for page in range(5):
        window.navigate(page)
        assert not window.event_label.text() and not window.event_icon.isVisible()
    assert window.bridge.application.snapshot().counters == before.counters


@pytest.mark.parametrize("command,pages", [("recipe", (1, 4)), ("batch", (0,))])
def test_local_command_feedback_stays_with_owning_pages(
    screen: tuple[ProductionWindow, FakeSession],
    command: str,
    pages: tuple[int, ...],
) -> None:
    window, _ = screen
    before = window.bridge.application.snapshot()
    window.bridge.command_failed.emit(
        command, ValueError("private configuration detail")
    )
    for page in range(5):
        window.navigate(page)
        assert bool(window.event_label.text()) == (page in pages)
        assert "private" not in window.event_label.toolTip()
        assert window.bridge.application.snapshot() == before
    window.bridge.command_finished.emit(command)
    for page in pages:
        window.navigate(page)
        assert not window.event_label.text()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("prefix", ["CAM5:", "CAM5 / frame2.png:"])
def test_source_failure_is_short_global_and_cleared_by_successful_start(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
    prefix: str,
) -> None:
    window, session = screen
    window.set_language(language)
    window.bridge.command_failed.emit(
        "folder_source",
        FolderSourceError(f"{prefix} Decode C:/private/sample.png: SDK details"),
    )
    for page in range(5):
        window.navigate(page)
        assert "CAM5" in window.event_label.text()
        assert text("source_error", language) in window.event_label.text()
        assert "private" not in window.event_label.toolTip()
        assert "SDK" not in window.event_label.toolTip()
    session.ready.set_result(None)
    startup = window.bridge.application.start()
    until(lambda: startup.done() and window.snapshot.state is ApplicationState.RUNNING)
    assert not window.event_label.text()
    # A current technical command error still reaches every page during RUNNING.
    window.bridge.command_failed.emit("stop", TimeoutError("private native timeout"))
    message = window.event_label.toolTip()
    for page in range(5):
        window.navigate(page)
        assert window.event_label.toolTip() == message
        assert "TimeoutError" in window.event_label.text()
    window.bridge.command_finished.emit("stop")
    assert not window.event_label.text()
