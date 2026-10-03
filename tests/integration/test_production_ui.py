"""Offscreen shell tests, actual application commands and controlled session Futures."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession, prepared, production
from tests.unit.test_production_inspection import source_frame

from vimo_demo.application import ApplicationState, ProductionApplication
from vimo_demo.inspection import CameraId, CycleId
from vimo_demo.inspection.production import ProductJudgment
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_style import Language, text
from vimo_demo.ui.production_window import ProductionWindow


@pytest.fixture
def screen(
    qapp: QApplication, tmp_path: Path
) -> Iterator[tuple[ProductionWindow, FakeSession]]:
    # Windows' offscreen plugin exposes no system fonts in this environment.
    # Read existing OS fonts for realistic metrics/Chinese glyphs; install nothing.
    if not QFontDatabase.families():
        for name in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    candidate = prepared(tmp_path)
    session = FakeSession(automatic_start=False)
    app = ProductionApplication(lambda _p, _g: session)
    app.configure(candidate)
    store = LatestDisplayFrameStore(candidate.configuration.layout.camera_ids)
    bridge = QtProductionBridge(app, store, poll_ms=10, close_timeout_ms=1000)
    choices = (
        RecipeChoice(candidate, "Recipe A"),
        RecipeChoice(
            replace(
                candidate, configuration=replace(candidate.configuration, recipe_id="B")
            ),
            "Recipe B",
        ),
    )
    window = ProductionWindow(bridge, choices)
    window.resize(1080, 1920)
    window.show()
    yield window, session
    if not session.ready.done():
        session.ready.set_result(None)
    if session.join_gate is not None:
        session.join_gate.set()
    session.cleanup_safe = True
    window.close()
    until(lambda: window.bridge.closed)
    assert app.join(0)
    window.deleteLater()


@pytest.mark.parametrize(
    "size,columns", [((1080, 1920), 2), ((1920, 1080), 3), ((900, 1200), 2)]
)
@pytest.mark.parametrize("language", ["zh", "en"])
def test_bilingual_geometry_and_navigation(
    screen: tuple[ProductionWindow, FakeSession],
    qapp: QApplication,
    size: tuple[int, int],
    columns: int,
    language: Language,
) -> None:
    window, _ = screen
    identities = tuple(id(card) for card in window.cards)
    window.set_language(language)
    window.resize(*size)
    qapp.processEvents()
    assert window.columns == columns and len(window.cards) == 6
    assert window.pages.currentIndex() == 0
    assert window.navigation[0].text() == text("production", language)
    for index, card in enumerate(window.cards):
        item = window.grid.itemAtPosition(index // columns, index % columns)
        assert item is not None and item.widget() is card
        assert (
            card.isVisible()
            and card.viewport.width() > 100
            and card.viewport.height() > 100
        )
        parent = card.parentWidget()
        assert parent is not None and parent.rect().contains(card.geometry())
        for button in card.indicators:
            assert card.rect().contains(button.geometry())
    for button in (
        *window.navigation,
        *window.buttons.values(),
        window.language_button,
    ):
        parent = button.parentWidget()
        assert parent is not None and parent.rect().contains(button.geometry())
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    window.navigation[2].click()
    assert window.pages.currentIndex() == 2
    assert text("placeholder", language) in window.placeholders["diagnostics"].text()
    window.navigation[0].click()
    window.resize(size[1], size[0])
    qapp.processEvents()
    assert tuple(id(card) for card in window.cards) == identities


class CommandReceiver(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.threads: list[int] = []

    @Slot(str)
    def receive(self, name: str) -> None:
        self.names.append(name)
        self.threads.append(get_ident())


def test_nonblocking_start_polling_commands_and_deferred_close(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    receiver = CommandReceiver()
    window.bridge.command_finished.connect(receiver.receive)
    assert (
        window.buttons["start"].isEnabled() and not window.buttons["stop"].isEnabled()
    )
    window.buttons["start"].click()
    until(session.start_entered.is_set)
    assert window.snapshot.state is ApplicationState.STARTING
    assert window.buttons["stop"].isEnabled() and not window.recipe.isEnabled()
    tick = Event()
    QTimer.singleShot(0, tick.set)
    until(tick.is_set)
    assert not session.ready.done()  # Qt is alive while startup is held.
    session.ready.set_result(None)
    until(lambda: "start" in receiver.names)
    assert receiver.threads == [get_ident()]
    assert window.snapshot.state is ApplicationState.RUNNING
    receipt = window.bridge.application.begin_cycle(CycleId(1))
    session.cycles[0].accepted.set_result(None)
    session.cycles[0].result.set_result(production(judgment=ProductJudgment.NG))
    assert receipt.result.done()
    until(lambda: window.metrics["channel_ng"][0].text() == "36")
    session.join_gate = Event()
    window.close()
    until(session.join_entered.is_set)
    assert window.isVisible() and not window.bridge.closed
    assert window.snapshot.state is ApplicationState.STOPPING
    assert all(not button.isEnabled() for button in window.buttons.values())
    tick.clear()
    QTimer.singleShot(0, tick.set)
    until(tick.is_set)
    window.close()  # Repeated close does not drop ownership or block.
    session.join_gate.set()
    until(lambda: window.bridge.closed)
    assert not window.isVisible()


def test_frames_match_identity_bgr_and_clear_stale_overlay(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    frame = source_frame()
    frame.image.setflags(write=True)
    frame.image[:] = (12, 34, 210)
    frame.image.setflags(write=False)
    store = window.bridge.frames
    store.publish(frame, generation=1)
    receipt = window.bridge.application.begin_cycle(CycleId(1))
    session.cycles[0].accepted.set_result(None)
    # Golden synthetic production has a real lightweight defect bbox in CAM1.
    from tests.unit.test_production_inspection import cycle_result, inferred, layout

    from vimo_demo.inference import BoundingRect
    from vimo_demo.inspection.production import ProductionPipeline

    result = ProductionPipeline(layout()).from_cycle(
        cycle_result(layout(), inferred(BoundingRect(2, 2, 3, 4)))
    )
    session.cycles[0].result.set_result(result)
    assert receipt.result.done()
    until(lambda: window.cards[0].result is not None)
    card = window.cards[0]
    assert card.viewport.boxes
    # A candidate layout change must not relabel the retained matching result.
    original_layout = card.camera_layout
    card.camera_layout = replace(
        original_layout,
        slots=tuple(
            replace(slot, channel_id=original_layout.slots[5 - i].channel_id)
            for i, slot in enumerate(original_layout.slots)
        ),
    )
    card.present(store.snapshot().frames[0], window.snapshot, window.language)
    assert "通道 1 ·" in card.indicators[0].accessibleName()
    card.camera_layout = original_layout
    color = card.viewport.image.pixelColor(0, 0)
    assert (color.red(), color.green(), color.blue()) == (210, 34, 12)
    assert window.cards[1].viewport.image.isNull()
    count = card.viewport.conversions
    for _ in range(3):
        window.bridge.refresh()
    assert card.viewport.conversions == count
    store.publish(source_frame(cycle=2), generation=1)
    until(lambda: card.result is None)
    assert not card.viewport.boxes and card.viewport.conversions == count + 1
    assert all(
        not button.text() and not button.icon().isNull() for button in card.indicators
    )
    # Correct camera address, independent of arrival ordering.
    store.publish(source_frame(CameraId("CAM6"), cycle=2), generation=1)
    until(lambda: not window.cards[5].viewport.image.isNull())
    assert window.cards[4].viewport.image.isNull()
    assert np.array_equal(frame.image[0, 0], (12, 34, 210))


def test_fault_controls_and_operator_message_redaction(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    session.failure = RuntimeError("secret SDK path C:/private/internal.dll")
    session.finished.set_result(None)
    until(
        lambda: (
            window.snapshot.state is ApplicationState.FAULT
            and not window.snapshot.runtime_owned
        )
    )
    assert (
        window.buttons["reset"].isEnabled() and not window.buttons["start"].isEnabled()
    )
    assert not window.recipe.isEnabled()
    assert "private" not in window.event_label.text()
    assert "SDK" not in window.event_label.text()
    window.buttons["reset"].click()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    window.bridge.select_recipe(window.choices[1])
    until(lambda: "B" in window.identity_label.text())
    assert (
        "product-a" in window.identity_label.text()
    )  # Last active differs from selected.
    window.bridge.start_batch("batch-X")
    until(lambda: "batch-X" in window.identity_label.text())


def test_close_failure_does_not_quit_or_release_unsafe_runtime(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    session.cleanup_safe = False
    window.close()
    until(lambda: window.snapshot.state is ApplicationState.FAULT)
    assert window.isVisible() and not window.bridge.closed
    assert window.snapshot.runtime_owned
    until(lambda: not window.bridge.closing)
    assert window.state_label.text() == text("FAULT", window.language)
    assert window.event_label.text() == text("fault_note", window.language)
    assert not window.buttons["reset"].isEnabled()
    session.cleanup_safe = True
    window.close()  # Explicit second attempt after safety has been restored.
    until(lambda: window.bridge.closed)


@pytest.mark.parametrize("scenario", ["normal", "ng", "warning", "fault"])
def test_demo_uses_real_application_runtime_and_exits(
    qapp: QApplication,
    tmp_path: Path,
    scenario: str,
) -> None:
    from threading import enumerate as threads
    from typing import cast

    from run_production_ui import Scenario, compose, demo_images

    from vimo_demo.inspection.production import TechnicalStatus

    composition = compose(demo_images(), tmp_path, cast(Scenario, scenario), 0.001)
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=10)
    window = ProductionWindow(bridge, composition.choices, simulation=True)
    window.show()
    try:
        bridge.start()
        until(
            lambda: (
                window.snapshot.state is ApplicationState.FAULT
                or window.snapshot.counters.cycles_completed > 0
            )
        )
        snapshot = window.snapshot
        if scenario == "fault":
            assert snapshot.state is ApplicationState.FAULT
        else:
            result = snapshot.latest_production_result
            assert result is not None and len(result.channels) == 36
            assert result.assessment.judgment is (
                ProductJudgment.NG
                if scenario == "ng"
                else ProductJudgment.UNKNOWN
                if scenario == "warning"
                else ProductJudgment.OK
            )
            assert result.assessment.technical_status is (
                TechnicalStatus.ERROR
                if scenario == "warning"
                else TechnicalStatus.SUCCESS
            )
        assert len(composition.store.snapshot().frames) == 6
    finally:
        window.close()
        until(lambda: bridge.closed)
        composition.source.close()
        assert composition.application.join(0)
        window.deleteLater()
    assert not any(t.name == "ProductionDemoSource" for t in threads())
