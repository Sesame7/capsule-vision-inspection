"""Offscreen shell tests, actual application commands and controlled session Futures."""

from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QInputDialog, QLabel, QWidget
from tests.application_support import FakeSession, production
from tests.business_support import channel, mixed_result
from tests.production_support import source_frame
from tests.qt_support import assert_full_caption, until
from tests.qt_support import screen as screen

from vimo_demo.application import (
    ApplicationState,
    ConfigurationIdentity,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.application.local_replay import ReplaySnapshot, ReplayState
from vimo_demo.application.preferences import LANGUAGES
from vimo_demo.inspection import CameraId, CycleId
from vimo_demo.inspection.production import (
    Assessment,
    FindingSource,
    OccupancyResult,
    OccupancyState,
    ProductJudgment,
    Reason,
    TechnicalStatus,
)
from vimo_demo.ui.camera_card import CameraCard
from vimo_demo.ui.display_frames import DisplayFrame
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_presentation import Indicator
from vimo_demo.ui.production_style import (
    Language,
    Style,
    camera_icon,
    indicator_icon,
    navigation_icon,
    production_icon,
    text,
)
from vimo_demo.ui.production_window import ProductionWindow


def test_snapshot_and_pixels_present_cards_once_per_refresh(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    calls: list[CameraId] = []
    original = CameraCard.present

    def present(
        card: CameraCard,
        frame: DisplayFrame | None,
        snapshot: ProductionApplicationSnapshot,
        language: Language,
    ) -> None:
        calls.append(card.camera_layout.camera_id)
        original(card, frame, snapshot, language)

    monkeypatch.setattr(CameraCard, "present", present)
    app = window.bridge.application
    receipt = app.begin_cycle(CycleId(1))
    session.cycles[-1].accepted.set_result(None)
    session.cycles[-1].result.set_result(production())
    assert receipt.result.done()
    for camera in window.bridge.frames.cameras:
        window.bridge.frames.publish(source_frame(camera), generation=1)
    window.bridge.refresh()
    assert calls == list(window.bridge.frames.cameras)
    assert all(
        card.result
        and card.result.frame_id == source_frame(card.camera_layout.camera_id).id
        for card in window.cards
    )
    assert window.snapshot.counters.cycles_completed == 1
    calls.clear()
    window.bridge.refresh()
    assert not calls  # Unchanged observation does no card work.
    styles: list[QWidget] = []
    apply_style = QWidget.setStyleSheet

    def stylesheet(widget: QWidget, value: str) -> None:
        styles.append(widget)
        apply_style(widget, value)

    monkeypatch.setattr(QWidget, "setStyleSheet", stylesheet)
    for card, frame in zip(
        window.cards, window.bridge.frames.snapshot().frames, strict=True
    ):
        card.present(frame, window.snapshot, window.language)
    assert not styles  # Identical live status must not repolish any card/button.


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
    ].text() == text("replay_start", window.language)
    window.buttons["operation"].click()
    until(session.start_entered.is_set)
    assert window.snapshot.state is ApplicationState.STARTING
    assert (
        window.buttons["stop"].isEnabled()
        and not window.buttons["operation"].isEnabled()
    )
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
    from tests.production_support import cycle_result, inferred, layout

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
    assert all(not button.pixmap().isNull() for button in card.indicators)
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
    assert window.buttons["secondary"].isEnabled()
    assert window.buttons["secondary"].accessibleName() == text(
        "reset", window.language
    )
    page = window.pages.widget(0)
    assert page is not None and not page.findChildren(QComboBox)
    assert "private" not in window.event_label.text()
    assert "SDK" not in window.event_label.text()
    window.buttons["secondary"].click()
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
    assert window.state_label.text() == text("state_FAULT", window.language)
    assert text("shutdown_error", window.language) in window.event_label.text()
    assert window.snapshot.last_issue is not None
    assert window.snapshot.last_issue.exception_type in window.event_label.text()
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
    window.resize(1920, 1080)
    window.show()
    try:
        first, next_recipe = composition.choices
        active_a = ConfigurationIdentity.from_prepared(first.prepared)
        candidate_b = ConfigurationIdentity.from_prepared(next_recipe.prepared)
        bridge.start_batch("LOT-20261009-001")
        qapp.processEvents()
        assert window.snapshot.state is ApplicationState.READY
        assert window.snapshot.candidate == active_a
        assert window.snapshot.active is None
        assert window.snapshot.counters == ProductionCounters()
        assert window.metrics["cycles"][0].text() == "0"
        assert window.metrics["channel_ng"][0].text() == "0"
        assert window.identity_label.text() == first.label
        assert not window.pending_label.isVisible()
        assert (
            window.batch_label.text()
            == f"{text('batch_id', window.language)} LOT-20261009-001"
        )
        bridge.start()
        until(
            lambda: (
                window.snapshot.state is ApplicationState.FAULT
                or window.snapshot.counters.cycles_completed > 0
            )
        )
        snapshot = window.snapshot
        assert snapshot.active == snapshot.candidate == active_a
        assert window.identity_label.text() == first.label
        assert not window.pending_label.isVisible()
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
        if scenario != "fault":
            bridge.stop()
            until(lambda: window.snapshot.state is ApplicationState.READY)
            composition.source.close()  # Readiness-only restart, with no new cycles.
            counters = window.snapshot.counters
            bridge.select_recipe(next_recipe)
            for language in ("zh", "en"):
                window.set_language(language)
                qapp.processEvents()
                assert window.snapshot.state is ApplicationState.READY
                assert window.snapshot.active == active_a
                assert window.snapshot.candidate == candidate_b
                assert composition.application.snapshot().active == active_a
                assert window.snapshot.counters == counters
                assert window.identity_label.text() == first.label
                assert window.pending_label.isVisible()
                assert (
                    window.pending_label.text()
                    == f"{text('pending_recipe', language)}: {next_recipe.label}"
                )
            bridge.start()
            until(lambda: window.snapshot.state is ApplicationState.RUNNING)
            assert window.snapshot.active == window.snapshot.candidate == candidate_b
            assert composition.application.snapshot().active == candidate_b
            for language in ("zh", "en"):
                window.set_language(language)
                qapp.processEvents()
                assert window.identity_label.text() == next_recipe.label
                assert not window.pending_label.isVisible()
                assert window.snapshot.counters == counters
            bridge.stop()
            until(lambda: window.snapshot.state is ApplicationState.READY)
            assert window.snapshot.active == candidate_b
            assert window.snapshot.counters == counters
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
def test_fixed_player_controls_follow_application_state(
    screen: tuple[ProductionWindow, FakeSession],
    state: ApplicationState,
    action: str,
    enabled: bool,
) -> None:
    window, _ = screen
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=state, runtime_owned=False)
    )
    target = (
        "secondary"
        if action == "reset"
        else "stop"
        if action in ("stop", "STOPPING")
        else "operation"
    )
    assert window.buttons[target].isEnabled() == enabled
    assert not window.buttons[
        "pause"
    ].isEnabled()  # This fixture has no replay controller.
    assert all(
        window.buttons[key].isVisible() for key in ("operation", "pause", "stop")
    )
    assert window.buttons["batch"].isEnabled() == (state is ApplicationState.READY)
    assert set(window.metrics) == {
        "cycles",
        "channel_ok",
        "channel_ng",
        "channel_empty",
        "processing",
    }
    if state is ApplicationState.FAULT:
        window.bridge.snapshot_changed.emit(
            replace(window.snapshot, runtime_owned=True)
        )
        assert not window.buttons["secondary"].isEnabled()


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
    window.buttons[
        "stop" if displayed_state is ApplicationState.RUNNING else "secondary"
    ].click()
    assert window.bridge.application.snapshot().state is ApplicationState.READY
    assert not session.start_entered.is_set()
    assert window.buttons["operation"].text() == text("replay_start", window.language)


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
    window.buttons["stop"].click()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    assert not card.viewport.image.isNull()
    assert card.result is not None
    assert not card.viewport.message
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
    assert not card.viewport.message
    assert card.viewport.conversions == conversions
    assert (
        card.viewport.image.isNull()
    )  # New runtime work hides prior-generation pixels.
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
    assert window.snapshot.active is None
    assert (
        window.identity_label.toolTip()
        == f"{text('production_recipe', window.language)}: Recipe A"
    )
    assert not window.pending_label.isVisible()
    assert window.snapshot.counters == ProductionCounters()
    assert window.snapshot.latest_production_result is None
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
    original = window.choices[0]
    replacement = replace(
        original,
        label="Updated A",
        prepared=replace(
            original.prepared,
            configuration=replace(
                original.prepared.configuration, display_name="Updated A"
            ),
        ),
    )
    window.bridge.select_recipe(replacement)
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
    "language,theme,size",
    [
        ("zh", "dark", (1080, 1700)),
        ("en", "light", (1920, 1017)),
    ],
)
def test_activated_display_name_uses_application_and_available_header_width(
    screen: tuple[ProductionWindow, FakeSession],
    tmp_path: Path,
    language: Language,
    theme: str,
    size: tuple[int, int],
) -> None:
    window, session = screen
    name = "阿莫西林胶囊-规格B-灯检方案"
    prepared = window.choices[0].prepared
    configured = replace(
        prepared, configuration=replace(prepared.configuration, display_name=name)
    )
    choice = RecipeChoice(configured, "Stale menu label")
    window.bridge.select_recipe(choice)
    window.parameters.applied.emit(choice)
    window.set_language(language)
    window.set_theme("light" if theme == "light" else "dark")
    window.resize(*size)
    window.bridge.start_batch("LOT-20261020-003")
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    QApplication.processEvents()
    assert window.snapshot.active_recipe_name == name
    assert window.grab().save(
        str(tmp_path / f"production-{language}-{theme}-{size[0]}-active-name.png")
    )
    assert window.identity_label.text() == name, (
        window.width(),
        window.identity_group.width(),
        window.recipe_identity.width(),
        window.recipe_identity.sizeHint().width(),
        window.identity_label.width(),
        window.identity_label.fontMetrics().horizontalAdvance(name),
        window.operation_group.width(),
    )
    assert name in window.identity_label.toolTip()
    assert "Stale menu label" not in window.identity_label.toolTip()
    assert (
        window.identity_label.fontMetrics().horizontalAdvance(name)
        <= window.identity_label.width()
    )
    assert "LOT-20261020-003" in window.batch_label.text()
    assert window.snapshot.counters == ProductionCounters()
    assert window.snapshot.latest_production_result is None
    window.bridge.stop()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    next_config = replace(
        configured,
        configuration=replace(
            configured.configuration, display_name="下次启动的新名称"
        ),
    )
    next_choice = RecipeChoice(next_config, next_config.configuration.display_name)
    window.bridge.select_recipe(next_choice)
    window.parameters.applied.emit(next_choice)
    QApplication.processEvents()
    assert window.identity_label.text() == name
    assert "下次启动的新名称" in window.pending_label.toolTip()
    assert window.snapshot.active_recipe_name == name
    assert window.snapshot.candidate_recipe_name == "下次启动的新名称"
    assert window.snapshot.counters == ProductionCounters()


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
        assert all(
            window.metrics[key][0].text() == "0"
            for key in ("cycles", "channel_ok", "channel_ng", "channel_empty")
        )
        assert window.metrics["processing"][0].text() == "--"
    else:
        assert window.snapshot.counters == counts
        assert window.snapshot.latest_production_result is not None


