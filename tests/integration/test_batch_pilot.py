"""Normal composition, fake Basler SDK, real local TCP; no operational assets."""

import json
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import perf_counter

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_basler_production import Engine, PassiveBackend, save_basler
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    saved_data,
)

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    _basler_sdk,
)
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import FinsMemoryArea, FinsTcpEndpoint
from capsule_vision.communication.batch_reporting import W48BatchReportingWorker
from capsule_vision.communication.operations import CommunicationRequest, RequestKind
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportingWriteProtectedError,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("mode", [ReportingMode.W48_OBSERVE, ReportingMode.W48_ACTIVE])
def test_normal_authorization_warmup_controls_and_stop(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    mode: ReportingMode,
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    with TcpPeer() as peer:
        selection = ReportingSelection(
            True, ReportingSettings(endpoint=FinsTcpEndpoint(port=peer.port), mode=mode)
        )
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app, identity = startup.application, startup.application.identity
        assert isinstance(app, SourceProductionApplication) and identity is not None
        try:
            app.set_reporting_selection(selection)
            app.connect_source()
            if role is Role.GUEST:
                identity.logout()
            elif role is not Role.ADMINISTRATOR:
                identity.create_account("Actor", role, TEST_PASSWORD).result(3)
                identity.login("Actor", TEST_PASSWORD).result(3)
            if role < Role.ENGINEER:
                with pytest.raises(AuthorizationError):
                    app.persist_reporting_selection(
                        selection, lambda: pytest.fail("Unauthorized save")
                    )
            else:
                app.persist_reporting_selection(
                    selection,
                    lambda: save_communication_preferences(
                        data.communication,
                        CommunicationPreferences(reporting=selection),
                    ),
                )
            if role is Role.GUEST:
                with pytest.raises(AuthorizationError):
                    app.start()
                assert not engine.calls and not peer.packets
                return
            app.start().result(3)
            assert not peer.packets and peer.connections == 0
            for action in (app.pause_source, app.step_source, app.resume_source):
                with pytest.raises(ApplicationStateError, match="W48"):
                    action()
            if role >= Role.ENGINEER:
                for address in (48, 49, 50, 51, 52):
                    with pytest.raises(ReportingWriteProtectedError):
                        app.validate_manual_request(
                            CommunicationRequest(
                                kind=RequestKind.WRITE,
                                start=address,
                                values=(7,),
                                area=FinsMemoryArea.W,
                                endpoint=selection.settings.endpoint,
                            )
                        )
            peer.set_words(48, (38,))
            for cycle in range(1, 5):
                backend.expose(cycle)
                until(
                    lambda cycle=cycle: (
                        app.snapshot().counters.cycles_completed >= cycle
                    )
                )
                if cycle == 1:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.pilot_batches
                            and s.pilot_batches[0].reads
                        )
                    )
            until(lambda: bool((s := app.reporting_snapshot()) and s.last_outcome))
            snapshot = app.reporting_snapshot()
            assert snapshot is not None
            assert snapshot.acknowledged_count + snapshot.observed_count == 1
            counters = app.snapshot().counters
            assert counters.channel_ok == 144
            assert counters.business_ok + counters.business_empty == 144
            assert counters.business_ng == counters.business_unknown == 0
            physical = app.snapshot().pilot_counters
            assert app.snapshot().pilot_counting
            assert physical.batches_completed == 1
            assert physical.channel_ok + physical.channel_empty == 36
            assert physical.channel_ng == physical.channel_unknown == 0
            assert peer.words(49) == (
                (38,) if mode is ReportingMode.W48_ACTIVE else (0,)
            )
            identity.logout()  # Stop/Close do not require a production role.
            app.stop().result(3)
            assert app.snapshot().state is ApplicationState.READY
            stopped = app.reporting_snapshot()
            assert stopped is not None and not stopped.thread_alive
            assert app.snapshot().pilot_counters == physical
        finally:
            close(startup)
        assert engine.closed and backend.closed


@pytest.mark.parametrize("mode", [ReportingMode.W48_OBSERVE, ReportingMode.W48_ACTIVE])
def test_folder_pilot_rejected_before_session_or_engine(
    tmp_path: Path, mode: ReportingMode
) -> None:
    data = saved_data(tmp_path)
    engine = Engine()
    startup = authenticated_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceProductionApplication)
    try:
        app.set_reporting_selection(
            ReportingSelection(
                True, ReportingSettings(endpoint=FinsTcpEndpoint(), mode=mode)
            )
        )
        before = app.snapshot()
        for command in (app.start, app.step_source):
            with pytest.raises(ApplicationStateError, match="requires Basler"):
                command()
        assert (
            app.snapshot() == before
            and not engine.calls
            and app.reporting_snapshot() is None
        )
    finally:
        close(startup)


