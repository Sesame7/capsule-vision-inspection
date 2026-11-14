"""Normal entry settings, separate live observations and explicit persistence."""

import json
import socket
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QPoint, QRect, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontMetricsF
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QStyle,
    QStyleOptionHeader,
    QStyleOptionSpinBox,
    QWidget,
)
from tests.support.application import WAIT
from tests.support.qt import assert_full_caption, close_window, ensure_test_fonts, until
from tests.support.startup import authenticated_startup, provision_account, saved_data

from capsule_vision.application import ApplicationState, ProductionCounters
from capsule_vision.application.identity_models import Role
from capsule_vision.application.startup import ApplicationData, Startup, load_startup
from capsule_vision.communication import (
    FinsMemoryArea,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
    ModbusSettings,
)
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
)
from capsule_vision.communication.reporting import (
    ReportIdentity,
    ReportingIssue,
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportingSnapshot,
    ReportObservation,
    ReportOutcome,
)
from capsule_vision.configuration import load_camera_sources, save_camera_sources
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.inspection import CycleId
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language
from capsule_vision.ui.bootstrap import create_window, main
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.style import Style, Theme, theme_colors
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime, WebRuntimeSnapshot, WebState


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
@pytest.mark.parametrize("modbus,base", [(False, 49), (False, 149), (True, 65532)])
def test_global_reporting_state_and_communication_details(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
    size: tuple[int, int],
    modbus: bool,
    base: int,
) -> None:
    startup, window = settings_window
    window.set_language(language)
    window.resize(*size)
    QApplication.processEvents()
    controller = window.bridge.reporting
    assert controller is not None
    before = startup.application.snapshot()
    original = controller.reporting_selection()
    selected = (
        replace(
            original,
            settings=ReportingSettings(base_address=base, endpoint=ModbusSettings()),
        )
        if modbus
        else replace(original, settings=replace(original.settings, base_address=base))
    )
    controller.set_reporting_selection(selected)
    panel = window.communication.reporting_panel
    assert panel is not None
    panel.set_selection(selected)
    ack = ReportObservation(
        ReportIdentity(1, CycleId(1)), ReportOutcome.ACKNOWLEDGED, sequence=42
    )
    report = ReportingSnapshot(
        generation=1,
        accepting=False,
        thread_alive=False,
        pending=0,
        peak_pending=1,
        active=None,
        queued_count=1,
        attempted_count=1,
        acknowledged_count=1,
        failed_count=0,
        skipped_count=0,
        rejected_count=0,
        next_sequence=43,
        last_queued=ack.identity,
        last_attempted=ack,
        last_acknowledged=ack,
        last_outcome=ack,
        last_failed=None,
        last_skipped=None,
        last_rejected=None,
        failure_issue=None,
        cleanup_issue=None,
    )
    observed: list[ReportingSnapshot | None] = [None]
    monkeypatch.setattr(controller, "reporting_snapshot", lambda: observed[0])

    def frozen_settings(generation: int) -> ReportingSettings | None:
        return selected.settings if generation == 1 else None

    monkeypatch.setattr(
        startup.application, "reporting_for_generation", frozen_settings
    )
    QApplication.processEvents()
    header_controls = (
        panel.title,
        panel.status_indicator,
        panel.mode_control,
        panel.merge_label,
        panel.merge_count,
        panel.service_status,
        panel.enabled,
    )
    header_geometry = tuple(widget.geometry() for widget in header_controls)
    strip_geometry = tuple(
        parent.geometry()
        for widget in (
            window.event_frame.plc,
            window.event_frame.alarm,
            window.event_label,
        )
        if (parent := widget.parentWidget()) is not None
    )

    def check(key: str, observation: ReportingSnapshot | None, health: str) -> None:
        observed[0] = observation
        window.event_frame.plc.setToolTip("stale diagnostics")
        window.bridge.timer.timeout.emit()
        label = window.event_frame.plc
        expected_ack = (
            observation.last_acknowledged.sequence
            if observation and observation.last_acknowledged
            else "—"
        )
        assert label.text() == f"{'HR ' if modbus else 'W'}{base} · {expected_ack}"
        assert label.accessibleName() == label.text()
        panel.refresh()
        QApplication.processEvents()
        assert (
            tuple(
                parent.geometry()
                for widget in (
                    window.event_frame.plc,
                    window.event_frame.alarm,
                    window.event_label,
                )
                if (parent := widget.parentWidget()) is not None
            )
            == strip_geometry
        )
        assert tuple(widget.geometry() for widget in header_controls) == header_geometry
        assert not label.toolTip() and not panel.service_status.toolTip()
        assert panel.service_status.accessibleName()
        assert health in panel.status_indicator.styleSheet()
        assert panel.status_indicator.size().toTuple() == (8, 8)
        assert not hasattr(panel, "summary")
        assert controller.reporting_snapshot() == observation
        assert startup.application.snapshot() == before

    try:
        colors = theme_colors(panel)
        check("service_off", None, colors.secondary)
        controller.set_reporting_selection(replace(selected, enabled=True))
        check("service_next_start", None, colors.warning)
        check("service_next_start", report, colors.warning)
        check(
            "service_next_start",
            replace(report, last_acknowledged=None),
            colors.warning,
        )
        live = replace(report, thread_alive=True, accepting=True)
        check("web_running", live, colors.ok)
        for number in (0, 9, 10, 9999, 65535):
            check(
                "web_running",
                replace(live, last_acknowledged=replace(ack, sequence=number)),
                colors.ok,
            )
        check(
            "web_running",
            replace(live, last_acknowledged=replace(ack, sequence=43)),
            colors.ok,
        )
        controller.set_reporting_selection(replace(selected, enabled=False))
        check("web_running", live, colors.ok)
        check("web_running", replace(live, accepting=False), colors.warning)
        error = ReportingIssue("TimeoutError", "uncertain sequence ACK")
        for outcome in (ReportOutcome.FAILED, ReportOutcome.REJECTED):
            failure = replace(ack, outcome=outcome, issue=error)
            check("technical_error", replace(live, last_outcome=failure), colors.ok)
        for field in ("failure_issue", "cleanup_issue"):
            check("technical_error", replace(report, **{field: error}), colors.fault)
        check("service_off", report, colors.secondary)
        controller.set_reporting_selection(
            replace(
                selected,
                enabled=False,
                settings=replace(
                    selected.settings, mode=ReportingMode.PER_CYCLE, base_address=120
                ),
            )
        )
        check(
            "service_off", report, colors.secondary
        )  # Old ACK retains its actual target.
    finally:
        observed[0] = None
        controller.set_reporting_selection(original)
        panel.set_selection(original)


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_web_power_only_persists_enablement_and_apply_uses_saved_network(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    port = available_port()
    panel.port.setValue(port)
    page.persist_settings()
    until(lambda: not page.busy)
    saved = load_communication_preferences(startup.data.communication)
    original_reporting = (
        page.reporting_panel.controller.reporting_selection()
        if page.reporting_panel
        else None
    )
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application,
            startup.frames,
            host=settings.host,
            port=settings.port,
            interval=settings.interval_ms / 1000,
        )
    )
    window.bind_web_runtime(runtime)
    draft_port = available_port()
    panel.port.setValue(draft_port)
    page.port.setValue(19602)
    assert page.reporting_panel is not None
    page.reporting_panel.enabled.setChecked(True)
    assert page.dirty and panel.power.isEnabled() and panel.apply_changes.isHidden()
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: panel.available)
    saved = replace(saved, web=replace(saved.web, enabled=True))
    QApplication.processEvents()
    assert panel.open_browser.isVisible()
    assert panel.open_browser.accessibleDescription() == f"http://127.0.0.1:{port}/"
    assert (
        panel.open_browser.mapTo(panel, QPoint()).y()
        <= panel.power.geometry().bottom()
        < panel.port.y()
    )
    assert runtime.snapshot().configuration == saved.web
    assert load_communication_preferences(startup.data.communication) == saved
    identity = startup.application.identity
    assert identity is not None
    revisions = identity.audit.revisions("communication", "communication.json").result(
        WAIT
    )
    assert (
        revisions[0].confirmed
        and revisions[0].record.operation.action == "communication.web_power"
    )
    assert (
        identity.audit.revision_content(revisions[0].record.operation.id).result(WAIT)
        == startup.data.communication.read_bytes()
    )
    assert panel.port.value() == draft_port and page.port.value() == 19602
    assert page.reporting_panel.enabled.isChecked() and page.dirty
    assert page.reporting_panel.controller.reporting_selection() is original_reporting
    urls: list[QUrl] = []

    def opened(url: QUrl) -> bool:
        urls.append(url)
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", opened)
    panel.open_browser.click()
    assert [url.toString() for url in urls] == [f"http://127.0.0.1:{port}/"]
    page.reload_settings()
    panel.port.setValue(draft_port)
    page.persist_settings()
    until(lambda: not page.busy)
    assert runtime.snapshot().configuration == saved.web  # Save is not Apply.
    assert panel.apply_changes.isVisible() and panel.apply_changes.isEnabled()
    assert panel.apply_changes.accessibleName() == text(
        "web_apply_changes", window.language
    )
    panel.host.setText("0.0.0.0")
    assert not panel.apply_changes.isEnabled()
    panel.requested.emit("apply")
    assert runtime.snapshot().configuration == saved.web
    page.reload_settings()
    panel.apply_changes.click()
    until(lambda: not runtime.snapshot().busy)
    until(lambda: panel.apply_changes.isHidden())
    assert runtime.snapshot().configuration == replace(saved.web, port=draft_port)
    # Even invalid network/other drafts cannot prevent a saved-only power-off.
    panel.host.setText("0.0.0.0")
    page.port.setValue(19602)
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: not panel.available)
    until(lambda: runtime.snapshot().state is WebState.STOPPED)
    assert runtime.snapshot().state is WebState.STOPPED
    assert panel.open_browser.isHidden()
    stopped = load_communication_preferences(startup.data.communication)
    assert stopped == replace(
        saved, web=replace(saved.web, enabled=False, port=draft_port)
    )
    assert panel.host.text() == "0.0.0.0" and page.port.value() == 19602 and page.dirty
    # This live power test continues using the first application. Restore the
    # saved Communication profile in its own root, without copying live SQLite.
    probe = ApplicationData.at_entry(tmp_path / "saved-communication-probe")
    probe.root.mkdir(parents=True)
    probe.communication.write_bytes(startup.data.communication.read_bytes())
    restored = load_startup(probe)
    try:
        assert not restored.communication.web.enabled
    finally:
        restored.application.close().result(5)
        assert restored.application.join(5)
    until(lambda: panel.power.isEnabled())
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: panel.available)
    assert runtime.snapshot().configuration == replace(saved.web, port=draft_port)
    assert load_communication_preferences(startup.data.communication).web.enabled
    # An explicit reload can also restore a saved disabled selection while Web
    # is running. Applying it stops that owner without editing the file.
    save_communication_preferences(startup.data.communication, stopped)
    page.reload_settings()
    assert runtime.snapshot().state is WebState.RUNNING
    assert panel.apply_changes.isEnabled()
    assert panel.apply_changes.accessibleName() == text(
        "web_apply_disabled", window.language
    )
    panel.apply_changes.click()
    until(lambda: runtime.snapshot().state is WebState.STOPPED)
    assert load_communication_preferences(startup.data.communication) == stopped
    assert startup.application.snapshot().generation == 0
    assert startup.application.snapshot().counters == ProductionCounters()


