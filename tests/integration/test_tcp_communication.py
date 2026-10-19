"""Checkpoint A: existing manual/watch owner against independent TCP frames."""

from collections.abc import Sequence
from pathlib import Path
from threading import Event, get_ident
from threading import enumerate as threads

import pytest
from PySide6.QtWidgets import QApplication
from tests.fins_tcp_support import Fault, Stage, TcpPeer
from tests.qt_support import until

from vimo_demo.application.communication_preferences import CommunicationPreferences
from vimo_demo.communication import FinsMemoryArea, FinsTcpClient, FinsTransport
from vimo_demo.communication.operations import (
    CommunicationRequest,
    CommunicationResponse,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)
from vimo_demo.ui.communication_page import CommunicationPage
from vimo_demo.ui.production_style import text


def close(page: CommunicationPage) -> None:
    page.request_close(lambda: None)
    until(lambda: page.closed)
    page.deleteLater()
    assert not any(t.name == "FinsCommunication" for t in threads())


def test_tcp_read_capture_does_not_follow_programmatic_protocol_change(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()
    captured: list[CommunicationRequest] = []

    def execute(request: CommunicationRequest) -> CommunicationResponse:
        captured.append(request)
        entered.set()
        assert release.wait(5)
        return execute_request(request)

    with TcpPeer() as peer:
        page = CommunicationPage(execute=execute)
        page.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        page.show()
        page.protocol.setCurrentIndex(1)
        page.port.setValue(peer.port)
        page.persist_settings()
        try:
            assert page.dispatch(RequestKind.READ)
            assert entered.wait(5)
            assert not page.protocol.isEnabled()
            page.protocol.setCurrentIndex(0)
            release.set()
            until(lambda: not page.busy)
            assert captured[0].transport is FinsTransport.TCP
            assert "FINS/TCP" in page.feedback_details
            assert len(peer.packets) == 1
        finally:
            release.set()
            close(page)


def test_tcp_watch_refresh_and_active_close_observe_actual_exit(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    with TcpPeer(sequence=77) as peer:
        page = CommunicationPage()
        page.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        page.show()
        page.protocol.setCurrentIndex(1)
        page.port.setValue(peer.port)
        page.persist_settings()
        try:
            assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100))
            assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 2))
            assert page.start_monitoring()
            until(lambda: not page.busy)
            assert page.auto_refresh and len(peer.packets) == 2
            until(lambda: len(peer.packets) == 4 and not page.busy)
            assert not page.protocol.isEnabled()
            item = page.watch_table.item(1, 2)
            assert item is not None and item.text() == "77"
            page.stop_monitoring()
            peer.hold_next("read")
            assert page.read_watch_once()
            assert peer.entered.wait(5)
            page.request_close(lambda: None)
            assert not page.closed and page.busy
            peer.release.set()
            until(lambda: page.closed)
            assert len(peer.packets) == 5  # Unstarted second group skipped on close.
        finally:
            peer.release.set()
            close(page)


@pytest.mark.parametrize(
    ("stage", "fault", "key"),
    [
        ("handshake", "reject", "comm_protocol_error"),
        ("read", "timeout", "comm_timeout_error"),
        ("read", "plc", "comm_plc_error"),
    ],
)
def test_tcp_watch_errors_use_existing_feedback_and_stop_refresh(
    qapp: QApplication,
    tmp_path: Path,
    stage: Stage,
    fault: Fault,
    key: str,
) -> None:
    with TcpPeer() as peer:
        peer.fail_next(stage, fault)
        page = CommunicationPage()
        page.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        page.show()
        page.protocol.setCurrentIndex(1)
        page.port.setValue(peer.port)
        page.persist_settings()
        page.timeout.setValue(0.05)
        page.persist_settings()
        try:
            page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.DM, 100))
            assert page.start_monitoring()
            until(lambda: not page.busy)
            assert not page.auto_refresh
            assert text(key, "zh") in page.feedback_text
            assert text("comm_watch_failed", "zh") in page.watch_status.text()
        finally:
            close(page)


