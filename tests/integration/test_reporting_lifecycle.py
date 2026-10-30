"""Fake Production + actual loopback reporting, independent of GUI result polling."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event, current_thread, get_ident
from threading import enumerate as threads

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.support.application import prepared
from tests.support.cycles import image_result
from tests.support.modbus import ModbusPeer
from tests.support.qt import until
from tests.support.reporting import WAIT, readback, seed, server, settings

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
    ProductionCounters,
)
from capsule_vision.application.composition import EngineeringComposition, compose
from capsule_vision.application.session import EngineFactory
from capsule_vision.communication import (
    FinsAddress,
    FinsMemoryArea,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
    HoldingRegisters,
    ModbusSettings,
    first_station_reject_words,
)
from capsule_vision.communication.operations import CommunicationRequest, RequestKind
from capsule_vision.communication.reporting import (
    ReportingCapacityError,
    ReportingSelection,
    ReportingSettings,
    ReportingWriteProtectedError,
    ReportStage,
)
from capsule_vision.configuration import (
    MachineLayoutDefinition,
    RecipeDefinition,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
)
from capsule_vision.inference import ImageResult
from capsule_vision.inference._validation import uint8_array
from capsule_vision.inspection import CycleStatus
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge

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
    reporting: ReportingSettings | None = None,
    observe: list[Future[None]] | None = None,
    engine_factory: EngineFactory | None = None,
    camera_count: int = 6,
    reporting_editable: bool = False,
) -> EngineeringComposition:
    config = prepared(folder).configuration
    config = replace(
        config,
        layout=replace(config.layout, cameras=config.layout.cameras[:camera_count]),
    )
    model, machine, recipe = (
        folder / name for name in ("general/model.json", "machine.tsv", "local.json")
    )
    save_model_definition(model, config.model)
    save_machine_layout(machine, MachineLayoutDefinition(config.layout))
    save_recipe(recipe, RecipeDefinition("local", "Local test", model_id="general"))
    paths: list[Path] = []
    for index in range(camera_count):
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
        reporting_editable=reporting_editable,
        observe_source=observe.append if observe is not None else lambda _: None,
    )


def close(local: EngineeringComposition) -> None:
    local.application.close().result(WAIT)
    assert local.application.join(WAIT)


@pytest.mark.parametrize("camera_count", [5, 6])
def test_custom_reporting_range_is_frozen_and_manual_guard_uses_actual_topology(
    tmp_path: Path, camera_count: int
) -> None:
    base = 120
    with server(base) as peer:
        engine = Engine()
        engine.hold_call = 1
        peer.hold_next(ReportStage.BITMAP)
        selected = settings(peer, timeout=2)
        local = composition(tmp_path, engine, selected, camera_count=camera_count)
        app = local.application
        future = app.start()
        try:
            until(engine.gate.is_set)
            assert app.snapshot().state is ApplicationState.STARTING
            words = 3 if camera_count == 5 else 4
            request = CommunicationRequest(
                kind=RequestKind.WRITE,
                start=base,
                values=(7,),
                area=FinsMemoryArea.W,
                endpoint=FinsUdpEndpoint(
                    host=selected.endpoint.host,
                    port=selected.endpoint.port,
                    timeout=1,
                    destination=FinsAddress(0, 2, 0),
                    source=FinsAddress(0, 1, 0),
                ),
            )

            def check_range() -> None:
                for start, count in ((base, 1), (base + words - 1, 1), (base - 1, 2)):
                    with pytest.raises(ReportingWriteProtectedError):
                        app.validate_manual_request(
                            replace(
                                request, start=start, count=count, values=(7,) * count
                            )
                        )
                # The former fixed range and the unused 30-channel fourth word are free.
                for start in (49, base - 1, base + words):
                    app.validate_manual_request(replace(request, start=start))
                app.validate_manual_request(replace(request, area=FinsMemoryArea.DM))
                app.validate_manual_request(
                    replace(request, kind=RequestKind.READ, values=())
                )
                app.validate_manual_request(
                    replace(
                        request,
                        endpoint=replace(
                            request.endpoint,
                            port=selected.endpoint.port + 1
                            if selected.endpoint.port < 65535
                            else selected.endpoint.port - 1,
                        ),
                    )
                )
                with pytest.raises(ReportingWriteProtectedError):
                    app.validate_manual_request(
                        replace(
                            request,
                            endpoint=FinsTcpEndpoint(
                                host=request.endpoint.host,
                                port=request.endpoint.port,
                                timeout=request.endpoint.timeout,
                            ),
                        )
                    )

            check_range()
            with pytest.raises(ApplicationStateError):
                app.set_reporting_selection(
                    ReportingSelection(True, replace(selected, base_address=200))
                )
            assert app.reporting_for_generation(1) == selected
            engine.release.set()
            future.result(WAIT)
            until(peer.entered.is_set)
            check_range()
            assert app.reporting_for_generation(1) == selected
        finally:
            engine.release.set()
            peer.release.set()
            close(local)


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
            # STOPPING confirms stop admission, not source Future completion.
            until(sources[0].done)
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
        window = MainWindow(
            bridge,
        )
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
            window.communication.dispatch(RequestKind.READ)
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


@pytest.mark.parametrize("camera_count", [5, 6])
def test_modbus_production_frozen_unit_guard_stop_drain_and_next_start(
    tmp_path: Path,
    camera_count: int,
) -> None:
    with ModbusPeer() as peer:
        base = 120
        peer.words.update({base: 9, 200: 20})
        bitmap_entered, bitmap_release = Event(), Event()
        factory_entered, factory_release = Event(), Event()
        first_bitmap = True
        engines = (Engine(), Engine())
        for engine in engines:
            engine.hold_call = 9  # Hold later inspection after the first report.
        remaining = iter(engines)

        def factory(config: PreparedConfiguration):
            if not factory_entered.is_set():
                factory_entered.set()
                assert factory_release.wait(WAIT)
            return next(remaining)

        def respond(request: bytes) -> tuple[bytes, ...]:
            nonlocal first_bitmap
            result = tuple(peer.normal_response(request))
            if request[7] == 16 and first_bitmap:
                first_bitmap = False
                bitmap_entered.set()
                assert bitmap_release.wait(WAIT)
            return result

        peer.respond = respond
        selected = ReportingSettings(
            base_address=base,
            endpoint=ModbusSettings(port=peer.port, timeout=3, unit_id=7),
        )
        local = composition(
            tmp_path,
            engines[0],
            selected,
            engine_factory=factory,
            camera_count=camera_count,
            reporting_editable=True,
        )
        app = local.application
        request = CommunicationRequest(
            kind=RequestKind.WRITE,
            start=base,
            values=(7,),
            area=HoldingRegisters.HOLDING,
            endpoint=ModbusSettings(
                host="127.0.0.1", port=peer.port, timeout=1, unit_id=7
            ),
        )
        words = 3 if camera_count == 5 else 4

        def check_guard() -> None:
            for start, count in ((base, 1), (base + words - 1, 1), (base - 1, 2)):
                with pytest.raises(ReportingWriteProtectedError):
                    app.validate_manual_request(
                        replace(request, start=start, count=count, values=(7,) * count)
                    )
            for permitted in (
                replace(request, endpoint=replace(request.endpoint, unit_id=8)),
                replace(
                    request,
                    endpoint=replace(
                        request.endpoint,
                        port=peer.port + 1 if peer.port < 65535 else peer.port - 1,
                    ),
                ),
                replace(request, endpoint=replace(request.endpoint, host="127.0.0.2")),
                replace(request, start=base + words),
                replace(request, start=base - 1),
                replace(request, kind=RequestKind.READ, values=()),
                replace(
                    request,
                    area=FinsMemoryArea.W,
                    endpoint=FinsUdpEndpoint(
                        host=request.endpoint.host,
                        port=request.endpoint.port,
                        timeout=request.endpoint.timeout,
                    ),
                ),
                replace(
                    request,
                    area=FinsMemoryArea.W,
                    endpoint=FinsTcpEndpoint(
                        host=request.endpoint.host,
                        port=request.endpoint.port,
                        timeout=request.endpoint.timeout,
                    ),
                ),
            ):
                app.validate_manual_request(permitted)

        try:
            started = app.start()
            until(factory_entered.is_set)
            assert app.snapshot().state is ApplicationState.STARTING
            check_guard()
            with pytest.raises(ApplicationStateError):
                app.set_reporting_selection(
                    ReportingSelection(
                        True,
                        replace(
                            selected,
                            base_address=200,
                            endpoint=replace(selected.endpoint, unit_id=8),
                        ),
                    )
                )
            factory_release.set()
            started.result(WAIT)
            until(bitmap_entered.is_set)
            assert app.reporting_for_generation(1) == selected
            check_guard()
            stopping = app.stop()
            engines[0].release.set()
            until(lambda: app.snapshot().state is ApplicationState.STOPPING)
            assert not stopping.done()
            check_guard()  # Unfinished owner still owns the frozen address/Unit ID.
            bitmap_release.set()
            stopping.result(WAIT)
            snapshot = app.reporting_snapshot()
            assert snapshot is not None and snapshot.acknowledged_count >= 1
            assert not snapshot.thread_alive and snapshot.last_acknowledged is not None
            assert snapshot.last_acknowledged.sequence is not None
            assert peer.words[base] == snapshot.last_acknowledged.sequence
            assert app.snapshot().state is ApplicationState.READY
            app.validate_manual_request(request)
            previous_counters = app.snapshot().counters
            changed = replace(
                selected,
                base_address=200,
                endpoint=replace(selected.endpoint, unit_id=8),
            )
            app.set_reporting_selection(ReportingSelection(True, changed))
            assert app.snapshot().counters == previous_counters
            assert app.reporting_for_generation(1) == selected
            before = len(peer.requests)
            app.start().result(WAIT)
            until(
                lambda: (
                    (snapshot := app.reporting_snapshot()) is not None
                    and snapshot.acknowledged_count >= 1
                )
            )
            assert app.reporting_for_generation(2) == changed
            assert peer.requests[before][6:] == bytes.fromhex("080300c80001")
            assert peer.words[200] >= 21
            assert all(r[6] == 8 for r in peer.requests[before:])
            stopping = app.stop()
            engines[1].release.set()
            stopping.result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
            assert all(e.closed for e in engines)
        finally:
            factory_release.set()
            bitmap_release.set()
            for engine in engines:
                engine.release.set()
            close(local)