def test_five_metrics_follow_cumulative_business_counts_and_batch(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, session = screen
    window.set_language("en")
    assert window.metrics["processing"][0].text() == "--"
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    for cycle in (1, 2):
        receipt = window.bridge.application.begin_cycle(CycleId(cycle))
        result = mixed_result(cycle)
        if cycle == 2:
            result = replace(result, inspection_started_at=None)
        session.cycles[-1].accepted.set_result(None)
        session.cycles[-1].result.set_result(result)
        until(
            lambda receipt=receipt, cycle=cycle: (
                receipt.result.done()
                and window.snapshot.counters.cycles_completed == cycle
            )
        )
        assert window.metrics["cycles"][0].text() == str(cycle)
        assert window.metrics["channel_ok"][0].text() == str(6 * cycle)
        assert window.metrics["channel_ng"][0].text() == str(12 * cycle)
        assert window.metrics["channel_empty"][0].text() == str(6 * cycle)
        assert window.metrics["processing"][0].text() == (
            "25 ms" if cycle == 1 else "--"
        )
    window.bridge.stop()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    counts = window.snapshot.counters
    window.bridge.select_recipe(window.choices[1])
    window.bridge.refresh()
    assert window.snapshot.counters == counts
    assert window.metrics["channel_empty"][0].text() == "12"
    window.bridge.start_batch("new-batch")
    window.bridge.refresh()
    assert window.snapshot.counters == ProductionCounters()
    assert all(
        window.metrics[key][0].text() == "0"
        for key in ("cycles", "channel_ok", "channel_ng", "channel_empty")
    )
    assert window.metrics["processing"][0].text() == "--"


def test_metrics_wrap_without_shrinking_labels(
    screen: tuple[ProductionWindow, FakeSession],
    qapp: QApplication,
) -> None:
    window, _ = screen
    window.resize(1080, 1400)
    window.set_language("en")
    for value, caption in window.metrics.values():
        caption.setStyleSheet("font-size: 36px;")
        value.setStyleSheet("font-size: 36px;")
    qapp.processEvents()
    until(lambda: window.metric_row.itemAtPosition(1, 0) is not None)
    for value, caption in window.metrics.values():
        for label in (value, caption):
            assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
            assert window.summary.rect().contains(label.mapTo(window.summary, QPoint()))


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
        # This fixture has no Occupancy evidence; omit routine unavailable prose.
        assert text("occupancy", window.language) not in (
            window.cards[0].indicators[0].toolTip()
        )
        if status is TechnicalStatus.SUCCESS:
            assert not window.event_label.text()
        else:
            assert "CAM1" in window.event_label.text()
            assert (
                text("inspection_issue", window.language) in window.event_label.text()
            )
            assert "private" not in window.event_label.text()
        assert window.event_frame.height() == strip_height
        assert tuple(card.geometry() for card in window.cards) == grid_geometry


@pytest.mark.parametrize("language", ["zh", "en"])
def test_long_recipe_and_batch_keep_pending_activation_visible(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
) -> None:
    window, session = screen
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    window.bridge.stop()
    until(lambda: window.snapshot.state is ApplicationState.READY)
    window.set_language(language)
    window.choices = (
        replace(window.choices[0], label="很长的配方名称 Long Recipe " * 12),
    )
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            batch_id="long-batch-id-" * 12,
            candidate_recipe_name=window.choices[0].label,
        )
    )
    QApplication.processEvents()
    assert window.pending_label.isVisible()
    assert text("pending_recipe", language) in window.pending_label.text()
    assert "Long Recipe" in window.pending_label.toolTip()
    assert window.batch_label.toolTip().endswith("long-batch-id-" * 12)
    assert "Recipe A" in window.identity_label.toolTip()
    assert not window.buttons["batch"].text()
    assert window.buttons["operation"].text() == text("replay_start", language)


