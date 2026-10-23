"""Communication workspace: controlled lifecycle and real loopback UDP, no GPU."""

import socket
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, get_ident
from threading import enumerate as threads

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from tests.application_support import FakeSession
from tests.qt_support import screen as screen
from tests.qt_support import until

from vimo_demo.application import ApplicationState
from vimo_demo.application.communication_preferences import CommunicationPreferences
from vimo_demo.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsPlcError,
    FinsProtocolError,
    FinsTimeoutError,
    FinsTransportError,
    FinsUdpClient,
)
from vimo_demo.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)
from vimo_demo.communication.simulator import FinsUdpSimulator
from vimo_demo.ui.communication_page import CommunicationPage
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow


def response(request: CommunicationRequest) -> CommunicationResponse:
    return CommunicationResponse(
        request,
        None if request.kind is RequestKind.WRITE else tuple(range(request.count)),
        1.25,
        datetime.now(UTC),
    )


class WatchProbe(CommunicationPage):
    @property
    def refresh_timer(self) -> QTimer:
        """Test-only deadline delivery; never change the production interval."""
        return self._auto_timer


@pytest.fixture
def page(qapp: QApplication, tmp_path: Path) -> Iterator[WatchProbe]:
    widget = WatchProbe()
    widget.configure_preferences(
        tmp_path / "communication.json", CommunicationPreferences()
    )
    widget.persist_settings()
    widget.show()
    yield widget
    widget.request_close(lambda: None)
    until(lambda: widget.closed)
    widget.deleteLater()


@pytest.fixture
def simulator() -> Iterator[FinsUdpSimulator]:
    stop = Event()
    failures: list[BaseException] = []
    with FinsUdpSimulator(port=0) as server:

        def serve() -> None:
            try:
                server.serve(stop)
            except BaseException as exc:
                failures.append(exc)

        owner = Thread(target=serve, name="h1b-test-simulator")
        owner.start()
        try:
            yield server
        finally:
            stop.set()
            owner.join(2)
            assert not owner.is_alive() and not failures
    assert server.closed


