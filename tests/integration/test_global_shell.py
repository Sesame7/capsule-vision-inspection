"""Shared navigation/status contracts, with existing controlled sessions and fake engines."""

from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QRect, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QPalette, QScreen
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyleOption,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)
from tests.support.application import FakeSession, production
from tests.support.qt import screen as screen
from tests.support.qt import until

from capsule_vision.application import ApplicationState
from capsule_vision.application.folder_replay import FolderSourceError
from capsule_vision.communication.reporting import (
    ReportIdentity,
    ReportingSnapshot,
    ReportObservation,
    ReportOutcome,
)
from capsule_vision.configuration.communication_preferences import WebPreferences
from capsule_vision.inspection import CycleId
from capsule_vision.inspection.production import ProductJudgment, TechnicalStatus
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import (
    LANGUAGE_DISPLAY_ORDER,
    LANGUAGE_LABELS,
    LANGUAGE_NAMES,
    LANGUAGES,
    Language,
)
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.style import Style, theme_colors
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime, WebRuntimeSnapshot, WebState


def test_compact_status_roles_reserve_remaining_width_for_global_feedback(
    screen: tuple[MainWindow, FakeSession],
) -> None:
    window, _ = screen
    before = window.bridge.application.snapshot()
    bar = window.event_frame
    labels = (bar.state, bar.plc, bar.web)
    parents: list[QWidget] = []
    for label in (*labels, window.event_label):
        parent = label.parentWidget()
        assert parent is not None
        parents.append(parent)
    status_groups, event_group = parents[:3], parents[3]
    previous_window_width = previous_event_width = 0
    fixed_geometry: dict[Language, tuple[int, ...]] = {}
    # Presentation-only captions exercise every role without opening a source,
    # dispatching a report, or creating a Web owner.
    with QSignalBlocker(window.bridge.timer):
        for size in ((1080, 1920), (1920, 1080), (1080, 1920)):
            window.resize(*size)
            QApplication.processEvents()
            for theme in ("dark", "light"):
                window.set_theme(theme)
                for language in LANGUAGES:
                    window.set_language(language)
                    for source, plc, web in (
                        ("source_folder_status", "service_off", "service_off"),
                        ("UNCONFIGURED", "service_next_start", "web_starting"),
                        ("state_RUNNING", "web_running", "web_running"),
                        ("state_FAULT", "technical_error", "technical_error"),
                    ):
                        failed = plc == "technical_error"
                        if failed:
                            window.bridge.command_failed.emit("stop", TimeoutError())
                        else:
                            window.bridge.command_finished.emit("stop")
                        captions = (
                            text(source, language),
                            f"PLC · {text(plc, language)}",
                            f"Web · {text(web, language)}",
                        )
                        for label, caption in zip(labels, captions, strict=True):
                            label.setText(caption)
                            label.setToolTip("")
                        QApplication.processEvents()
                        geometry = tuple(
                            value
                            for group in status_groups
                            for value in (group.x(), group.width())
                        )
                        if language not in fixed_geometry:
                            fixed_geometry[language] = geometry
                        assert geometry == fixed_geometry[language], (
                            size,
                            theme,
                            language,
                            plc,
                        )
                        # Each language's concise states share stable budgets,
                        # while feedback retains a useful narrow-window
                        # region and receives every additional resize pixel.
                        assert event_group.width() >= bar.width() // 4
                        assert bar.height() == 44
                        assert window.width() == size[0]
                        for label, caption in zip(labels, captions, strict=True):
                            assert label.accessibleName() == caption
                            assert not label.toolTip()
                            assert (
                                label.fontMetrics().horizontalAdvance(label.text())
                                <= label.width()
                            )
                            assert label.text() == caption, (language, caption)
                        for a, b in zip(parents[:-1], parents[1:], strict=True):
                            assert a.geometry().right() < b.geometry().left()
                        if failed:
                            assert "TimeoutError" in window.event_label.toolTip()
                            assert (
                                window.event_label.accessibleName()
                                == window.event_label.toolTip()
                            )
                            assert window.event_icon.isVisible()
                            assert (
                                theme_colors(window).warning
                                in window.event_label.styleSheet()
                            )
                        assert window.bridge.application.snapshot() == before
            # Keep the error icon and message unchanged when comparing resize deltas.
            if previous_window_width:
                assert (
                    window.event_label.width() - previous_event_width
                    == window.width() - previous_window_width
                )
            previous_window_width = window.width()
            previous_event_width = window.event_label.width()
    # Unexpectedly long source/state values remain bounded and accessible;
    # reserving their entire width must not displace actionable feedback.
    long_caption = "Long source observation " * 20
    bar.state.setText(long_caption)
    QApplication.processEvents()
    assert bar.state.text().endswith("…")
    assert bar.state.accessibleName() == long_caption
    assert not bar.state.toolTip()
    assert event_group.width() >= bar.width() // 4


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
def test_web_runtime_status_is_compact_and_keeps_details_and_global_errors(
    screen: tuple[MainWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
    size: tuple[int, int],
) -> None:
    window, _ = screen
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    before = window.bridge.application.snapshot()
    timers = window.findChildren(QTimer)
    panel = window.communication.web_panel
    assert panel.runtime is None and not panel.available
    assert window.event_frame.web.accessibleName().endswith(
        text("service_off", language)
    )
    assert panel.actual.text() == "OFF"
    assert not panel.power.isEnabled()
    assert panel.apply_changes.isHidden() and not panel.apply_changes.isEnabled()
    assert panel.open_browser.isHidden() and not panel.open_browser.isEnabled()

    def forbidden(_settings: WebPreferences) -> ProductionSnapshotService:
        raise AssertionError("Presentation must not start a Web service")

    runtime = WebRuntime(forbidden)
    observed = [WebRuntimeSnapshot()]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "snapshot", lambda: observed[0])
            window.bind_web_runtime(runtime)
            window.bridge.command_failed.emit("stop", TimeoutError("native shutdown"))
            feedback = window.event_label.toolTip()
            assert "TimeoutError" in feedback
            for state, key in (
                (WebState.STARTING, "web_starting"),
                (WebState.RUNNING, "web_running"),
                (WebState.STOPPING, "STOPPING"),
                (WebState.STOPPED, "service_off"),
                (WebState.FAILED, "technical_error"),
            ):
                window.event_frame.web.setToolTip("stale service diagnostics")
                window.event_frame.web_icon.setToolTip("stale URL")
                observed[0] = WebRuntimeSnapshot(
                    state=state,
                    configuration=WebPreferences(port=8876, interval_ms=1000),
                    url="http://127.0.0.1:8876/",
                    error="bind failed" if state is WebState.FAILED else "",
                )
                window.bridge.timer.timeout.emit()
                label = window.event_frame.web
                assert label.accessibleName() == f"Web · {text(key, language)}"
                assert "ms" not in label.text() and "http" not in label.text()
                assert not label.toolTip() and not window.event_frame.web_icon.toolTip()
                assert not window.communication.web_panel.actual.toolTip()
                assert runtime.snapshot() == observed[0]
                assert (
                    label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
                )
                assert window.event_frame.height() == 44
                assert window.event_label.toolTip() == feedback
                assert window.event_icon.isVisible()
                assert window.bridge.application.snapshot() == before
            # A failed restart may restore a running owner and retain its error.
            observed[0] = replace(observed[0], state=WebState.RUNNING, restored=True)
            window.bridge.timer.timeout.emit()
            assert (
                window.event_frame.web.accessibleName()
                == f"Web · {text('web_running', language)}"
            )
            assert not window.event_frame.web.toolTip()
            assert (
                runtime.snapshot().restored
                and runtime.snapshot().error == "bind failed"
            )
            assert not window.communication.web_panel.actual.toolTip()
            # Even without an error string, FAILED must not look disabled.
            observed[0] = WebRuntimeSnapshot(state=WebState.FAILED)
            window.bridge.timer.timeout.emit()
            assert (
                window.event_frame.web.accessibleName()
                == f"Web · {text('technical_error', language)}"
            )
            assert set(window.findChildren(QTimer)) == set(timers)
    finally:
        runtime.request_close()
        assert runtime.join(2)


