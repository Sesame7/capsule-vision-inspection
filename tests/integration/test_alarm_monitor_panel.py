"""Alarm observations are role-independent views of the existing owner, never reads."""

from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread, main_thread

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QComboBox, QMenu
from tests.support.application import WAIT
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, ensure_test_fonts, until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    provision_account,
    saved_data,
)
from tests.unit.test_batch_reporting import wait

from capsule_vision.application import ApplicationState
from capsule_vision.application.alarm_monitor import AlarmMonitor, AlarmSnapshot
from capsule_vision.application.identity_models import Role
from capsule_vision.communication.endpoints import FinsTcpEndpoint
from capsule_vision.configuration import ConfigurationPublication
from capsule_vision.configuration.alarm_definitions import (
    AlarmDefinitions,
    load_alarm_definitions,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    StatusPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.alarm_monitor_panel import AlarmMonitorPanel
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.style import Style, Theme, apply_theme, theme_colors

EXAMPLES = Path(__file__).parents[1] / "fixtures" / "alarms"


def cell(panel: AlarmMonitorPanel, row: int, column: int) -> str:
    item = panel.table.item(row, column)
    assert item is not None
    return item.text()


@pytest.mark.parametrize("address", (0, 18, 77, 118, 511))
def test_explicit_word_is_displayed_saved_and_discarded(
    qapp: QApplication, tmp_path: Path, address: int
) -> None:
    data = saved_data(tmp_path)
    saved = replace(
        CommunicationPreferences(),
        status=StatusPreferences(word_address=address),
    )
    save_communication_preferences(data.communication, saved)
    original = data.communication.read_bytes()
    startup = authenticated_startup(data)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page, panel = window.communication, window.communication.alarm_panel
    try:
        assert data.communication.read_bytes() == original
        assert not panel.findChildren(QComboBox) and not panel.findChildren(QMenu)
        assert panel.address.value() == address
        assert not panel.address.specialValueText()
        assert panel.address.text() == f"W{address}"
        panel.expand.click()
        until(lambda: not page.busy and panel.table.rowCount() == 11)
        bit = panel.table.item(0, 1)
        assert (
            bit
            and bit.text() == "0"
            and bit.toolTip() == f"W{saved.status.word_address}.00"
        )
        page.alarm_port.setValue(10001)
        page.persist_settings()
        until(lambda: not page.busy)
        persisted = load_communication_preferences(data.communication).status
        assert persisted.word_address == address
        assert startup.application.alarm_snapshot().selection == persisted
        assert not persisted.enabled
        page.alarm_address.setValue(42)
        page.reload_settings()
        until(lambda: not page.busy)
        assert page.alarm_address.value() == address
        page.alarm_address.setValue(211)
        page.persist_settings()
        until(lambda: not page.busy)
        persisted = load_communication_preferences(data.communication).status
        assert persisted.word_address == 211
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("language", ("zh", "en", "de", "ti"))
def test_signal_section_disclosure_layout_and_freshness(
    qapp: QApplication, language: Language
) -> None:
    ensure_test_fonts()
    panel = AlarmMonitorPanel()
    apply_theme(panel, "dark")
    panel.set_language(language)
    panel.resize(1000, panel.sizeHint().height())
    panel.show()
    definitions = load_alarm_definitions(EXAMPLES / "alternate-6.json")
    selected = StatusPreferences(True, word_address=77)
    panel.address.setValue(77)
    snapshot = AlarmSnapshot(
        "online",
        read_ok=True,
        write_ok=True,
        active=tuple(definitions.signals[:3]),
        selection=selected,
        definitions=definitions,
        raw_word=1,
        observed_at=1770000000,
    )
    try:
        panel.set_snapshot(snapshot)
        QApplication.processEvents()
        assert panel.applied_target.isHidden()
        assert panel.signal_header.y() > panel.controls.geometry().bottom()
        assert panel.controls.y() > panel.enabled.geometry().bottom()
        assert panel.details.isHidden() and panel.signal_header.isVisible()
        assert panel.active_count.text() == text("alarm_active_count", language).format(
            count=3
        )
        assert (
            "127.0.0.1" not in panel.observation.text()
            and "W77" not in panel.observation.text()
        )
        panel.expand.click()
        assert panel.details.isVisible()
        for column, key in enumerate(
            ("alarm_signal", "records_bit", "export_value", "alarm_state")
        ):
            header = panel.table.horizontalHeaderItem(column)
            assert header is not None and header.text() == text(key, language)
        active = panel.table.item(1, 3)
        assert active is not None and active.text() == text("alarm_active", language)
        assert cell(panel, 1, 2) == "0"  # Active-low stays distinct from the raw value.
        assert active.foreground().color().name() == theme_colors(panel).warning
        last_read = panel.observation.text()
        panel.set_snapshot(
            replace(snapshot, state="unavailable", read_ok=False, issue="read failed")
        )
        assert not panel.active_count.text() and panel.observation.text() == last_read
        assert active.foreground().color().name() == theme_colors(panel).secondary
        assert panel.evidence_status.text() == text("alarm_stale_values", language)
        for next_language in ("en", "zh", language):
            panel.set_language(next_language)
            panel.refresh()
            assert panel.expand.isChecked() and panel.details.isVisible()
        panel.controls.setFixedWidth(300)
        QApplication.processEvents()
        assert (
            panel.port.mapTo(panel.controls, panel.port.rect().topLeft()).y()
            > panel.protocol.mapTo(
                panel.controls, panel.protocol.rect().bottomRight()
            ).y()
        )
        for _ in range(3):
            panel.controls.setFixedWidth(970)
            QApplication.processEvents()
            position = panel.port.mapTo(panel.controls, panel.port.rect().topLeft())
            assert (
                position.y()
                == panel.protocol.mapTo(
                    panel.controls, panel.protocol.rect().topLeft()
                ).y()
            )
            panel.refresh()
            QApplication.processEvents()
            assert position == panel.port.mapTo(
                panel.controls, panel.port.rect().topLeft()
            )
        assert panel.table.horizontalScrollBar().maximum() == 0
        panel.expand.click()
        panel.set_language("en")
        panel.refresh()
        assert panel.details.isHidden() and panel.controls.isVisible()
    finally:
        panel.close()


@pytest.mark.parametrize("width", (1080, 1920))
@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("language", ("zh", "en", "de", "ti"))
def test_independent_address_row_is_aligned_and_equal_width(
    qapp: QApplication, width: int, theme: Theme, language: Language
) -> None:
    ensure_test_fonts()
    panel = AlarmMonitorPanel()
    apply_theme(panel, theme)
    panel.set_language(language)
    panel.resize(width, panel.sizeHint().height())
    panel.show()
    addresses = (
        panel.heartbeat_address,
        panel.fault_address,
        panel.address,
        panel.running_address,
    )
    try:
        for expanded in (True, False, True):
            panel.expand.setChecked(expanded)
            QApplication.processEvents()
            points = [
                widget.mapTo(panel, widget.rect().topLeft()) for widget in addresses
            ]
            widths = [widget.width() for widget in addresses]
            assert len({point.y() for point in points}) == 1
            assert max(widths) - min(widths) <= 1
            assert all(points[i].x() + widths[i] < points[i + 1].x() for i in (0, 1, 2))
            assert (
                panel.host.mapTo(panel, panel.host.rect().bottomRight()).y()
                < points[0].y()
            )
            assert all(
                widget.isVisible() and widget.isEnabled() for widget in addresses
            )
            for widget in addresses:
                assert panel.rect().contains(
                    widget.mapTo(panel, widget.rect().bottomRight())
                )
                assert widget.accessibleName() and widget.toolTip()
                assert widget.minimum() == 0 and widget.maximum() == 511
            before = tuple(widget.geometry() for widget in addresses)
            panel.refresh()
            panel.set_language(language)
            QApplication.processEvents()
            assert before == tuple(widget.geometry() for widget in addresses)
        panel.heartbeat_address.setValue(511)
        panel.fault_address.setValue(211)
        assert panel.address.value() == 18 and panel.heartbeat_address.value() == 511
        assert panel.running_address.text() == "W16.1"
        panel.running_address.setValue(511)
        assert panel.running_address.text() == "W511.1"
        panel.controls.setFixedWidth(300)
        QApplication.processEvents()
        assert (
            panel.running_address.mapTo(
                panel, panel.running_address.rect().topLeft()
            ).y()
            > panel.heartbeat_address.mapTo(
                panel, panel.heartbeat_address.rect().bottomRight()
            ).y()
        )
    finally:
        panel.close()


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("language", ("zh", "en"))
def test_service_health_and_draft_are_distinct_and_keep_header_slots(
    qapp: QApplication, theme: Theme, language: Language
) -> None:
    panel = AlarmMonitorPanel()
    apply_theme(panel, theme)
    panel.set_language(language)
    panel.resize(1000, panel.sizeHint().height())
    panel.show()
    definitions = load_alarm_definitions(EXAMPLES / "alternate-6.json")
    selection = StatusPreferences(True, word_address=77)
    colors = theme_colors(panel)
    try:
        panel.expand.click()
        QApplication.processEvents()
        widgets = (
            panel.title,
            panel.status,
            panel.enabled,
        )
        geometry = tuple(widget.geometry() for widget in widgets)
        panel.enabled.click()
        assert "*" in panel.enabled.text()
        assert panel.status.text() == text("service_off", language)
        assert colors.secondary in panel.status_indicator.styleSheet()
        assert text("status_enable_tip", language) in panel.enabled.toolTip()
        for snapshot, caption, color in (
            (
                AlarmSnapshot("waiting", selection=selection),
                "service_waiting",
                colors.warning,
            ),
            (
                AlarmSnapshot(
                    "unavailable", issue="first read failed", selection=selection
                ),
                "unavailable",
                colors.warning,
            ),
            (
                AlarmSnapshot(
                    "partial",
                    read_ok=True,
                    write_issue="write rejected",
                    issue="write rejected",
                    selection=selection,
                    definitions=definitions,
                    raw_word=1,
                    observed_at=1770000000,
                ),
                "service_partial",
                colors.warning,
            ),
            (
                AlarmSnapshot(
                    "online",
                    read_ok=True,
                    write_ok=True,
                    active=(definitions.signals[0],),
                    selection=selection,
                    definitions=definitions,
                    raw_word=1,
                    observed_at=1770000000,
                ),
                "service_online",
                colors.ok,
            ),
        ):
            panel.set_snapshot(snapshot)
            QApplication.processEvents()
            assert panel.status.text() == text(caption, language)
            assert color in panel.status_indicator.styleSheet()
            assert tuple(widget.geometry() for widget in widgets) == geometry
            assert panel.status_indicator.size().toTuple() == (8, 8)
            assert "*" not in panel.enabled.text()
        assert panel.active_count.text() == text("alarm_active_count", language).format(
            count=1
        )
        panel.set_snapshot(
            replace(panel.snapshot, state="unavailable", read_ok=False, issue="timeout")
        )
        assert colors.warning in panel.status_indicator.styleSheet()
        assert panel.evidence_status.isVisible()
        assert not panel.active_count.text()
        assert "2026" in panel.observation.text()
        assert cell(panel, 0, 3) == text("alarm_active", language)
        assert cell(panel, 0, 2) == "1"
        panel.expand.click()
        assert panel.controls.isVisible() and panel.enabled.isVisible()
        assert panel.enabled.height() == Style.touch
    finally:
        panel.close()


def test_alarm_header_power_requires_save_and_discard_restores_draft(
    qapp: QApplication, tmp_path: Path
) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        original = replace(
            CommunicationPreferences(),
            status=StatusPreferences(False, FinsTcpEndpoint(port=peer.port), 77),
        )
        save_communication_preferences(data.communication, original)
        startup = authenticated_startup(data)
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        page, panel = window.communication, window.communication.alarm_panel
        try:
            window.show()
            window.navigate(2)
            QApplication.processEvents()
            page.scroll_area.ensureWidgetVisible(panel.enabled)
            panel.enabled.setFocus()
            QTest.keyClick(panel.enabled, Qt.Key.Key_Space)
            assert panel.enabled.hasFocus() and page.dirty
            assert panel.snapshot.state == "off" and not peer.packets
            assert load_communication_preferences(data.communication) == original
            page.reload_settings()
            until(lambda: not page.busy)
            assert not page.dirty and not panel.enabled.isChecked()
            panel.enabled.click()
            page.persist_settings()
            until(lambda: not page.busy and panel.snapshot.state == "online")
            assert load_communication_preferences(data.communication).status.enabled
            panel.enabled.click()
            assert page.dirty and panel.snapshot.state == "online"
            assert "*" in panel.enabled.text()
            assert load_communication_preferences(data.communication).status.enabled
            page.persist_settings()
            until(lambda: not page.busy and panel.snapshot.state == "off")
            assert not load_communication_preferences(data.communication).status.enabled
            assert all(
                p[10:18]
                in (
                    bytes.fromhex("0101 b1 004d 00 0001"),
                    bytes.fromhex("0102 b1 002f 00 0001"),
                    bytes.fromhex("0102 b1 002e 00 0001"),
                )
                for p in peer.packets
            )
        finally:
            close_window(window)
            delivery.detach()


@pytest.mark.parametrize("language", ("zh", "en", "de", "ti"))
@pytest.mark.parametrize("width", (1000, 1840))
def test_signal_table_current_stale_offline_and_geometry(
    qapp: QApplication, language: Language, width: int
) -> None:
    ensure_test_fonts()
    definitions = load_alarm_definitions(EXAMPLES / "alternate-6.json")
    panel = AlarmMonitorPanel()
    panel.set_language(language)
    panel.resize(width, panel.sizeHint().height())
    panel.show()
    try:
        panel.expand.click()
        panel.set_preview(definitions)
        QApplication.processEvents()
        assert panel.table.rowCount() == 6
        assert cell(panel, 1, 2) == cell(panel, 1, 3) == "—"
        snapshot = AlarmSnapshot(
            "online",
            read_ok=True,
            write_ok=True,
            selection=StatusPreferences(True, word_address=211),
            definitions=definitions,
            raw_word=1,
            observed_at=1770000000.0,
        )
        panel.address.setValue(211)
        panel.set_snapshot(snapshot)
        assert cell(panel, 0, 2) == "1"
        assert cell(panel, 1, 2) == "0"
        assert cell(panel, 0, 3) == cell(panel, 1, 3) == text("alarm_active", language)
        assert cell(panel, 3, 3) == text("alarm_inactive", language)
        assert cell(panel, 1, 1) == "2"
        address = panel.table.item(1, 1)
        assert address is not None and address.toolTip() == "W211.02"
        assert panel.applied_target.isHidden()
        assert panel.refresh_preview.isHidden()
        panel.address.setValue(17)
        panel.host.setText("127.0.0.2")
        assert address.toolTip() == "W211.02"
        assert "127.0.0.1:9600" in panel.applied_target.text()
        assert "127.0.0.1" not in panel.observation.text()
        assert "127.0.0.1" not in panel.title.text()
        panel.set_snapshot(
            replace(snapshot, state="unavailable", read_ok=False, issue="test timeout")
        )
        assert cell(panel, 1, 2) == "0"
        assert cell(panel, 1, 3) == text("alarm_active", language)
        assert panel.evidence_status.text() == text("alarm_stale_values", language)
        assert panel.table.accessibleDescription() == panel.evidence_status.text()
        assert "2026" in panel.observation.text()
        assert "test timeout" in panel.evidence_status.toolTip()
        assert definitions.digest in panel.signal_count.accessibleDescription()
        long_signal = replace(
            definitions.signals[0], en="Long equipment description " * 5
        )
        panel.set_snapshot(
            replace(
                snapshot,
                definitions=replace(
                    definitions, signals=(long_signal, *definitions.signals[1:])
                ),
            )
        )
        panel.table.selectRow(2)
        QApplication.processEvents()
        columns = tuple(panel.table.columnWidth(c) for c in range(4))
        for _ in range(3):
            panel.refresh()
            QApplication.processEvents()
        assert tuple(panel.table.columnWidth(c) for c in range(4)) == columns
        assert panel.table.currentRow() == 2
        assert panel.table.horizontalScrollBar().maximum() == 0
        assert (
            panel.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
        )
        for widget in (panel.expand, panel.controls, panel.table):
            assert panel.rect().contains(
                widget.mapTo(panel, widget.rect().bottomRight())
            )
        panel.set_snapshot(
            AlarmSnapshot(
                "waiting", selection=snapshot.selection, definitions=definitions
            )
        )
        assert cell(panel, 1, 2) == "—" and panel.evidence_status.isHidden()
        assert not panel.observation.text()
        panel.set_snapshot(
            replace(
                panel.snapshot,
                state="unavailable",
                read_ok=False,
                issue="first read failed",
            )
        )
        assert cell(panel, 1, 2) == "—"
        assert "first read failed" == panel.evidence_status.text()
    finally:
        panel.close()


def test_live_card_interactions_do_not_admit_plc_reads(
    qapp: QApplication, tmp_path: Path
) -> None:
    monitor = AlarmMonitor(
        EXAMPLES / "alternate-6.json",
        lambda _: None,
        lambda: (123.0, None, None),
        state=lambda: ApplicationState.READY,
        interval=60,
    )
    panel = AlarmMonitorPanel()
    with TcpPeer() as peer:
        try:
            monitor.apply_saved(
                StatusPreferences(True, FinsTcpEndpoint(port=peer.port), 77)
            )
            wait(lambda: monitor.snapshot().state == "online")
            for _ in range(10):
                panel.set_snapshot(monitor.snapshot())
                panel.expand.click()
                panel.refresh()
                panel.set_language("en")
            assert len(peer.packets) == 3
            assert peer.packets[0][10:18] == bytes.fromhex("0102 b1 002f 00 0001")
            assert peer.packets[1][10:18] == bytes.fromhex("0102 b1 002e 00 0001")
            assert peer.packets[2][10:18] == bytes.fromhex("0101 b1 004d 00 0001")
            assert peer.connections == 1
        finally:
            monitor.request_stop()
            assert monitor.join(2)
            panel.close()


def test_all_roles_preview_offline_and_only_engineers_edit(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(data)
    app, identity = startup.application, startup.application.identity
    assert identity is not None
    provision_account(identity, "Operator", Role.OPERATOR, TEST_PASSWORD)
    provision_account(identity, "Engineer", Role.ENGINEER, TEST_PASSWORD)
    path = app.alarm_definitions_path()
    path.write_bytes((EXAMPLES / "standard-11.json").read_bytes())
    original = app.preview_alarm_definitions
    reads: list[str] = []

    def preview() -> AlarmDefinitions:
        assert current_thread() is not main_thread()
        reads.append("read")
        return original()

    def no_plc(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline preview contacted PLC")

    monkeypatch.setattr(app, "preview_alarm_definitions", preview)
    monkeypatch.setattr(
        "capsule_vision.application.alarm_monitor.FinsTcpClient", no_plc
    )
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page, panel = window.communication, window.communication.alarm_panel
    try:
        window.show()
        window.navigate(2)
        for username, role in (
            (None, Role.GUEST),
            ("Operator", Role.OPERATOR),
            ("Engineer", Role.ENGINEER),
            ("TestAdmin", Role.ADMINISTRATOR),
        ):
            if username is None:
                identity.logout()
            else:
                identity.login(username, TEST_PASSWORD).result(WAIT)
            window.identity_controls.refresh()
            panel.expand.click()
            until(lambda: not page.busy and panel.table.rowCount() == 11)
            assert panel.expand.isEnabled() and panel.table.isEnabled()
            assert panel.address.isEnabled() == (role >= Role.ENGINEER)
            assert panel.enabled.isEnabled() == (role >= Role.ENGINEER)
            assert text("alarm_preview", "en") in panel.observation.text()
            assert cell(panel, 0, 2) == "—"
            panel.expand.click()
        assert len(reads) == 4
        path.write_bytes((EXAMPLES / "alternate-6.json").read_bytes())
        panel.expand.click()
        until(lambda: not page.busy and panel.table.rowCount() == 6)
        window.set_language("de")
        assert cell(panel, 1, 0) == "Schutztür offen"
        assert cell(panel, 3, 0) == "Feeder fault"
        item = panel.table.item(1, 0)
        assert (
            item is not None
            and item.data(Qt.ItemDataRole.AccessibleTextRole) == "Schutztür offen"
        )
        path.write_bytes(b"{invalid")
        panel.refresh_preview.click()
        until(lambda: not page.busy)
        assert panel.table.rowCount() == 0 and panel.preview_issue
        assert path.read_bytes() == b"{invalid"
        path.write_bytes((EXAMPLES / "standard-11.json").read_bytes())
        QTest.mouseClick(panel.refresh_preview, Qt.MouseButton.LeftButton)
        until(lambda: not page.busy and panel.table.rowCount() == 11)
    finally:
        close_window(window)
        delivery.detach()


def test_offline_preview_close_waits_for_actual_worker(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    entered, release = Event(), Event()

    def preview() -> AlarmDefinitions:
        entered.set()
        assert release.wait(WAIT)
        return load_alarm_definitions(EXAMPLES / "standard-11.json")

    monkeypatch.setattr(app, "preview_alarm_definitions", preview)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    try:
        window.show()
        window.communication.alarm_panel.expand.click()
        until(entered.is_set)
        window.communication.request_close(lambda: None)
        assert not window.communication.closed
        release.set()
        until(lambda: window.communication.closed)
        assert window.communication.alarm_panel.preview is None
    finally:
        release.set()
        close_window(window)
        delivery.detach()


def test_late_offline_preview_cannot_replace_frozen_activation(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    app = startup.application
    entered, release = Event(), Event()
    observed = [AlarmSnapshot()]

    def preview() -> AlarmDefinitions:
        entered.set()
        assert release.wait(WAIT)
        return load_alarm_definitions(EXAMPLES / "standard-11.json")

    monkeypatch.setattr(app, "preview_alarm_definitions", preview)
    monkeypatch.setattr(app, "alarm_snapshot", lambda: observed[0])
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    page, panel = window.communication, window.communication.alarm_panel
    try:
        window.show()
        panel.expand.click()
        until(entered.is_set)
        observed[0] = AlarmSnapshot(
            "waiting",
            selection=StatusPreferences(True, word_address=120),
            definitions=load_alarm_definitions(EXAMPLES / "alternate-6.json"),
        )
        page.update_alarm_snapshot(observed[0])
        release.set()
        until(lambda: not page.busy)
        assert panel.preview is None and panel.table.rowCount() == 6
        assert cell(panel, 1, 1) == "2" and cell(panel, 1, 2) == "—"
        address = panel.table.item(1, 1)
        assert address and address.toolTip() == "W120.02"
    finally:
        release.set()
        close_window(window)
        delivery.detach()


def test_failed_address_save_preserves_applied_selection_and_discard(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        saved = replace(
            CommunicationPreferences(),
            status=StatusPreferences(True, FinsTcpEndpoint(port=peer.port), 77),
        )
        save_communication_preferences(data.communication, saved)
        startup = authenticated_startup(data)
        app = startup.application
        delivery = FutureDelivery()
        window = create_window(startup, delivery)
        page = window.communication
        try:
            until(lambda: app.alarm_snapshot().state == "online")
            original = data.communication.read_bytes()
            page.alarm_address.setValue(205)

            def denied(_publication: ConfigurationPublication) -> None:
                raise PermissionError("isolated save failure")

            with monkeypatch.context() as fail:
                fail.setattr(
                    "capsule_vision.configuration.ConfigurationPublication.publish",
                    denied,
                )
                page.persist_settings()
                until(lambda: not page.busy)
            assert (
                page.dirty
                and "isolated save failure" in page.preferences_status.toolTip()
            )
            assert data.communication.read_bytes() == original
            assert app.alarm_snapshot().selection == saved.status
            assert all(
                p[10:18]
                in (
                    bytes.fromhex("0101 b1 004d 00 0001"),
                    bytes.fromhex("0102 b1 002f 00 0001"),
                    bytes.fromhex("0102 b1 002e 00 0001"),
                )
                for p in peer.packets
            )
            page.reload_settings()
            until(lambda: not page.busy)
            assert not page.dirty and page.alarm_address.value() == 77
            assert load_communication_preferences(data.communication) == saved
        finally:
            close_window(window)
            delivery.detach()