@pytest.mark.parametrize("failure", [False, True])
def test_empty_technical_error_and_failed_occupancy_remain_visible(
    screen: tuple[ProductionWindow, FakeSession],
    failure: bool,
) -> None:
    window, _ = screen
    window.bridge.frames.publish(source_frame(), generation=1)
    result = production()
    camera = result.cameras[0]
    first = channel(
        camera.channels[0],
        OccupancyState.EMPTY,
        TechnicalStatus.ERROR,
        ProductJudgment.UNKNOWN,
    )
    if failure:
        first = replace(
            first,
            traditional_skip=None,
            occupancy=OccupancyResult(
                error_code="occupancy.execution_error",
                exception_type="RuntimeError",
                message="C:/private/sdk.dll",
            ),
        )
    camera = replace(camera, channels=(first, *camera.channels[1:]))
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            state=ApplicationState.RUNNING,
            generation=1,
            latest_production_result=replace(
                result, cameras=(camera, *result.cameras[1:])
            ),
        )
    )
    window.bridge.frames_changed.emit(window.bridge.frames.snapshot())
    button = window.cards[0].indicators[0]
    state = Indicator.ERROR if failure else Indicator.EMPTY
    assert not button.pixmap().isNull()
    assert text(state.value, window.language) in button.accessibleName()
    assert Style.warning in button.styleSheet()
    assert text("technical_error", window.language) in button.toolTip()
    assert "execution_error" in button.toolTip()
    assert (
        "private" not in button.toolTip() and "sdk.dll" not in button.accessibleName()
    )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_recipe_summary_candidate_active_and_unconfigured(
    screen: tuple[ProductionWindow, FakeSession],
    language: Language,
) -> None:
    window, _ = screen
    window.set_language(language)
    baseline = window.snapshot
    a, b = (
        ConfigurationIdentity.from_prepared(choice.prepared)
        for choice in window.choices
    )
    for active, candidate, name, pending in (
        (None, a, "Recipe A", False),
        (a, a, "Recipe A", False),
        (a, b, "Recipe A", True),
        (None, None, "--", False),
    ):
        window.bridge.snapshot_changed.emit(
            replace(
                baseline,
                active=active,
                candidate=candidate,
                active_recipe_name="Recipe A" if active else None,
                candidate_recipe_name="Recipe A"
                if candidate == a
                else "Recipe B"
                if candidate == b
                else None,
            )
        )
        QApplication.processEvents()
        assert window.identity_label.text() == name
        assert window.identity_label.accessibleName().startswith(
            f"{text('production_recipe', language)}: {name}"
        )
        assert (
            window.recipe_icon.pixmap().toImage()
            == navigation_icon(1).pixmap(Style.icon, Style.icon).toImage()
        )
        assert window.recipe_icon.toolTip() == window.identity_label.toolTip()
        assert window.pending_label.isVisible() == pending
        if pending:
            assert (
                window.pending_label.text()
                == f"{text('pending_recipe', language)}: Recipe B"
            )
        else:
            assert (
                text("pending_recipe", language) not in window.identity_label.toolTip()
            )
    # Presentation snapshots never promote the application's initial candidate.
    assert window.bridge.application.snapshot().active is None
    assert window.bridge.application.snapshot().counters == ProductionCounters()


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("font_pixels", [19, 23])
def test_summary_long_identities_adapt_to_narrower_and_larger_text(
    screen: tuple[ProductionWindow, FakeSession],
    tmp_path: Path,
    language: Language,
    font_pixels: int,
) -> None:
    window, _ = screen
    window.set_language(language)
    a, b = window.choices
    window.choices = (a, replace(b, label="Long next Recipe 配方名称 " * 20))
    for label in (
        window.state_label,
        window.identity_label,
        window.pending_label,
        window.batch_label,
    ):
        label.setStyleSheet(f"font-size: {font_pixels}px;")
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            active=ConfigurationIdentity.from_prepared(a.prepared),
            candidate=ConfigurationIdentity.from_prepared(b.prepared),
            candidate_recipe_name=window.choices[1].label,
            batch_id="long-batch-identity-" * 20,
        )
    )
    for index, size in enumerate(
        ((1920, 1080), (1080, 720), (1080, 1920), (1920, 1080))
    ):
        window.resize(*size)
        QApplication.processEvents()
        for widget in (
            window.identity_group,
            window.batch_group,
            window.buttons["operation"],
        ):
            start = widget.mapTo(window.summary, QPoint())
            assert window.summary.rect().contains(start)
            assert window.summary.rect().contains(
                start + QPoint(widget.width() - 1, widget.height() - 1)
            )
        identity_left = window.identity_group.mapTo(window.summary, QPoint()).x()
        identity_right = (
            window.identity_group.mapTo(window.summary, QPoint()).x()
            + window.identity_group.width()
        )
        batch_left = window.batch_group.mapTo(window.summary, QPoint()).x()
        batch_right = batch_left + window.batch_group.width()
        operation_left = window.buttons["operation"].mapTo(window.summary, QPoint()).x()
        assert (
            batch_left
            == window.summary.contentsRect().left()
            + window.summary_layout.contentsMargins().left()
        )
        assert batch_right + Style.space <= identity_left
        assert identity_right + Style.space <= operation_left
        assert window.pending_label.text().startswith(text("pending_recipe", language))
        assert window.buttons["batch"].size() == QSize(Style.touch, Style.touch)
        for widget in (
            window.state_label,
            window.recipe_icon,
            window.identity_label,
            window.pending_label,
        ):
            assert window.identity_group.rect().contains(widget.geometry()), (
                widget.geometry(),
                window.identity_group.rect(),
            )
        assert (
            window.recipe_identity.geometry().bottom()
            < window.pending_label.geometry().top()
        )
        assert window.batch_group.rect().contains(window.batch_label.geometry())
        assert (
            window.buttons["batch"].x()
            == window.batch_label.x() + window.batch_label.width() + Style.small
        )
        for label in (window.identity_label, window.pending_label, window.batch_label):
            assert (
                label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
            ), (label.text(), label.width())
        assert window.grab().save(
            str(tmp_path / f"header-{language}-{index}-{size[0]}x{size[1]}-pending.png")
        )