@pytest.mark.parametrize("area", list(FinsMemoryArea))
def test_tcp_roundtrip_bilingual_owner_and_routes(
    qapp: QApplication,
    tmp_path: Path,
    area: FinsMemoryArea,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership: list[tuple[str, int]] = []
    clients: list[FinsTcpClient] = []

    class ObservedClient(FinsTcpClient):
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            ownership.append(("create", get_ident()))
            super().__init__(host, port=port, timeout=timeout)
            assert self.source.node == 101 and self.destination.node == 20
            clients.append(self)

        def read_words(
            self,
            start: int,
            count: int = 1,
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> tuple[int, ...]:
            ownership.append(("read", get_ident()))
            return super().read_words(start, count, area=area)

        def write_words(
            self,
            start: int,
            values: Sequence[int],
            *,
            area: FinsMemoryArea = FinsMemoryArea.DM,
        ) -> None:
            ownership.append(("write", get_ident()))
            super().write_words(start, values, area=area)

        def close(self) -> None:
            ownership.append(("close", get_ident()))
            super().close()

    monkeypatch.setattr(
        "vimo_demo.communication.operations.FinsTcpClient", ObservedClient
    )
    with TcpPeer() as peer:
        page = CommunicationPage()
        page.configure_preferences(
            tmp_path / "communication.json", CommunicationPreferences()
        )
        page.set_language("zh")
        page.show()
        try:
            assert page.protocol.currentText() == "FINS/UDP"
            page.destination[1].setValue(33)
            page.source[1].setValue(44)
            page.advanced_button.click()
            page.protocol.setCurrentIndex(1)
            assert page.advanced.isHidden() and page.advanced_button.isHidden()
            page.port.setValue(peer.port)
            page.persist_settings()
            page.area.setCurrentIndex(0 if area is FinsMemoryArea.DM else 1)
            page.address.setValue(100)
            page.persist_settings()
            assert page.dispatch(RequestKind.TEST)
            until(lambda: not page.busy)
            observed = page.last_response
            for language in ("zh", "en"):
                page.set_language(language)
                assert page.last_response is observed
                assert page.feedback_text == text("comm_test_ok", language)
            page.set_language("zh")
            assert page.last_response is not None
            assert page.last_response.request.transport is FinsTransport.TCP
            page.write_values.setText("7 65535")
            assert page.dispatch(RequestKind.WRITE)
            until(lambda: not page.busy)
            for language in ("zh", "en"):
                page.set_language(language)
                assert page.feedback_text == text("comm_write_ok", language)
                assert peer.words(100, 2, area.value) == (7, 65535)
            page.set_language("zh")
            assert page.dispatch(RequestKind.READ, count=2)
            until(lambda: not page.busy)
            assert page.last_read is not None and page.last_read.values == (7, 65535)
            for language in ("zh", "en"):
                page.set_language(language)
                assert page.feedback_text == text("comm_read_ok", language)
                assert page.last_read is not None and page.last_read.values == (
                    7,
                    65535,
                )
                assert "FINS/TCP" in page.success.text()
            header = bytes.fromhex("80 00 02 00 14 00 00 65 00 01")
            location = bytes((area.value,)) + bytes.fromhex("00 64 00")
            assert peer.packets == (
                header + b"\x01\x01" + location + b"\x00\x01",
                header + b"\x01\x02" + location + bytes.fromhex("00 02 00 07 FF FF"),
                header + b"\x01\x01" + location + b"\x00\x02",
            ), area
            assert all(
                p[3:9] == bytes.fromhex("00 14 00 00 65 00") for p in peer.packets
            )
            page.protocol.setCurrentIndex(0)
            assert page.advanced.isEnabled() and page.advanced.isVisible()
            for language in ("zh", "en"):
                page.set_language(language)
                assert (
                    page.destination[1].value() == 33 and page.source[1].value() == 44
                )
        finally:
            close(page)
    assert all(c.closed for c in clients) and len(clients) == 3
    assert [action for action, _ in ownership] == [
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
        assert len({ident for _, ident in ownership[offset : offset + 3]}) == 1
    assert all(ident != get_ident() for _, ident in ownership)