def test_unavailable_ip_refresh_and_discard_preserve_saved_identity(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    monkeypatch.setattr("capsule_vision.web.hosts.local_interfaces", lambda: ())
    panel.host.setText("192.0.2.10")
    page.persist_settings()
    until(lambda: not page.busy)
    assert not page.dirty
    panel.refresh_addresses()
    assert (
        panel.host.text() == "192.0.2.10" and panel.addresses.itemText(0) == "127.0.0.1"
    )
    assert not page.dirty
    panel.host.setText("127.0.0.1")
    page.reload_settings()
    assert panel.host.text() == "192.0.2.10" and not page.dirty
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: not runtime.snapshot().busy)
    until(lambda: runtime.snapshot().state is WebState.FAILED)
    assert runtime.snapshot().state is WebState.FAILED
    assert not panel.actual.toolTip()
    assert "not assigned" in runtime.snapshot().error
    assert not runtime.snapshot().owned
    assert (
        load_communication_preferences(startup.data.communication).web.host
        == "192.0.2.10"
    )


def test_web_power_save_failure_preserves_running_service_and_drafts(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    panel.port.setValue(available_port())
    page.persist_settings()
    until(lambda: not page.busy)
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: panel.available)
    saved = startup.data.communication.read_bytes()
    actual = runtime.snapshot().configuration
    panel.host.setText("0.0.0.0")
    page.port.setValue(19602)

    def denied(*_args: object) -> None:
        raise PermissionError("controlled power persistence failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    panel.power.click()
    until(lambda: not page.busy)
    assert startup.data.communication.read_bytes() == saved
    assert panel.saved.enabled and runtime.snapshot().state is WebState.RUNNING
    assert runtime.snapshot().configuration == actual
    assert panel.host.text() == "0.0.0.0" and page.port.value() == 19602
    assert (
        page.dirty
        and "controlled power persistence failure" in page.preferences_status.toolTip()
    )
    assert panel.power.text() == text("web_turn_off", window.language)


def test_web_power_bind_failure_keeps_saved_enablement_and_reports_actual_failure(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        panel.port.setValue(occupied.getsockname()[1])
        page.persist_settings()
        until(lambda: not page.busy)
        panel.port.setValue(
            available_port()
        )  # This draft must never be used as fallback.
        panel.power.click()
        until(lambda: not page.busy)
        until(lambda: runtime.snapshot().state is WebState.FAILED)
        until(lambda: runtime.snapshot().state is WebState.FAILED)
        assert load_communication_preferences(startup.data.communication).web.enabled
        assert panel.saved.enabled and not runtime.snapshot().owned
        assert panel.power.text() == text("web_turn_on", window.language)
        assert not panel.available and panel.apply_changes.isHidden() and page.dirty
        assert startup.application.snapshot().generation == 0
        assert startup.application.snapshot().counters == ProductionCounters()


def test_web_power_only_changes_enable_bit_in_actual_file(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    page.persist_settings()
    until(lambda: not page.busy)
    old = load_communication_preferences(startup.data.communication)
    actual = replace(
        old,
        diagnostic=replace(old.diagnostic, port=19612),
        reporting=replace(
            old.reporting,
            settings=replace(
                old.reporting.settings,
                endpoint=replace(old.reporting.settings.endpoint, port=19613),
            ),
        ),
        web=WebPreferences(False, port=available_port()),
    )
    save_communication_preferences(startup.data.communication, actual)
    assert page.reporting_panel is not None
    selected = page.reporting_panel.controller.reporting_selection()
    page.port.setValue(19602)
    panel.port.setValue(19603)
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: not page.busy)
    until(lambda: panel.available)
    assert load_communication_preferences(startup.data.communication) == replace(
        actual, web=replace(actual.web, enabled=True)
    )
    assert runtime.snapshot().configuration == replace(actual.web, enabled=True)
    assert page.port.value() == 19602 and panel.port.value() == 19603
    assert page.reporting_panel.controller.reporting_selection() is selected


@pytest.mark.parametrize("invalid_file", [False, True])
def test_web_power_first_use_defaults_or_invalid_file_preserve_drafts(
    settings_window: tuple[Startup, MainWindow],
    invalid_file: bool,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    defaults = CommunicationPreferences(web=WebPreferences(port=available_port()))
    page.configure_preferences(startup.data.communication, defaults)
    if invalid_file:
        startup.data.communication.write_text('{"unexpected":true}', encoding="utf-8")
    panel.port.setValue(19603)
    page.port.setValue(19602)
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: not page.busy)
    if invalid_file:
        assert (
            startup.data.communication.read_text(encoding="utf-8")
            == '{"unexpected":true}'
        )
        assert runtime.snapshot().state is WebState.STOPPED
        assert panel.operation_error and not runtime.snapshot().owned
        failure = panel.operation_error
        assert failure in panel.power.accessibleDescription()
        page.save_settings.click()
        until(lambda: not page.busy)
        assert runtime.snapshot().state is WebState.STOPPED
        assert failure in panel.power.toolTip()
        assert (
            text("web_power_failed", window.language) in page.preferences_status.text()
        )
        assert failure in page.preferences_status.toolTip()
        assert not page.dirty
    else:
        until(lambda: panel.available)
        assert load_communication_preferences(startup.data.communication) == defaults
        assert runtime.snapshot().configuration == defaults.web
    assert panel.port.value() == 19603 and page.port.value() == 19602
    assert page.dirty == (not invalid_file)


def test_web_ipv4_choices_are_visible_and_refresh_preserves_manual_draft(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    monkeypatch.setattr(
        "capsule_vision.ui.web_settings_panel.local_ipv4_addresses",
        lambda: ("127.0.0.1", "192.0.2.10"),
    )
    panel.host.setText("192.0.2.77")
    panel.address_refresh.click()
    assert [panel.addresses.itemText(i) for i in range(panel.addresses.count())] == [
        "127.0.0.1",
        "192.0.2.10",
    ]
    assert panel.host.text() == "192.0.2.77" and page.dirty
    assert panel.addresses.isVisible() and panel.addresses.isEditable()
    QTest.mouseClick(
        panel.addresses,
        Qt.MouseButton.LeftButton,
        pos=QPoint(panel.addresses.width() - 20, panel.addresses.height() // 2),
    )
    until(lambda: panel.addresses.view().isVisible())
    panel.addresses.hidePopup()
    assert panel.host.text() == "192.0.2.77"
    panel.addresses.setCurrentIndex(1)
    assert panel.host.text() == "192.0.2.10" and page.dirty


def test_window_close_during_web_start_keeps_dispatch_and_ownership_observable(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    entered, release = Event(), Event()

    def factory(settings: WebPreferences) -> ProductionSnapshotService:
        entered.set()
        assert release.wait(5)
        return ProductionSnapshotService(
            startup.application, startup.frames, port=settings.port
        )

    runtime = WebRuntime(factory)
    window.bind_web_runtime(runtime)
    try:
        runtime.start(WebPreferences(port=available_port()))
        until(entered.is_set)
        window.close()
        alive: list[bool] = []
        QTimer.singleShot(0, lambda: alive.append(True))
        until(lambda: bool(alive))
        assert not window.communication.closed and not runtime.closed
        assert window.isVisible()
    finally:
        release.set()
    until(lambda: runtime.closed and window.communication.closed)
    until(lambda: not window.isVisible())


@pytest.fixture
def settings_window(
    qapp: QApplication, tmp_path: Path
) -> Generator[tuple[Startup, MainWindow]]:
    ensure_test_fonts()
    startup = authenticated_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    window.navigate(2)
    try:
        yield startup, window
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("language", ("zh", "en"))
@pytest.mark.parametrize("size", ((1080, 1920), (1920, 1080)))
def test_numeric_values_and_prefixes_fit_shared_stepper_geometry(
    settings_window: tuple[Startup, MainWindow],
    theme: Theme,
    language: Language,
    size: tuple[int, int],
) -> None:
    startup, window = settings_window
    window.resize(*size)
    window.set_language(language)
    window.set_theme(theme)
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    before = startup.application.snapshot()
    panel.advanced_button.click()
    panel.merge_count.setCurrentIndex(0)
    page.advanced_button.click()
    page.watch_editor_button.click()
    page.manual_button.click()
    for protocol in ("fins_tcp", "modbus_tcp"):
        panel.protocol.setCurrentIndex(1 if protocol == "fins_tcp" else 2)
        page.protocol.setCurrentIndex(1 if protocol == "fins_tcp" else 2)
        spins = page.findChildren(QSpinBox) + page.findChildren(QDoubleSpinBox)
        for spin in spins:
            if isinstance(spin, QSpinBox):
                spin.setValue(spin.maximum())
            else:
                spin.setValue(spin.maximum())
        QApplication.processEvents()
        assert page.body.width() <= page.scroll_area.viewport().width()
        for spin in spins:
            if not spin.isVisible():
                continue
            option = QStyleOptionSpinBox()
            spin.initStyleOption(option)
            up = spin.style().subControlRect(
                QStyle.ComplexControl.CC_SpinBox,
                option,
                QStyle.SubControl.SC_SpinBoxUp,
                spin,
            )
            assert up.width() == 21 and up.right() == spin.rect().right()
            assert spin.height() >= Style.touch
            line = spin.lineEdit()
            assert (
                line.fontMetrics().horizontalAdvance(line.text()) + 4 <= line.width()
            ), (spin.accessibleName(), line.text(), line.size())
        assert page.web_panel.port.isVisible()
        assert page.alarm_panel.port.isVisible()
    assert startup.application.snapshot() == before
    assert not startup.data.communication.exists()


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("language", ("zh", "en"))
def test_web_service_dot_uses_observed_health_and_neutral_power_slots(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    theme: Theme,
    language: Language,
) -> None:
    _, window = settings_window
    window.set_theme(theme)
    window.set_language(language)
    page, panel = window.communication, window.communication.web_panel

    def no_service(settings: WebPreferences) -> ProductionSnapshotService:
        raise AssertionError("Presentation does not start a Web service")

    runtime = WebRuntime(no_service)
    window.bind_web_runtime(runtime)
    observed = [WebRuntimeSnapshot()]
    colors = theme_colors(panel)
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "snapshot", lambda: observed[0])
        panel.refresh()
        QApplication.processEvents()
        widgets = (panel.status_indicator, panel.actual, panel.power)
        geometry = tuple(widget.geometry() for widget in widgets)
        for state, color in (
            (WebState.STOPPED, colors.secondary),
            (WebState.STARTING, colors.warning),
            (WebState.RUNNING, colors.ok),
            (WebState.STOPPING, colors.warning),
            (WebState.FAILED, colors.fault),
        ):
            observed[0] = WebRuntimeSnapshot(
                state=state, owned=state is not WebState.STOPPED
            )
            panel.refresh()
            QApplication.processEvents()
            assert color in panel.status_indicator.styleSheet()
            assert panel.status_indicator.size().toTuple() == (8, 8)
            assert tuple(widget.geometry() for widget in widgets) == geometry
        observed[0] = WebRuntimeSnapshot(state=WebState.RUNNING, owned=True)
        panel.operation_error = "isolated preference save failure"
        panel.refresh()
        assert colors.ok in panel.status_indicator.styleSheet()
        assert panel.actual.text() == text("web_running", language)
        reporting = page.reporting_panel
        assert reporting is not None
        assert (
            panel.power.size()
            == reporting.enabled.size()
            == page.alarm_panel.enabled.size()
        )


@pytest.mark.parametrize("width", (1080, 1920))
def test_service_header_alignment_survives_language_theme_and_resize(
    settings_window: tuple[Startup, MainWindow], width: int
) -> None:
    startup, window = settings_window
    page = window.communication
    reporting, web, alarms = page.reporting_panel, page.web_panel, page.alarm_panel
    assert reporting is not None
    groups = (
        (
            reporting,
            reporting.status_indicator,
            reporting.service_status,
            reporting.enabled,
        ),
        (web, web.status_indicator, web.actual, web.power),
        (alarms, alarms.status_indicator, alarms.status, alarms.enabled),
    )
    before = startup.application.snapshot()
    window.resize(width, 1920 if width == 1080 else 1080)
    with QSignalBlocker(window.bridge.timer):
        slots: tuple[tuple[int, ...], ...] | None = None
        for theme in ("dark", "light"):
            window.set_theme(theme)
            for language in LANGUAGES:
                window.set_language(language)
                QApplication.processEvents()
                local = tuple(
                    (
                        status.width(),
                        card.width() - action.x(),
                        action.width(),
                        action.height(),
                    )
                    for card, _dot, status, action in groups
                )
                assert local[0] == local[1] == local[2], (language, local)
                if slots is None:
                    slots = local
                assert local == slots, (language, theme)
                for card, dot, status, action in groups:
                    assert dot.x() - card.title.geometry().right() == Style.small + 1
                    assert dot.size().toTuple() == (8, 8)
                    assert (
                        status.fontMetrics().horizontalAdvance(status.text())
                        <= status.width()
                    )
                    assert dot.geometry().right() < status.x()
                    assert status.geometry().right() < action.x()
                    assert card.rect().contains(action.geometry())
                    assert action.height() >= Style.touch
                    assert_full_caption(action, action.text())
                stacked = page.service_layout.itemAtPosition(1, 0) is not None
                expected_title_width = max(
                    card.title.fontMetrics().horizontalAdvance(card.title.text()) + 1
                    for card, *_ in groups
                )
                assert all(
                    card.title.width() == expected_title_width for card, *_ in groups
                )
                assert len({dot.x() for _card, dot, _status, _action in groups}) == 1
                if stacked:
                    assert (
                        len(
                            {
                                dot.mapTo(page.body, QPoint()).x()
                                for _card, dot, _status, _action in groups
                            }
                        )
                        == 1
                    )
                else:
                    assert reporting.geometry().right() < web.x()
                    assert (
                        reporting.status_indicator.mapTo(page.body, QPoint()).x()
                        < web.status_indicator.mapTo(page.body, QPoint()).x()
                    )
                assert alarms.title.text() == text("status_service", language)
                assert (
                    alarms.title.toolTip()
                    == alarms.title.accessibleName()
                    == text("status_service_tip", language)
                )
                assert page.scroll_area.horizontalScrollBar().maximum() == 0
        for next_width in (1920, 1080, 1920, 1080, width):
            window.resize(next_width, window.height())
            QApplication.processEvents()
            assert page.scroll_area.horizontalScrollBar().maximum() == 0
            assert (page.service_layout.itemAtPosition(1, 0) is not None) == (
                next_width == 1080
            )
        assert startup.application.snapshot() == before
        assert reporting.controller.reporting_snapshot() is None
        assert startup.application.alarm_snapshot().state == "off"


@pytest.mark.parametrize("width", (1080, 1920))
def test_service_titles_remeasure_on_font_change_and_keep_local_alignment(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    _, window = settings_window
    page = window.communication
    reporting, web, alarms = page.reporting_panel, page.web_panel, page.alarm_panel
    assert reporting is not None
    cards = (reporting, web, alarms)
    window.resize(width, 1920 if width == 1080 else 1080)
    window.set_language("en")
    QApplication.processEvents()
    original = reporting.title.width()
    with QSignalBlocker(window.bridge.timer):
        reporting.title.setStyleSheet("font-size: 24px; font-weight: 600;")
        QApplication.processEvents()
        assert reporting.title.width() > original
        for language in ("zh", "en", "de", "ru"):
            window.set_language(language)
            QApplication.processEvents()
            expected = max(
                card.title.fontMetrics().horizontalAdvance(card.title.text()) + 1
                for card in cards
            )
            assert all(card.title.width() == expected for card in cards)
            geometry = tuple(card.status_indicator.geometry() for card in cards)
            assert len({position.x() for position in geometry}) == 1

            def unexpected_title_measurement() -> None:
                pytest.fail("An unchanged service refresh remeasured a title")

            with monkeypatch.context() as patch:
                for card in cards:
                    patch.setattr(
                        card.title, "fontMetrics", unexpected_title_measurement
                    )
                for _ in range(3):
                    for card in cards:
                        card.refresh()
                    QApplication.processEvents()
                    assert (
                        tuple(card.status_indicator.geometry() for card in cards)
                        == geometry
                    )
            assert page.scroll_area.horizontalScrollBar().maximum() == 0
        reporting.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        window.set_language("en")
        QApplication.processEvents()
        assert all(card.title.width() == original for card in cards)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("width", [1080, 1920])
def test_reporting_merge_count_keyboard_draft_save_discard_and_header_slots(
    settings_window: tuple[Startup, MainWindow], language: Language, width: int
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.reporting_panel
    assert panel is not None
    window.resize(width, 1000)
    window.set_language(language)
    QApplication.processEvents()
    count = panel.merge_count
    assert panel.merge_label.text() == ("合并" if language == "zh" else "Merge")
    assert count.count() == 9 and count.currentIndex() == 4
    assert count.currentText() == "4" and not count.isEditable()
    for index in range(9):
        item = count.model().index(index, 0)
        flags = item.flags()
        assert bool(flags & Qt.ItemFlag.ItemIsEnabled) is (index in (0, 1, 4))
        assert bool(flags & Qt.ItemFlag.ItemIsSelectable) is (index in (0, 1, 4))
        assert count.itemText(index) == str(index)
        if index not in (0, 1, 4):
            assert text("plc_merge_unsupported", language) in count.itemData(
                index, Qt.ItemDataRole.AccessibleTextRole
            )
        assert count.itemData(index, Qt.ItemDataRole.AccessibleTextRole)
    assert not panel.enabled.isChecked()
    assert panel.feedback.isHidden()
    neighbors = (
        panel.title,
        panel.mode_control,
        panel.merge_label,
        count,
        panel.service_status,
        panel.enabled,
    )
    geometry = tuple(widget.geometry() for widget in neighbors)
    assert panel.title.geometry().right() < panel.status_indicator.x()
    assert panel.service_status.geometry().right() < panel.mode_control.x()
    assert panel.service_status.geometry().right() < panel.enabled.x()
    assert panel.mode_control.y() == panel.enabled.y()
    assert panel.host.y() > panel.mode_control.geometry().bottom()
    assert count.height() >= Style.touch
    assert panel.mode_control.rect().contains(count.geometry())
    assert count.accessibleName() == text("plc_merge_count", language)
    assert text("plc_next_start", language) in count.toolTip()
    before = startup.application.snapshot()
    selected = panel.controller.reporting_selection()
    count.setFocus()
    QTest.keyClick(count, Qt.Key.Key_Up)
    assert count.hasFocus() and count.currentIndex() == 1
    assert panel.selection().settings.mode is ReportingMode.SINGLE_CYCLE
    assert page.dirty and panel.controller.reporting_selection() == selected
    assert not panel.enabled.isChecked() and not startup.data.communication.exists()
    QTest.keyClick(count, Qt.Key.Key_Down)
    assert count.hasFocus() and count.currentIndex() == 4
    QTest.keyClick(count, Qt.Key.Key_Down)
    assert count.currentIndex() == 4  # Remaining visible entries are disabled.
    QTest.keyClick(count, Qt.Key.Key_Up)
    assert count.hasFocus() and count.currentIndex() == 1
    page.save_settings.click()
    until(lambda: not page.busy)
    assert not page.dirty
    assert (
        load_communication_preferences(
            startup.data.communication
        ).reporting.settings.mode
        is ReportingMode.SINGLE_CYCLE
    )
    saved = json.loads(startup.data.communication.read_text(encoding="utf-8"))
    assert not {"group_count", "merge_count"} & saved["reporting"].keys()
    count.setCurrentIndex(4)
    assert page.dirty and not panel.enabled.isChecked()
    page.request_discard()
    dialog = page.findChild(QMessageBox)
    assert dialog is not None
    next(
        button
        for button in dialog.buttons()
        if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole
    ).click()
    assert not page.dirty and count.currentIndex() == 1
    for theme in ("light", "dark"):
        window.set_theme(theme)
        panel.refresh()
        QApplication.processEvents()
        assert tuple(widget.geometry() for widget in neighbors) == geometry
    assert startup.application.snapshot() == before
    assert panel.controller.reporting_snapshot() is None


@pytest.mark.parametrize("role", list(Role))
def test_reporting_merge_count_permissions_and_runtime_locks(
    settings_window: tuple[Startup, MainWindow], role: Role
) -> None:
    startup, window = settings_window
    identity = startup.application.identity
    panel = window.communication.reporting_panel
    assert identity is not None and panel is not None
    if role is Role.GUEST:
        identity.logout()
    elif role is not Role.ADMINISTRATOR:
        provision_account(identity, "ModeEditor", role, "isolated test password")
        identity.login("ModeEditor", "isolated test password").result(5)
    window.identity_controls.refresh()
    count = panel.merge_count
    assert count.isEnabled() is (role >= Role.ENGINEER)
    count.setCurrentIndex(0)
    assert (count.currentIndex() == 0) is (role >= Role.ENGINEER)
    window.communication.persist_settings()
    until(lambda: not window.communication.busy)
    assert startup.data.communication.exists() is (role >= Role.ENGINEER)
    for theme in ("light", "dark"):
        window.set_theme(theme)
        panel.update_state(ApplicationState.RUNNING)
        # Locked controls must still reveal which reporting mode is selected.
        assert count.currentText() == ("0" if role >= Role.ENGINEER else "4")
        assert not count.grab().isNull()
    for state in (
        ApplicationState.STARTING,
        ApplicationState.RUNNING,
        ApplicationState.STOPPING,
        ApplicationState.FAULT,
    ):
        panel.update_state(state)
        before = panel.selection()
        assert not count.isEnabled()
        for index in (0, 1, 4):
            count.setCurrentIndex(index)
        assert panel.selection() == before and not panel.enabled.isEnabled()
    panel.update_state(ApplicationState.READY)
    assert count.isEnabled() is (role >= Role.ENGINEER)


@pytest.mark.parametrize(
    "mode,count",
    (
        (ReportingMode.PER_CYCLE, 0),
        (ReportingMode.SINGLE_CYCLE, 1),
        (ReportingMode.FOUR_CYCLE, 4),
    ),
)
def test_reporting_merge_count_restores_saved_modes(
    qapp: QApplication, tmp_path: Path, mode: ReportingMode, count: int
) -> None:
    data = saved_data(tmp_path)
    selected = ReportingSelection(
        True,
        ReportingSettings(
            endpoint=FinsTcpEndpoint(port=19600), base_address=149, mode=mode
        ),
    )
    save_communication_preferences(
        data.communication, CommunicationPreferences(reporting=selected)
    )
    original = data.communication.read_bytes()
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        panel = window.communication.reporting_panel
        assert panel is not None
        assert panel.merge_count.currentIndex() == count
        assert panel.merge_count.currentText() == str(count)
        assert panel.enabled.isChecked()  # Zero is a mode, not a power action.
        assert panel.selection() == selected
        assert panel.controller.reporting_selection() == selected
        assert panel.controller.reporting_snapshot() is None
        assert data.communication.read_bytes() == original
    finally:
        close_window(window)
        delivery.detach()


def test_reporting_unsupported_counts_cannot_select_or_publish(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.reporting_panel
    assert panel is not None
    count = panel.merge_count
    page.persist_settings()
    until(lambda: not page.busy)
    original = startup.data.communication.read_bytes()
    selected = panel.controller.reporting_selection()
    edited = QSignalSpy(panel.edited)
    for index in (-1, 2, 3, 5, 6, 7, 8, 9):
        count.setCurrentIndex(index)
        assert count.currentIndex() == 4
        assert panel.selection() == selected
    assert edited.count() == 0

    count.showPopup()
    QApplication.processEvents()
    view = count.view()
    disabled_row = view.model().index(2, 0)
    assert view.visualRect(disabled_row).isValid()
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=view.visualRect(disabled_row).center(),
    )
    count.hidePopup()
    assert count.currentIndex() == 4 and edited.count() == 0

    # Even bypassing the UI change signal cannot turn a future count into a
    # reporting definition. Save keeps the existing file and admitted selection.
    with QSignalBlocker(count):
        count.setCurrentIndex(2)
    with pytest.raises(ValueError, match="Unsupported reporting merge count"):
        panel.selection()
    page.persist_settings()
    until(lambda: not page.busy)
    assert (
        text("comm_settings_failed", window.language) in page.preferences_status.text()
    )
    assert startup.data.communication.read_bytes() == original
    assert panel.controller.reporting_selection() == selected
    count.setCurrentIndex(0)
    panel.base_address.setValue(120)
    count.setCurrentIndex(4)
    count.setCurrentIndex(6)
    assert count.currentIndex() == 4 and panel.base_address.value() == 120
    with pytest.raises(ValueError, match="W49 or W149"):
        panel.selection()
    assert panel.controller.reporting_snapshot() is None


def test_reporting_base_draft_save_discard_and_protocol_visibility(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    # First-show default, without an extra resize.
    QApplication.processEvents()
    assert panel.base_address.value() == 49
    assert panel.base_address.parentWidget() is panel.base_field
    assert panel.rect().contains(panel.base_field.geometry())
    assert panel.protocol.count() == page.protocol.count() == 3
    panel.merge_count.setCurrentIndex(
        0
    )  # Arbitrary bases belong to per-cycle reporting.
    panel.base_address.setValue(120)
    assert (
        page.dirty
        and panel.controller.reporting_selection().settings.base_address == 49
    )
    panel.source[1].setValue(9)
    panel.advanced_button.setChecked(True)
    page.advanced_button.setChecked(True)
    page.source[1].setValue(7)
    for language in ("en", "zh"):
        window.set_language(language)
        for combo in (panel.protocol, page.protocol):
            combo.setCurrentIndex(1)
        QApplication.processEvents()
        assert not panel.advanced.isVisible() and not page.advanced.isVisible()
        assert (
            not panel.advanced_button.isVisible()
            and not page.advanced_button.isVisible()
        )
        assert panel.base_address.accessibleName() == text("plc_base_address", language)
    page.persist_settings()
    until(lambda: not page.busy)
    assert not page.dirty
    stored = json.loads(startup.data.communication.read_text(encoding="utf-8"))
    assert stored["reporting"] == {"protocol": "fins_tcp", "base_address": 120}
    assert stored["diagnostic"] == {"protocol": "fins_tcp"}
    assert panel.controller.reporting_selection().settings.base_address == 120
    assert not panel.controller.reporting_selection().enabled
    panel.base_address.setValue(200)
    panel.protocol.setCurrentIndex(0)
    page.protocol.setCurrentIndex(0)
    panel.source[1].setValue(11)
    page.request_discard()
    dialog = page.findChild(QMessageBox)
    assert dialog is not None
    next(
        button
        for button in dialog.buttons()
        if dialog.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole
    ).click()
    assert panel.base_address.value() == 120 and panel.protocol.currentIndex() == 1
    assert page.protocol.currentIndex() == 1 and not page.dirty
    assert panel.source[1].value() == 1 and page.source[1].value() == 1
    assert startup.application.snapshot().generation == 0
    assert startup.application.snapshot().counters == ProductionCounters()
    assert panel.controller.reporting_snapshot() is None


def test_explicit_save_restart_independent_endpoints_and_no_live_state(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    files = {p: p.read_bytes() for p in startup.data.root.rglob("*.json")}
    baseline = startup.application.snapshot()
    panel.protocol.setCurrentIndex(1)
    panel.host.setText("192.0.2.42")
    panel.host.editingFinished.emit()
    panel.port.setValue(19601)
    panel.source[1].setValue(5)
    panel.enabled.setChecked(True)
    page.protocol.setCurrentIndex(0)  # Exercise independently saved UDP routing.
    page.port.setValue(19602)
    page.destination[1].setValue(7)
    page.web_panel.port.setValue(8766)
    page.web_panel.interval.setCurrentIndex(2)
    assert not startup.data.communication.exists()
    page.save_settings.click()
    until(lambda: not page.busy)
    saved = load_communication_preferences(startup.data.communication)
    assert (
        saved.reporting.enabled
        and saved.reporting.settings.endpoint.host == "192.0.2.42"
    )
    assert saved.reporting.settings.endpoint.protocol == "fins_tcp"
    assert isinstance(saved.reporting.settings.endpoint, FinsTcpEndpoint)
    assert not hasattr(saved.reporting.settings.endpoint, "source")
    assert not hasattr(saved.reporting.settings.endpoint, "destination")
    assert isinstance(saved.diagnostic, FinsUdpEndpoint)
    assert saved.diagnostic.port == 19602 and saved.diagnostic.destination.node == 7
    assert saved.web == WebPreferences(False, "127.0.0.1", 8766, 2000)
    assert {p: p.read_bytes() for p in files} == files
    assert startup.application.snapshot() == baseline

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Settings restoration must not create a PLC client")

    monkeypatch.setattr(
        "capsule_vision.communication.endpoints.FinsUdpClient", forbidden
    )
    window.close()
    until(lambda: window.bridge.closed and window.communication.closed)
    restored = load_startup(startup.data)
    delivery = FutureDelivery()
    second = create_window(restored, delivery)
    try:
        restored_panel = second.communication.reporting_panel
        assert restored_panel is not None
        assert restored_panel.controller.reporting_selection() == saved.reporting
        assert restored_panel.controller.reporting_snapshot() is None
        assert second.communication.port.value() == 19602
        assert second.communication.web_panel.preferences() == saved.web
        assert not second.communication.web_panel.available
        assert second.communication.last_response is None
        snapshot = restored.application.snapshot()
        assert snapshot.counters == ProductionCounters() and snapshot.generation == 0
        assert snapshot.latest_production_result is None and not snapshot.runtime_owned
    finally:
        close_window(second)
        delivery.detach()


def test_invalid_web_bind_and_atomic_failure_keep_previous_settings(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    page.save_settings.click()
    until(lambda: not page.busy)
    before = startup.data.communication.read_bytes()
    page.web_panel.host.setText("0.0.0.0")
    page.save_settings.click()
    until(lambda: not page.busy)
    assert startup.data.communication.read_bytes() == before
    assert (
        page.preferences_status.text() and "IPv4" in page.preferences_status.toolTip()
    )
    page.web_panel.host.setText("192.0.2.10")
    monkeypatch.setattr("capsule_vision.web.hosts.local_interfaces", lambda: ())
    page.save_settings.click()
    until(lambda: not page.busy)
    # Preserve unavailable saved IPs; assignment is checked by actual startup.
    assert (
        load_communication_preferences(startup.data.communication).web.host
        == "192.0.2.10"
    )

    def denied(*args: object) -> None:
        raise PermissionError("controlled save failure")

    before = startup.data.communication.read_bytes()
    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    page.web_panel.port.setValue(9000)
    page.save_settings.click()
    until(lambda: not page.busy)
    assert startup.data.communication.read_bytes() == before
    assert "controlled save failure" in page.preferences_status.toolTip()


def test_invalid_file_is_preserved_visible_and_recovers_without_network(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    data.communication.write_text('{"schema_version":99}', encoding="utf-8")
    before = data.communication.read_bytes()
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert data.communication.read_bytes() == before
        assert startup.communication_error
        assert not startup.communication.web.enabled
        assert not startup.communication.reporting.enabled
        assert (
            startup.communication_error
            in window.communication.preferences_status.toolTip()
        )
        assert window.communication.reporting_panel is not None
        assert (
            window.communication.reporting_panel.controller.reporting_snapshot() is None
        )
        window.communication.save_settings.click()  # Explicit repair; no silent overwrite.
        until(lambda: not window.communication.busy)
        assert (
            load_communication_preferences(data.communication) == startup.communication
        )
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_missing_preferences_are_unsaved_disabled_and_source_independent(
    qapp: QApplication, tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sockets: list[bool] = []

    def unexpected_socket(*args: object, **kwargs: object) -> None:
        sockets.append(True)
        raise AssertionError("Initial communication defaults must not open sockets")

    monkeypatch.setattr(socket, "socket", unexpected_socket)
    data = saved_data(tmp_path)
    sources = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources,
        replace(
            sources,
            mode=mode,
            cameras=tuple(
                replace(camera, basler_serial=str(12340000 + index))
                for index, camera in enumerate(sources.cameras)
            ),
        ),
    )
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert not startup.communication_error and not data.communication.exists()
        assert startup.communication == CommunicationPreferences()
        assert not startup.communication.reporting.enabled
        assert not startup.communication.web.enabled
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.communication.diagnostic == FinsTcpEndpoint()
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None
        assert panel.controller.reporting_selection() == startup.communication.reporting
        assert panel.selection().settings.endpoint == FinsTcpEndpoint()
        assert panel.protocol.currentText() == page.protocol.currentText() == "FINS/TCP"
        assert panel.port.value() == page.port.value() == 9600
        assert panel.host.text() == page.host.text() == "127.0.0.1"
        assert not sockets
        assert panel.controller.reporting_snapshot() is None
        assert not page.web_panel.saved_available and not page.dirty
        assert page.preferences_status.text() == text(
            "comm_settings_defaults", window.language
        )
        assert page.save_settings.isEnabled() and not page.discard_settings.isEnabled()
        panel.enabled.setChecked(True)
        assert page.dirty and not panel.controller.reporting_selection().enabled
        page.reload_settings()  # No saved file exists to discard to.
        assert page.dirty and panel.enabled.isChecked()
        assert page.preferences_status.text() == text(
            "comm_reload_failed", window.language
        )
        panel.enabled.setChecked(False)
        page.persist_settings()  # Explicitly establish neutral disabled settings.
        until(lambda: not page.busy)
        payload = data.communication.read_bytes()
        assert json.loads(payload) == {
            "reporting": {"protocol": "fins_tcp", "mode": "four_cycle"},
            "diagnostic": {"protocol": "fins_tcp"},
        }
        assert not page.dirty and page.web_panel.saved_available
        assert page.preferences_status.text() == text("saved", window.language)
        assert not page.preferences_status.toolTip()
        window.close()
        until(lambda: window.bridge.closed and window.communication.closed)
        restored = load_startup(data)
        try:
            assert restored.communication == CommunicationPreferences()
            assert not restored.communication_error
            assert data.communication.read_bytes() == payload
        finally:
            restored.application.close().result(5)
            assert restored.application.join(5)
    finally:
        close_window(window)
        delivery.detach()
    assert not sockets


@pytest.mark.parametrize("protocol", [None, "fins_udp", "fins_tcp", "modbus_tcp"])
def test_existing_preferences_keep_protocol_in_both_panels(
    qapp: QApplication, tmp_path: Path, protocol: str | None
) -> None:
    data = ApplicationData.at_entry(tmp_path)
    data.communication.parent.mkdir(parents=True)
    raw = (
        {section: {"protocol": protocol} for section in ("reporting", "diagnostic")}
        if protocol is not None
        else {}
    )
    data.communication.write_text(json.dumps(raw), encoding="utf-8")
    original = data.communication.read_bytes()
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        page, panel = window.communication, window.communication.reporting_panel
        assert panel is not None
        expected = {"fins_udp": 0, "fins_tcp": 1, "modbus_tcp": 2}[
            protocol or "fins_udp"
        ]
        assert panel.protocol.currentIndex() == page.protocol.currentIndex() == expected
        assert panel.selection() == startup.communication.reporting
        assert page.diagnostic_selection() == startup.communication.diagnostic
        assert not panel.controller.reporting_selection().enabled
        assert panel.controller.reporting_snapshot() is None
        assert data.communication.read_bytes() == original
    finally:
        close_window(window)
        delivery.detach()


def test_normal_cli_help_and_retired_operator_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(tmp_path, ["--help"])
    assert help_exit.value.code == 0
    assert "--web" not in capsys.readouterr().out
    for flag in (
        "--web-snapshots",
        "--no-web-snapshots",
        "--web-host",
        "--web-port",
        "--web-interval-ms",
    ):
        with pytest.raises(SystemExit) as removed:
            main(tmp_path, [flag])
        assert removed.value.code == 2


def test_drafts_do_not_redirect_diagnostic_writes_or_reporting(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    page.persist_settings()
    until(lambda: not page.busy)
    saved = load_communication_preferences(startup.data.communication)
    requests: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        return CommunicationResponse(request, None, 1, datetime.now(UTC))

    monkeypatch.setattr(page, "_execute", execute)
    panel.enabled.setChecked(True)
    panel.port.setValue(19601)
    page.port.setValue(19602)
    assert page.dirty and page.discard_settings.isEnabled()
    assert panel.controller.reporting_selection() == saved.reporting
    assert "9600" in page.manual_endpoint.text()
    assert page.dispatch(RequestKind.WRITE)
    assert "Writing" in page.status.text()
    until(lambda: not page.busy)
    assert requests[-1].endpoint.port == 9600
    page.persist_settings()
    until(lambda: not page.busy)
    assert panel.controller.reporting_selection().enabled
    assert panel.controller.reporting_selection().settings.endpoint.port == 19601
    assert not page.dirty and not page.discard_settings.isEnabled()
    assert page.dispatch(RequestKind.WRITE)
    until(lambda: not page.busy)
    assert requests[-1].endpoint.port == 19602
    assert not panel.controller.reporting_snapshot()


def test_authenticated_save_marks_existing_watch_rows_stale_immediately(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, window = settings_window
    page = window.communication
    window.set_language("en")
    requests: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        return CommunicationResponse(request, (72,), 1, datetime.now(UTC))

    monkeypatch.setattr(page, "_execute", execute)
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100))
    assert page.read_watch_once()
    until(lambda: not page.busy)
    observed = page.watch_table.item(0, 4)
    assert observed is not None
    stamp = observed.text()
    page.port.setValue(19602)
    page.persist_settings()
    until(lambda: not page.busy)
    assert len(requests) == 1
    assert page.watch_status.accessibleName() == text("comm_watch_stale", "en")
    group = page.watch_table.item(0, 0)
    retained = page.watch_table.item(0, 4)
    assert group is not None and not group.icon().isNull()
    assert retained is not None and retained.text() == stamp


def test_failed_save_preserves_draft_file_and_effective_reporting(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    page.persist_settings()
    until(lambda: not page.busy)
    before = startup.data.communication.read_bytes()
    original = panel.controller.reporting_selection()
    panel.enabled.setChecked(True)
    panel.port.setValue(19601)

    def denied(*args: object) -> None:
        raise PermissionError("controlled persistence failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    page.persist_settings()
    until(lambda: not page.busy)
    assert startup.data.communication.read_bytes() == before
    assert panel.controller.reporting_selection() is original
    assert panel.enabled.isChecked() and panel.port.value() == 19601 and page.dirty


def test_discard_confirmation_and_reload_actual_persisted_definitions(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    page.persist_settings()
    until(lambda: not page.busy)
    saved = load_communication_preferences(startup.data.communication)
    page.port.setValue(19600)
    page.request_discard()
    dialog = page.findChild(QMessageBox)
    assert dialog is not None
    next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) is QMessageBox.ButtonRole.RejectRole
    ).click()
    QApplication.processEvents()
    assert page.port.value() == 19600 and page.dirty
    # Another valid writer has changed the file since the page loaded it.
    actual = replace(
        saved,
        diagnostic=replace(saved.diagnostic, port=19610),
        web=WebPreferences(enabled=False),
    )
    save_communication_preferences(startup.data.communication, actual)
    page.request_discard()
    dialog = next(d for d in page.findChildren(QMessageBox) if d.isVisible())
    next(
        b
        for b in dialog.buttons()
        if dialog.buttonRole(b) is QMessageBox.ButtonRole.AcceptRole
    ).click()
    until(lambda: not page.dirty)
    assert page.port.value() == 19610 and not page.web_panel.saved.enabled
    assert load_communication_preferences(startup.data.communication) == actual
    assert page.web_panel.saved == actual.web and not page.discard_settings.isEnabled()
    assert startup.application.snapshot().counters == ProductionCounters()


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_reload_failure_preserves_entire_draft(
    settings_window: tuple[Startup, MainWindow],
    failure: str,
) -> None:
    startup, window = settings_window
    page = window.communication
    page.persist_settings()
    until(lambda: not page.busy)
    page.port.setValue(19600)
    page.web_panel.port.setValue(8766)
    panel = page.reporting_panel
    assert panel is not None
    panel.enabled.setChecked(True)
    effective = panel.controller.reporting_selection()
    if failure == "missing":
        startup.data.communication.unlink()
    elif failure == "invalid":
        startup.data.communication.write_text('{"unexpected":true}', encoding="utf-8")
    page.reload_settings()
    assert page.port.value() == 19600 and page.web_panel.port.value() == 8766
    assert panel.enabled.isChecked() and page.dirty
    assert panel.controller.reporting_selection() is effective
    assert page.preferences_status.toolTip()


def test_discard_does_not_undo_observed_write(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    page.persist_settings()
    until(lambda: not page.busy)
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        return CommunicationResponse(request, None, 1, datetime.now(UTC))

    monkeypatch.setattr(page, "_execute", execute)
    assert page.dispatch(RequestKind.WRITE)
    until(lambda: not page.busy)
    observed = page.last_response
    page.port.setValue(19600)
    page.reload_settings()
    assert page.port.value() == 9600 and not page.dirty
    assert len(calls) == 1 and page.last_response is observed
    assert startup.application.history is not None
    assert not startup.application.history.query().result(5).records
    assert page.success.text().startswith(text("comm_write_ok", page.language))
    assert page.status.isHidden()


@pytest.mark.parametrize("size", [(1080, 1857), (1300, 1017), (1920, 1017)])
def test_first_navigation_uses_settled_geometry_then_resize_and_maximize(
    qapp: QApplication, tmp_path: Path, size: tuple[int, int]
) -> None:
    ensure_test_fonts()
    startup = load_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.set_language("en")
    window.resize(*size)  # Initial size, before the first show; no resize workaround.
    try:
        window.show()
        qapp.processEvents()  # Production is visible; Communication is still hidden.
        window.navigate(2)
        qapp.processEvents()
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None
        initial_position = (
            (0, 1) if page.service_layout.itemAtPosition(0, 1) is not None else (1, 0)
        )
        assert page.service_layout.itemAtPosition(*initial_position) is not None
        if size[0] in (1080, 1920):
            assert initial_position == ((0, 1) if size[0] == 1920 else (1, 0))
        assert page.body.width() <= page.scroll_area.viewport().width()
        assert panel.title.geometry().right() < panel.status_indicator.x()
        assert panel.status_indicator.geometry().right() < panel.service_status.x()
        assert panel.service_status.y() < panel.host.y()
        assert panel.host.isVisible() and panel.host.text() == "127.0.0.1"
        assert panel.port.accessibleName() == text("comm_port", window.language)
        for control in (panel.enabled, panel.port, panel.timeout):
            assert control.width() >= control.minimumSizeHint().width()
        assert page.save_settings.isVisible() and page.discard_settings.isVisible()
        assert window.event_frame.height() == 44

        window.resize(*((1080, 1857) if initial_position == (0, 1) else (1920, 1017)))
        opposite_position = (1, 0) if initial_position == (0, 1) else (0, 1)
        until(
            lambda: page.service_layout.itemAtPosition(*opposite_position) is not None
        )
        qapp.processEvents()
        panel = page.reporting_panel
        assert panel is not None
        assert panel.feedback.isHidden()
        window.showMaximized()
        qapp.processEvents()
        # Maximization uses this platform's available geometry (offscreen/native
        # screens differ). Either arrangement must contain both complete cards.
        assert page.body.width() <= page.scroll_area.viewport().width()
        if page.service_layout.itemAtPosition(0, 1) is not None:
            assert panel.geometry().right() < page.web_panel.x()
            assert panel.width() >= panel.sizeHint().width()
            assert page.web_panel.width() >= page.web_panel.sizeHint().width()
        else:
            assert panel.width() == page.web_panel.width() == page.service_cards.width()
            assert panel.geometry().bottom() < page.web_panel.y()
        assert panel.feedback.isHidden() and not hasattr(panel, "summary")
        if size[0] == 1300:
            # Expanded controls must fit the viewport after first navigation,
            # language changes and shrinking from a previously wide grid.
            panel.protocol.setCurrentIndex(0)
            page.protocol.setCurrentIndex(0)
            panel.advanced_button.click()
            page.advanced_button.click()
            page.watch_editor_button.click()
            page.manual_button.click()
            assert panel.advanced.isVisible() and page.advanced.isVisible()
            before = startup.application.snapshot()
            for width in (1300, 1920, 1080, 1300):
                window.showNormal()
                window.resize(width, 1017)
                for language in ("zh", "en"):
                    window.set_language(language)
                    qapp.processEvents()
                    available = page.scroll_area.viewport().width()
                    assert page.body.width() <= available
                    position = (
                        (
                            (0, 1)
                            if page.service_layout.itemAtPosition(0, 1) is not None
                            else (1, 0)
                        )
                        if width == 1300
                        else ((0, 1) if width == 1920 else (1, 0))
                    )
                    until(
                        lambda pos=position: (
                            page.service_layout.itemAtPosition(*pos) is not None
                        )
                    )
                    assert page.body.width() <= available, (
                        width,
                        language,
                        available,
                        page.service_cards.width(),
                        panel.minimumSizeHint(),
                        page.web_panel.minimumSizeHint(),
                    )
                    for control in page.body.findChildren(QWidget):
                        if control.isVisible() and isinstance(
                            control,
                            (
                                QAbstractButton,
                                QAbstractSpinBox,
                                QComboBox,
                                QLineEdit,
                                QLabel,
                            ),
                        ):
                            point = control.mapTo(page.body, QPoint())
                            parent = control.parentWidget()
                            assert parent is not None
                            assert parent.rect().contains(control.geometry()), (
                                control.accessibleName(),
                                control.geometry(),
                                parent.rect(),
                            )
                            if not isinstance(control, QLabel) and not isinstance(
                                parent, (QAbstractSpinBox, QComboBox)
                            ):
                                assert control.height() >= Style.touch
                            assert (
                                0 <= point.x()
                                and point.x() + control.width() <= available
                            ), (control.accessibleName(), control.geometry(), available)
                    if width in (1300, 1920) and language == "en":
                        page.grab()
            assert startup.application.snapshot() == before
    finally:
        close_window(window)
        delivery.detach()


def test_multilingual_communication_actions_preserve_slots_and_full_meaning(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    before = startup.application.snapshot()
    selected = panel.controller.reporting_selection()
    buttons = (
        panel.enabled,
        page.web_panel.power,
        page.save_settings,
        page.watch_editor_button,
        page.watch_remove,
        page.watch_once,
        page.watch_auto,
    )
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "Watch group " * 5)
    )
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.DM, 32767, 1, "Other group")
    )
    group = page.watch_groups
    requests: list[CommunicationRequest] = []
    completed = datetime(2026, 10, 14, 13, 22, 35, 123456, tzinfo=UTC)

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        return CommunicationResponse(request, (1234,) * request.count, 1, completed)

    monkeypatch.setattr(page, "_execute", execute)
    page.watch_once.click()
    until(lambda: not page.busy)
    ordered = [(RequestKind.READ, item.area, item.start, item.count) for item in group]
    assert [(r.kind, r.area, r.start, r.count) for r in requests] == ordered
    expected_time = completed.astimezone().strftime("%H:%M:%S.%f")[:-3]
    assert page.watch_table.columnCount() == 5 and page.watch_table.rowCount() == 5
    action_sizes = tuple(button.size() for button in buttons[4:])
    slots: dict[int, tuple[QRect, ...]] = {}
    dimensions: dict[int, tuple[QRect, ...]] = {}
    columns: dict[int, tuple[int, ...]] = {}
    for button, keys in (
        (panel.enabled, ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off")),
        (
            page.web_panel.power,
            ("plc_enable", "plc_disable", "web_turn_on", "web_turn_off"),
        ),
        (page.save_settings, ("save",)),
        (
            page.watch_once,
            (
                "comm_watch_remove",
                "comm_watch_once_short",
                "comm_watch_auto_short",
                "comm_watch_stop_short",
            ),
        ),
    ):
        caption_width = ceil(
            max(
                QFontMetricsF(button.font()).horizontalAdvance(text(key, language))
                for key in keys
                for language in LANGUAGES
            )
        )
        assert button.width() <= (
            caption_width
            + button.iconSize().width()
            + Style.icon_text_gap
            + Style.space
        )
        assert button.height() >= Style.touch
    for width, height in ((1080, 1920), (1920, 1080), (1080, 1920)):
        window.resize(width, height)
        QApplication.processEvents()
        # Reflow follows the actual compact group widths, not a resolution threshold.
        until(
            lambda: (
                page.watch_tools.rect().contains(page.watch_actions.geometry())
                and page.watch_actions.y() == 0
                and page.watch_selection.height() == Style.touch
                and page.watch_actions.height() == Style.touch
                and page.watch_selection.y() == 0
                and (
                    (
                        page.watch_actions.y() == Style.touch + Style.small
                        and page.watch_selection.geometry().bottom()
                        < page.watch_actions.geometry().top()
                    )
                    or (
                        page.watch_actions.y() == 0
                        and page.watch_selection.geometry().right()
                        < page.watch_actions.geometry().left()
                    )
                )
            )
        )
        sizes = tuple(button.size() for button in buttons)
        assert tuple(button.size() for button in buttons[4:]) == action_sizes
        assert page.watch_actions.width() == 3 * page.watch_once.width() + 2 * (
            Style.small // 2
        )
        assert (
            page.watch_remove.size() == page.watch_once.size() == page.watch_auto.size()
        )
        geometry = tuple(
            QRect(button.mapTo(page.watch_tools, QPoint()), button.size())
            for button in buttons[3:]
        )
        for language in LANGUAGES:
            window.set_language(language)
            QApplication.processEvents()
            assert tuple(button.size() for button in buttons) == sizes
            assert geometry == tuple(
                QRect(button.mapTo(page.watch_tools, QPoint()), button.size())
                for button in buttons[3:]
            ), language
            assert panel.enabled.size() == page.web_panel.power.size()
            for button in (
                page.watch_editor_button,
                page.watch_add,
                page.advanced_button,
                panel.advanced_button,
            ):
                assert button.text() == "" and not button.icon().isNull()
                assert button.width() == button.height() == Style.touch
                assert button.accessibleName() in button.toolTip()
            assert_full_caption(page.manual_button, text("comm_registers", language))
            assert page.manual_button.toolTip() == text("comm_registers", language)
            for button in buttons[:2]:
                assert_full_caption(button, button.accessibleName())
                assert button.accessibleName() in button.toolTip()
            assert_full_caption(page.save_settings, text("save", language))
            assert (
                page.save_settings.accessibleName()
                == page.save_settings.toolTip()
                == text("comm_save_settings", language)
            )
            assert not page.save_settings.icon().isNull()
            assert panel.title.text() == text("short_reporting", language)
            assert panel.merge_label.text() == text("plc_merge_count", language)
            assert (
                panel.merge_label.fontMetrics().horizontalAdvance(
                    panel.merge_label.text()
                )
                <= panel.merge_label.contentsRect().width()
            )
            assert (
                panel.title.toolTip()
                == panel.title.accessibleName()
                == text("plc_reporting", language)
            )
            assert panel.enabled.accessibleName() == text("plc_enable", language)
            assert page.web_panel.title.text() == "Web"
            assert page.web_panel.title.toolTip() == text("web_observer", language)
            assert text("web_boundary", language) in page.web_panel.host.toolTip()
            assert text("web_boundary", language) not in page.web_panel.actual.toolTip()
            for label in page.findChildren(QLabel):
                if label.accessibleName() == text("comm_timeout", language):
                    assert label.text() == text("short_timeout", language) + " · s"
                    assert label.toolTip() == text("comm_timeout", language)
            assert text("target", language) in page.manual_endpoint.text()
            assert (
                text("comm_effective_endpoint", language)
                in page.manual_endpoint.toolTip()
            )
            assert_full_caption(page.watch_remove, text("comm_watch_remove", language))
            assert_full_caption(
                page.watch_once, text("comm_watch_once_short", language)
            )
            assert_full_caption(
                page.watch_auto, text("comm_watch_auto_short", language)
            )
            for button in buttons[4:]:
                assert not button.icon().isNull()
                assert button.height() == Style.touch
                assert button.iconSize() == page.watch_editor_button.iconSize()
            assert "1" in page.watch_auto.text()
            for button, key in (
                (page.watch_remove, "comm_watch_remove"),
                (page.watch_once, "comm_watch_once"),
                (page.watch_auto, "comm_watch_auto"),
            ):
                assert button.accessibleName() == text(key, language)
                assert text(key, language) in button.toolTip()
                assert text("comm_watch", language) in button.toolTip()
                assert button.accessibleDescription() == button.toolTip()
            page.watch_selector.setCurrentIndex(1 - page.watch_selector.currentIndex())
            assert tuple(button.size() for button in buttons) == sizes
            assert (
                page.watch_selector.currentText()
                == page.watch_selector.toolTip()
                == page.watch_selector.accessibleDescription()
            )
            assert page.watch_actions.y() == 0
            assert geometry == tuple(
                QRect(button.mapTo(page.watch_tools, QPoint()), button.size())
                for button in buttons[3:]
            )
            editor_rect = QRect(
                page.watch_editor_button.mapTo(page.watch_tools, QPoint()),
                page.watch_editor_button.size(),
            )
            remove_rect = QRect(
                page.watch_remove.mapTo(page.watch_tools, QPoint()),
                page.watch_remove.size(),
            )
            assert (
                editor_rect.bottom() < remove_rect.top()
                or editor_rect.right() < remove_rect.left()
            )
            assert page.body.width() <= page.scroll_area.viewport().width()
            assert page.watch_table.horizontalScrollBar().maximum() == 0
            widths = [page.watch_table.columnWidth(i) for i in range(5)]
            assert widths[0] < page.watch_table.width() / 4
            assert sum(widths) == page.watch_table.viewport().width()
            assert widths[4] >= max(widths[:4])
            assert (
                widths[4]
                >= page.watch_table.fontMetrics().horizontalAdvance(expected_time) + 8
            )
            for row in range(page.watch_table.rowCount()):
                item = page.watch_table.item(row, 4)
                assert item is not None and item.text() == expected_time
            header = page.watch_table.horizontalHeader()
            for column in range(5):
                item = page.watch_table.horizontalHeaderItem(column)
                assert item is not None
                option = QStyleOptionHeader()
                header.initStyleOptionForIndex(option, column)
                option.rect = QRect(0, 0, header.sectionSize(column), header.height())
                contents = header.style().subElementRect(
                    QStyle.SubElement.SE_HeaderLabel, option, header
                )
                assert (
                    header.fontMetrics().horizontalAdvance(item.text())
                    <= contents.width()
                ), (language, column)
            assert page.watch_status.text().startswith("OK · ")
            assert not page.watch_status.toolTip()
            assert text("comm_watch_ok", language) == page.watch_status.accessibleName()
        slots[width] = geometry
        dimensions[width] = tuple(button.rect() for button in buttons)
        columns[width] = tuple(page.watch_table.columnWidth(i) for i in range(5))
    assert len(requests) == len(group)

    # Complete one real automatic pass, then hold only its cadence signal while
    # inspecting translations. Dedicated watch tests exercise subsequent ticks.
    timers = [
        timer
        for timer in page.findChildren(QTimer)
        if timer.parent() is page and timer.isSingleShot() and timer.interval() == 1000
    ]
    assert len(timers) == 1
    panel.enabled.click()
    assert panel.controller.reporting_selection() == selected
    with QSignalBlocker(timers[0]):
        page.watch_auto.click()
        until(lambda: not page.busy)
        assert [(r.kind, r.area, r.start, r.count) for r in requests] == ordered * 2
        for width, height in ((1080, 1920), (1920, 1080), (1080, 1920)):
            window.resize(width, height)
            for language in LANGUAGES:
                window.set_language(language)
                QApplication.processEvents()
                assert page.auto_refresh and page.watch_auto.isChecked()
                assert_full_caption(panel.enabled, text("plc_disable", language))
                assert_full_caption(
                    page.watch_auto, text("comm_watch_stop_short", language)
                )
                assert page.watch_auto.text().endswith("1s")
                assert page.watch_auto.accessibleName() == text(
                    "comm_watch_stop", language
                )
                assert text("comm_watch_stop", language) in page.watch_auto.toolTip()
                assert text("comm_watch", language) in page.watch_auto.toolTip()
                assert (
                    page.watch_auto.accessibleDescription() == page.watch_auto.toolTip()
                )
                assert (
                    tuple(
                        QRect(button.mapTo(page.watch_tools, QPoint()), button.size())
                        for button in buttons[3:]
                    )
                    == slots[width]
                ), language
                assert tuple(button.rect() for button in buttons) == dimensions[width]
                assert (
                    tuple(page.watch_table.columnWidth(i) for i in range(5))
                    == columns[width]
                )
        page.watch_auto.click()
        assert not page.auto_refresh and not page.watch_auto.isChecked()
        assert_full_caption(
            page.watch_auto, text("comm_watch_auto_short", window.language)
        )
        assert page.watch_auto.accessibleName() == text(
            "comm_watch_auto", window.language
        )
    panel.enabled.click()
    assert len(requests) == 2 * len(group) and page.watch_groups == group
    assert page.watch_selector is page.watch_selection.findChild(QComboBox)
    assert buttons == (
        panel.enabled,
        page.web_panel.power,
        page.save_settings,
        page.watch_editor_button,
        page.watch_remove,
        page.watch_once,
        page.watch_auto,
    )
    assert startup.application.snapshot() == before and not page.dirty


def test_watch_toolbar_wraps_only_when_client_space_is_insufficient(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    before = startup.application.snapshot()
    window.resize(1080, 1920)
    window.navigate(2)
    until(
        lambda: (
            page.watch_actions.y() == 0
            and page.watch_selection.geometry().right() < page.watch_actions.x()
        )
    )
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "Long group name " * 4)
    )
    groups = page.watch_groups
    buttons = (page.watch_remove, page.watch_once, page.watch_auto)
    sizes = tuple(button.size() for button in buttons)
    selection_minimum = page.watch_selection.minimumSizeHint().width()
    actions_minimum = page.watch_actions.minimumSizeHint().width()
    required = selection_minimum + actions_minimum + Style.small
    assert page.watch_tools.width() >= required
    maximum = page.watch_tools.maximumWidth()
    # Model reduced client space without changing the supported window width.
    constrained = (max(selection_minimum, actions_minimum) + required) // 2
    try:
        page.watch_tools.setMaximumWidth(constrained)
        until(
            lambda: (
                page.watch_tools.width() <= constrained
                and page.watch_actions.y() == Style.touch + Style.small
            )
        )
        geometry = tuple(button.geometry() for button in buttons)
        for language in LANGUAGES:
            window.set_language(language)
            QApplication.processEvents()
            assert page.watch_tools.width() < required
            assert page.watch_selection.geometry().bottom() < page.watch_actions.y()
            assert tuple(button.size() for button in buttons) == sizes
            assert tuple(button.geometry() for button in buttons) == geometry
            assert page.watch_selection.y() == 0
            assert page.watch_actions.y() == Style.touch + Style.small
            for widget in (page.watch_selection, page.watch_actions):
                assert page.watch_tools.rect().contains(widget.geometry())
            assert (
                page.watch_selector.currentText()
                == page.watch_selector.toolTip()
                == page.watch_selector.accessibleDescription()
            )
            assert page.body.width() <= page.scroll_area.viewport().width()
            assert page.watch_table.horizontalScrollBar().maximum() == 0
    finally:
        page.watch_tools.setMaximumWidth(maximum)
    until(
        lambda: (
            page.watch_actions.y() == 0
            and page.watch_selection.geometry().right() < page.watch_actions.x()
        )
    )
    assert page.watch_groups == groups
    assert tuple(button.size() for button in buttons) == sizes
    assert startup.application.snapshot() == before


def test_reporting_power_edits_only_draft_until_save_or_discard(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    page.persist_settings()
    until(lambda: not page.busy)
    original = load_communication_preferences(startup.data.communication)
    web_runtime = page.web_panel.runtime
    assert not panel.enabled.isChecked() and panel.enabled.isCheckable()
    panel.enabled.click()
    assert panel.enabled.isChecked() and page.dirty
    assert panel.enabled.text() == text("plc_disable", window.language)
    assert load_communication_preferences(startup.data.communication) == original
    assert not panel.controller.reporting_selection().enabled
    assert panel.service_status.text() == text("service_off", window.language)
    assert panel.service_status.accessibleName() == text(
        "plc_disabled", window.language
    )
    page.reload_settings()
    assert not panel.enabled.isChecked() and not page.dirty
    panel.enabled.click()
    page.persist_settings()
    until(lambda: not page.busy)
    assert load_communication_preferences(startup.data.communication).reporting.enabled
    assert panel.service_status.text() == text("service_next_start", window.language)
    assert panel.service_status.accessibleName() == text("plc_enabled", window.language)
    panel.enabled.click()
    assert page.dirty and panel.controller.reporting_selection().enabled
    page.reload_settings()
    assert panel.enabled.isChecked() and not page.dirty
    assert panel.controller.reporting_snapshot() is None
    assert page.web_panel.runtime is web_runtime
    assert startup.application.snapshot().counters == ProductionCounters()


@pytest.mark.parametrize("language,width", [("zh", 1080), ("en", 1920)])
def test_modbus_reporting_required_base_save_discard_and_protocol_fields(
    settings_window: tuple[Startup, MainWindow],
    language: Language,
    width: int,
) -> None:
    startup, window = settings_window
    window.resize(width, 1080)
    window.set_language(language)
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None and panel.protocol.count() == 3
    panel.merge_count.setCurrentIndex(0)  # Modbus remains a per-cycle protocol.
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 502 and panel.base_address.value() == -1
    assert (
        panel.base_address.prefix() == "HR " and panel.base_address.maximum() == 65532
    )
    assert panel.base_address.specialValueText() == text(
        "plc_modbus_base_required", language
    )
    page.persist_settings()  # Disabled, explicitly incomplete definition is valid.
    until(lambda: not page.busy)
    saved = startup.data.communication
    before = saved.read_bytes()
    assert not panel.controller.reporting_selection().enabled
    assert panel.controller.reporting_selection().settings.base_address is None
    panel.enabled.click()
    page.persist_settings()
    until(lambda: not page.busy)
    assert saved.read_bytes() == before and page.dirty
    assert text("comm_settings_failed", language) in page.preferences_status.text()
    panel.base_address.setValue(0)
    panel.advanced_button.click()
    assert panel.modbus_advanced.isVisible() and not panel.advanced.isVisible()
    panel.unit_id.setValue(7)
    page.persist_settings()
    until(lambda: not page.busy)
    selected = panel.controller.reporting_selection()
    assert selected == ReportingSelection(
        True, ReportingSettings(base_address=0, endpoint=ModbusSettings(unit_id=7))
    )
    assert not page.dirty and "next" in text("plc_next_start", "en").lower()
    assert json.loads(saved.read_text(encoding="utf-8"))["reporting"] == {
        "protocol": "modbus_tcp",
        "enabled": True,
        "base_address": 0,
        "unit_id": 7,
    }
    panel.base_address.setValue(200)
    panel.unit_id.setValue(8)
    assert panel.controller.reporting_selection() == selected and page.dirty
    page.reload_settings()
    assert (
        panel.base_address.value() == 0
        and panel.unit_id.value() == 7
        and not page.dirty
    )
    panel.protocol.setCurrentIndex(1)
    assert panel.port.value() == 9600 and panel.base_address.prefix() == "W"
    assert panel.base_address.value() == 49 and not panel.modbus_advanced.isVisible()
    assert not panel.advanced.isVisible() and not panel.advanced_button.isVisible()
    page.persist_settings()
    until(lambda: not page.busy)
    panel.port.setValue(1502)
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 1502 and panel.base_address.value() == -1
    panel.base_address.setValue(65532)
    panel.unit_id.setValue(255)
    page.persist_settings()
    until(lambda: not page.busy)
    assert (
        load_communication_preferences(saved).reporting.settings.endpoint.port == 1502
    )
    window.set_language("en" if language == "zh" else "zh")
    until(
        lambda: all(
            panel.rect().contains(field.geometry())
            for field in (
                panel.protocol,
                panel.host,
                panel.port,
                panel.timeout,
                panel.base_address,
            )
        )
    )
    panel.update_state(ApplicationState.RUNNING)
    assert all(
        not field.isEnabled()
        for field in (
            panel.protocol,
            panel.host,
            panel.port,
            panel.base_address,
            panel.unit_id,
            panel.enabled,
        )
    )
    panel.base_address.setValue(100)
    assert panel.base_address.value() == 65532
    panel.update_state(ApplicationState.READY)


def test_reporting_explicit_default_looking_port_survives_protocol_change(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    _, window = settings_window
    panel = window.communication.reporting_panel
    assert panel is not None
    panel.port.editingFinished.emit()  # Merely leaving the field is not an edit.
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 502
    panel.protocol.setCurrentIndex(0)
    edit = panel.port.lineEdit()
    assert edit is not None
    edit.selectAll()
    QTest.keyClicks(edit, "9600")
    QTest.keyClick(edit, Qt.Key.Key_Tab)
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 9600
    panel.port.setValue(502)
    panel.protocol.setCurrentIndex(0)
    assert panel.port.value() == 502