@pytest.mark.parametrize("language", ["zh", "en"])
def test_specific_error_and_long_source_status_are_single_row(
    screen: tuple[ProductionWindow, FakeSession], language: Language
) -> None:
    window, _ = screen
    window.set_language(language)
    failure = production(status=TechnicalStatus.ERROR, judgment=ProductJudgment.UNKNOWN)
    cameras = tuple(
        replace(
            camera,
            channels=tuple(
                replace(
                    channel,
                    checks=(
                        channel.checks[0],
                        replace(
                            channel.checks[1],
                            reasons=(
                                Reason(
                                    FindingSource.VIMO,
                                    "execution_error",
                                    exception_type="ValueError",
                                ),
                            ),
                        ),
                    ),
                )
                for channel in camera.channels
            ),
        )
        for camera in failure.cameras
    )
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot, latest_production_result=replace(failure, cameras=cameras)
        )
    )
    QApplication.processEvents()
    assert (
        "CAM1" in window.event_label.text()
        and "ValueError" in window.event_label.text()
    )
    assert text("execution_error", language) in window.event_label.text()
    assert text("technical_note", language) not in window.event_label.text()
    before = tuple(card.geometry() for card in window.cards)
    height = window.event_frame.height()
    assert height <= Style.touch and not window.event_label.wordWrap()
    error = "CAM6: unreadable image at C:/" + "long-folder/" * 30 + "sample.png"
    window.bridge.command_failed.emit("folder_source", ValueError(error))
    QApplication.processEvents()
    assert "CAM6" in window.event_label.text()
    assert "ValueError" in window.event_label.toolTip()
    assert "long-folder" not in window.event_label.toolTip()
    assert window.event_label.accessibleName() == window.event_label.toolTip()
    assert "\n" not in window.event_label.text()
    assert (
        window.event_label.fontMetrics().horizontalAdvance(window.event_label.text())
        <= window.event_label.width()
    )
    assert height == window.event_frame.height()
    assert before == tuple(card.geometry() for card in window.cards)
    window.navigate(1)
    QApplication.processEvents()
    assert window.event_frame.height() == height and not window.event_label.wordWrap()
    assert (
        "CAM6" in window.event_label.text()
    )  # Same short technical evidence on every page.
    window.navigate(0)
    QApplication.processEvents()
    assert height == window.event_frame.height()
    window.bridge.command_finished.emit("start")
    window.bridge.command_failed.emit(
        "start", RuntimeError("private SDK error details")
    )
    assert text("start", language) in window.event_label.text()
    assert "RuntimeError" in window.event_label.text()
    assert "private" not in window.event_label.toolTip()
    assert height == window.event_frame.height()


