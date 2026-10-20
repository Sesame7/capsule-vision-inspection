"""Normal entry settings, separate live observations and explicit persistence."""

import socket
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)
from tests.bootstrap_support import saved_data
from tests.qt_support import close_window, ensure_test_fonts, until

from vimo_demo.application import ApplicationState, ProductionCounters
from vimo_demo.application.communication_preferences import (
    CommunicationPreferences,
    WebPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from vimo_demo.communication import FinsMemoryArea, FinsTransport
from vimo_demo.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
)
from vimo_demo.communication.reporting import LocalReportingSettings, ReportingSelection
from vimo_demo.configuration import load_camera_sources, save_camera_sources
from vimo_demo.ui.bootstrap import (
    Startup,
    create_window,
    load_startup,
    main,
)
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_style import Language, Style, text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.web import ProductionSnapshotService
from vimo_demo.web.runtime import WebRuntime, WebState


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_web_power_only_persists_enablement_and_apply_uses_saved_network(
    settings_window: tuple[Startup, ProductionWindow],
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
    settings_window: tuple[Startup, ProductionWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    monkeypatch.setattr("vimo_demo.web.hosts.local_interfaces", lambda: ())
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
    until(lambda: "not assigned" in panel.actual.toolTip())
    assert runtime.snapshot().state is WebState.FAILED
    assert "not assigned" in panel.actual.toolTip()
    assert not runtime.snapshot().owned
    assert (
        load_communication_preferences(startup.data.communication).web.host
        == "192.0.2.10"
    )


def test_web_power_save_failure_preserves_running_service_and_drafts(
    settings_window: tuple[Startup, ProductionWindow],
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

    monkeypatch.setattr(
        "vimo_demo.application.communication_preferences.os.replace", denied
    )
    panel.power.click()
    assert startup.data.communication.read_bytes() == saved
    assert panel.saved.enabled and runtime.snapshot().state is WebState.RUNNING
    assert runtime.snapshot().configuration == actual
    assert panel.host.text() == "0.0.0.0" and page.port.value() == 19602
    assert (
        page.dirty and "controlled power persistence failure" in panel.actual.toolTip()
    )
    assert panel.power.text() == text("web_turn_off", window.language)


def test_web_power_bind_failure_keeps_saved_enablement_and_reports_actual_failure(
    settings_window: tuple[Startup, ProductionWindow],
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
        until(lambda: "OSError" in panel.actual.toolTip())
        assert load_communication_preferences(startup.data.communication).web.enabled
        assert panel.saved.enabled and not runtime.snapshot().owned
        assert panel.power.text() == text("web_turn_on", window.language)
        assert not panel.available and panel.apply_changes.isHidden() and page.dirty
        assert startup.application.snapshot().generation == 0
        assert startup.application.snapshot().counters == ProductionCounters()


def test_web_power_only_changes_enable_bit_in_actual_file(
    settings_window: tuple[Startup, ProductionWindow],
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    page.persist_settings()
    old = load_communication_preferences(startup.data.communication)
    actual = replace(
        old,
        diagnostic=replace(old.diagnostic, port=19612),
        reporting=replace(
            old.reporting, settings=replace(old.reporting.settings, port=19613)
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
    settings_window: tuple[Startup, ProductionWindow],
    invalid_file: bool,
) -> None:
    startup, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    defaults = CommunicationPreferences(web=WebPreferences(port=available_port()))
    page.configure_preferences(startup.data.communication, defaults)
    if invalid_file:
        startup.data.communication.write_text("{}", encoding="utf-8")
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
        assert startup.data.communication.read_text(encoding="utf-8") == "{}"
        assert runtime.snapshot().state is WebState.STOPPED
        assert panel.operation_error and not runtime.snapshot().owned
    else:
        until(lambda: panel.available)
        assert load_communication_preferences(startup.data.communication) == defaults
        assert runtime.snapshot().configuration == defaults.web
    assert panel.port.value() == 19603 and page.port.value() == 19602 and page.dirty


def test_web_ipv4_choices_are_visible_and_refresh_preserves_manual_draft(
    settings_window: tuple[Startup, ProductionWindow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, window = settings_window
    page, panel = window.communication, window.communication.web_panel
    monkeypatch.setattr(
        "vimo_demo.ui.web_settings_panel.local_ipv4_addresses",
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
    settings_window: tuple[Startup, ProductionWindow],
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
) -> Generator[tuple[Startup, ProductionWindow]]:
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


def test_explicit_save_restart_independent_endpoints_and_no_live_state(
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
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
    assert saved.reporting.enabled and saved.reporting.settings.host == "192.0.2.42"
    assert saved.reporting.settings.transport is FinsTransport.TCP
    assert saved.reporting.settings.source.node == 5
    assert saved.diagnostic.port == 19602 and saved.diagnostic.destination.node == 7
    assert saved.web == WebPreferences(False, "127.0.0.1", 8766, 2000)
    assert {p: p.read_bytes() for p in files} == files
    assert startup.application.snapshot() == baseline

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Settings restoration must not create a PLC client")

    monkeypatch.setattr("vimo_demo.communication.reporting._client", forbidden)
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


def test_web_edit_does_not_reconfigure_actual_service(
    settings_window: tuple[Startup, ProductionWindow],
) -> None:
    startup, window = settings_window
    window.observe_web(lambda: (True, "http://127.0.0.1:8765/"), interval_ms=500)
    panel = window.communication.web_panel
    assert panel.available and panel.apply_changes.isHidden()
    panel.port.setValue(8766)
    panel.interval.setCurrentIndex(1)
    assert panel.available and panel.apply_changes.isHidden()
    assert "8765" in panel.actual.toolTip()  # Actual URL stays unchanged.
    window.communication.save_settings.click()
    assert load_communication_preferences(startup.data.communication).web.port == 8766
    assert panel.available and panel.saved.port == 8766
    window.observe_web(lambda: (False, "controlled service failure"), interval_ms=500)
    assert (
        not panel.available and "controlled service failure" in panel.actual.toolTip()
    )


def test_invalid_web_bind_and_atomic_failure_keep_previous_settings(
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr("vimo_demo.web.hosts.local_interfaces", lambda: ())
    page.save_settings.click()
    # Preserve unavailable saved IPs; assignment is checked by actual startup.
    assert (
        load_communication_preferences(startup.data.communication).web.host
        == "192.0.2.10"
    )

    def denied(*args: object) -> None:
        raise PermissionError("controlled save failure")

    before = startup.data.communication.read_bytes()
    monkeypatch.setattr(
        "vimo_demo.application.communication_preferences.os.replace", denied
    )
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
            window.communication.preferences_status.toolTip()
            == startup.communication_error
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
        assert startup.communication.diagnostic == LocalReportingSettings()
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
        assert page.preferences_status.text() == text(
            "comm_settings_saved", window.language
        )
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
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
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
    assert requests[-1].port == 9600
    page.persist_settings()
    assert panel.controller.reporting_selection().enabled
    assert panel.controller.reporting_selection().settings.port == 19601
    assert not page.dirty and not page.discard_settings.isEnabled()
    assert page.dispatch(RequestKind.WRITE)
    until(lambda: not page.busy)
    assert requests[-1].port == 19602
    assert not panel.controller.reporting_snapshot()


def test_failed_save_preserves_draft_file_and_effective_reporting(
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
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

    monkeypatch.setattr(
        "vimo_demo.application.communication_preferences.os.replace", denied
    )
    page.persist_settings()
    assert startup.data.communication.read_bytes() == before
    assert panel.controller.reporting_selection() is original
    assert panel.enabled.isChecked() and panel.port.value() == 19601 and page.dirty


def test_discard_confirmation_and_reload_actual_persisted_definitions(
    settings_window: tuple[Startup, ProductionWindow],
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
    settings_window: tuple[Startup, ProductionWindow],
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
        startup.data.communication.write_text("{}", encoding="utf-8")
    page.reload_settings()
    assert page.port.value() == 19600 and page.web_panel.port.value() == 8766
    assert panel.enabled.isChecked() and page.dirty
    assert panel.controller.reporting_selection() is effective
    assert page.preferences_status.toolTip()


def test_discard_does_not_undo_observed_write(
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
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
        required = (
            panel.minimumSizeHint().width()
            + page.web_panel.minimumSizeHint().width()
            + Style.space
        )
        initial_position = (
            (0, 1) if page.scroll_area.viewport().width() >= required else (1, 0)
        )
        assert page.service_layout.itemAtPosition(*initial_position) is not None
        assert page.body.width() <= page.scroll_area.viewport().width()

        window.resize(size[1], size[0])
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
        required = (
            panel.minimumSizeHint().width()
            + page.web_panel.minimumSizeHint().width()
            + page.service_layout.horizontalSpacing()
        )
        wide = page.scroll_area.viewport().width() >= required
        assert (
            page.service_layout.itemAtPosition(*(0, 1) if wide else (1, 0)) is not None
        )
        assert panel.summary.text() == "ACK -- · Successful 0 · ERR 0"
        if size[0] == 1300:
            # Expanded controls must fit the viewport after first navigation,
            # language changes and shrinking from a previously wide grid.
            panel.advanced_button.click()
            page.advanced_button.click()
            page.watch_editor_button.click()
            page.manual_button.click()
            before = startup.application.snapshot()
            for width in (1300, 1920, 1080, 1300):
                window.showNormal()
                window.resize(width, 1017)
                for language in ("zh", "en"):
                    window.set_language(language)
                    qapp.processEvents()
                    available = page.scroll_area.viewport().width()
                    assert page.body.width() <= available
                    required = (
                        panel.minimumSizeHint().width()
                        + page.web_panel.minimumSizeHint().width()
                        + Style.space
                    )
                    position = (0, 1) if available >= required else (1, 0)
                    until(
                        lambda pos=position: (
                            page.service_layout.itemAtPosition(*pos) is not None
                        )
                    )
                    assert page.body.width() <= available, (
                        width,
                        language,
                        available,
                        required,
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
                            assert (
                                0 <= point.x()
                                and point.x() + control.width() <= available
                            ), (control.accessibleName(), control.geometry(), available)
                    if width in (1300, 1920) and language == "en":
                        assert page.grab().save(
                            str(tmp_path / f"communication-{width}.png")
                        )
            assert startup.application.snapshot() == before
    finally:
        close_window(window)
        delivery.detach()


def test_reporting_power_edits_only_draft_until_save_or_discard(
    settings_window: tuple[Startup, ProductionWindow],
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
    assert panel.service_status.text() == text("plc_disabled", window.language)
    page.reload_settings()
    assert not panel.enabled.isChecked() and not page.dirty
    panel.enabled.click()
    page.persist_settings()
    assert load_communication_preferences(startup.data.communication).reporting.enabled
    assert panel.service_status.text() == text("plc_enabled", window.language)
    panel.enabled.click()
    assert page.dirty and panel.controller.reporting_selection().enabled
    page.reload_settings()
    assert panel.enabled.isChecked() and not page.dirty
    assert panel.controller.reporting_snapshot() is None
    assert page.web_panel.runtime is web_runtime
    assert startup.application.snapshot().counters == ProductionCounters()


def _assert_service_cards_reflow(
    settings_window: tuple[Startup, ProductionWindow], landscape: bool
) -> None:
    _, window = settings_window
    page = window.communication
    window.resize(*((1920, 1017) if landscape else (1080, 1857)))
    window.set_language("en")
    QApplication.processEvents()
    assert (
        page.service_layout.itemAtPosition(*((0, 1) if landscape else (1, 0)))
        is not None
    )
    panel = page.reporting_panel
    assert panel is not None
    panel.advanced_button.setChecked(True)
    QApplication.processEvents()
    assert panel.advanced.isVisible()
    for control in (
        panel.port,
        panel.timeout,
        page.web_panel.host,
        page.web_panel.interval,
    ):
        parent = control.parentWidget()
        assert parent is not None and parent.rect().contains(control.geometry())
    assert page.body.width() <= page.scroll_area.viewport().width()
    assert page.save_settings.isVisible() and page.discard_settings.isVisible()


@pytest.mark.parametrize(
    "language,size",
    [
        ("zh", (1080, 1920)),
        ("en", (1920, 1080)),
        ("en", (1080, 1920)),
        ("zh", (1920, 1080)),
    ],
)
def test_communication_presentation_matrix(
    settings_window: tuple[Startup, ProductionWindow],
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup, window = settings_window
    page = window.communication
    panel = page.reporting_panel
    assert panel is not None
    before = startup.application.snapshot()
    # Retain the original two work-area heights as internal resize observations.
    if language == "en" and size in ((1080, 1920), (1920, 1080)):
        _assert_service_cards_reflow(settings_window, size[0] > size[1])
        panel.advanced_button.setChecked(False)
    window.set_language(language)
    window.resize(*size)
    window.navigate(2)
    qapp.processEvents()
    width, height = size
    assert (
        window.communication.body.width()
        <= window.communication.scroll_area.viewport().width()
    )
    assert panel.host.isVisible()
    assert window.grab().save(
        str(tmp_path / f"communication-empty-{language}-{width}x{height}.png")
    )
    panel.advanced_button.click()
    window.communication.advanced_button.click()
    qapp.processEvents()
    assert panel.title.text() == text("plc_reporting", language)
    assert panel.summary.y() == panel.service_status.y()
    assert panel.title.geometry().right() < panel.summary.x()
    assert panel.summary.geometry().right() < panel.service_status.x()
    assert panel.summary.y() < panel.host.y()
    assert panel.enabled.accessibleName() == text("plc_enable", language)
    assert panel.port.accessibleName() == text("comm_port", language)
    assert panel.host.text() == "127.0.0.1" and panel.advanced.isVisible()
    assert panel.geometry().width() <= window.width()
    assert panel.geometry().width() <= width
    for widget in (panel.enabled, panel.port, panel.timeout, panel.advanced_button):
        assert widget.width() >= widget.minimumSizeHint().width()
    for widget in (
        panel.port,
        panel.timeout,
        panel.advanced_button,
        window.communication.host,
        window.communication.port,
        window.communication.timeout,
        window.communication.advanced_button,
    ):
        parent = widget.parentWidget()
        assert parent is not None and parent.rect().contains(widget.geometry())

    page.watch_editor_button.click()
    page.manual_button.click()
    completed = datetime(2026, 10, 14, 13, 22, 35, 123456, tzinfo=UTC)

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        return CommunicationResponse(
            request, tuple(range(request.count)), 1.25, completed
        )

    monkeypatch.setattr(page, "_execute", execute)
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "提交 / Commit")
    )
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.DM, 100, 3, "测试 / Test")
    )
    assert page.dispatch(RequestKind.READ, count=3)
    until(lambda: not page.busy)
    assert page.read_watch_once()
    until(lambda: not page.busy)
    qapp.processEvents()
    page = window.communication
    for control in (
        page.host,
        page.port,
        page.timeout,
        page.address,
        page.write_button,
        page.advanced_button,
        page.area,
        page.watch_area,
        page.watch_address,
        page.watch_count,
        page.watch_name,
        page.watch_selector,
        page.watch_add,
        page.watch_remove,
        page.watch_once,
        page.watch_auto,
        *page.destination,
        *page.source,
    ):
        parent = control.parentWidget()
        assert parent is not None and parent.rect().contains(control.geometry())
        assert control.height() >= Style.touch
    assert page.body.width() <= page.scroll_area.viewport().width()
    for button in (page.watch_add, page.watch_remove, page.watch_once, page.watch_auto):
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 20
        )
    assert window.event_frame.height() == 44
    assert window.grab().save(
        str(tmp_path / f"communication-{language}-{size[0]}x{size[1]}.png")
    )

    # The reporting layout's original five-row input remains a separate stage.
    while page.watch_groups:
        assert page.remove_watch_group(0)
    assert window.communication.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "首工位 / First station")
    )
    assert window.communication.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.DM, 100, 1, "诊断 / Diagnostic")
    )
    assert window.communication.read_watch_once()
    until(lambda: not window.communication.busy)
    qapp.processEvents()
    assert (
        window.communication.watch_table.columnWidth(0)
        < window.communication.watch_table.width() / 4
    )
    widths = [window.communication.watch_table.columnWidth(c) for c in range(5)]
    assert max(widths) - min(widths) <= 1
    assert window.grab().save(
        str(tmp_path / f"communication-populated-{language}-{width}x{height}.png")
    )

    expected = completed.astimezone().strftime("%H:%M:%S.%f")[:-3]
    assert page.watch_table.columnCount() == 5
    assert (
        page.watch_table.columnWidth(4)
        >= page.watch_table.fontMetrics().horizontalAdvance(expected) + 8
    )
    for row in range(page.watch_table.rowCount()):
        item = page.watch_table.item(row, 4)
        assert item is not None and item.text() == expected
    assert page.save_settings.isVisible() and page.discard_settings.isVisible()
    assert startup.application.snapshot() == before
