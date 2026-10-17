"""Normal entry settings, separate live observations and explicit persistence."""

from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from ipaddress import IPv4Interface
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.bootstrap_support import saved_data
from tests.qt_support import close_window, until

from vimo_demo.application import ProductionCounters
from vimo_demo.application.communication_preferences import (
    WebPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from vimo_demo.communication import FinsTransport
from vimo_demo.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
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
from vimo_demo.ui.production_window import ProductionWindow


@pytest.fixture
def settings_window(
    qapp: QApplication, tmp_path: Path
) -> Generator[tuple[Startup, ProductionWindow]]:
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
    page.web_panel.enabled.setChecked(False)
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
    assert panel.available and panel.restart.isHidden()
    panel.enabled.setChecked(False)
    panel.port.setValue(8766)
    panel.interval.setCurrentIndex(1)
    assert panel.available and panel.restart.isVisible()
    assert "8765" in panel.actual.toolTip() and "8766" not in panel.actual.toolTip()
    window.communication.save_settings.click()
    assert load_communication_preferences(startup.data.communication).web.port == 8766
    assert panel.available and panel.launched == WebPreferences()
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
    assert startup.data.communication.read_bytes() == before
    assert "not assigned" in page.preferences_status.toolTip()
    monkeypatch.setattr(
        "vimo_demo.web.hosts.local_interfaces",
        lambda: (IPv4Interface("192.0.2.10/24"),),
    )
    page.save_settings.click()
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


def test_missing_preferences_keep_source_specific_previous_defaults(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    sources = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources,
        replace(
            sources,
            mode="basler",
            cameras=tuple(
                replace(camera, basler_serial=str(12340000 + index))
                for index, camera in enumerate(sources.cameras)
            ),
        ),
    )
    startup = load_startup(data)
    try:
        assert not startup.communication_error and not data.communication.exists()
        assert startup.communication.reporting == ReportingSelection(
            False,
            LocalReportingSettings(host="192.168.251.1", transport=FinsTransport.TCP),
        )
        assert startup.communication.diagnostic == LocalReportingSettings()
        assert startup.communication.web == WebPreferences()
    finally:
        startup.application.close().result(5)
        assert startup.application.join(5)


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
    assert page.port.value() == 19610 and not page.web_panel.enabled.isChecked()
    assert load_communication_preferences(startup.data.communication) == actual
    assert (
        page.web_panel.launched == saved.web and not page.discard_settings.isEnabled()
    )
    assert startup.application.snapshot().counters == ProductionCounters()


@pytest.mark.parametrize("failure", ["missing", "invalid", "unassigned"])
def test_reload_failure_preserves_entire_draft(
    settings_window: tuple[Startup, ProductionWindow],
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
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
    else:
        value = load_communication_preferences(startup.data.communication)
        save_communication_preferences(
            startup.data.communication,
            replace(value, web=WebPreferences(host="192.0.2.10")),
        )
        monkeypatch.setattr("vimo_demo.web.hosts.local_interfaces", lambda: ())
    page.reload_settings()
    assert page.port.value() == 19600 and page.web_panel.port.value() == 8766
    assert panel.enabled.isChecked() and page.dirty
    assert panel.controller.reporting_selection() is effective
    assert page.preferences_status.toolTip()


def test_discard_does_not_undo_observed_write(
    settings_window: tuple[Startup, ProductionWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, window = settings_window
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
    assert page.success.text() and page.status.text()


@pytest.mark.parametrize("landscape", [False, True])
def test_service_cards_reflow_without_clipping(
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