def test_running_and_disabled_batch_icons_are_distinct(qapp: QApplication) -> None:
    running = production_icon("RUNNING").pixmap(Style.icon, Style.icon).toImage()
    start = production_icon("start").pixmap(Style.icon, Style.icon).toImage()
    assert tuple(
        running.pixelColor(x, y).alpha() > 0
        for x in range(running.width())
        for y in range(running.height())
    ) != tuple(
        start.pixelColor(x, y).alpha() > 0
        for x in range(start.width())
        for y in range(start.height())
    )  # Different shapes, not merely different colors.
    icon = production_icon("batch")
    normal = icon.pixmap(Style.icon, Style.icon, QIcon.Mode.Normal).toImage()
    disabled = icon.pixmap(Style.icon, Style.icon, QIcon.Mode.Disabled).toImage()
    assert disabled != normal
    assert sum(
        disabled.pixelColor(x, y).alpha()
        for x in range(disabled.width())
        for y in range(disabled.height())
    ) < sum(
        normal.pixelColor(x, y).alpha()
        for x in range(normal.width())
        for y in range(normal.height())
    )


@pytest.mark.parametrize("language,size", [("zh", (1080, 1920)), ("en", (1920, 1080))])
def test_concise_lifecycle_text_and_center_identity_fit_summary(
    screen: tuple[ProductionWindow, FakeSession],
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
) -> None:
    window, _ = screen
    window.set_language(language)
    window.resize(*size)
    styles: list[str] = []
    for key, state in (
        ("READY", ApplicationState.READY),
        ("STARTING", ApplicationState.STARTING),
        ("RUNNING", ApplicationState.RUNNING),
        ("STOPPING", ApplicationState.STOPPING),
        ("FAULT", ApplicationState.FAULT),
    ):
        window.bridge.snapshot_changed.emit(replace(window.snapshot, state=state))
        QApplication.processEvents()
        label = window.state_label
        assert label.text() == text(f"state_{key}", language)
        assert text(key, language) in label.accessibleName()
        assert label.toolTip() == label.accessibleName()
        assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
        styles.append(label.styleSheet())
        assert "background:" not in label.styleSheet()
        assert (
            sum(
                not child.pixmap().isNull()
                for child in window.identity_group.findChildren(QLabel)
            )
            == 1
        )
        for widget in (
            label,
            window.identity_group,
            window.batch_group,
            window.operation_group,
        ):
            position = widget.mapTo(window.summary, QPoint())
            assert window.summary.rect().contains(position)
            assert window.summary.rect().contains(
                position + QPoint(widget.width() - 1, widget.height() - 1)
            )
        assert window.grab().save(str(tmp_path / f"production-{language}-{key}.png"))
    assert len(set(styles)) == 5


@pytest.mark.parametrize("language", ["zh", "en"])
def test_quiet_empty_canvas_does_not_misclassify_valid_dark_pixels(
    screen: tuple[ProductionWindow, FakeSession], language: Language
) -> None:
    window, _ = screen
    window.set_language(language)
    card = window.cards[0]
    assert card.viewport.image.isNull()
    assert text("no_display_frame", language) in card.viewport.accessibleName()
    assert not card.viewport.message
    # The shared seed is black; publish transfers an independent owned display copy.
    frame = source_frame()
    assert np.count_nonzero(frame.image) == 0
    window.bridge.frames.publish(frame, generation=1)
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=ApplicationState.RUNNING, generation=1)
    )
    window.bridge.frames_changed.emit(window.bridge.frames.snapshot())
    assert not card.viewport.image.isNull()
    assert card.viewport.image.pixelColor(0, 0) == QColor(0, 0, 0)
    assert text("no_display_frame", language) not in card.viewport.accessibleName()
    assert card.result is None
    assert card.viewport.message == text("pending", language)
    assert window.snapshot.counters == ProductionCounters()
    # A new generation suppresses old pixels; lack of evidence alone did not.
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=ApplicationState.STARTING, generation=2)
    )
    assert card.viewport.image.isNull() and not card.viewport.message
    assert text("no_display_frame", language) in card.viewport.accessibleName()


def test_production_focus_is_visible_and_keyboard_targets_stay_usable(
    screen: tuple[ProductionWindow, FakeSession],
) -> None:
    window, _ = screen
    window.activateWindow()
    QApplication.processEvents()
    batch = window.buttons["batch"]
    batch.setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    assert batch.hasFocus()
    assert batch.accessibleName() and batch.toolTip()
    QTest.keyClick(batch, Qt.Key.Key_Tab)
    assert window.buttons["operation"].hasFocus()
    operation = window.buttons["operation"]
    # Read-only status symbols do not introduce 36 redundant keyboard actions.
    assert all(
        not item.focusPolicy() for card in window.cards for item in card.indicators
    )
    window.bridge.snapshot_changed.emit(
        replace(window.snapshot, state=ApplicationState.STOPPING)
    )
    operation = window.buttons["operation"]
    assert not operation.isEnabled() and operation.accessibleName() == operation.text()
    operation.setFocus(Qt.FocusReason.TabFocusReason)
    assert not operation.hasFocus()


