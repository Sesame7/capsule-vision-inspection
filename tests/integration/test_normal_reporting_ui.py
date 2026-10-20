"""Normal bootstrap reporting preferences, loopback observations and ownership."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.bootstrap_support import saved_data
from tests.cycle_support import image_result
from tests.fins_tcp_support import TcpPeer
from tests.local_input_support import input_files
from tests.qt_support import close_window, until
from tests.reporting_support import WAIT, readback, seed, server
from tests.unit.test_application_bootstrap import close

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
)
from vimo_demo.application.communication_preferences import CommunicationPreferences
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsTransport,
    FinsUdpClient,
    first_station_reject_words,
)
from vimo_demo.communication.operations import (
    CommunicationRequest,
    RegisterWatchGroup,
    RequestKind,
)
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ReportingSelection,
    ReportStage,
)
from vimo_demo.configuration import load_camera_sources, save_camera_sources
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.ui.bootstrap import ApplicationData, Startup, create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.local_production import compose
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_style import text
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.reporting_panel import ReportingPanel


class FatalError(BaseException):
    pass


class Engine:
    def __init__(self, *, hold: int = 9, fatal: bool = False) -> None:
        self.hold, self.fatal = hold, fatal
        self.entered, self.release = Event(), Event()
        self.calls = 0
        self.closed = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls += 1
        if self.calls == self.hold:
            self.entered.set()
            assert self.release.wait(WAIT)
        if self.fatal and self.calls == 3:
            raise FatalError("controlled runtime failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        self.closed = True


@contextmanager
def normal_window(
    root: Path, *engines: Engine
) -> Generator[tuple[Startup, ProductionWindow, ReportingPanel]]:
    data = saved_data(root)
    sources = load_camera_sources(data.sources)
    save_camera_sources(data.sources, replace(sources, loop=True))
    remaining = iter(engines)
    startup = load_startup(data, engine_factory=lambda _: next(remaining))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    panel = window.communication.reporting_panel
    assert panel is not None
    window.show()
    try:
        yield startup, window, panel
    finally:
        for engine in engines:
            engine.release.set()
        close_window(window)
        delivery.detach()
        assert not any(t.name.startswith("FirstStationReporting-") for t in threads())


def test_normal_defaults_disabled_no_reporting_and_relaunch_resets(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = Engine()
    with normal_window(tmp_path, engine) as (startup, window, panel):
        commands = panel.controller
        assert commands.reporting_selection() == ReportingSelection()
        assert panel.protocol.currentText() == "FINS/UDP"
        assert commands.reporting_snapshot() is None
        assert text("plc_disabled", window.language) in panel.service_status.text()
        assert (
            "ACK --" in panel.summary.toolTip()
            and text("plc_last_error", window.language) not in panel.summary.toolTip()
        )
        before = {p: p.read_bytes() for p in startup.data.root.rglob("*.json")}
        startup.application.start().result(WAIT)
        assert engine.entered.wait(WAIT)
        assert commands.reporting_snapshot() is None
        engine.release.set()
        startup.application.stop().result(WAIT)
        panel.update_state(ApplicationState.READY)
        panel.port.setValue(19600)
        panel.enabled.setChecked(True)
        assert not commands.reporting_selection().enabled
        assert panel.selection().enabled
        assert {p: p.read_bytes() for p in before} == before
        data = startup.data
    restored = load_startup(data)
    try:
        delivery = FutureDelivery()
        window = create_window(restored, delivery)
        panel = window.communication.reporting_panel
        assert panel is not None
        assert panel.controller.reporting_selection() == ReportingSelection()
        assert panel.protocol.currentText() == "FINS/UDP"
        close_window(window)
        delivery.detach()
    finally:
        close(restored)
    assert engine.closed


def test_unconfigured_reporting_preferences_do_not_invent_assets(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = load_startup(ApplicationData.at_entry(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        panel = window.communication.reporting_panel
        assert panel is not None and panel.enabled.isEnabled()
        panel.enabled.setChecked(True)
        panel.port.setValue(19600)
        assert not panel.controller.reporting_selection().enabled
        assert panel.selection().enabled
        assert startup.application.snapshot().state is ApplicationState.UNCONFIGURED
        assert panel.controller.reporting_snapshot() is None
        assert not list(tmp_path.rglob("*"))
    finally:
        close_window(window)
        delivery.detach()


def test_normal_start_settings_propagation_ack_and_manual_write_protection(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[tuple[str, int, float, FinsAddress, FinsAddress, int]] = []

    class ObservedClient(FinsUdpClient):
        def __init__(
            self,
            host: str,
            *,
            port: int,
            timeout: float,
            destination: FinsAddress,
            source: FinsAddress,
        ) -> None:
            clients.append((host, port, timeout, destination, source, get_ident()))
            super().__init__(
                host, port=port, timeout=timeout, destination=destination, source=source
            )

    monkeypatch.setattr(
        "vimo_demo.communication.reporting.FinsUdpClient", ObservedClient
    )
    with (
        server() as peer,
        normal_window(tmp_path, Engine()) as (startup, window, panel),
    ):
        seed(peer, 88)
        panel.port.setValue(peer.port)
        panel.timeout.setValue(0.2)
        panel.source[1].setValue(3)
        panel.source[2].setValue(7)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        selected = panel.controller.reporting_selection()
        window.bridge.start()
        until(lambda: startup.application.snapshot().state is ApplicationState.RUNNING)
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.acknowledged_count == 1
            )
        )
        window.bridge.refresh()
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and observed.last_acknowledged is not None
        assert observed.last_acknowledged.sequence == 89
        assert selected.settings.source == FinsAddress(0, 3, 7)
        assert len(clients) == 1
        assert clients[0][:5] == (
            "127.0.0.1",
            peer.port,
            0.2,
            FinsAddress(0, 2, 0),
            FinsAddress(0, 3, 7),
        )
        assert clients[0][5] != get_ident()
        assert not panel.enabled.isEnabled() and not panel.port.isEnabled()
        assert text("plc_active", window.language) in panel.service_status.text()
        assert "ACK 89" in panel.summary.toolTip()
        assert "generation=1" in panel.summary.toolTip()
        packets = peer.packets[1:]  # Exclude the independent sequence seeding request.
        assert len(packets) == 3
        assert all(
            p[3:6] == bytes((0, 2, 0)) and p[6:9] == bytes((0, 3, 7)) for p in packets
        )
        result = startup.application.snapshot().latest_production_result
        assert result is not None
        assert readback(peer) == (
            89,
            *first_station_reject_words(
                (r.channel_id, r.business_status) for r in result.channels
            ),
        )
        # A programmatic UI mutation and direct command both obey the live state.
        panel.port.setValue(peer.port + 1 if peer.port < 65535 else peer.port - 1)
        assert panel.port.value() == peer.port
        with pytest.raises(ApplicationStateError):
            panel.controller.set_reporting_selection(ReportingSelection())
        assert panel.controller.reporting_selection() is selected
        assert not panel.port.isEnabled() and window.communication.host.isEnabled()
        window.communication.host.setText("192.0.2.10")
        assert panel.controller.reporting_selection().settings.port == peer.port
        window.communication.host.setText("127.0.0.1")

        page = window.communication
        page.port.setValue(peer.port)
        window.communication.persist_settings()
        page.area.setCurrentIndex(1)  # W, independent from automatic settings.
        page.write_values.setText("7")
        counters = startup.application.snapshot().counters
        for start, values in ((49, "7"), (52, "7"), (48, "7 8")):
            page.address.setValue(start)
            page.write_values.setText(values)
            before = len(peer.packets)
            assert not page.dispatch(RequestKind.WRITE)
            assert len(peer.packets) == before and not page.busy
            assert text("plc_reserved_write", window.language) in page.status.text()
        page.address.setValue(49)
        assert page.dispatch(RequestKind.READ, count=4)
        until(lambda: not page.busy)
        assert page.last_response is not None and page.last_response.values == readback(
            peer
        )
        assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 4))
        assert page.read_watch_once()
        until(lambda: not page.busy)
        for area, start in ((1, 48), (1, 53), (0, 49)):
            page.area.setCurrentIndex(area)
            page.address.setValue(start)
            page.write_values.setText("123")
            assert page.dispatch(RequestKind.WRITE)
            until(lambda: not page.busy)
            assert (
                page.last_response is not None
                and page.last_response.request.kind is RequestKind.WRITE
            )
        assert startup.application.snapshot().counters == counters
        assert panel.controller.reporting_selection() is selected


def test_settings_locked_starting_stopping_then_restart_has_new_generation(
    qapp: QApplication, tmp_path: Path
) -> None:
    first, second = Engine(hold=1), Engine(hold=1)
    with (
        server() as peer,
        normal_window(tmp_path, first, second) as (startup, window, panel),
    ):
        panel.port.setValue(peer.port)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        selected = panel.controller.reporting_selection()
        future = startup.application.start()
        assert first.entered.wait(WAIT)
        window.bridge.refresh()
        assert startup.application.snapshot().state is ApplicationState.STARTING
        assert panel.service_status.text() == text("plc_starting", window.language)
        assert not panel.enabled.isEnabled()
        assert not peer.packets  # Warm-up has zero protocol/result side effects.
        with pytest.raises(ApplicationStateError):
            panel.controller.set_reporting_selection(ReportingSelection())
        assert panel.controller.reporting_selection() is selected
        stop = startup.application.stop()
        window.bridge.refresh()
        assert (
            not stop.done()
            and startup.application.snapshot().state is ApplicationState.STOPPING
        )
        assert not panel.port.isEnabled()
        with pytest.raises(ApplicationStateError):
            panel.controller.set_reporting_selection(ReportingSelection())
        first.release.set()
        with pytest.raises(ApplicationError):
            future.result(WAIT)
        stop.result(WAIT)
        window.bridge.refresh()
        assert panel.enabled.isEnabled()
        assert panel.controller.reporting_selection() is selected
        future = startup.application.start()
        assert second.entered.wait(WAIT)
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and observed.generation == 2
        assert observed.acknowledged_count == observed.failed_count == 0
        assert first.closed
        second.release.set()
        future.result(WAIT)


def test_reporting_failure_feedback_does_not_fault_production(
    qapp: QApplication, tmp_path: Path
) -> None:
    with (
        server() as peer,
        normal_window(tmp_path, Engine()) as (startup, window, panel),
    ):
        peer.fail_next(ReportStage.INITIALIZATION, "plc")
        panel.port.setValue(peer.port)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        window.bridge.start()
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.failed_count == 1
            )
        )
        window.bridge.refresh()
        snapshot = startup.application.snapshot()
        assert (
            snapshot.state is ApplicationState.RUNNING and snapshot.last_issue is None
        )
        assert snapshot.latest_production_result is not None
        assert snapshot.counters.cycles_completed == 1
        window.navigate(2)
        assert (
            "ACK --" in panel.summary.toolTip()
            and text("plc_last_error", window.language) in panel.summary.toolTip()
        )
        assert "initialization" in panel.summary.toolTip()
        assert text("plc_active", window.language) in panel.service_status.text()
        assert "ERR 1" in panel.summary.toolTip()
        assert (
            "generation=1" in panel.summary.toolTip()
            and "FinsPlcError" in panel.summary.toolTip()
        )
        assert "FinsPlcError" not in window.event_label.toolTip()


def test_active_report_drains_before_ready_and_restart_resnapshots(
    qapp: QApplication, tmp_path: Path
) -> None:
    first, second, disabled = Engine(), Engine(), Engine()
    with (
        server() as peer,
        normal_window(tmp_path, first, second, disabled) as (startup, window, panel),
    ):
        seed(peer, 50)
        peer.hold_next(ReportStage.SEQUENCE)
        panel.port.setValue(peer.port)
        panel.timeout.setValue(2)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        startup.application.start().result(WAIT)
        assert peer.entered.wait(WAIT)
        stop = startup.application.stop()
        first.release.set()
        window.bridge.refresh()
        assert not stop.done() and not panel.port.isEnabled()
        assert panel.service_status.text() == text("plc_stopping", window.language)
        page = window.communication
        page.port.setValue(peer.port)
        window.communication.persist_settings()
        page.area.setCurrentIndex(1)
        page.address.setValue(49)
        page.write_values.setText("7")
        assert not page.dispatch(RequestKind.WRITE)
        peer.release.set()
        stop.result(WAIT)
        window.bridge.refresh()
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and not observed.thread_alive
        assert (
            observed.acknowledged_count == 1 and observed.last_acknowledged is not None
        )
        assert observed.last_acknowledged.sequence == 51 and first.closed
        assert panel.service_status.text() == text("plc_enabled", window.language)
        assert "ACK 51" in panel.summary.toolTip()
        panel.timeout.setValue(0.2)
        window.communication.persist_settings()
        startup.application.start().result(WAIT)
        assert second.entered.wait(WAIT)
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.acknowledged_count == 1
            )
        )
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and observed.generation == 2
        assert (
            observed.last_acknowledged is not None
            and observed.last_acknowledged.sequence == 52
        )
        second.release.set()
        startup.application.stop().result(WAIT)
        window.bridge.refresh()
        panel.enabled.setChecked(False)
        window.communication.persist_settings()
        before = len(peer.packets)
        startup.application.start().result(WAIT)
        assert disabled.entered.wait(WAIT)
        assert panel.controller.reporting_snapshot() is None
        assert len(peer.packets) == before


def test_manual_execution_rechecks_protection_after_dispatch(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = Engine(hold=1)
    with server() as peer, normal_window(tmp_path, engine) as (startup, window, panel):
        panel.port.setValue(peer.port)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        guard = panel.controller.validate_manual_request
        caller = get_ident()
        entered, release = Event(), Event()

        def delayed_guard(request: CommunicationRequest) -> None:
            if get_ident() != caller:
                entered.set()
                assert release.wait(WAIT)
            guard(request)

        monkeypatch.setattr(panel.controller, "validate_manual_request", delayed_guard)
        page = window.communication
        page.port.setValue(peer.port)
        window.communication.persist_settings()
        page.area.setCurrentIndex(1)
        page.address.setValue(49)
        page.write_values.setText("7")
        try:
            assert page.dispatch(RequestKind.WRITE)  # No automatic owner yet.
            assert entered.wait(WAIT)
            startup.application.start()
            assert engine.entered.wait(WAIT)
            release.set()
            until(lambda: not page.busy)
            assert not peer.packets and page.last_response is None
            assert text("plc_reserved_write", window.language) in page.status.text()
        finally:
            release.set()


@pytest.mark.parametrize("transport", list(FinsTransport))
def test_internal_composition_keeps_fixed_settings_and_paused_lock(
    qapp: QApplication,
    tmp_path: Path,
    transport: FinsTransport,
) -> None:
    model, machine, recipe, paths = input_files(tmp_path)
    with ExitStack() as stack:
        peer = (
            stack.enter_context(TcpPeer())
            if transport is FinsTransport.TCP
            else stack.enter_context(server())
        )
        engine = Engine(hold=100)
        local = compose(
            model,
            machine,
            recipe,
            paths,
            engine_factory=lambda _: engine,
            reporting=LocalReportingSettings(
                port=peer.port, timeout=0.2, transport=transport
            ),
            replay_controls=True,
        )
        bridge = QtProductionBridge(local.application, local.store)
        window = ProductionWindow(bridge, (local.choice,))
        window.communication.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        try:
            assert window.communication.reporting_panel is None
            assert not local.application.reporting_editable
            local.application.step_replay().result(WAIT)
            until(
                lambda: (
                    (s := local.application.reporting_snapshot()) is not None
                    and s.acknowledged_count == 1
                )
            )
            replay = local.application.replay_snapshot()
            assert replay is not None and replay.state is ReplayState.PAUSED
            assert local.application.snapshot().state is ApplicationState.RUNNING
            with pytest.raises(ApplicationStateError):
                local.application.set_reporting_selection(ReportingSelection())
            window.communication.port.setValue(peer.port)
            window.communication.persist_settings()
            window.communication.area.setCurrentIndex(1)
            window.communication.address.setValue(49)
            window.communication.write_values.setText("7")
            assert not window.communication.dispatch(RequestKind.WRITE)
        finally:
            close_window(window)
        assert engine.closed


def test_tcp_application_stop_retains_active_reporting_owner_until_exit(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    with TcpPeer() as peer:
        peer.hold_next("bitmap")
        with normal_window(tmp_path, Engine(hold=100)) as (startup, window, panel):
            panel.protocol.setCurrentIndex(1)
            panel.port.setValue(peer.port)
            panel.timeout.setValue(5)
            panel.enabled.setChecked(True)
            window.communication.persist_settings()
            try:
                window.bridge.step_source()
                assert peer.entered.wait(WAIT)
                window.bridge.stop()
                until(
                    lambda: (
                        startup.application.snapshot().state
                        is ApplicationState.STOPPING
                    )
                )
                window.bridge.refresh()
                observed = panel.controller.reporting_snapshot()
                assert observed is not None and observed.thread_alive
                assert not panel.protocol.isEnabled()
                page = window.communication
                page.port.setValue(peer.port)
                window.communication.persist_settings()
                page.area.setCurrentIndex(1)
                page.address.setValue(49)
                page.write_values.setText("7")
                assert not page.dispatch(RequestKind.WRITE)
                peer.release.set()
                until(
                    lambda: (
                        startup.application.snapshot().state is ApplicationState.READY
                    )
                )
                window.bridge.refresh()
                stopped = panel.controller.reporting_snapshot()
                assert stopped is not None and not stopped.thread_alive
                assert stopped.acknowledged_count == 1 and stopped.cleanup_issue is None
                assert panel.protocol.isEnabled()
            finally:
                peer.release.set()


def test_fault_and_closed_keep_preferences_locked(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = Engine(fatal=True, hold=100)
    with normal_window(tmp_path, engine) as (startup, window, panel):
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        startup.application.start().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.FAULT)
        window.bridge.refresh()
        assert not panel.enabled.isEnabled()
        with pytest.raises(ApplicationStateError):
            panel.controller.set_reporting_selection(ReportingSelection())
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)
        window.bridge.refresh()
        assert not panel.port.isEnabled()
        with pytest.raises(ApplicationStateError):
            panel.controller.set_reporting_selection(ReportingSelection())


@pytest.mark.parametrize("manual_transport", list(FinsTransport))
@pytest.mark.parametrize("automatic_transport", list(FinsTransport))
def test_independent_transports_capture_reservation_and_stop_start_generation(
    qapp: QApplication,
    tmp_path: Path,
    manual_transport: FinsTransport,
    automatic_transport: FinsTransport,
) -> None:
    first, second = Engine(hold=1), Engine(hold=100)
    with ExitStack() as stack:
        automatic = (
            stack.enter_context(TcpPeer(88))
            if automatic_transport is FinsTransport.TCP
            else stack.enter_context(server())
        )
        manual = (
            stack.enter_context(TcpPeer())
            if manual_transport is FinsTransport.TCP
            else stack.enter_context(server())
        )
        if not isinstance(automatic, TcpPeer):
            seed(automatic, 88)
        before = len(automatic.packets)
        startup, window, panel = stack.enter_context(
            normal_window(tmp_path, first, second)
        )
        files = {p: p.read_bytes() for p in startup.data.root.rglob("*.json")}
        page = window.communication
        page.protocol.setCurrentIndex(1 if manual_transport is FinsTransport.TCP else 0)
        page.port.setValue(manual.port)
        window.communication.persist_settings()
        panel.source[1].setValue(3)
        panel.protocol.setCurrentIndex(
            1 if automatic_transport is FinsTransport.TCP else 0
        )
        panel.port.setValue(automatic.port)
        panel.timeout.setValue(0.2)
        panel.enabled.setChecked(True)
        window.communication.persist_settings()
        selected = panel.controller.reporting_selection()
        assert selected.settings.transport is automatic_transport
        window.bridge.step_source()
        assert first.entered.wait(WAIT)
        window.bridge.refresh()
        assert startup.application.snapshot().state is ApplicationState.STARTING
        assert len(automatic.packets) == before
        if isinstance(automatic, TcpPeer):
            assert automatic.connections == 0  # Warm-up does not even negotiate.
        assert not panel.protocol.isEnabled()
        panel.protocol.setCurrentIndex(1 - panel.protocol.currentIndex())
        assert panel.controller.reporting_selection() is selected
        assert panel.protocol.currentIndex() == (
            1 if automatic_transport is FinsTransport.TCP else 0
        )
        first.release.set()
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.acknowledged_count == 1
            )
        )
        until(
            lambda: (
                (r := window.bridge.control_snapshot()) is not None
                and r.state is ReplayState.PAUSED
            )
        )
        window.bridge.refresh()
        assert startup.application.snapshot().state is ApplicationState.RUNNING
        assert not panel.protocol.isEnabled()
        observed = panel.controller.reporting_snapshot()
        assert observed is not None and observed.generation == 1
        assert (
            observed.last_acknowledged is not None
            and observed.last_acknowledged.sequence == 89
        )
        result = startup.application.snapshot().latest_production_result
        assert result is not None
        words = first_station_reject_words(
            (r.channel_id, r.business_status) for r in result.channels
        )
        if isinstance(automatic, TcpPeer):
            assert automatic.words(49, 4) == (89, *words)
            assert all(
                p[3:9] == bytes.fromhex("00 14 00 00 65 00") for p in automatic.packets
            )
        else:
            assert readback(automatic) == (89, *words)
        counters = startup.application.snapshot().counters
        page.area.setCurrentIndex(1)
        page.address.setValue(49)
        page.write_values.setText("7")
        manual_before = len(manual.packets)
        assert page.dispatch(RequestKind.WRITE)
        until(lambda: not page.busy)
        assert len(manual.packets) > manual_before
        guarded = CommunicationRequest(
            RequestKind.WRITE,
            "127.0.0.1",
            manual.port,
            0.2,
            FinsAddress(0, 2, 0),
            FinsAddress(0, 1, 0),
            49,
            values=(7,),
            area=FinsMemoryArea.W,
            transport=manual_transport,
        )
        panel.controller.validate_manual_request(guarded)  # Independent endpoint.
        with pytest.raises(PermissionError):
            panel.controller.validate_manual_request(
                replace(guarded, port=automatic.port)
            )
        assert page.dispatch(RequestKind.READ)  # Reservation never blocks reads.
        until(lambda: not page.busy)
        assert page.last_response is not None
        assert page.last_response.request.transport is manual_transport
        assert panel.controller.reporting_selection() is selected
        assert startup.application.snapshot().counters == counters
        window.bridge.stop()
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        window.bridge.refresh()
        stopped = panel.controller.reporting_snapshot()
        assert (
            stopped is not None
            and not stopped.thread_alive
            and stopped.cleanup_issue is None
        )
        assert panel.controller.reporting_selection() is selected
        window.bridge.step_source()
        until(
            lambda: (
                (s := panel.controller.reporting_snapshot()) is not None
                and s.generation == 2
                and s.acknowledged_count == 1
            )
        )
        outcome = panel.controller.reporting_snapshot()
        assert outcome is not None and outcome.last_acknowledged is not None
        assert outcome.last_acknowledged.sequence == 90
        window.bridge.stop()
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        window.bridge.refresh()
        panel.protocol.setCurrentIndex(1 - panel.protocol.currentIndex())
        window.communication.persist_settings()
        assert (
            panel.controller.reporting_selection().settings.transport
            is not automatic_transport
        )
        assert page.protocol.currentIndex() == (
            1 if manual_transport is FinsTransport.TCP else 0
        )
        panel.protocol.setCurrentIndex(
            1 if automatic_transport is FinsTransport.TCP else 0
        )
        assert panel.source[1].value() == 3  # Inactive UDP values survive TCP switches.
        assert {p: p.read_bytes() for p in files} == files


def test_reporting_host_editor_preserves_invalid_draft_and_saved_endpoint(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    with normal_window(tmp_path) as (_, window, panel):
        panel.host.setText("192.0.2.42")
        assert panel.controller.reporting_selection().settings.host == "127.0.0.1"
        window.communication.persist_settings()
        assert panel.controller.reporting_selection().settings.host == "192.0.2.42"
        assert text("plc_next_start", panel.language) in panel.enabled.toolTip()
        panel.host.setText("not-an-ip")
        window.communication.persist_settings()
        assert panel.host.text() == "not-an-ip"
        assert panel.controller.reporting_selection().settings.host == "192.0.2.42"
        assert "not-an-ip" in window.communication.preferences_status.toolTip()
        assert panel.controller.reporting_snapshot() is None


def test_unsaved_reporting_draft_does_not_admit_or_change_active_session(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    engine = Engine(hold=9)
    with normal_window(tmp_path, engine) as (startup, window, panel):
        page = window.communication
        page.persist_settings()
        before = startup.data.communication.read_bytes()
        original = panel.controller.reporting_selection()
        panel.enabled.setChecked(True)
        panel.port.setValue(19600)
        startup.application.start().result(WAIT)
        assert engine.entered.wait(WAIT)
        window.bridge.refresh()
        assert not panel.enabled.isEnabled() and not panel.port.isEnabled()
        panel.enabled.click()
        assert (
            panel.enabled.isChecked()
        )  # A disabled draft control cannot stop reporting.
        assert panel.controller.reporting_selection() is original
        assert panel.controller.reporting_snapshot() is None
        page.web_panel.port.setValue(8766)
        page.persist_settings()  # Changed reporting is locked, including Save/Start races.
        assert startup.data.communication.read_bytes() == before
        assert page.dirty and panel.enabled.isChecked()
        engine.release.set()
        startup.application.stop().result(WAIT)
        window.bridge.refresh()
        page.persist_settings()
        assert panel.controller.reporting_selection().enabled
        assert panel.controller.reporting_selection().settings.port == 19600
        assert panel.controller.reporting_snapshot() is None


def test_save_and_start_share_admission_lock(
    qapp: QApplication, tmp_path: Path
) -> None:
    engine = Engine(hold=1)
    with normal_window(tmp_path, engine) as (startup, _, panel):
        entered, release, requested, finished = Event(), Event(), Event(), Event()
        errors: list[BaseException] = []
        selected = ReportingSelection(False, LocalReportingSettings(port=19601))

        def persist() -> None:
            entered.set()
            assert release.wait(WAIT)

        def save() -> None:
            try:
                panel.controller.persist_reporting_selection(selected, persist)
            except BaseException as exc:
                errors.append(exc)

        def start() -> None:
            requested.set()
            try:
                startup.application.start().result(WAIT)
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        saving = Thread(target=save, name="CommunicationSaveTest")
        starting = Thread(target=start, name="CommunicationStartTest")
        saving.start()
        try:
            assert entered.wait(WAIT)
            starting.start()
            assert requested.wait(WAIT)
            release.set()
            assert engine.entered.wait(WAIT)
            assert panel.controller.reporting_selection() == selected
            engine.release.set()
            assert finished.wait(WAIT)
            assert not errors and panel.controller.reporting_snapshot() is None
        finally:
            release.set()
            engine.release.set()
            saving.join(WAIT)
            if starting.ident is not None:
                starting.join(WAIT)
            assert not saving.is_alive() and not starting.is_alive()