def test_idle_communication_drain_does_not_reenter_first_close(
    screen: tuple[ProductionWindow, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _ = screen
    close_states: list[tuple[bool, bool]] = []
    original_close = window.close

    def observed_close() -> bool:
        close_states.append((window.bridge.closed, window.communication.closed))
        return original_close()

    monkeypatch.setattr(window, "close", observed_close)
    assert not window.close()
    # Idle drain notification is synchronous inside this first close event.
    assert close_states == [(False, False)]
    until(lambda: not window.isVisible())
    assert close_states == [(False, False), (True, True)]
    assert window.bridge.application.join(0)


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("area", list(FinsMemoryArea))
def test_validation_remote_read_and_write(
    qapp: QApplication, tmp_path: Path, language: Language, area: FinsMemoryArea
) -> None:
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        return response(request)

    page = CommunicationPage(execute=execute)
    page.configure_preferences(
        tmp_path / "communication.json", CommunicationPreferences()
    )
    page.set_language(language)
    page.area.setCurrentIndex(0 if area is FinsMemoryArea.DM else 1)
    page.show()
    try:
        assert not calls and page.feedback_text == text("comm_idle", language)
        assert page.protocol.count() == 2 and page.protocol.isEnabled()
        assert page.host.isVisible() and not calls
        assert not page.advanced.isVisible()
        page.advanced_button.click()
        assert page.advanced.isVisible()
        page.host.setText("192.0.2.10")
        assert page.write_button.isEnabled() and page.write_button.isEnabled()
        assert text("comm_local_write", language) == page.permission.text()
        page.write_values.setText("4321")
        page.persist_settings()
        assert page.dispatch(RequestKind.WRITE)
        until(lambda: not page.busy)
        assert calls[-1].host == "192.0.2.10"
        page.destination[1].setValue(19)
        page.persist_settings()
        assert page.dispatch(RequestKind.READ)
        until(lambda: not page.busy)
        assert calls[-1].host == "192.0.2.10" and calls[-1].destination.node == 19
        page.host.setText("127.0.0.2")
        page.persist_settings()
        assert page.write_button.isEnabled() and page.dispatch(RequestKind.WRITE)
        until(lambda: not page.busy)
        assert calls[-1].host == "127.0.0.2"
        page.host.setText("localhost")
        page.persist_settings()
        assert "localhost" in page.preferences_status.toolTip()
        assert page.dirty
        page.host.setText("127.0.0.1")
        for values in ("", "-1", "65536", "abc", "1," + "0," * 998):
            page.write_values.setText(values)
            assert not page.dispatch(RequestKind.WRITE)
        page.address.setValue(area.word_count - 1)
        assert not page.dispatch(RequestKind.READ, count=2)
        assert len(calls) == 3
        assert page.write_button.toolTip() == text("comm_local_write", language)
    finally:
        page.request_close(lambda: None)
        until(lambda: page.closed)
        page.deleteLater()


def test_real_udp_timeout_and_malformed_response_feedback(
    page: CommunicationPage,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        page.port.setValue(silent.getsockname()[1])
        page.timeout.setValue(0.05)
        page.persist_settings()
        page.write_button.click()
        until(lambda: not page.busy)
        assert page.feedback_text == text("comm_write_unknown", page.language)
        silent.settimeout(0.05)
        assert silent.recvfrom(65535)[0][10:12] == b"\x01\x02"
        with pytest.raises(TimeoutError):
            silent.recvfrom(65535)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as malformed:
        malformed.bind(("127.0.0.1", 0))
        malformed.settimeout(1)
        page.port.setValue(malformed.getsockname()[1])
        page.timeout.setValue(1)
        page.persist_settings()
        failures: list[BaseException] = []

        def send() -> None:
            try:
                _, sender = malformed.recvfrom(65535)
                malformed.sendto(b"broken", sender)
            except BaseException as exc:
                failures.append(exc)

        owner = Thread(target=send, name="h1b-test-malformed")
        owner.start()
        try:
            page.dispatch(RequestKind.READ)
            until(lambda: not page.busy)
        finally:
            owner.join(2)
        assert not owner.is_alive() and not failures
        assert page.feedback_text == text("comm_protocol_error", page.language)


@pytest.mark.parametrize("fail", [False, True])
def test_navigation_busy_snapshot_and_production_isolation(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    window, _ = screen
    entered, release, tick = Event(), Event(), Event()
    requests: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        entered.set()
        assert release.wait(3)
        if fail:
            raise FinsProtocolError("controlled local failure")
        return response(request)

    # Inject only before dispatch; use the real page/close composition.
    monkeypatch.setattr(window.communication, "_execute", execute)
    page = window.communication
    baseline = window.bridge.application.snapshot()
    window.navigate(2)
    try:
        assert page.dispatch(RequestKind.READ)
        until(entered.is_set)
        assert not page.dispatch(RequestKind.READ)
        assert not page.host.isEnabled() and not page.write_button.isEnabled()
        QTimer.singleShot(0, tick.set)
        until(tick.is_set)
        page.host.setText("192.0.2.99")  # Programmatic edit cannot redirect snapshot.
        assert page.host.isVisible() and page.busy
        window.navigate(3)
        assert window.pages.currentIndex() == 3 and page.busy
        assert window.bridge.application.snapshot() == baseline
    finally:
        release.set()
        until(lambda: not page.busy)
    assert len(requests) == 1 and requests[0].host == "127.0.0.1"
    if fail:
        assert page.last_response is None
        assert page.feedback_text == text("comm_protocol_error", page.language)
    else:
        assert (
            page.last_response is not None
            and page.last_response.request.host == "127.0.0.1"
        )
    assert window.bridge.application.snapshot() == baseline
    assert not window.event_label.text()
    window.navigate(2)
    assert (page.last_response is not None) == (not fail)


def test_close_waits_for_actual_exit_not_future_notification(
    screen: tuple[ProductionWindow, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _ = screen
    returned, release = Event(), Event()

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        return response(request)

    class HeldThread(Thread):
        def run(self) -> None:
            super().run()
            returned.set()
            assert release.wait(3)

    monkeypatch.setattr("vimo_demo.ui.communication_page.Thread", HeldThread)
    monkeypatch.setattr(window.communication, "_execute", execute)
    page = window.communication
    try:
        assert page.dispatch(RequestKind.READ)
        until(returned.is_set)
        QApplication.processEvents()
        assert page.busy and page.last_response is None
        window.close()
        window.close()
        until(lambda: window.bridge.closed)
        assert window.isVisible() and not page.closed
        assert not page.dispatch(RequestKind.READ)
        with pytest.raises(RuntimeError, match="active"):
            page.dispose()
    finally:
        release.set()
        until(lambda: page.closed and not window.isVisible())
    assert page.last_response is None  # Success suppressed after closing.
    assert not any(t.name == "FinsCommunication" for t in threads())


def test_close_during_real_udp_wait_keeps_socket_on_worker_until_reply(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    window, _ = screen
    entered, release = Event(), Event()
    clients: list[FinsUdpClient] = []
    closes: list[int] = []
    failures: list[BaseException] = []

    class TrackedClient(FinsUdpClient):
        def __init__(
            self,
            host: str,
            *,
            destination: FinsAddress,
            source: FinsAddress,
            port: int,
            timeout: float,
        ) -> None:
            super().__init__(
                host, destination=destination, source=source, port=port, timeout=timeout
            )
            clients.append(self)

        def close(self) -> None:
            closes.append(get_ident())
            super().close()

    monkeypatch.setattr(
        "vimo_demo.communication.operations.FinsUdpClient", TrackedClient
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.bind(("127.0.0.1", 0))
        peer.settimeout(2)

        def serve() -> None:
            try:
                packet, sender = peer.recvfrom(65535)
                entered.set()
                assert release.wait(3)
                reply = (
                    bytes.fromhex("C0 00 02 00 01 00 00 02 00")
                    + packet[9:12]
                    + bytes.fromhex("00 00 04 D2")
                )
                peer.sendto(reply, sender)
            except BaseException as exc:
                failures.append(exc)

        owner = Thread(target=serve, name="h1b-test-close-peer")
        owner.start()
        page = window.communication
        page.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        page.port.setValue(peer.getsockname()[1])
        page.persist_settings()
        page.timeout.setValue(2)
        page.persist_settings()
        try:
            assert page.dispatch(RequestKind.READ)
            until(entered.is_set)
            window.close()
            until(lambda: window.bridge.closed)
            assert window.isVisible() and page.busy and not page.closed
            assert len(clients) == 1 and not clients[0].closed and not closes
        finally:
            release.set()
            owner.join(3)
            until(lambda: page.closed and not window.isVisible())
        assert not owner.is_alive() and not failures
        assert clients[0].closed and len(closes) == 1 and closes[0] != get_ident()
        assert not any(t.name == "FinsCommunication" for t in threads())


def test_application_close_failure_keeps_communication_usable(
    screen: tuple[ProductionWindow, FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    session.cleanup_safe = False
    entered, release = Event(), Event()

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        entered.set()
        assert release.wait(3)
        return response(request)

    monkeypatch.setattr(window.communication, "_execute", execute)
    page = window.communication
    try:
        assert page.dispatch(RequestKind.READ)
        until(entered.is_set)
        window.close()
        until(
            lambda: (
                not window.bridge.closing
                and window.snapshot.state is ApplicationState.FAULT
            )
        )
        assert window.isVisible() and page.busy and not page.closed
    finally:
        release.set()
        session.cleanup_safe = True
        until(lambda: not page.busy)
    assert page.write_button.isEnabled() and not page.closed
    assert page.last_response is not None
    assert page.dispatch(RequestKind.READ)
    until(lambda: not page.busy)


def test_stale_delivery_and_bounded_log(qapp: QApplication) -> None:
    class DeliveryProbe(CommunicationPage):
        def replay(self, name: str) -> None:
            self._completed(name, None, FinsProtocolError("stale"))

    page = DeliveryProbe(execute=response)
    try:
        for _ in range(102):
            assert page.dispatch(RequestKind.TEST)
            until(lambda: not page.busy)
        assert page.log_count == 100 and len(page.feedback_details.splitlines()) == 100
        current = page.feedback_text
        page.replay("1")
        assert page.feedback_text == current
        page.request_close(lambda: None)
        page.dispose()
        page.replay("102")
        assert page.closed and page.log_count == 100
    finally:
        page.request_close(lambda: None)
        page.deleteLater()


def test_failed_thread_start_leaves_no_owner_or_disabled_controls(
    page: CommunicationPage, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unstarted(Thread):
        def start(self) -> None:
            raise RuntimeError("thread startup unavailable")

    monkeypatch.setattr("vimo_demo.ui.communication_page.Thread", Unstarted)
    assert not page.dispatch(RequestKind.READ)
    assert not page.busy and page.write_button.isEnabled()
    assert page.feedback_text == text("comm_unexpected_error", page.language)
    assert not any(t.name == "FinsCommunication" for t in threads())


def test_unconfigured_shell_communication_has_no_production_dependency(
    qapp: QApplication, tmp_path: Path
) -> None:
    from vimo_demo.ui.bootstrap import ApplicationData, create_window, load_startup
    from vimo_demo.ui.inference_bridge import FutureDelivery

    delivery = FutureDelivery()
    startup = load_startup(ApplicationData.at_entry(tmp_path))
    window = create_window(startup, delivery)
    try:
        window.show()
        window.navigate(2)
        assert window.snapshot.state is ApplicationState.UNCONFIGURED
        assert window.communication.write_button.isEnabled()
        assert window.communication.last_response is None
        assert window.communication.log_count == 0
        assert not window.snapshot.runtime_owned
    finally:
        window.close()
        until(lambda: window.bridge.closed and window.communication.closed)
        window.deleteLater()
        delivery.detach()


def watch_tip(page: CommunicationPage, row: int, column: int) -> str:
    item = page.watch_table.item(row, column)
    assert item is not None
    return item.toolTip()


def watch_cell(page: CommunicationPage, row: int, column: int) -> str:
    item = page.watch_table.item(row, column)
    assert item is not None
    return item.text()


def test_compact_empty_and_bounded_populated_watch(
    page: CommunicationPage, qapp: QApplication
) -> None:
    assert page.watch_table.isHidden()
    assert page.watch_empty.isVisible()
    assert page.watch_editor.isHidden() and page.manual_body.isHidden()
    assert page.host.isVisible() and page.write_button.toolTip()
    page.watch_editor_button.click()
    assert page.watch_editor.isVisible()
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "A long watch-group name")
    )
    qapp.processEvents()
    short_height = page.watch_table.height()
    assert page.watch_table.isVisible() and not page.watch_empty.isVisible()
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100, 20, "Other"))
    qapp.processEvents()
    assert page.watch_table.height() > short_height
    height = page.watch_table.height()
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 200, 20))
    qapp.processEvents()
    assert page.watch_table.height() == height
    assert page.watch_table.verticalScrollBar().maximum() > 0
    assert page.watch_table.columnWidth(0) > 0
    for column, label in ((2, "DEC"), (3, "HEX")):
        header = page.watch_table.horizontalHeaderItem(column)
        assert header is not None and header.text() == label
    item = page.watch_table.item(0, 0)
    assert item is not None and "A long watch-group name" in item.toolTip()
    while page.watch_groups:
        assert page.remove_watch_group(0)
    assert page.watch_table.isHidden() and page.watch_empty.isVisible()


def test_settings_and_disclosure_do_not_dispatch_or_change_active_watch(
    page: CommunicationPage, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[CommunicationRequest] = []
    entered, release = Event(), Event()

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        entered.set()
        assert release.wait(3)
        return response(request)

    monkeypatch.setattr(page, "_execute", execute)
    page.port.setValue(19600)
    page.source[1].setValue(4)
    page.advanced_button.click()
    assert not requests and page.port.value() == 19600
    page.persist_settings()
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 4))
    assert page.start_monitoring()
    until(entered.is_set)
    try:
        assert not page.port.isEnabled()
        page.manual_button.click()
        page.watch_editor_button.click()
        assert page.manual_body.isVisible()
        assert not page.watch_add.isEnabled() and not page.write_button.isEnabled()
        page.set_language("en")
        assert page.host.accessibleName() == text("comm_host", "en")
        page.manual_button.click()
        assert page.auto_refresh and page.busy and len(requests) == 1
        assert requests[0].port == 19600 and requests[0].source.node == 4
    finally:
        page.stop_monitoring()
        release.set()
        until(lambda: not page.busy)
    assert not page.auto_refresh and not page.manual_body.isVisible()
    assert not requests[0].values


def test_watch_editor_validation_and_identity(page: CommunicationPage) -> None:
    assert not page.watch_groups and page.watch_table.rowCount() == 0
    assert not page.watch_once.isEnabled() and not page.watch_auto.isEnabled()
    assert not page.start_monitoring()
    page.watch_area.setCurrentIndex(1)
    page.watch_address.setValue(511)
    page.watch_count.setValue(2)
    page.watch_add.click()
    assert not page.watch_groups and not page.busy
    assert page.feedback_text == text("comm_invalid", page.language)
    page.watch_address.setValue(49)
    page.watch_count.setValue(4)
    page.watch_name.setText("First station")
    page.watch_add.click()
    assert page.watch_groups == (
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "First station"),
    )
    assert [watch_cell(page, i, 1) for i in range(4)] == ["W49", "W50", "W51", "W52"]
    assert watch_cell(page, 0, 2) == "--"
    assert watch_cell(page, 0, 4) == "--"
    assert text("comm_not_read", page.language) in watch_tip(page, 0, 4)
    # Equal ranges are separate named groups, not silently merged.
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "Other"))
    page.watch_selector.setCurrentIndex(0)
    page.watch_remove.click()
    assert page.watch_groups == (RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "Other"),)
    assert page.watch_table.rowCount() == 4
    page.watch_remove.click()
    assert not page.watch_groups and not page.watch_remove.isEnabled()
    assert (
        page.watch_table.editTriggers() == page.watch_table.EditTrigger.NoEditTriggers
    )


