"""Offscreen shell tests, actual application commands and controlled session Futures."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QObject, QSize, QTimer, Slot
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QComboBox, QInputDialog, QWidget
from tests.qt_support import until
from tests.unit.test_production_application import FakeSession, prepared, production
from tests.unit.test_production_inspection import source_frame

from vimo_demo.application import (
    ApplicationState,
    ProductionApplication,
    ProductionCounters,
)
from vimo_demo.inspection import CameraId, CycleId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_style import Language, Style, text
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
    assert window.buttons["operation"].isEnabled() and window.buttons[
        "operation"
    ].text() == text("start", window.language)
    window.buttons["operation"].click()
    until(session.start_entered.is_set)
    assert window.snapshot.state is ApplicationState.STARTING
    assert window.buttons["operation"].isEnabled()
    page = window.pages.widget(0)
    assert page is not None and not page.findChildren(QComboBox)
    tick = Event()
    QTimer.singleShot(0, tick.set)
    until(tick.is_set)
    assert not session.ready.done()  # Qt is alive while startup is held.
    session.ready.set_result(None)
    until(lambda: "start" in receiver.names)
    assert receiver.threads == [get_ident()]
    assert window.snapshot.state is ApplicationState.RUNNING
    assert window.snapshot.counters == ProductionCounters()
    assert window.snapshot.latest_production_result is None
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
    assert window.buttons["operation"].isEnabled() and window.buttons[
        "operation"
    ].text() == text("reset", window.language)
    page = window.pages.widget(0)
    assert page is not None and not page.findChildren(QComboBox)
    assert "private" not in window.event_label.text()
    assert "SDK" not in window.event_label.text()
    window.buttons["operation"].click()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    window.bridge.select_recipe(window.choices[1])
    until(lambda: "Recipe B" in window.identity_label.toolTip())
    assert (
        "Recipe A" in window.identity_label.toolTip()
    )  # Last active differs from selected.
    window.bridge.start_batch("batch-X")
    until(lambda: "batch-X" in window.batch_label.text())


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
    assert not window.buttons["operation"].isEnabled()
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


@pytest.mark.parametrize(
    "state, action, enabled",
    [
        (ApplicationState.UNCONFIGURED, "start", False),
        (ApplicationState.READY, "start", True),
        (ApplicationState.STARTING, "stop", True),
        (ApplicationState.RUNNING, "stop", True),
        (ApplicationState.STOPPING, "STOPPING", False),
        (ApplicationState.FAULT, "reset", True),
        (ApplicationState.CLOSED, "CLOSED", False),
    ],
)
def test_single_stable_operation_area(
    screen: tuple[ProductionWindow, FakeSession],
    state: ApplicationState,
    action: str,
    enabled: bool,
) -> None:
    window, _ = screen
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=state, runtime_owned=False)
    )
    operation = window.buttons["operation"]
    assert operation.text() == text(action, window.language)
    assert operation.isEnabled() == enabled
    assert operation.size() == QSize(4 * Style.touch, Style.touch)
    assert not operation.icon().isNull()
    assert window.buttons["batch"].isEnabled() == (state is ApplicationState.READY)
    page = window.pages.widget(0)
    assert page is not None and not page.findChildren(QComboBox)
    assert set(window.metrics) == {"cycles", "channel_ng", "processing"}
    if state is ApplicationState.FAULT:
        window.bridge.snapshot_changed.emit(
            replace(window.snapshot, runtime_owned=True)
        )
        assert not operation.isEnabled()


@pytest.mark.parametrize(
    "displayed_state", [ApplicationState.RUNNING, ApplicationState.FAULT]
)
def test_stale_operation_does_not_turn_stop_or_reset_into_start(
    screen: tuple[ProductionWindow, FakeSession],
    displayed_state: ApplicationState,
) -> None:
    window, session = screen
    # Backend is already READY, but the last displayed operation is Stop/Reset.
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=displayed_state, runtime_owned=False)
    )
    window.buttons["operation"].click()
    assert window.bridge.application.snapshot().state is ApplicationState.READY
    assert not session.start_entered.is_set()
    assert window.buttons["operation"].text() == text("start", window.language)


def test_initial_reference_pixels_are_not_production_frames(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, _ = screen
    for camera in window.bridge.frames.cameras:
        window.bridge.frames.publish(source_frame(camera), generation=0)
    window.bridge.refresh()
    assert len(window.bridge.frames.snapshot().frames) == 6
    for card in window.cards:
        assert card.viewport.image.isNull() and not card.viewport.message
        assert card.result is None and not card.viewport.boxes
        assert all(
            text("pending", window.language) in b.accessibleName()
            for b in card.indicators
        )
    assert not window.event_label.text()
    assert window.snapshot.counters == ProductionCounters()
    assert window.snapshot.latest_production_result is None


def test_retained_stop_frames_and_generation_suppression(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    window.bridge.frames.publish(source_frame(), generation=1)
    receipt = window.bridge.application.begin_cycle(CycleId(1))
    session.cycles[0].accepted.set_result(None)
    session.cycles[0].result.set_result(production(judgment=ProductJudgment.NG))
    until(lambda: receipt.result.done() and window.cards[0].result is not None)
    card = window.cards[0]
    conversions = card.viewport.conversions
    counts = window.snapshot.counters
    window.buttons["operation"].click()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    assert not card.viewport.image.isNull()
    assert card.result is not None
    assert card.viewport.message == text("previous_inspection", window.language)
    assert window.snapshot.counters == counts
    # Immutable snapshot presentation: a new session never reuses old result indicators.
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            state=ApplicationState.STARTING,
            generation=2,
            latest_production_result=None,
        )
    )
    assert card.result is None and not card.viewport.boxes
    assert all(
        text("pending", window.language) in b.accessibleName() for b in card.indicators
    )
    assert card.viewport.message == text("previous_inspection", window.language)
    assert card.viewport.conversions == conversions
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            state=ApplicationState.RUNNING,
            latest_production_result=production(judgment=ProductJudgment.NG),
        )
    )
    assert (
        card.result is None
    )  # Even an identical cycle/sequence needs the new generation.
    window.bridge.frames.publish(source_frame(), generation=2)
    window.bridge.frames_changed.emit(window.bridge.frames.snapshot())
    assert card.result is not None and not card.viewport.message


def test_active_recipe_name_and_unactivated_replacement(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    assert (
        "Recipe: --" in window.identity_label.toolTip()
        or "配方: --" in window.identity_label.toolTip()
    )
    assert text("pending_recipe", window.language) in window.identity_label.toolTip()
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    assert "Recipe A" in window.identity_label.toolTip()
    assert (
        text("pending_recipe", window.language) not in window.identity_label.toolTip()
    )
    window.bridge.stop()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    # Name edits retain recipe ID; don't promote the new display name before Start.
    replacement = replace(window.choices[0], label="Updated A")
    window.parameters.applied.emit(replacement)
    assert "Recipe A" in window.identity_label.toolTip()
    assert (
        f"{text('pending_recipe', window.language)}: Updated A"
        in window.identity_label.toolTip()
    )
    window.bridge.select_recipe(
        window.choices[0]
    )  # Other saved Recipe B is now candidate.
    until(lambda: "Recipe B" in window.identity_label.toolTip())
    assert "Recipe A" in window.identity_label.toolTip()


@pytest.mark.parametrize(
    "accepted, value", [(False, "cancelled"), (True, ""), (True, "batch-Y")]
)
def test_batch_edit_confirmation_resets_only_when_accepted(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
    value: str,
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    receipt = window.bridge.application.begin_cycle(CycleId(1))
    session.cycles[0].accepted.set_result(None)
    session.cycles[0].result.set_result(production(judgment=ProductJudgment.NG))
    until(
        lambda: receipt.result.done() and window.snapshot.counters.cycles_completed == 1
    )
    counts = window.snapshot.counters
    window.bridge.stop()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    assert window.snapshot.counters == counts
    prompts: list[str] = []

    def answer(
        _parent: QWidget, _title: str, prompt: str, *, text: str
    ) -> tuple[str, bool]:
        prompts.append(prompt)
        return value, accepted

    monkeypatch.setattr(QInputDialog, "getText", answer)
    window.buttons["batch"].click()
    window.bridge.refresh()
    assert prompts == [text("batch_prompt", window.language)]
    assert "归档" in prompts[0] or "not archived" in prompts[0]
    if accepted:
        assert window.snapshot.counters == ProductionCounters()
        assert window.snapshot.latest_production_result is None
        assert window.snapshot.batch_id == (value or None)
    else:
        assert window.snapshot.counters == counts
        assert window.snapshot.latest_production_result is not None


def test_ng_is_quiet_but_technical_errors_keep_fixed_status_geometry(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    window.bridge.frames.publish(source_frame(), generation=1)
    QApplication.processEvents()
    grid_geometry = tuple(card.geometry() for card in window.cards)
    strip_height = window.event_frame.height()
    for cycle, status in ((1, TechnicalStatus.SUCCESS), (2, TechnicalStatus.ERROR)):
        receipt = window.bridge.application.begin_cycle(CycleId(cycle))
        session.cycles[-1].accepted.set_result(None)
        session.cycles[-1].result.set_result(
            production(cycle, status, ProductJudgment.NG)
        )
        until(
            lambda receipt=receipt, cycle=cycle: (
                receipt.result.done()
                and window.snapshot.counters.cycles_completed == cycle
            )
        )
        QApplication.processEvents()
        assert window.metrics["channel_ng"][0].text() == str(36 * cycle)
        if status is TechnicalStatus.SUCCESS:
            assert not window.event_label.text()
        else:
            assert text("technical_note", window.language) in window.event_label.text()
            assert "private" not in window.event_label.text()
        assert window.event_frame.height() == strip_height
        assert tuple(card.geometry() for card in window.cards) == grid_geometry


@pytest.mark.parametrize(
    "language,size,scenario",
    [
        ("zh", (1080, 1920), "ready"),
        ("zh", (1920, 1080), "warning"),
        ("en", (1080, 1920), "ng"),
        ("en", (1920, 1080), "ng"),
        ("en", (1920, 1080), "retained"),
    ],
)
def test_production_simplification_screenshot(
    qapp: QApplication,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    scenario: str,
) -> None:
    from run_production_ui import compose, demo_images

    if not QFontDatabase.families():
        for name in ("segoeui.ttf", "msyh.ttc"):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
    composition = compose(
        demo_images(), tmp_path, "warning" if scenario == "warning" else "ng", 0.001
    )
    bridge = QtProductionBridge(composition.application, composition.store, poll_ms=5)
    window = ProductionWindow(
        bridge,
        composition.choices,
        recipe_editor=composition.editor,
        simulation=True,
        language=language,
    )
    window.resize(*size)
    window.show()
    try:
        if scenario != "ready":
            bridge.start()
            until(lambda: window.snapshot.counters.cycles_completed >= 1)
            composition.source.close()  # Freeze one real fake-engine inspection frame set.
            if scenario == "retained":
                bridge.stop()
                until(lambda: window.snapshot.state is ApplicationState.READY)
        qapp.processEvents()
        assert window.summary.height() < 120
        assert sum(card.height() for card in window.cards) > window.height()
        for button in window.buttons.values():
            parent = button.parentWidget()
            assert parent is not None and parent.rect().contains(button.geometry())
            assert button.height() >= Style.touch
        if scenario == "ready":
            assert all(
                card.viewport.image.isNull() and not card.viewport.message
                for card in window.cards
            )
        else:
            assert all(not card.viewport.image.isNull() for card in window.cards)
        if scenario == "ng":
            assert not window.event_label.text()
            assert any(card.viewport.boxes for card in window.cards)
        if scenario == "warning":
            assert text("technical_note", language) in window.event_label.text()
        if scenario == "retained":
            assert all(
                text("previous_inspection", language) in card.viewport.message
                for card in window.cards
            )
        assert window.grab().save(
            str(tmp_path / f"production-{language}-{size[0]}x{size[1]}-{scenario}.png")
        )
    finally:
        window.close()
        until(lambda: bridge.closed)
        composition.source.close()
        assert composition.application.join(0)
        window.deleteLater()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_long_recipe_and_batch_keep_pending_activation_visible(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
) -> None:
    window, _ = screen
    window.set_language(language)
    window.choices = (
        replace(window.choices[0], label="很长的配方名称 Long Recipe " * 12),
    )
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, batch_id="long-batch-id-" * 12)
    )
    QApplication.processEvents()
    assert window.pending_label.isVisible()
    assert text("pending_badge", language) in window.pending_label.text()
    assert "Long Recipe" in window.pending_label.toolTip()
    assert window.batch_label.toolTip().endswith("long-batch-id-" * 12)
    assert window.identity_label.text().endswith("--")
    assert not window.buttons["batch"].text()
    assert window.buttons["operation"].text() == text("start", language)
