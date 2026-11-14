"""Saved status ownership and write fencing through normal isolated composition."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ReplayEngine
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)
from tests.unit.test_batch_reporting import wait

from capsule_vision.application import ApplicationState
from capsule_vision.application.audit import AuditQuery
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.manual_plc import execute_manual
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import (
    FinsMemoryArea,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
)
from capsule_vision.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RequestKind,
)
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportingWriteProtectedError,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


def test_active_status_write_protection_survives_pending_disable(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        peer.hold_next("sequence")
        selected = StatusPreferences(
            True, FinsTcpEndpoint(port=peer.port), fault_address=211
        )
        save_communication_preferences(
            data.communication, CommunicationPreferences(status=selected)
        )
        startup = authenticated_startup(data)
        app = startup.application
        assert isinstance(app, SourceProductionApplication) and app.identity

        def denied_io(_request: CommunicationRequest) -> CommunicationResponse:
            pytest.fail("Protected manual write reached transport")

        request = CommunicationRequest(
            RequestKind.WRITE, selected.endpoint, 46, values=(1,), area=FinsMemoryArea.W
        )
        try:
            assert peer.entered.wait(WAIT)
            for address, count in ((45, 2), (46, 1), (210, 2), (211, 1)):
                with pytest.raises(ReportingWriteProtectedError) as denied:
                    app.validate_manual_request(
                        replace(
                            request, start=address, count=count, values=(1,) * count
                        )
                    )
                assert denied.value.owner == "status"
            with pytest.raises(ReportingWriteProtectedError):
                execute_manual(
                    request,
                    denied_io,
                    app.validate_manual_request,
                    app.identity,
                    app.identity.snapshot().revision,
                )
            with pytest.raises(ReportingWriteProtectedError):
                app.validate_manual_request(
                    replace(request, endpoint=FinsUdpEndpoint(port=peer.port))
                )
            for change in (
                replace(request, start=47, count=164, values=(1,) * 164),
                replace(request, start=18),  # Alarm read does not reserve writes.
                replace(request, start=48),
                replace(request, area=FinsMemoryArea.DM),
                replace(request, kind=RequestKind.READ, values=()),
                replace(
                    request, endpoint=replace(selected.endpoint, port=peer.port + 1)
                ),
            ):
                app.validate_manual_request(change)
            disabled = replace(
                startup.communication, status=replace(selected, enabled=False)
            )
            app.persist_reporting_selection(
                disabled.reporting,
                lambda: save_communication_preferences(data.communication, disabled),
                status=disabled.status,
            )
            with pytest.raises(ReportingWriteProtectedError):
                app.validate_manual_request(request)
            peer.release.set()

            def released() -> bool:
                try:
                    app.validate_manual_request(request)
                except ReportingWriteProtectedError:
                    return False
                return True

            wait(released)
            app.validate_manual_request(request)
            rows = app.identity.audit_page(AuditQuery()).result(WAIT)
            writes = [row for row in rows if row.operation.action == "plc.write"]
            assert len(writes) == 1 and writes[0].outcome == "failed"
            assert (
                len(peer.packets) == 1
            )  # Only automatic status write, no final clearing write.
        finally:
            peer.release.set()
            close(startup)


@pytest.mark.parametrize("role", list(Role))
def test_status_draft_permissions_save_discard_and_fault_observation(
    qapp: QApplication, tmp_path: Path, role: Role
) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        initial = CommunicationPreferences(
            status=StatusPreferences(False, FinsTcpEndpoint(port=peer.port))
        )
        save_communication_preferences(data.communication, initial)
        startup = authenticated_startup(
            data, engine_factory=lambda _: ReplayEngine(fail="fatal")
        )
        app = startup.application
        identity = app.identity
        assert isinstance(app, SourceProductionApplication) and identity
        if role is Role.GUEST:
            identity.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(identity, "Selected", role, TEST_PASSWORD)
            identity.login("Selected", TEST_PASSWORD).result(WAIT)
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        page, panel = window.communication, window.communication.alarm_panel
        try:
            window.show()
            window.navigate(2)
            assert panel.heartbeat_address.isEnabled() == (role >= Role.ENGINEER)
            assert panel.fault_address.isEnabled() == (role >= Role.ENGINEER)
            assert panel.running_address.isEnabled() == (role >= Role.ENGINEER)
            assert peer.connections == 0
            panel.heartbeat_address.setValue(146)
            assert panel.fault_address.value() == 47
            panel.fault_address.setValue(211)
            panel.address.setValue(118)
            panel.running_address.setValue(216)
            panel.enabled.setChecked(True)
            assert panel.fault_address.text() == "W211" and peer.connections == 0
            if role < Role.ENGINEER:
                with pytest.raises(AuthorizationError):
                    app.persist_reporting_selection(
                        initial.reporting,
                        lambda: pytest.fail("Unauthorized persistence"),
                        status=replace(initial.status, enabled=True),
                    )
                page.persist_settings()
                until(lambda: not page.busy)
                assert (
                    load_communication_preferences(data.communication) == initial
                    and peer.connections == 0
                )
                return
            page.persist_settings()
            until(lambda: not page.busy and app.alarm_snapshot().state == "online")
            applied = load_communication_preferences(data.communication).status
            assert applied.enabled and (
                applied.heartbeat_address,
                applied.fault_address,
                applied.word_address,
                applied.running_address,
            ) == (146, 211, 118, 216)
            assert (
                peer.words(211) == (0,)
                and app.snapshot().state is not ApplicationState.FAULT
            )
            # Isolated Folder Replay fake engine: a real Application FAULT,
            # without vendor inference or any physical equipment.
            app.start().result(WAIT)
            until(lambda: app.snapshot().state is ApplicationState.FAULT)
            until(lambda: app.alarm_snapshot().fault == 1)
            assert peer.words(211) == (1,)
            before = data.communication.read_bytes()
            panel.fault_address.setValue(146)
            page.persist_settings()
            until(lambda: not page.busy)
            assert "distinct" in page.preferences_status.toolTip()
            assert data.communication.read_bytes() == before
            assert app.alarm_snapshot().selection == applied
            panel.heartbeat_address.setValue(246)
            panel.fault_address.setValue(311)
            panel.running_address.setValue(316)
            assert app.alarm_snapshot().selection == applied
            page.reload_settings()
            until(lambda: not page.busy)
            assert (
                panel.heartbeat_address.value() == 146
                and panel.fault_address.text() == "W211"
                and panel.running_address.text() == "W216.1"
            )
            # No periodic audit stream: only the normal Save command is audited.
            rows = identity.audit_page(AuditQuery()).result(WAIT)
            assert not [row for row in rows if row.operation.action == "plc.write"]
        finally:
            close_window(window)
            delivery.detach()


def test_start_rejects_conflict_until_old_status_owner_retires(tmp_path: Path) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        peer.hold_next("sequence")
        status = StatusPreferences(
            True, FinsTcpEndpoint(port=peer.port), heartbeat_address=49
        )
        save_communication_preferences(
            data.communication, CommunicationPreferences(status=status)
        )
        startup = authenticated_startup(data)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        report = ReportingSelection(
            True, ReportingSettings(status.endpoint, 49, mode=ReportingMode.PER_CYCLE)
        )
        try:
            assert peer.entered.wait(WAIT)
            # Trusted composition changing next-Start reporting cannot bypass an
            # old status writer that is still retiring after a saved disable.
            published: list[bool] = []
            with pytest.raises(ValueError, match="overlap"):
                app.persist_reporting_selection(
                    report,
                    lambda: published.append(True),
                    status=replace(
                        status, enabled=False, heartbeat_address=46, running_address=49
                    ),
                )
            assert not published and app.alarm_snapshot().selection == status
            app.persist_reporting_selection(
                report, lambda: None, status=replace(status, enabled=False)
            )
            before = app.snapshot()
            with pytest.raises(ValueError, match="overlap"):
                app.start()
            assert app.snapshot().generation == before.generation
            assert app.snapshot().state == before.state
            assert len(peer.packets) == 1
        finally:
            peer.release.set()
            close(startup)
