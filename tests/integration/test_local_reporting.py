"""Fake Production + actual loopback reporting, independent of GUI result polling."""

from concurrent.futures import Future
from pathlib import Path
from threading import Event, current_thread, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.application_support import prepared
from tests.cycle_support import image_result
from tests.qt_support import until
from tests.reporting_support import WAIT, readback, seed, server, settings

from vimo_demo.application import ApplicationError, ApplicationState, ProductionCounters
from vimo_demo.application.communication_preferences import CommunicationPreferences
from vimo_demo.application.session import EngineFactory
from vimo_demo.communication import first_station_reject_words
from vimo_demo.communication.operations import RequestKind
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ReportingCapacityError,
    ReportStage,
)
from vimo_demo.configuration import (
    MachineLayoutDefinition,
    RecipeDefinition,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CycleStatus
from vimo_demo.ui.local_production import LocalComposition, compose
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_window import ProductionWindow

pytestmark = pytest.mark.usefixtures("qapp")  # until() needs a live Qt event loop.


class FatalEngineError(BaseException):
    pass


class Engine:
    def __init__(self, *, fail: bool = False, fatal: bool = False) -> None:
        self.calls: list[tuple[int, int, str]] = []
        self.closed = False
        self.fail, self.fatal = fail, fatal
        self.gate, self.release = Event(), Event()
        self.hold_call: int | None = None

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.calls.append((int(image_bgr[0, 0, 0]), get_ident(), current_thread().name))
        if len(self.calls) == self.hold_call:
            self.gate.set()
            assert self.release.wait(WAIT)
        if len(self.calls) == 3:
            if self.fatal:
                raise FatalEngineError("controlled runtime terminal failure")
            if self.fail:
                raise ValueError("controlled ordinary frame failure")
        return image_result(uint8_array(image_bgr))

    def close(self) -> None:
        assert get_ident() == self.calls[0][1]
        self.closed = True


def composition(
    folder: Path,
    engine: Engine,
    reporting: LocalReportingSettings | None = None,
    observe: list[Future[None]] | None = None,
    engine_factory: EngineFactory | None = None,
) -> LocalComposition:
    config = prepared(folder).configuration
    model, machine, recipe = (
        folder / name for name in ("model.json", "machine.json", "local.recipe.json")
    )
    save_model_definition(model, config.model)
    save_machine_layout(machine, MachineLayoutDefinition(config.layout))
    save_recipe(recipe, RecipeDefinition("local", "Local test"))
    paths: list[Path] = []
    for index in range(6):
        path = folder / f"frame-{index}.png"
        ok, encoded = cv2.imencode(
            ".png", np.full((20, 60, 3), index + 1, dtype=np.uint8)
        )
        assert ok
        encoded.tofile(path)
        paths.append(path)
    return compose(
        model,
        machine,
        recipe,
        paths,
        engine_factory=engine_factory or (lambda _: engine),
        reporting=reporting,
        observe_source=observe.append if observe is not None else lambda _: None,
    )


def close(local: LocalComposition) -> None:
    local.application.close().result(WAIT)
    assert local.application.join(WAIT)


@pytest.mark.parametrize("fail", [False, True])
def test_production_completion_reports_once_without_qt_polling_and_preserves_counters(
    tmp_path: Path, fail: bool
) -> None:
    with server() as peer:
        seed(peer, 200)
        engine = Engine(fail=fail)
        engine.hold_call = 9  # Second inspection's first frame; first pass is complete.
        local = composition(tmp_path, engine, settings(peer))
        app = local.application
        try:
            assert app.reporting_snapshot() is None
            app.start().result(WAIT)
            assert engine.gate.wait(WAIT)
            # No Qt bridge, painting or polling is needed for the report handoff.
            until(
                lambda: (
                    (snapshot := app.reporting_snapshot()) is not None
                    and snapshot.acknowledged_count == 1
                )
            )
            snapshot = app.snapshot()
            result = snapshot.latest_production_result
            assert result is not None and result.cycle_id == 1
            assert result.cycle_status is (
                CycleStatus.FAILED if fail else CycleStatus.COMPLETE
            )
            assert snapshot.state is ApplicationState.RUNNING
            expected = ProductionCounters().admitted().completed(result).admitted()
            assert snapshot.counters == expected
            assert readback(peer) == (
                201,
                *first_station_reject_words(
                    (row.channel_id, row.business_status) for row in result.channels
                ),
            )
            observed = app.reporting_snapshot()
            assert observed is not None and observed.last_acknowledged is not None
            assert (
                observed.last_acknowledged.identity.generation
                == snapshot.generation
                == 1
            )
            assert observed.last_acknowledged.identity.cycle_id == 1
            assert len({owner for _, owner, _ in engine.calls}) == 1
            assert all(
                not name.startswith("FirstStationReporting-")
                for _, _, name in engine.calls
            )
            stopped = app.stop()
            engine.release.set()
            stopped.result(WAIT)
            final = app.reporting_snapshot()
            assert (
                final is not None
                and not final.thread_alive
                and final.acknowledged_count == 1
            )
            assert final.skipped_count == 1  # Active inference finished after Stop.
            assert engine.closed and app.snapshot().state is ApplicationState.READY
        finally:
            engine.release.set()
            close(local)


@pytest.mark.parametrize("enabled", [False, True])
def test_default_disabled_and_warmup_produces_no_reporting(
    tmp_path: Path, enabled: bool
) -> None:
    with server() as peer:
        engine = Engine()
        engine.hold_call = 1
        local = composition(tmp_path, engine, settings(peer) if enabled else None)
        app = local.application
        try:
            startup = app.start()
            assert engine.gate.wait(WAIT)
            observed = app.reporting_snapshot()
            if enabled:
                assert (
                    observed is not None
                    and observed.queued_count == observed.attempted_count == 0
                )
            else:
                assert observed is None
                assert not any(
                    t.name.startswith("FirstStationReporting-") for t in threads()
                )
            assert (
                peer.packets == () and app.snapshot().counters == ProductionCounters()
            )
            stopped = app.stop()
            engine.release.set()
            with pytest.raises(ApplicationError):
                startup.result(WAIT)
            stopped.result(WAIT)
            assert app.snapshot().counters == ProductionCounters()
        finally:
            engine.release.set()
            close(local)


def test_network_failure_does_not_stop_inspection_or_fault_application(
    tmp_path: Path,
) -> None:
    with server() as peer:
        peer.fail_next(ReportStage.BITMAP, "plc")
        engine = Engine()
        engine.hold_call = 15  # Third pass waits; two completed cycles continue.
        local = composition(tmp_path, engine, settings(peer))
        app = local.application
        try:
            app.start().result(WAIT)
            assert engine.gate.wait(WAIT)
            until(
                lambda: (
                    (s := app.reporting_snapshot()) is not None
                    and s.acknowledged_count == 1
                )
            )
            s = app.reporting_snapshot()
            assert s is not None and s.failed_count == 1 and s.last_failed is not None
            assert (
                s.last_failed.identity.cycle_id == 1
                and s.last_failed.stage is ReportStage.BITMAP
            )
            assert (
                s.last_acknowledged is not None
                and s.last_acknowledged.identity.cycle_id == 2
            )
            assert (
                s.last_acknowledged.sequence == 2
            )  # Failed cycle reserved 1, no retry.
            snapshot = app.snapshot()
            assert (
                snapshot.state is ApplicationState.RUNNING
                and snapshot.last_issue is None
            )
            assert snapshot.counters.cycles_completed == 2
            assert snapshot.counters.channel_ng == snapshot.counters.business_ng == 0
            stopped = app.stop()
            engine.release.set()
            stopped.result(WAIT)
        finally:
            engine.release.set()
            close(local)


def test_backlog_overload_stops_source_with_explicit_rejection_and_safe_join(
    tmp_path: Path,
) -> None:
    with server() as peer:
        peer.hold_next(ReportStage.BITMAP)
        engine = Engine()
        sources: list[Future[None]] = []
        local = composition(
            tmp_path, engine, settings(peer, max_pending=1, timeout=2), sources
        )
        app = local.application
        try:
            app.start().result(WAIT)
            assert peer.entered.wait(WAIT)
            until(
                lambda: (
                    (s := app.reporting_snapshot()) is not None
                    and s.rejected_count == 1
                )
            )
            snapshot = app.reporting_snapshot()
            assert (
                snapshot is not None and snapshot.pending == snapshot.peak_pending == 1
            )
            assert (
                snapshot.last_rejected is not None
                and snapshot.last_rejected.identity.cycle_id == 3
            )
            assert app.snapshot().state is ApplicationState.STOPPING
            assert app.snapshot().counters.cycles_completed == 3
            assert sources[0].done() and isinstance(
                sources[0].exception(), ReportingCapacityError
            )
            assert not app.join(0)  # Lifecycle stays owned while reporting is blocked.
            peer.release.set()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            final = app.reporting_snapshot()
            assert (
                final is not None
                and final.acknowledged_count == 1
                and final.skipped_count == 1
            )
            assert final.rejected_count == 1 and not final.thread_alive
            assert app.snapshot().counters.business_ng == 0
        finally:
            peer.release.set()
            close(local)


def test_stop_start_resynchronizes_new_generation_without_old_writes(
    tmp_path: Path,
) -> None:
    with server() as peer:
        engines: list[Engine] = []

        def factory(_prepared: object) -> Engine:
            engine = Engine()
            engine.hold_call = 9
            engines.append(engine)
            return engine

        local = composition(tmp_path, Engine(), settings(peer), engine_factory=factory)
        app = local.application
        try:
            for generation, initial in ((1, 100), (2, 60000)):
                seed(peer, initial)
                app.start().result(WAIT)
                engine = engines[-1]
                assert engine.gate.wait(WAIT)
                until(
                    lambda: (
                        (s := app.reporting_snapshot()) is not None
                        and s.acknowledged_count == 1
                    )
                )
                s = app.reporting_snapshot()
                assert (
                    s is not None
                    and s.generation == generation
                    and s.last_acknowledged is not None
                )
                assert s.last_acknowledged.identity.cycle_id == 1
                assert s.last_acknowledged.sequence == initial + 1
                stopped = app.stop()
                engine.release.set()
                stopped.result(WAIT)
                assert not any(
                    t.name.startswith("FirstStationReporting-") for t in threads()
                )
                assert readback(peer)[0] == initial + 1
        finally:
            for engine in engines:
                engine.release.set()
            close(local)


def test_exceptional_runtime_outcome_is_skipped_not_reported(tmp_path: Path) -> None:
    with server() as peer:
        local = composition(tmp_path, Engine(fatal=True), settings(peer))
        app = local.application
        try:
            app.start().result(WAIT)
            until(
                lambda: (
                    app.snapshot().state is ApplicationState.FAULT
                    and not app.snapshot().runtime_owned
                )
            )
            s = app.reporting_snapshot()
            assert s is not None and s.skipped_count == 1 and s.last_skipped is not None
            assert s.last_skipped.identity.cycle_id == 1
            assert s.queued_count == s.attempted_count == s.acknowledged_count == 0
            assert peer.packets == ()
        finally:
            close(local)


def test_slow_reporter_keeps_gui_responsive_and_close_waits_actual_exit(
    qapp: QApplication, tmp_path: Path
) -> None:
    with server() as peer, server() as manual_peer:
        peer.hold_next(ReportStage.BITMAP)
        engine = Engine()
        engine.hold_call = 15
        local = composition(tmp_path, engine, settings(peer, timeout=2))
        app = local.application
        bridge = QtProductionBridge(app, local.store)
        window = ProductionWindow(bridge, (local.choice,))
        window.show()
        try:
            bridge.start()
            until(peer.entered.is_set)
            until(lambda: app.snapshot().counters.cycles_completed >= 2)
            until(engine.gate.is_set)
            window.navigate(2)
            assert window.communication.write_button.isEnabled()
            assert not window.communication.auto_refresh  # Independent manual owner.
            window.communication.configure_preferences(
                tmp_path / "communication.json", CommunicationPreferences()
            )
            window.communication.port.setValue(manual_peer.port)
            window.communication.persist_settings()
            window.communication.dispatch(RequestKind.TEST)
            until(lambda: not window.communication.busy)
            response = window.communication.last_response
            assert response is not None and response.values == (1234,)
            assert (
                not peer.release.is_set()
            )  # Manual client didn't borrow the report client.
            window.close()
            qapp.processEvents()
            assert window.isVisible() and not bridge.closed
            assert app.snapshot().state is ApplicationState.STOPPING
            peer.release.set()
            engine.release.set()
            until(lambda: bridge.closed and not window.isVisible())
            assert app.join(0)
        finally:
            peer.release.set()
            engine.release.set()
            window.close()
            until(lambda: bridge.closed)
            window.deleteLater()
        assert not any(t.name.startswith("FirstStationReporting-") for t in threads())
