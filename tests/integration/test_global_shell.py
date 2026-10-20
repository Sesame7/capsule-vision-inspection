"""Shared navigation/status contracts, with existing controlled sessions and fake engines."""

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication
from tests.application_support import FakeSession, production
from tests.qt_support import screen as screen
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.application.folder_replay import FolderSourceError
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
            expected = (
                ("生产", "配方", "通信", "历史", "视觉")
                if current == "zh"
                else ("Production", "Recipe", "Comms", "History", "Vision")
            )
            assert tuple(button.text() for button in window.navigation) == expected, (
                case
            )
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