@pytest.mark.parametrize("word_limit", [False, True])
def test_watch_bounds(page: CommunicationPage, word_limit: bool) -> None:
    groups = (
        tuple(RegisterWatchGroup(FinsMemoryArea.DM, 0, n) for n in (999, 999, 50))
        if word_limit
        else tuple(RegisterWatchGroup(FinsMemoryArea.DM, i) for i in range(16))
    )
    for group in groups:
        assert page.add_watch_group(group)
    assert not page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 0))
    assert page.watch_groups == groups
    assert page.watch_table.rowCount() == (2048 if word_limit else 16)


def test_watch_localhost_mixed_groups_read_once_and_thread_ownership(
    page: CommunicationPage,
    simulator: FinsUdpSimulator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with FinsUdpClient(
        "127.0.0.1",
        port=simulator.port,
        destination=FinsAddress(0, 2, 0),
        source=FinsAddress(0, 1, 0),
    ) as client:
        client.write_words(49, (10, 32768, 65535, 15), area=FinsMemoryArea.W)
    calls: list[tuple[CommunicationRequest, int]] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append((request, get_ident()))
        return execute_request(request)

    monkeypatch.setattr(page, "_execute", execute)
    page.port.setValue(simulator.port)
    page.persist_settings()
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "Reject"))
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100, 1))
    assert not calls
    assert page.read_watch_once()
    until(lambda: not page.busy)
    assert [(r.area, r.start, r.count, r.kind) for r, _ in calls] == [
        (FinsMemoryArea.W, 49, 4, RequestKind.READ),
        (FinsMemoryArea.DM, 100, 1, RequestKind.READ),
    ]
    assert len({ident for _, ident in calls}) == 1
    assert all(ident != get_ident() for _, ident in calls)
    assert [watch_cell(page, i, 2) for i in range(5)] == [
        "10",
        "32768",
        "65535",
        "15",
        "1234",
    ]
    assert [watch_cell(page, i, 3) for i in range(5)] == [
        "000A",
        "8000",
        "FFFF",
        "000F",
        "04D2",
    ]
    assert watch_cell(page, 4, 1) == "D100"
    assert all(
        text("comm_current", page.language) in watch_tip(page, i, 4) for i in range(5)
    )
    assert page.last_read is None
    assert page.log_count == 1 and not page.auto_refresh
    assert not any(t.name == "FinsCommunication" for t in threads())


