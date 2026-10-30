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
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QStyle,
    QStyleOptionHeader,
    QWidget,
)
from tests.support.qt import assert_full_caption, close_window, ensure_test_fonts, until
from tests.support.startup import saved_data

from capsule_vision.application import ApplicationState, ProductionCounters
from capsule_vision.application.startup import Startup, load_startup
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
from capsule_vision.ui.style import Style
from capsule_vision.web import ProductionSnapshotService
from capsule_vision.web.runtime import WebRuntime, WebState


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
@pytest.mark.parametrize("modbus", [False, True])
def test_global_reporting_state_and_communication_details(
    settings_window: tuple[Startup, MainWindow],
    monkeypatch: pytest.MonkeyPatch,
    language: Language,
    size: tuple[int, int],
    modbus: bool,
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
            settings=ReportingSettings(base_address=100, endpoint=ModbusSettings()),
        )
        if modbus
        else original
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

    def check(key: str, observation: ReportingSnapshot | None) -> None:
        observed[0] = observation
        window.event_frame.plc.setToolTip("stale diagnostics")
        window.bridge.timer.timeout.emit()
        label = window.event_frame.plc
        assert label.accessibleName() == f"PLC · {text(key, language)}"
        assert "ACK" not in label.text() and "42" not in label.text()
        panel.summary.setToolTip("stale ACK and worker telemetry")
        panel.summary.setAccessibleDescription("stale detail")
        panel.refresh()
        assert not label.toolTip() and not panel.service_status.toolTip()
        assert not panel.summary.toolTip() and not panel.summary.accessibleDescription()
        assert panel.service_status.accessibleName()
        expected_ack = (
            observation.last_acknowledged.sequence
            if observation and observation.last_acknowledged
            else "--"
        )
        assert f"ACK {expected_ack}" in panel.summary.text()
        assert controller.reporting_snapshot() == observation
        assert startup.application.snapshot() == before

    try:
        check("service_off", None)
        controller.set_reporting_selection(replace(selected, enabled=True))
        check("service_next_start", None)
        check("service_next_start", report)
        live = replace(report, thread_alive=True, accepting=True)
        check("web_running", live)
        check("web_running", replace(live, last_acknowledged=replace(ack, sequence=43)))
        controller.set_reporting_selection(replace(selected, enabled=False))
        check("web_running", live)
        check("web_running", replace(live, accepting=False))
        error = ReportingIssue("TimeoutError", "uncertain sequence ACK")
        for outcome in (ReportOutcome.FAILED, ReportOutcome.REJECTED):
            failure = replace(ack, outcome=outcome, issue=error)
            check("technical_error", replace(live, last_outcome=failure))
        for field in ("failure_issue", "cleanup_issue"):
            check("technical_error", replace(report, **{field: error}))
        check("service_off", report)
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
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    port = available_port()
    panel.port.setValue(port)
    page.persist_settings()
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
    assert runtime.snapshot().configuration == saved.web  # Save is not Apply.
    assert panel.apply_changes.isVisible() and panel.apply_changes.isEnabled()
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
    until(lambda: not panel.available)
    until(lambda: runtime.snapshot().state is WebState.STOPPED)
    assert runtime.snapshot().state is WebState.STOPPED
    assert panel.open_browser.isHidden()
    stopped = load_communication_preferences(startup.data.communication)
    assert stopped == replace(
        saved, web=replace(saved.web, enabled=False, port=draft_port)
    )
    assert panel.host.text() == "0.0.0.0" and page.port.value() == 19602 and page.dirty
    restored = load_startup(startup.data)
    try:
        assert not restored.communication.web.enabled
    finally:
        restored.application.close().result(5)
        assert restored.application.join(5)
    until(lambda: panel.power.isEnabled())
    panel.power.click()
    until(lambda: panel.available)
    assert runtime.snapshot().configuration == replace(saved.web, port=draft_port)
    assert load_communication_preferences(startup.data.communication).web.enabled
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
    runtime = WebRuntime(
        lambda settings: ProductionSnapshotService(
            startup.application, startup.frames, host=settings.host, port=settings.port
        )
    )
    window.bind_web_runtime(runtime)
    panel.power.click()
    until(lambda: panel.available)
    saved = startup.data.communication.read_bytes()
    actual = runtime.snapshot().configuration
    panel.host.setText("0.0.0.0")
    page.port.setValue(19602)

    def denied(*_args: object) -> None:
        raise PermissionError("controlled power persistence failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    panel.power.click()
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
        panel.port.setValue(
            available_port()
        )  # This draft must never be used as fallback.
        panel.power.click()
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
    if invalid_file:
        assert (
            startup.data.communication.read_text(encoding="utf-8")
            == '{"unexpected":true}'
        )
        assert runtime.snapshot().state is WebState.STOPPED
        assert panel.operation_error and not runtime.snapshot().owned
    else:
        until(lambda: panel.available)
        assert load_communication_preferences(startup.data.communication) == defaults
        assert runtime.snapshot().configuration == defaults.web
    assert panel.port.value() == 19603 and page.port.value() == 19602 and page.dirty


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
    startup = load_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    window.navigate(2)
    try:
        yield startup, window
    finally:
        close_window(window)
        delivery.detach()


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
    assert panel.base_address.parentWidget() is panel
    assert panel.rect().contains(panel.base_address.geometry())
    assert panel.protocol.count() == page.protocol.count() == 3
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
    page.port.setValue(19602)
    page.destination[1].setValue(7)
    page.web_panel.port.setValue(8766)
    page.web_panel.interval.setCurrentIndex(2)
    assert not startup.data.communication.exists()
    page.save_settings.click()
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
    before = startup.data.communication.read_bytes()
    page.web_panel.host.setText("0.0.0.0")
    page.save_settings.click()
    assert startup.data.communication.read_bytes() == before
    assert (
        page.preferences_status.text() and "IPv4" in page.preferences_status.toolTip()
    )
    page.web_panel.host.setText("192.0.2.10")
    monkeypatch.setattr("capsule_vision.web.hosts.local_interfaces", lambda: ())
    page.save_settings.click()
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
    assert startup.data.communication.read_bytes() == before
    assert "controlled save failure" in page.preferences_status.toolTip()


def test_invalid_file_is_preserved_visible_and_recovers_without_network(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    data.communication.write_text('{"schema_version":99}', encoding="utf-8")
    before = data.communication.read_bytes()
    startup = load_startup(data)
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
        assert (
            load_communication_preferences(data.communication) == startup.communication
        )
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("mode", ["folder", "basler"])
def test_missing_preferences_are_unsaved_disabled_and_source_independent(
    qapp: QApplication, tmp_path: Path, mode: str
) -> None:
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
    startup = load_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        assert not startup.communication_error and not data.communication.exists()
        assert startup.communication == CommunicationPreferences()
        assert not startup.communication.reporting.enabled
        assert not startup.communication.web.enabled
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.communication.diagnostic == FinsUdpEndpoint()
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None
        assert panel.controller.reporting_selection() == ReportingSelection()
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
        payload = data.communication.read_bytes()
        assert not page.dirty and page.web_panel.saved_available
        assert page.preferences_status.text() == text("saved", window.language)
        assert not page.preferences_status.toolTip()
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
    assert panel.controller.reporting_selection().enabled
    assert panel.controller.reporting_selection().settings.endpoint.port == 19601
    assert not page.dirty and not page.discard_settings.isEnabled()
    assert page.dispatch(RequestKind.WRITE)
    until(lambda: not page.busy)
    assert requests[-1].endpoint.port == 19602
    assert not panel.controller.reporting_snapshot()


def test_failed_save_preserves_draft_file_and_effective_reporting(
    settings_window: tuple[Startup, MainWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    page.persist_settings()
    before = startup.data.communication.read_bytes()
    original = panel.controller.reporting_selection()
    panel.enabled.setChecked(True)
    panel.port.setValue(19601)

    def denied(*args: object) -> None:
        raise PermissionError("controlled persistence failure")

    monkeypatch.setattr("capsule_vision.configuration._io.os.replace", denied)
    page.persist_settings()
    assert startup.data.communication.read_bytes() == before
    assert panel.controller.reporting_selection() is original
    assert panel.enabled.isChecked() and panel.port.value() == 19601 and page.dirty


def test_discard_confirmation_and_reload_actual_persisted_definitions(
    settings_window: tuple[Startup, MainWindow],
) -> None:
    startup, window = settings_window
    page = window.communication
    page.persist_settings()
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
    assert page.success.text() and page.status.text()


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
        assert panel.summary.y() == panel.service_status.y()
        assert panel.title.geometry().right() < panel.summary.x()
        assert panel.summary.geometry().right() < panel.service_status.x()
        assert panel.summary.y() < panel.host.y()
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
        assert panel.summary.text() == "ACK -- · Successful 0 · ERR 0"
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
        assert panel.summary.text() == "ACK -- · Successful 0 · ERR 0"
        if size[0] == 1300:
            # Expanded controls must fit the viewport after first navigation,
            # language changes and shrinking from a previously wide grid.
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
            assert_full_caption(page.manual_button, text("comm_write", language))
            assert page.manual_button.accessibleName() == text(
                "comm_registers", language
            )
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
    original = load_communication_preferences(startup.data.communication)
    web_runtime = page.web_panel.runtime
    assert not panel.enabled.isChecked() and panel.enabled.isCheckable()
    panel.enabled.click()
    assert panel.enabled.isChecked() and page.dirty
    assert panel.enabled.text() == text("plc_disable", window.language)
    assert load_communication_preferences(startup.data.communication) == original
    assert not panel.controller.reporting_selection().enabled
    assert panel.service_status.text() == "OFF"
    assert panel.service_status.accessibleName() == text(
        "plc_disabled", window.language
    )
    page.reload_settings()
    assert not panel.enabled.isChecked() and not page.dirty
    panel.enabled.click()
    page.persist_settings()
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
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 502 and panel.base_address.value() == -1
    assert (
        panel.base_address.prefix() == "HR " and panel.base_address.maximum() == 65532
    )
    assert panel.base_address.specialValueText() == text(
        "plc_modbus_base_required", language
    )
    page.persist_settings()  # Disabled, explicitly incomplete definition is valid.
    saved = startup.data.communication
    before = saved.read_bytes()
    assert not panel.controller.reporting_selection().enabled
    assert panel.controller.reporting_selection().settings.base_address is None
    panel.enabled.click()
    page.persist_settings()
    assert saved.read_bytes() == before and page.dirty
    assert text("comm_settings_failed", language) in page.preferences_status.text()
    panel.base_address.setValue(0)
    panel.advanced_button.click()
    assert panel.modbus_advanced.isVisible() and not panel.advanced.isVisible()
    panel.unit_id.setValue(7)
    page.persist_settings()
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
    panel.port.setValue(1502)
    panel.protocol.setCurrentIndex(2)
    assert panel.port.value() == 1502 and panel.base_address.value() == -1
    panel.base_address.setValue(65532)
    panel.unit_id.setValue(255)
    page.persist_settings()
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