@pytest.mark.parametrize("size", [(1080, 1857), (1920, 1017)])
def test_all_languages_fit_shell_and_preserve_active_inspection(
    screen: tuple[MainWindow, FakeSession],
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    window.resize(*size)
    window.set_language("zh")
    QApplication.processEvents()
    ready = window.bridge.application.snapshot()
    assert ready.state is ApplicationState.READY and window.buttons["batch"].isEnabled()
    timers = set(window.findChildren(QTimer))
    width = window.language_selector.width()
    navigation = tuple(button.geometry() for button in window.navigation)
    row = window.navbar.layout()
    assert row is not None
    assert window.navigation[0].x() == (
        window.navbar.contentsRect().left() + row.contentsMargins().left()
    )
    # Check stopped navigation once; the active traversal owns all-language fit.
    for page, button in enumerate(window.navigation):
        button.click()
        QApplication.processEvents()
        assert window.pages.currentIndex() == page
        assert tuple(b.isChecked() for b in window.navigation) == tuple(
            i == page for i in range(5)
        )
        assert window.event_frame.isVisible()
        assert window.event_frame.state.text() == window.state_label.text()
        assert "PLC" in window.event_frame.plc.text()
        assert "Web" in window.event_frame.web.text()
        assert not window.event_label.text() and not window.event_label.wordWrap()
        assert window.bridge.application.snapshot() == ready
        if page == 2:
            assert window.pages.currentWidget() is window.communication
            assert window.communication.write_button.text() == text("comm_write", "zh")
    for theme in ("light", "dark"):
        window.theme_button.click()
        assert window.theme == theme
        for language in ("zh", "en"):
            window.language_selector.activated.emit(
                window.language_selector.findData(language)
            )
            QApplication.processEvents()
            assert window.language == language and window.navigation[4].isChecked()
            assert window.pages.currentWidget() is window.vision_page
            assert window.language_selector.width() == width
            assert (
                tuple(button.geometry() for button in window.navigation) == navigation
            )
            palette = window.recipe_page.metadata["display_name"].palette()
            assert palette.color(
                QPalette.ColorGroup.Active, QPalette.ColorRole.Text
            ) != palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Base)
            for widget in (
                window.event_frame.state_icon,
                window.event_frame.state,
                window.event_frame.plc_icon,
                window.event_frame.plc,
                window.event_frame.web_icon,
                window.event_frame.web,
                window.event_label,
            ):
                assert window.event_frame.rect().contains(
                    QRect(widget.mapTo(window.event_frame, QPoint()), widget.size())
                )
            assert window.columns == (2 if size[0] < size[1] else 3)
            assert window.buttons["batch"].isEnabled()
            assert window.bridge.application.snapshot() == ready
    assert set(window.findChildren(QTimer)) == timers
    window.set_language("zh")
    window.navigate(0)
    QApplication.processEvents()
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    complete_cycle(window, session, 1, TechnicalStatus.SUCCESS, ProductJudgment.NG)
    before = window.bridge.application.snapshot()
    assert before.generation == 1 and before.counters.cycles_completed == 1
    assert not window.buttons["batch"].isEnabled()
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
        assert not window.buttons["batch"].isEnabled()
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
            (
                "page_production",
                "page_recipe",
                "page_communication",
                "page_history",
                "page_vision",
            ),
            window.navigation,
            strict=True,
        ):
            full = "PLC / Web" if key == "page_communication" else text(key, language)
            visible = button.text()
            assert visible == full or (
                visible.endswith("…") and full.startswith(visible.removesuffix("…"))
            ), language
            assert not button.icon().isNull() and button.height() >= Style.touch
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
    screen: tuple[MainWindow, FakeSession],
) -> None:
    window, _ = screen
    selector = window.language_selector
    selected = selector.currentData()
    application = window.bridge.application.snapshot()
    assert selector.count() == selector.maxVisibleItems() == len(LANGUAGES)
    assert [
        (selector.itemText(i), selector.itemData(i)) for i in range(selector.count())
    ] == [(LANGUAGE_NAMES[language], language) for language in LANGUAGE_DISPLAY_ORDER]
    selector.showPopup()
    try:
        QApplication.processEvents()
        view = selector.view()
        popup = view.window()
        assert popup.isVisible()
        assert popup.screen().availableGeometry().contains(popup.frameGeometry())
        for row in range(selector.count()):
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
    assert selector.currentData() == "km" and window.language == "km"
    assert window.bridge.application.snapshot() == application