def test_auto_slow_pass_no_overlap_or_tick_backlog_and_snapshot(
    page: WatchProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    requests: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(3)
        return response(request)

    monkeypatch.setattr(page, "_execute", execute)
    for group in (
        RegisterWatchGroup(FinsMemoryArea.W, 50, 2),
        RegisterWatchGroup(FinsMemoryArea.DM, 112),
    ):
        assert page.add_watch_group(group)
    auto_size = page.watch_auto.size()
    assert page.start_monitoring()
    try:
        until(entered.is_set)
        assert page.auto_refresh and page.busy and page.watch_auto.isEnabled()
        assert page.watch_auto.size() == auto_size
        assert (
            not page.refresh_timer.isActive() and page.refresh_timer.interval() == 1000
        )
        assert not page.dispatch(RequestKind.READ) and not page.dispatch(
            RequestKind.WRITE
        )
        assert not page.read_watch_once() and not page.start_monitoring()
        assert not page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 3))
        assert not page.remove_watch_group(0)
        assert not page.host.isEnabled() and not page.watch_name.isEnabled()
        controls = (
            page.watch_selector,
            page.watch_editor_button,
            page.watch_remove,
            page.watch_once,
            page.watch_auto,
        )
        for width in (1920, 1080, 1920):
            page.resize(width, 1080)
            page.set_language("el")
            QApplication.processEvents()
            assert page.busy and page.auto_refresh and page.watch_auto.isChecked()
            assert not page.watch_remove.isEnabled() and not page.watch_once.isEnabled()
            assert controls == (
                page.watch_selector,
                page.watch_editor_button,
                page.watch_remove,
                page.watch_once,
                page.watch_auto,
            )
            assert (
                len(requests) == 1
            )  # Reflow never restarts or duplicates the active read.
        page.host.setText("192.0.2.99")
        page.destination[1].setValue(33)
        for _ in range(10):
            page.refresh_timer.timeout.emit()
            QApplication.processEvents()
        assert len(requests) == 1
    finally:
        release.set()
        until(lambda: not page.busy)
    assert len(requests) == 2
    assert all(r.host == "127.0.0.1" and r.destination.node == 2 for r in requests)
    assert page.refresh_timer.isActive() and not page.host.isEnabled()
    assert not page.dispatch(RequestKind.TEST)
    assert page.log_count == 0
    page.refresh_timer.stop()
    page.refresh_timer.timeout.emit()
    until(lambda: not page.busy)
    assert len(requests) == 4 and page.log_count == 0
    page.stop_monitoring()
    page.refresh_timer.timeout.emit()
    QApplication.processEvents()
    assert len(requests) == 4 and not page.auto_refresh
    assert page.watch_auto.size() == auto_size
    assert page.host.isEnabled() and page.write_button.isEnabled()