def _assert_grid_and_navigation(
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
    full_caption = text("production", language)
    navigation = window.navigation[0]
    assert navigation.accessibleName() == navigation.toolTip() == full_caption
    assert navigation.text() == full_caption or (
        navigation.text().endswith("…")
        and full_caption.startswith(navigation.text()[:-1])
    )
    for index, card in enumerate(window.cards):
        number = str(card.camera_layout.camera_id).removeprefix("CAM")
        assert card.identity.text() == number
        name = f"{text('camera_number', language).format(number=number)} ({card.camera_layout.camera_id})"
        assert card.identity.toolTip() == card.identity.accessibleName() == name
        assert (
            card.camera_icon.pixmap().toImage()
            == camera_icon().pixmap(Style.icon, Style.icon).toImage()
        )
        assert card.camera_icon.size() == QSize(Style.icon, Style.icon)
        assert card.identity_group.rect().contains(card.camera_icon.geometry())
        assert card.identity_group.rect().contains(card.identity.geometry())
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
    ):
        parent = button.parentWidget()
        assert parent is not None and parent.rect().contains(button.geometry())
        assert (
            button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 15
        )
    window.navigation[2].click()
    assert window.pages.currentIndex() == 2
    assert window.pages.currentWidget() is window.communication
    assert window.communication.write_button.text() == text("comm_write", language)
    window.navigation[0].click()
    window.resize(size[1], size[0])
    qapp.processEvents()
    assert tuple(id(card) for card in window.cards) == identities


def _inspect_and_assert_business_presentation(
    screen: tuple[ProductionWindow, FakeSession],
    qapp: QApplication,
    tmp_path: Path,
    size: tuple[int, int],
    language: Language,
) -> None:
    from run_production_ui import demo_images

    from vimo_demo.inspection import Frame

    window, session = screen
    window.set_language(language)
    window.resize(*size)
    session.ready.set_result(None)
    window.bridge.start()
    until(lambda: window.snapshot.state is ApplicationState.RUNNING)
    result = mixed_result()
    # Existing fake pixels, independent owned display copies and matching FrameIds.
    images = demo_images()
    for image in images:
        # Test-specific EMPTY/EMPTY+NG pixels, before Frame ownership transfer.
        for slot in (1, 3):
            image[:, slot * image.shape[1] // 6 : (slot + 1) * image.shape[1] // 6] = 0
    result = replace(
        result,
        cameras=tuple(
            replace(camera, image_shape=(image.shape[0], image.shape[1], 3))
            for camera, image in zip(result.cameras, images, strict=True)
        ),
    )
    for camera, image in zip(result.cameras, images, strict=True):
        assert camera.frame_id is not None
        window.bridge.frames.publish(Frame(camera.frame_id, 0, image), generation=1)
    receipt = window.bridge.application.begin_cycle(CycleId(1))
    session.cycles[0].accepted.set_result(None)
    session.cycles[0].result.set_result(result)
    until(
        lambda: (
            receipt.result.done()
            and all(card.result is not None for card in window.cards)
        )
    )
    assert (
        window.snapshot.counters.channel_ok == 18
    )  # Original judgments, including EMPTY/UNKNOWN.
    assert window.metrics["channel_ok"][0].text() == "6"  # Business OK only.
    assert window.metrics["channel_ng"][0].text() == "12"
    assert window.metrics["channel_empty"][0].text() == "6"
    assert window.metrics["channel_ok"][1].text() == "CH OK"
    assert window.metrics["channel_ng"][1].text() == "CH NG"
    assert window.metrics["channel_empty"][1].text() == "CH EMPTY"
    assert set(window.metrics) == {
        "cycles",
        "channel_ok",
        "channel_ng",
        "channel_empty",
        "processing",
    }
    expected = (
        Indicator.OK,
        Indicator.EMPTY,
        Indicator.UNKNOWN,
        Indicator.ERROR_NG,
        Indicator.ERROR,
        Indicator.NG,
    )
    for camera, card in zip(result.cameras, window.cards, strict=True):
        for row, button, state in zip(
            camera.channels, card.indicators, expected, strict=True
        ):
            assert (
                f"{text('channel', language)} {row.channel_id} ·"
                in button.accessibleName()
            )
            assert not button.pixmap().isNull()
            assert text(state.value, language) in button.accessibleName()
            assert button.toolTip() == button.accessibleName()
            assert text("business_status", language) in button.toolTip()
            assert text("occupancy", language) in button.toolTip()
            assert text("product_judgment", language) in button.toolTip()
            assert text("technical_status", language) in button.toolTip()
            assert button.size() == QSize(Style.touch, Style.touch)
            if size != Style.minimum:
                assert card.indicator_group.rect().contains(button.geometry())
        empty, conflict = card.indicators[1], card.indicators[3]
        assert text("traditional_skip_empty", language) in empty.toolTip()
        assert text("occupancy_empty", language) in conflict.toolTip()
        assert text("error_ng", language) in conflict.toolTip()
        assert "execution_error" in conflict.toolTip()
        assert Style.warning in conflict.styleSheet()
    neutral = indicator_icon(Indicator.EMPTY).pixmap(18, 18).toImage()
    assert neutral != indicator_icon(Indicator.DISABLED).pixmap(18, 18).toImage()
    assert neutral != indicator_icon(Indicator.PENDING).pixmap(18, 18).toImage()
    qapp.processEvents()
    for value, caption in window.metrics.values():
        for widget in (value, caption):
            assert widget.width() >= widget.fontMetrics().horizontalAdvance(
                widget.text()
            )
            position = widget.mapTo(window.summary, QPoint())
            assert window.summary.rect().contains(position)
            assert window.summary.rect().contains(
                position + QPoint(widget.width() - 1, widget.height() - 1)
            )
    counts = window.snapshot.counters
    for _ in range(3):
        window.bridge.refresh()
    assert window.snapshot.counters == counts
    assert window.grab().save(
        str(tmp_path / f"e2-production-{language}-{size[0]}x{size[1]}.png")
    )


def _assert_camera_header_stability(
    screen: tuple[ProductionWindow, FakeSession], size: tuple[int, int]
) -> None:
    window, _ = screen
    window.resize(*size)
    camera_id = CameraId("CAM6")
    # The preceding result stage already published this FrameId. A fresh display
    # generation preserves monotonic latest-only publication and matching evidence.
    generation = window.snapshot.generation + 1
    assert window.bridge.frames.publish(source_frame(camera_id), generation=generation)
    window.bridge.frames_changed.emit(window.bridge.frames.snapshot())
    baseline = production()
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            state=ApplicationState.RUNNING,
            generation=generation,
            latest_production_result=baseline,
        )
    )
    QApplication.processEvents()
    card = window.cards[-1]
    positions = tuple(button.mapTo(card, QPoint()) for button in card.indicators)
    group = card.indicator_group
    center = group.mapTo(card, QPoint()).x() + group.width() / 2
    assert abs(center - card.width() / 2) <= 1
    states = (
        (TechnicalStatus.SUCCESS, ProductJudgment.OK),
        (TechnicalStatus.SUCCESS, ProductJudgment.NG),
        (TechnicalStatus.ERROR, ProductJudgment.UNKNOWN),
        (TechnicalStatus.ERROR, ProductJudgment.NG),
        (TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN),
        (TechnicalStatus.SUCCESS, ProductJudgment.OK),
    )
    camera = baseline.cameras[-1]
    mixed = replace(
        camera,
        channels=tuple(
            replace(
                channel,
                checks=(
                    channel.checks[0],
                    replace(
                        channel.checks[1],
                        assessment=Assessment(status, judgment),
                        reasons=(Reason(FindingSource.VIMO, "execution_error"),)
                        if status is TechnicalStatus.ERROR
                        or judgment is ProductJudgment.NG
                        else (),
                    ),
                ),
            )
            for channel, (status, judgment) in zip(camera.channels, states, strict=True)
        ),
    )
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            latest_production_result=replace(
                baseline, cameras=(*baseline.cameras[:-1], mixed)
            ),
        )
    )
    QApplication.processEvents()
    assert (
        tuple(button.mapTo(card, QPoint()) for button in card.indicators) == positions
    )
    for index, button in enumerate(card.indicators):
        assert button.size() == QSize(Style.touch, Style.touch)
        assert str(camera.layout.slots[index].channel_id) in button.accessibleName()
        assert group.rect().contains(button.geometry())
    assert not card.viewport.message
    assert text("execution_error", window.language) in window.event_label.text()