def test_failed_warmup_never_reads_w48(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.fail_warmup = True
    engine.release_warm.set()
    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        endpoint=FinsTcpEndpoint(port=peer.port),
                        mode=ReportingMode.W48_ACTIVE,
                    ),
                )
            )
            app.connect_source()
            active = app.snapshot().active
            with pytest.raises(ApplicationError):
                app.start().result(3)
            assert (
                app.snapshot().state is ApplicationState.READY
                and app.snapshot().active == active
            )
            assert peer.connections == 0 and not peer.packets
            assert not app.snapshot().pilot_counting
            assert app.snapshot().pilot_counters.batches_completed == 0
            snapshot = app.reporting_snapshot()
            assert snapshot is not None and not snapshot.thread_alive
        finally:
            close(startup)


@pytest.mark.parametrize("mode", list(ReportingMode))
def test_preferences_roundtrip_explicit_pilot(
    tmp_path: Path, mode: ReportingMode
) -> None:
    path = tmp_path / "communication.json"
    value = CommunicationPreferences(
        reporting=ReportingSelection(
            True, ReportingSettings(endpoint=FinsTcpEndpoint(), mode=mode)
        )
    )
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    payload = json.loads(path.read_text())
    assert payload["reporting"].get("mode", "per_cycle") == mode.value


def test_pilot_ui_draft_save_localization_and_permission(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.resize(1080, 900)
    window.show()
    try:
        window.navigate(2)
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None and panel.mode.currentIndex() == 0
        panel.mode.setCurrentIndex(1)
        panel.enabled.setChecked(True)
        assert (
            panel.controller.reporting_selection().settings.mode
            is ReportingMode.PER_CYCLE
        )
        page.save_settings.click()
        until(
            lambda: (
                panel.controller.reporting_selection().settings.mode
                is ReportingMode.W48_OBSERVE
            )
        )
        assert (
            load_communication_preferences(startup.data.communication).reporting
            == panel.selection()
        )
        for language in LANGUAGES:
            window.set_language(language)
            qapp.processEvents()
            assert panel.mode.currentIndex() == 1 and panel.mode.accessibleName()
            assert panel.mode.toolTip() and panel.mode.width() >= 180
            assert panel.evidence.isVisible() and not panel.evidence.isEnabled()
        identity = startup.application.identity
        assert identity is not None
        identity.logout()
        window.identity_controls.refresh()
        assert not panel.mode.isEnabled() and not panel.enabled.isEnabled()
        # A phase fence is an error even with no failed socket request.
        worker = W48BatchReportingWorker(1, panel.selection().settings)
        window.bridge.reporting_snapshot = replace(
            worker.snapshot(), phase_issue="W48 changed"
        )
        window.set_language("en")
        assert window.event_frame.plc.text() == f"PLC · {text('technical_error', 'en')}"
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("hold_native", [False, True])
def test_four_trigger_burst_latency_and_unchanged_native_ownership(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hold_native: bool,
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    if hold_native:
        engine.release_active.clear()
    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        endpoint=FinsTcpEndpoint(port=peer.port),
                        mode=ReportingMode.W48_ACTIVE,
                    ),
                )
            )
            app.connect_source()
            app.start().result(3)
            peer.set_words(48, (44,))
            start = perf_counter()
            for index in range(4):
                Event().wait(max(0, start + index * 0.060 - perf_counter()))
                backend.expose(index + 1)
            until(lambda: bool((s := app.reporting_snapshot()) and s.last_outcome))
            snapshot = app.reporting_snapshot()
            assert snapshot is not None and snapshot.acknowledged_count == 1
            batch = snapshot.pilot_batches[-1]
            assert batch.report is not None and batch.report.acknowledged_at is not None
            elapsed = (batch.report.acknowledged_at - start) * 1000
            evidence = {
                "held_native": hold_native,
                "elapsed_ms": elapsed,
                "completion_to_ack_ms": batch.report.completion_to_ack_ms,
                "terminal_outcomes": batch.terminal_outcomes,
                "missed_assumed_240ms_window": elapsed >= 240,
            }
            print(json.dumps(evidence))
            assert len(batch.terminal_outcomes) == 4
            if hold_native:
                assert all(
                    status == "TIMED_OUT" for _, status in batch.terminal_outcomes
                )
                assert not engine.closed
                stopping = app.stop()
                assert not stopping.done()  # Deadline never releases the SDK owner.
                engine.release_active.set()
                stopping.result(3)
            else:
                assert all(
                    status == "COMPLETE" for _, status in batch.terminal_outcomes
                )
                app.stop().result(3)
        finally:
            engine.release_active.set()
            close(startup)
