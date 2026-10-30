"""Native diagnostic composition, real localhost Modbus, no physical PLC."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QTableWidgetItem
from tests.support.modbus import ModbusPeer, frame
from tests.support.qt import until

from capsule_vision.communication import FinsMemoryArea
from capsule_vision.communication.modbus_tcp import HoldingRegisters, ModbusSettings
from capsule_vision.communication.operations import (
    CommunicationRequest,
    RegisterWatchGroup,
    RequestKind,
    execute_request,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
)
from capsule_vision.localization.languages import Language
from capsule_vision.ui.communication_page import CommunicationPage


def cell(page: CommunicationPage, row: int, column: int) -> QTableWidgetItem:
    item = page.watch_table.item(row, column)
    assert item is not None
    return item


@pytest.fixture
def page(qapp: QApplication, tmp_path: Path) -> Iterator[CommunicationPage]:
    widget = CommunicationPage()
    widget.configure_preferences(
        tmp_path / "communication.json", CommunicationPreferences()
    )
    widget.persist_settings()
    widget.show()
    yield widget
    widget.request_close(lambda: None)
    until(lambda: widget.closed)
    widget.deleteLater()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_protocol_save_discard_manual_and_watch_io(
    page: CommunicationPage,
    tmp_path: Path,
    language: Language,
) -> None:
    page.set_language(language)
    assert page.protocol.count() == 3
    page.protocol.setCurrentIndex(2)
    assert page.port.value() == 502
    assert page.area.count() == page.watch_area.count() == 1
    assert page.area.currentData() is HoldingRegisters.HOLDING
    assert page.watch_count.maximum() == 125
    assert page.address.maximum() == 65535
    page.advanced_button.click()
    assert page.modbus_advanced.isVisible() and not page.advanced.isVisible()
    assert page.unit_id.value() == 255
    with ModbusPeer() as peer:
        page.port.setValue(peer.port)
        page.unit_id.setValue(17)
        page.persist_settings()
        saved = load_communication_preferences(tmp_path / "communication.json")
        assert saved.diagnostic == ModbusSettings(port=peer.port, unit_id=17)
        assert saved.reporting.settings.endpoint.protocol == "fins_udp"
        assert not saved.reporting.enabled and not page.dirty
        page.host.setText("192.0.2.99")
        page.port.setValue(1)
        page.unit_id.setValue(99)
        assert page.dirty and page.dispatch(RequestKind.READ, count=2)
        until(lambda: not page.busy)
        assert page.last_read is not None and page.last_read.values == (1234, 65535)
        endpoint = page.last_read.request.endpoint
        assert isinstance(endpoint, ModbusSettings)
        assert endpoint.host == "127.0.0.1" and endpoint.unit_id == 17
        page.reload_settings()
        assert not page.dirty and page.unit_id.value() == 17
        for values in ("4321", "1, 65535"):
            page.write_values.setText(values)
            assert page.dispatch(RequestKind.WRITE)
            until(lambda: not page.busy)
            assert not page.feedback_error
        assert [packet[7] for packet in peer.requests] == [3, 6, 16]
        assert page.add_watch_group(
            RegisterWatchGroup(HoldingRegisters.HOLDING, 100, 2, "HR")
        )
        assert page.start_monitoring()
        until(lambda: not page.busy)
        page.stop_monitoring()
        assert cell(page, 0, 1).text() == "HR 100"
        assert cell(page, 0, 2).text() == "1"
        assert cell(page, 1, 3).text() == "FFFF"
        assert len(cell(page, 0, 4).text()) == 12
        assert all(packet[6] == 17 for packet in peer.requests)
        page.protocol.setCurrentIndex(0)
        assert page.port.value() == 9600 and not page.modbus_advanced.isVisible()
        assert page.area.count() == 2
        assert not page.dispatch(
            RequestKind.READ
        )  # Saved Modbus + explicit FINS area is incompatible.
        page.reload_settings()
        assert page.area.currentData() is HoldingRegisters.HOLDING


def test_existing_fins_watch_is_not_reinterpreted(page: CommunicationPage) -> None:
    assert page.add_watch_group(RegisterWatchGroup(FinsMemoryArea.W, 49, 4, "WR"))
    with ModbusPeer() as peer:
        page.protocol.setCurrentIndex(2)
        page.port.setValue(peer.port)
        page.persist_settings()
        assert not page.read_watch_once() and not page.start_monitoring()
        assert page.watch_groups[0].area is FinsMemoryArea.W
        assert not peer.requests and "Holding Register" in page.feedback_error
        assert page.remove_watch_group(0)
        assert page.add_watch_group(RegisterWatchGroup(HoldingRegisters.HOLDING, 100))
        assert page.read_watch_once()
        until(lambda: not page.busy)
        assert cell(page, 0, 2).text() == "1234"


def test_failed_watch_retains_success_and_close_observes_real_worker(
    page: CommunicationPage,
) -> None:
    with ModbusPeer() as peer:
        page.protocol.setCurrentIndex(2)
        page.port.setValue(peer.port)
        page.timeout.setValue(0.2)
        page.persist_settings()
        page.add_watch_group(RegisterWatchGroup(HoldingRegisters.HOLDING, 100))
        assert page.read_watch_once()
        until(lambda: not page.busy)
        timestamp = cell(page, 0, 4).text()

        def invalid(request: bytes) -> tuple[bytes, ...]:
            return (frame(b"\x03\x01\x00\x00"),)

        peer.respond = invalid
        assert page.read_watch_once()
        until(lambda: not page.busy)
        assert cell(page, 0, 4).text() == timestamp
        assert cell(page, 0, 2).text() == "1234"
        assert "ERR" in cell(page, 0, 0).text()
        assert "ERR" in cell(page, 0, 0).text() and page.feedback_error
        peer.received.clear()

        def hold(request: bytes) -> None:
            return None

        peer.respond = hold
        assert page.read_watch_once()
        until(peer.received.is_set)
        closed: list[bool] = []
        page.request_close(lambda: closed.append(True))
        assert page.busy and not page.closed and not closed
        until(lambda: page.closed)
        assert closed == [True] and len(peer.requests) == 3


def test_modbus_socket_work_runs_off_qt_dispatch(
    qapp: QApplication, tmp_path: Path
) -> None:
    owners: list[bool] = []

    def execute(request: CommunicationRequest):
        owners.append(QThread.currentThread() == qapp.thread())
        return execute_request(request)

    page = CommunicationPage(execute=execute)
    try:
        with ModbusPeer() as peer:
            settings = CommunicationPreferences(
                diagnostic=ModbusSettings(port=peer.port)
            )
            page.configure_preferences(tmp_path / "communication.json", settings)
            page.persist_settings()
            page.show()
            assert page.dispatch(RequestKind.READ)
            until(lambda: not page.busy)
            assert page.last_read is not None and page.last_read.values == (1234,)
            assert owners == [False]
    finally:
        page.request_close(lambda: None)
        until(lambda: page.closed)
        page.deleteLater()