def _assert_long_summary(
    screen: tuple[ProductionWindow, FakeSession],
    tmp_path: Path,
    size: tuple[int, int],
    language: Language,
) -> None:
    window, _ = screen
    window.set_language(language)
    window.resize(*size)
    window.choices = (
        replace(window.choices[0], label="Long Recipe 很长的配方名称 " * 15),
    )
    window.bridge.snapshot_changed.emit(
        replace(
            window.snapshot,
            batch_id="batch-with-a-long-identity-" * 15,
            candidate_recipe_name=window.choices[0].label,
        )
    )
    QApplication.processEvents()
    batch = window.batch_group
    assert (
        batch.mapTo(window.summary, QPoint()).x()
        == window.summary.contentsRect().left()
        + window.summary_layout.contentsMargins().left()
    )
    identity = window.identity_group
    center = identity.mapTo(window.summary, QPoint()).x() + identity.width() / 2
    if (
        2 * window.operation_group.width() + identity.width() + 2 * Style.space
        <= window.summary.width()
    ):
        assert abs(center - window.summary.width() / 2) <= 1
    else:
        assert batch.geometry().right() < identity.geometry().left()
        assert identity.geometry().right() < window.operation_group.geometry().left()
    assert not window.pending_label.isVisible()
    assert "Long Recipe" in window.identity_label.toolTip()
    operation = window.buttons["operation"]
    right = (
        window.operation_group.mapTo(window.summary, QPoint()).x()
        + window.operation_group.width()
    )
    assert window.summary.width() - right <= Style.space + 1
    for widget in (window.identity_group, batch, operation):
        position = widget.mapTo(window.summary, QPoint())
        assert window.summary.rect().contains(position)
        assert window.summary.rect().contains(
            position + QPoint(widget.width() - 1, widget.height() - 1)
        )
    assert len(window.metrics) == 5 and "channel_empty" in window.metrics
    assert window.grab().save(
        str(tmp_path / f"header-{language}-{size[0]}x{size[1]}-long-identities.png")
    )