def test_monitor_failure_retains_per_group_timestamp_and_requires_restart(
    page: WatchProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    calls: list[CommunicationRequest] = []
    fail = False

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        if fail and request.area is FinsMemoryArea.W:
            raise FinsTimeoutError("controlled timeout")
        return CommunicationResponse(
            request, (len(calls),), 1, now + timedelta(seconds=len(calls))
        )

    monkeypatch.setattr(page, "_execute", execute)
    for group in (
        RegisterWatchGroup(FinsMemoryArea.DM, 112),
        RegisterWatchGroup(FinsMemoryArea.W, 50),
        RegisterWatchGroup(FinsMemoryArea.DM, 200),
    ):
        assert page.add_watch_group(group)
    assert page.read_watch_once()
    until(lambda: not page.busy)
    retained = watch_cell(page, 1, 4).split(" · ")[0]
    fail = True
    assert page.start_monitoring()
    until(lambda: not page.busy)
    assert len(calls) == 5
    assert not page.auto_refresh and not page.refresh_timer.isActive()
    assert page.feedback_text == text("comm_timeout_error", page.language)
    assert text("comm_watch_failed", page.language) in page.watch_status.text()
    assert watch_cell(page, 0, 2) == "4" and text(
        "comm_current", page.language
    ) in watch_tip(page, 0, 4)
    assert watch_cell(page, 1, 2) == "2"
    assert watch_cell(page, 1, 4) == retained
    assert "ERR" in watch_cell(page, 1, 0)
    assert text("comm_previous", page.language) in watch_tip(page, 1, 4)
    assert text("comm_previous", page.language) in watch_tip(page, 2, 4)
    assert page.log_count == 2
    page.refresh_timer.timeout.emit()
    QApplication.processEvents()
    assert len(calls) == 5
    page.set_language("en")
    assert text("comm_watch_failed", "en") in page.watch_status.text()
    assert "Previous" in watch_tip(page, 1, 4)
    fail = False
    assert page.start_monitoring()
    until(lambda: not page.busy)
    page.stop_monitoring()
    assert len(calls) == 8 and page.log_count == 2
    page.host.setText("localhost")  # Unsaved invalid text cannot redirect watches.
    page.persist_settings()
    assert page.dirty
    assert page.start_monitoring()
    until(lambda: not page.busy)
    page.stop_monitoring()
    assert len(calls) == 11 and not page.auto_refresh
    assert all(request.host == "127.0.0.1" for request in calls)


def test_watch_local_milliseconds_survive_invalid_read_and_match_auto_refresh(
    page: WatchProbe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = datetime(2026, 10, 14, 13, 22, 35, 123456, tzinfo=UTC)
    malformed = False

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        return CommunicationResponse(
            request, None if malformed else (1234,), 1, completed
        )

    monkeypatch.setattr(page, "_execute", execute)
    assert not page.watch_table.isVisible()
    assert page.add_watch_group(
        RegisterWatchGroup(FinsMemoryArea.DM, 100, name="Read timing")
    )
    assert page.read_watch_once()
    until(lambda: not page.busy)
    expected = completed.astimezone().strftime("%H:%M:%S.%f")[:-3]
    assert watch_cell(page, 0, 4) == expected and expected.endswith(".123")
    assert (
        page.watch_table.columnWidth(4)
        >= page.watch_table.fontMetrics().horizontalAdvance(expected) + 8
    )
    assert page.watch_table.columnCount() == 5
    heading = page.watch_table.horizontalHeaderItem(4)
    assert heading is not None and heading.text() == text(
        "comm_last_read", page.language
    )
    malformed = True
    completed = completed.replace(second=36, microsecond=567890)
    assert page.start_monitoring()
    until(lambda: not page.busy)
    assert watch_cell(page, 0, 4) == expected and watch_cell(page, 0, 2) == "1234"
    assert "ERR" in watch_cell(page, 0, 0) and "Invalid watch response" in watch_tip(
        page, 0, 4
    )
    assert text("comm_previous", page.language) in watch_tip(page, 0, 4)
    malformed = False
    assert page.start_monitoring()
    until(lambda: not page.busy)
    page.stop_monitoring()
    assert watch_cell(page, 0, 4) == completed.astimezone().strftime("%H:%M:%S.%f")[:-3]
    assert watch_cell(page, 0, 4).endswith(".567") and "ERR" not in watch_cell(
        page, 0, 0
    )
    assert page.remove_watch_group(0)
    assert not page.watch_table.isVisible() and page.watch_empty.isVisible()


@pytest.mark.parametrize("action", ["stop", "leave", "close"])
def test_active_monitor_stops_unstarted_groups_and_preserves_owner(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    window, _ = screen
    window.navigate(2)
    page = window.communication
    baseline = window.bridge.application.snapshot()
    entered, release = Event(), Event()
    calls: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        entered.set()
        assert release.wait(3)
        return response(request)

    monkeypatch.setattr(page, "_execute", execute)
    for group in (
        RegisterWatchGroup(FinsMemoryArea.W, 49, 4),
        RegisterWatchGroup(FinsMemoryArea.DM, 100),
    ):
        assert page.add_watch_group(group)
    assert page.start_monitoring()
    try:
        until(entered.is_set)
        if action == "stop":
            page.watch_auto.click()
        elif action == "leave":
            window.navigate(0)
        else:
            window.close()
            until(lambda: window.bridge.closed)
            assert window.isVisible() and not page.closed
        assert not page.auto_refresh and page.busy
        assert not page.write_button.isEnabled()
        with pytest.raises(RuntimeError, match="active"):
            page.dispose()
    finally:
        release.set()
        until(lambda: not page.busy)
    assert len(calls) == 1 and not any(t.name == "FinsCommunication" for t in threads())
    if action == "close":
        until(lambda: not window.isVisible())
        assert page.closed
    else:
        assert window.bridge.application.snapshot() == baseline
        assert not window.event_label.text()
        window.navigate(2)
        QApplication.processEvents()
        assert not page.auto_refresh and len(calls) == 1
        assert page.host.isEnabled()


def test_auto_waits_for_actual_exit_and_stopped_delivery_does_not_restart(
    page: WatchProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    returned, release = Event(), Event()

    class HeldThread(Thread):
        def run(self) -> None:
            super().run()
            returned.set()
            assert release.wait(3)

    monkeypatch.setattr("vimo_demo.ui.communication_page.Thread", HeldThread)
    monkeypatch.setattr(page, "_execute", response)
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 50))
    assert page.start_monitoring()
    try:
        until(returned.is_set)
        QApplication.processEvents()
        assert page.busy and not page.refresh_timer.isActive()
        assert watch_cell(page, 0, 2) == "--"
        page.stop_monitoring()
    finally:
        release.set()
        until(lambda: not page.busy)
    assert not page.auto_refresh and not page.refresh_timer.isActive()
    assert text("comm_watch_stopped", page.language) in page.watch_status.text()
    assert not any(t.name == "FinsCommunication" for t in threads())


def test_auto_start_failure_leaves_no_timer_or_owner(
    page: WatchProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unstarted(Thread):
        def start(self) -> None:
            raise RuntimeError("controlled startup failure")

    monkeypatch.setattr("vimo_demo.ui.communication_page.Thread", Unstarted)
    page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100))
    assert not page.start_monitoring()
    assert not page.auto_refresh and not page.busy and not page.refresh_timer.isActive()
    assert text("comm_watch_failed", page.language) in page.watch_status.text()


@pytest.mark.parametrize("area", list(FinsMemoryArea))
def test_udp_roundtrip_bilingual_owner(
    page: CommunicationPage,
    simulator: FinsUdpSimulator,
    monkeypatch: pytest.MonkeyPatch,
    area: FinsMemoryArea,
) -> None:
    packets: list[tuple[bytes, bytes | None]] = []
    handle = simulator.handle_datagram

    def capture(packet: bytes) -> bytes | None:
        reply = handle(packet)
        packets.append((packet, reply))
        return reply

    monkeypatch.setattr(simulator, "handle_datagram", capture)
    identities: list[tuple[str, int]] = []
    clients: list[FinsUdpClient] = []

    class TrackedClient(FinsUdpClient):
        def __init__(
            self,
            host: str,
            *,
            destination: FinsAddress,
            source: FinsAddress,
            port: int,
            timeout: float,
        ) -> None:
            identities.append(("create", get_ident()))
            super().__init__(
                host, destination=destination, source=source, port=port, timeout=timeout
            )
            clients.append(self)

        def read_words(
            self,
            start: int,
            count: int = 1,
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> tuple[int, ...]:
            identities.append(("read", get_ident()))
            return super().read_words(start, count, area=area)

        def write_words(
            self,
            start: int,
            values: Sequence[int],
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> None:
            identities.append(("write", get_ident()))
            super().write_words(start, values, area=area)

        def close(self) -> None:
            identities.append(("close", get_ident()))
            super().close()

    monkeypatch.setattr(
        "vimo_demo.communication.operations.FinsUdpClient", TrackedClient
    )
    page.set_language("zh")
    page.port.setValue(simulator.port)
    page.persist_settings()
    page.area.setCurrentIndex(0 if area is FinsMemoryArea.DM else 1)
    page.persist_settings()
    page.dispatch(RequestKind.TEST)
    until(lambda: not page.busy)
    assert page.last_response is not None and page.last_response.values == (
        (1234,) if area is FinsMemoryArea.DM else (0,)
    )
    assert page.last_response.request.area is area
    observed = page.last_response
    for language in ("zh", "en"):
        page.set_language(language)
        assert page.last_response is observed
        assert page.feedback_text == text("comm_test_ok", language)
    page.set_language("zh")
    page.write_values.setText("0, 65535 4321")
    page.write_button.click()
    until(lambda: not page.busy)
    assert page.last_response is not None and page.last_response.values is None
    observed = page.last_response
    for language in ("zh", "en"):
        page.set_language(language)
        assert page.last_response is observed
        assert page.feedback_text == text("comm_write_ok", language)
    page.set_language("zh")
    assert page.last_read is not None and page.last_read.values == (
        (1234,) if area is FinsMemoryArea.DM else (0,)
    )
    page.dispatch(RequestKind.READ, count=3)
    until(lambda: not page.busy)
    assert page.last_response is not None and page.last_response.values == (
        0,
        65535,
        4321,
    )
    for language in ("zh", "en"):
        page.set_language(language)
        assert page.feedback_text == text("comm_read_ok", language)
        assert page.last_response is not None and page.last_response.values == (
            0,
            65535,
            4321,
        )
    assert page.log_count == 3 and "ms" in page.success.text()
    assert [name for name, _ in identities] == [
        "create",
        "read",
        "close",
        "create",
        "write",
        "close",
        "create",
        "read",
        "close",
    ]
    for offset in (0, 3, 6):
        assert len({ident for _, ident in identities[offset : offset + 3]}) == 1
    assert all(ident != get_ident() for _, ident in identities)
    assert all(client.closed for client in clients)
    assert not any(t.name == "FinsCommunication" for t in threads())
    header = bytes.fromhex("80 00 02 00 02 00 00 01 00 01")
    reply_header = bytes.fromhex("C0 00 02 00 01 00 00 02 00 01")
    location = bytes((area.value,)) + bytes.fromhex("00 64 00")
    first = bytes.fromhex("04 D2") if area is FinsMemoryArea.DM else bytes(2)
    assert packets == [
        (
            header + b"\x01\x01" + location + b"\x00\x01",
            reply_header + b"\x01\x01\x00\x00" + first,
        ),
        (
            header + b"\x01\x02" + location + bytes.fromhex("00 03 00 00 FF FF 10 E1"),
            reply_header + b"\x01\x02\x00\x00",
        ),
        (
            header + b"\x01\x01" + location + b"\x00\x03",
            reply_header + bytes.fromhex("01 01 00 00 00 00 FF FF 10 E1"),
        ),
    ], area


@pytest.mark.parametrize("language", ["zh", "en"])
def test_localized_failures_settle_each_operation(
    qapp: QApplication, language: Language
) -> None:
    rows = (
        (FinsTimeoutError("deadline"), "comm_timeout_error"),
        (FinsTransportError("network unreachable"), "comm_transport_error"),
        (FinsProtocolError("wrong SID"), "comm_protocol_error"),
        (FinsPlcError(b"\x01\x01", 0x1103, b""), "comm_plc_error"),
    )
    calls: list[CommunicationRequest] = []
    error: BaseException = rows[0][0]

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        calls.append(request)
        raise error

    page = CommunicationPage(execute=execute)
    try:
        for index, (error, key) in enumerate(rows):
            try:
                page.set_language(language)
                assert not page.busy and len(calls) == page.log_count == index
                assert page.dispatch(RequestKind.READ)
                until(
                    lambda index=index: (
                        not page.busy and len(calls) == page.log_count == index + 1
                    )
                )
                assert text(key, language) in page.feedback_text
                assert type(error).__name__ in page.feedback_error
                assert str(error) in page.feedback_error
                if isinstance(error, FinsPlcError):
                    assert (
                        "0x1103" in page.feedback_text
                        and "0x1103" in page.feedback_details
                    )
                page.set_language("en" if language == "zh" else "zh")
                assert text(key, page.language) in page.feedback_text
                assert not any(t.name == "FinsCommunication" for t in threads())
            except Exception as exc:
                exc.add_note(
                    f"Localized failure: language={language}, row={index}, key={key}, error={error!r}"
                )
                raise
    finally:
        page.request_close(lambda: None)
        until(lambda: page.closed)
        page.deleteLater()
