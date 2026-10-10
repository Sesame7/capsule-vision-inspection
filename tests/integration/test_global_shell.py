"""Shared navigation/status contracts, with existing controlled sessions and fake engines."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_production_ui import screen as screen
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession, production

from vimo_demo.application import ApplicationState
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.inspection import CycleId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import Language, Style, camera_icon, text
from vimo_demo.ui.production_window import ProductionWindow


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
@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_five_destinations_switch_language_and_keep_navigation_contract(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
    size: tuple[int, int],
) -> None:
    window, _ = screen
    window.resize(*size)
    window.set_language(language)
    expected = (
        ("生产", "配方", "通信", "历史", "视觉")
        if language == "zh"
        else ("Production", "Recipe", "Communication", "History", "Vision")
    )
    before = window.bridge.application.snapshot()
    assert tuple(button.text() for button in window.navigation) == expected
    assert (
        window.navigation[4].icon().pixmap(Style.icon, Style.icon).toImage()
        == camera_icon(Style.secondary).pixmap(Style.icon, Style.icon).toImage()
    )
    for index, button in enumerate(window.navigation):
        assert button.toolTip() == button.accessibleName() == expected[index]
        assert not button.icon().isNull()
        assert button.minimumHeight() >= Style.touch
        button.click()
        QApplication.processEvents()
        assert window.pages.currentIndex() == index
        assert tuple(b.isChecked() for b in window.navigation) == tuple(
            i == index for i in range(5)
        )
        assert window.event_frame.isVisible() and window.event_frame.height() == 33
        assert not window.event_label.wordWrap()
        assert not window.event_label.text()
        assert window.bridge.application.snapshot() == before
    assert window.pages.currentWidget() is window.parameters.settings
    window.navigation[2].click()
    assert (
        text("diagnostics_note", language) in window.placeholders["diagnostics"].text()
    )
    window.language_button.click()
    alternate = (
        ("Production", "Recipe", "Communication", "History", "Vision")
        if language == "zh"
        else ("生产", "配方", "通信", "历史", "视觉")
    )
    assert tuple(b.text() for b in window.navigation) == alternate
    assert all(b.toolTip() == b.accessibleName() == b.text() for b in window.navigation)
    assert window.navigation[2].isChecked()


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
        assert window.event_frame.height() == 33
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
        assert window.event_frame.isVisible() and window.event_frame.height() == 33
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
        assert window.event_frame.height() == 33
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
        assert window.event_frame.height() == 33
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
        assert window.event_frame.height() == 33
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
        assert window.event_frame.height() == 33
    window.bridge.command_finished.emit("stop")
    assert not window.event_label.text()


@pytest.mark.parametrize(
    "language,size,page,scenario",
    [
        ("zh", (1080, 1920), 4, "normal"),
        ("en", (1920, 1080), 0, "normal"),
        ("zh", (1920, 1080), 2, "normal"),
        ("en", (1080, 1920), 3, "fault"),
    ],
)
def test_global_shell_screenshot(
    screen: tuple[ProductionWindow, FakeSession],
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    page: int,
    scenario: str,
) -> None:
    from run_production_ui import compose, demo_images

    # The reused screen fixture supplies realistic existing system fonts.
    composition = compose(
        demo_images(), tmp_path, "fault" if scenario == "fault" else "normal", 0.001
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
        if scenario == "fault":
            bridge.start()
            until(
                lambda: (
                    window.snapshot.state is ApplicationState.FAULT
                    and not window.snapshot.runtime_owned
                )
            )
        elif page == 0:
            bridge.start()
            until(lambda: window.snapshot.counters.cycles_completed >= 1)
        composition.source.close()
        window.navigate(page)
        until(lambda: not window.parameters.busy)
        qapp.processEvents()
        if page == 4:
            workspace = window.parameters.roi_workspace
            assert workspace is not None and workspace.frame is not None
        if scenario == "fault":
            assert window.snapshot.last_issue is not None
            assert window.snapshot.last_issue.stage == "shutdown"
            assert window.event_icon.isVisible()
            assert text("shutdown_error", language) in window.event_label.text()
            assert Style.fault in window.event_label.styleSheet()
        else:
            assert not window.event_label.text()
        assert window.event_frame.height() == 33
        for button in window.navigation:
            assert button.height() >= Style.touch
            assert (
                button.width()
                >= button.fontMetrics().horizontalAdvance(button.text())
                + Style.icon
                + Style.small
            )
        name = ("production", "recipe", "communication", "history", "vision")[page]
        assert window.grab().save(
            str(
                tmp_path / f"shell-{language}-{size[0]}x{size[1]}-{name}-{scenario}.png"
            )
        )
    finally:
        window.close()
        until(lambda: bridge.closed)
        composition.source.close()
        assert composition.application.join(0)
        window.deleteLater()