def _assert_batch_snapshots(
    window: ProductionWindow,
    tmp_path: Path,
    language: Language,
    size: tuple[int, int],
    batch_id: str | None,
    baseline: ProductionApplicationSnapshot,
) -> None:
    full_text = f"{text('batch_id', language)} {batch_id or '—'}"
    operation_positions: list[QPoint] = []
    for state in (
        ApplicationState.READY,
        ApplicationState.RUNNING,
        ApplicationState.READY,
    ):
        window.bridge.snapshot_changed.emit(
            replace(
                baseline,
                state=state,
                batch_id=batch_id,
                active=baseline.candidate
                if state is ApplicationState.RUNNING or operation_positions
                else baseline.active,
                generation=1 if operation_positions else baseline.generation,
            )
        )
        until(lambda state=state: window.snapshot.state is state)
        QApplication.processEvents()
        # Qt posts another layout request when a resize changes the text width.
        until(
            lambda: (
                window.buttons["batch"].x()
                == window.batch_label.x() + window.batch_label.width() + Style.small
            )
        )
        if batch_id is None or len(batch_id) < 100:
            assert window.batch_label.text() == full_text
        else:
            assert window.batch_label.text() != full_text
            assert "…" in window.batch_label.text()
        assert window.batch_label.toolTip() == full_text
        assert window.batch_label.accessibleName() == full_text
        assert window.batch_label.isEnabled()
        assert window.buttons["batch"].isEnabled() == (state is ApplicationState.READY)
        assert (
            window.batch_label.fontMetrics().horizontalAdvance(
                window.batch_label.text()
            )
            <= window.batch_label.width()
        )
        edit = window.buttons["batch"]
        assert (
            edit.x()
            == window.batch_label.x() + window.batch_label.width() + Style.small
        )
        assert edit.size() == QSize(Style.touch, Style.touch)
        group = window.batch_group
        assert (
            group.mapTo(window.summary, QPoint()).x()
            == window.summary.contentsRect().left()
            + window.summary_layout.contentsMargins().left()
        )
        identity = window.identity_group
        center = identity.mapTo(window.summary, QPoint()).x() + identity.width() / 2
        if (
            2 * window.operation_group.width() + identity.width() + 2 * Style.space
            <= window.summary.width()
        ):
            assert abs(center - window.summary.width() / 2) <= 1
        else:
            assert group.geometry().right() < identity.geometry().left()
            assert (
                identity.geometry().right() < window.operation_group.geometry().left()
            )
        operation_positions.append(
            window.buttons["operation"].mapTo(window.summary, QPoint())
        )
        if batch_id == "LOT-20261009-001":
            assert window.grab().save(
                str(
                    tmp_path
                    / f"header-{language}-{size[0]}x{size[1]}-{state.name}-{window.snapshot.generation}-batch.png"
                )
            )
    assert operation_positions[0] == operation_positions[1] == operation_positions[2]


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_batch_and_summary_presentation(
    screen: tuple[ProductionWindow, FakeSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    language: Language,
) -> None:
    window, session = screen
    window.set_language(language)
    window.resize(*size)
    authoritative = window.bridge.application.snapshot()
    batch_ids = (
        (
            None,
            "121",
            "12313123",
            "1231312213123",
            "LOT-20261009-001",
            "long-batch-" * 100,
        )
        if size[0] == 1080
        else (None, "121", "LOT-20261009-001", "long-batch-" * 100)
    )
    # Freeze only the observation used by periodic presentation refresh. No real
    # session is started; independent command/accounting tests retain that seam.
    with monkeypatch.context() as patch:
        patch.setattr(window.bridge.application, "snapshot", lambda: window.snapshot)
        for batch_id in batch_ids:
            try:
                _assert_batch_snapshots(
                    window, tmp_path, language, size, batch_id, authoritative
                )
            except Exception as exc:
                exc.add_note(
                    f"Batch presentation: size={size}, language={language}, batch={batch_id!r}"
                )
                raise
        try:
            window.bridge.snapshot_changed.emit(authoritative)
            _assert_long_summary(screen, tmp_path, size, language)
        except Exception as exc:
            exc.add_note(f"Long summary: size={size}, language={language}")
            raise
    assert window.bridge.application.snapshot() == authoritative
    assert not session.cycles
    window.bridge.refresh()


@pytest.mark.parametrize(
    "size,columns",
    [((1080, 1920), 2), ((1920, 1080), 3)],
)
@pytest.mark.parametrize("language", ["zh", "en"])
def test_production_layout_indicators_and_header_stability(
    screen: tuple[ProductionWindow, FakeSession],
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    columns: int,
    language: Language,
) -> None:
    window, _ = screen
    try:
        _assert_grid_and_navigation(screen, qapp, size, columns, language)
        _inspect_and_assert_business_presentation(
            screen, qapp, tmp_path, size, language
        )
        actual = window.bridge.application.snapshot()
        # This original header stage was already presentation-only. Keep the real
        # mixed-result admission above and prevent refresh from replacing this view.
        with monkeypatch.context() as patch:
            patch.setattr(
                window.bridge.application, "snapshot", lambda: window.snapshot
            )
            _assert_camera_header_stability(screen, size)
        assert window.bridge.application.snapshot() == actual
        window.bridge.refresh()
    except Exception as exc:
        exc.add_note(f"Production stages: size={size}, language={language}")
        raise


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_multilingual_player_captions_fit_without_moving_camera_layout(
    screen: tuple[ProductionWindow, FakeSession],
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
) -> None:
    window, session = screen
    window.resize(*size)
    QApplication.processEvents()
    before = window.bridge.application.snapshot()
    buttons = tuple(window.buttons[key] for key in ("operation", "pause", "stop"))
    slots = tuple(button.geometry() for button in buttons)
    cameras = tuple(card.geometry() for card in window.cards)
    monkeypatch.setattr(
        type(window.bridge), "controls_enabled", property(lambda _: True)
    )
    for language in LANGUAGES:
        for paused in (False, True):
            actions = {
                "operation": "replay_continue" if paused else "start",
                "pause": "replay_step" if paused else "replay_pause",
                "stop": "stop",
                "secondary": None,
            }

            def selected_actions(
                value: dict[str, str | None] = actions,
            ) -> dict[str, str | None]:
                return value

            monkeypatch.setattr(window, "_actions", selected_actions)
            window.bridge.replay_snapshot = ReplaySnapshot(
                1, ReplayState.PAUSED if paused else ReplayState.AUTOMATIC
            )
            window.set_language(language)
            window.bridge.replay_changed.emit(window.bridge.replay_snapshot)
            keys = (
                "replay_continue" if paused else "replay_start",
                "replay_step" if paused else "replay_pause",
                "production_stop",
            )
            assert tuple(button.geometry() for button in buttons) == slots
            assert window.size().toTuple() == size, (
                language,
                window.size().toTuple(),
                window.minimumSizeHint().toTuple(),
            )
            assert tuple(card.geometry() for card in window.cards) == cameras, language
            for button, key in zip(buttons, keys, strict=True):
                assert_full_caption(button, text(key, language))
                assert button.accessibleName() == text(key, language)
                assert button.toolTip() and not button.icon().isNull()
                assert window.operation_group.rect().contains(button.geometry())
            assert window.summary.rect().contains(window.operation_group.geometry())
    assert window.bridge.application.snapshot() == before and not session.cycles