@pytest.mark.parametrize("height", [500, 1040, 1857])
def test_language_popup_uses_available_work_area_before_scrolling(
    screen: tuple[MainWindow, FakeSession],
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
            if height == 1857:
                assert view.viewport().height() >= rows_height
                assert view.verticalScrollBar().maximum() == 0
            elif height == 500:
                assert view.verticalScrollBar().maximum() > 0
            assert (view.verticalScrollBar().maximum() == 0) == (
                rows_height <= view.viewport().height()
            )
            for row in range(selector.count()):
                index = selector.model().index(row, 0)
                view.scrollTo(index)
                QApplication.processEvents()
                assert view.viewport().rect().contains(view.visualRect(index)), row
        finally:
            selector.hidePopup()


@pytest.mark.parametrize("outcome", [ReportOutcome.ACKNOWLEDGED, ReportOutcome.FAILED])
def test_reporting_observation_is_not_a_connectivity_or_application_state_claim(
    screen: tuple[MainWindow, FakeSession], outcome: ReportOutcome
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
        window.event_frame.plc.setToolTip("stale ACK 42 / failed report")
        window.event_frame.plc_icon.setToolTip("stale PLC diagnostics")
        window.set_language(language)
        message = window.event_frame.plc.text()
        assert "42" not in message and "ACK" not in message
        assert "connected" not in message.lower() and "已连接" not in message
        assert not window.event_frame.plc.toolTip()
        assert not window.event_frame.plc_icon.toolTip()
        assert (
            window.event_frame.plc.accessibleName()
            == f"PLC · {text('service_off' if outcome is ReportOutcome.ACKNOWLEDGED else 'technical_error', language)}"
        )
        assert window.snapshot.state is ApplicationState.READY
        assert window.bridge.application.snapshot() == before


def complete_cycle(
    window: MainWindow,
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
    screen: tuple[MainWindow, FakeSession],
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
    screen: tuple[MainWindow, FakeSession],
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
    screen: tuple[MainWindow, FakeSession],
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
    screen: tuple[MainWindow, FakeSession],
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
        assert window.event_label.accessibleName() == message
        assert "TimeoutError" in message
        assert "TimeoutError" in window.event_label.text()
        assert "private" not in message
        assert text("stop", language) in window.event_label.text()
        assert text("operation_error", language) in window.event_label.text()
        assert theme_colors(window).warning in window.event_label.styleSheet()
    # A narrow row keeps the action and failure visible; detailed exception text
    # remains accessible and becomes fully visible when more space is available.
    window.resize(1920, 1080)
    QApplication.processEvents()
    assert "TimeoutError" in window.event_label.text()
    assert window.event_label.toolTip() == message
    window.bridge.command_finished.emit("stop")
    assert not window.event_label.text()
