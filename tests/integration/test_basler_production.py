"""Developer single-cycle and saved passive sources, using controlled fake owners."""

import subprocess
import sys
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Condition, Event, get_ident
from threading import enumerate as threads
from time import perf_counter
from typing import Never

import numpy as np
import pytest
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication
from tests.application_support import WAIT
from tests.bootstrap_support import saved_data
from tests.cycle_support import image_result
from tests.local_input_support import input_files
from tests.qt_support import until
from tests.reporting_support import seed, server

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ProductionCounters,
    _basler_sdk,
)
from vimo_demo.application.basler import (
    AcquiredImage,
    AcquisitionGroup,
    AcquisitionIssue,
    AcquisitionMetadata,
    AcquisitionReport,
    ArmedAcquisition,
    BaslerAcquisitionCancelled,
    BaslerAcquisitionError,
    BaslerAcquisitionSource,
    CameraCleanup,
    CameraReadiness,
)
from vimo_demo.application.basler_control import BaslerControlSnapshot
from vimo_demo.application.continuous_basler import (
    ContinuousArmedAcquisition,
    ContinuousBaslerSource,
)
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import (
    LocalReportingSettings,
    ReportingSelection,
    ReportStage,
)
from vimo_demo.configuration import (
    CameraFolder,
    CameraSources,
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
)
from vimo_demo.inference import ImageResult
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.ui import local_production
from vimo_demo.ui.bootstrap import create_window, load_startup
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge
from vimo_demo.ui.production_presentation import matching_camera
from vimo_demo.ui.production_style import text

# Provisional field mapping in configured CAM1..CAM6 order; explicitly includes
# the CAM5/CAM6 order that differs from sorting serials. This is test data only.
SERIALS = ("25334173", "25334174", "25334178", "25334180", "25334188", "25334184")


class Engine:
    def __init__(self) -> None:
        self.warm, self.release_warm = Event(), Event()
        self.active, self.release_active = Event(), Event()
        self.release_active.set()
        self.calls: list[tuple[int, int]] = []
        self.closed = False
        self.fail_warmup = False

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        image = uint8_array(image_bgr)
        self.calls.append((int(image[0, 0, 0]), get_ident()))
        if len(self.calls) == 1:
            self.warm.set()
            assert self.release_warm.wait(WAIT)
            if self.fail_warmup:
                raise ValueError("Warm-up failed")
        if len(self.calls) == 3:
            self.active.set()
            assert self.release_active.wait(WAIT)
        assert not image.flags.writeable
        return image_result(image)

    def close(self) -> None:
        self.closed = True


class Acquisition(BaslerAcquisitionSource):
    def __init__(self, serials: Sequence[str], *, timeout: float = 30.0) -> None:
        super().__init__(serials, timeout=timeout)
        self.entered, self.release, self.cleaned = Event(), Event(), Event()
        self.called = self.running = self.unsafe = self.ignore_cancel = False
        self.problem = ""
        self.wrong_order = self.wrong_shape = False
        self.owner: int | None = None

    @property
    def cleanup_safe(self) -> bool:
        return not self.running and not self.unsafe

    def acquire(
        self,
        *,
        on_armed: Callable[[ArmedAcquisition], None] | None = None,
        cancel: Event | None = None,
    ) -> AcquisitionGroup:
        self.called, self.running, self.owner = True, True, get_ident()
        now = perf_counter()
        armed = ArmedAcquisition(
            self.serials,
            now,
            now + self.timeout,
            tuple(CameraReadiness(s, True, True, now) for s in self.serials),
        )
        try:
            if on_armed is not None:
                on_armed(armed)
            self.entered.set()
            assert self.release.wait(
                WAIT
            )  # Controlled native-call boundary, no sleeps.
            images: list[AcquiredImage] = []
            for index, serial in enumerate(self.serials):
                pixels = np.full(
                    (10, 30, 3) if self.wrong_shape else (20, 60, 3),
                    100 + index,
                    dtype=np.uint8,
                )
                pixels.setflags(write=False)
                metadata = AcquisitionMetadata(
                    serial,
                    now,
                    True,
                    0,
                    "",
                    60,
                    20,
                    123,
                    1200,
                    9000 + index,
                    87,
                    777777,
                    0,
                )
                images.append(AcquiredImage(serial, pixels, metadata))
            if self.problem == "missing":
                images.pop()
            issue = (
                (AcquisitionIssue("acquisition", None, self.problem),)
                if self.problem
                else ()
            )
            cleanup = tuple(
                CameraCleanup(s, True, not self.unsafe, True, True)
                for s in self.serials
            )
            report = AcquisitionReport(
                self.serials,
                now,
                armed,
                perf_counter(),
                (),
                tuple(i.metadata for i in images),
                issue,
                cleanup,
            )
            if self.unsafe or self.problem:
                raise BaslerAcquisitionError(report)
            if cancel is not None and cancel.is_set() and not self.ignore_cancel:
                raise BaslerAcquisitionCancelled(report)
            if self.wrong_order:
                images.reverse()
            return AcquisitionGroup(tuple(images), report)
        finally:
            self.running = False
            self.cleaned.set()


@pytest.fixture
def acquisitions(monkeypatch: pytest.MonkeyPatch) -> list[Acquisition]:
    sources: list[Acquisition] = []

    def factory(serials: Sequence[str], *, timeout: float = 30.0) -> Acquisition:
        source = Acquisition(serials, timeout=timeout)
        sources.append(source)
        return source

    monkeypatch.setattr(local_production, "BaslerAcquisitionSource", factory)
    return sources


def cleanup(
    composition: local_production.LocalComposition,
    engines: list[Engine],
    sources: list[Acquisition],
) -> None:
    for source in sources:
        source.release.set()
        source.unsafe = False  # Fake recovery control only, no native API calls.
    for engine in engines:
        engine.release_warm.set()
        engine.release_active.set()
    composition.application.close().result(WAIT)
    assert composition.application.join(WAIT)


@pytest.mark.parametrize("saved_folders", [False, True])
def test_one_cycle_warmup_mapping_accounting_and_matching_display(
    qapp: QApplication,
    tmp_path: Path,
    acquisitions: list[Acquisition],
    saved_folders: bool,
) -> None:
    engine = Engine()
    sources: list[Future[None]] = []
    original = {t.ident for t in threads()}
    notices: list[
        ArmedAcquisition | ContinuousArmedAcquisition | AcquisitionReport
    ] = []
    model, machine, recipe, paths = input_files(tmp_path)
    if saved_folders:
        save_camera_sources(
            tmp_path / "camera_sources.json",
            CameraSources(
                tuple(
                    CameraFolder(camera, str(tmp_path))
                    for camera in load_machine_layout(machine).layout.camera_ids
                )
            ),
        )
    composition = local_production.compose(
        model,
        machine,
        recipe,
        () if saved_folders else paths,
        engine_factory=lambda _: engine,
        observe_source=sources.append,
        basler_serials=SERIALS,
        observe_acquisition=notices.append,
    )
    app = composition.application
    bridge = QtProductionBridge(app, composition.store, poll_ms=5)
    try:
        assert not composition.store.snapshot().frames
        startup = app.start()
        until(engine.warm.is_set)
        assert app.snapshot().state is ApplicationState.STARTING
        assert app.snapshot().counters == ProductionCounters()
        assert not any(source.called for source in acquisitions)
        assert not composition.store.snapshot().frames
        engine.release_warm.set()
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        assert startup.done() and startup.exception() is None
        assert app.snapshot().state is ApplicationState.RUNNING
        assert app.snapshot().latest_production_result is None
        assert app.snapshot().counters == ProductionCounters()
        assert [value for value, _ in engine.calls] == [1, 1]
        assert source.serials == SERIALS and source.owner != get_ident()
        source.release.set()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        snapshot = app.snapshot()
        result = snapshot.latest_production_result
        assert result is not None and result.cycle_id == CycleId(1)
        assert len(result.channels) == 36
        assert (
            snapshot.counters.cycles_started == snapshot.counters.cycles_completed == 1
        )
        assert (
            snapshot.counters.channel_ok
            + snapshot.counters.channel_ng
            + snapshot.counters.channel_unknown
            + snapshot.counters.channel_disabled
            == 36
        )
        assert [value for value, _ in engine.calls] == [1, 1, *range(100, 106)]
        assert len({owner for _, owner in engine.calls}) == 1
        assert engine.calls[0][1] not in (get_ident(), source.owner)
        assert engine.closed and source.cleaned.is_set()
        frames = composition.store.snapshot().frames
        assert (
            tuple(row.frame_id.camera_id for row in frames) == composition.store.cameras
        )
        assert [int(row.image[0, 0, 0]) for row in frames] == list(range(100, 106))
        assert all(
            row.frame_id.sequence == 0 and row.frame_id.cycle_id == 1 for row in frames
        )
        assert all(matching_camera(row, snapshot) is not None for row in frames)
        bridge.refresh()
        qapp.processEvents()
        assert (
            app.snapshot().counters.cycles_completed == 1
        )  # No automatic second attempt.
        assert sources[0].done() and sources[0].exception() is None
        assert sum(isinstance(n, ArmedAcquisition) for n in notices) == 1
        assert sum(isinstance(n, AcquisitionReport) for n in notices) == 1
    finally:
        cleanup(composition, [engine], acquisitions)
        bridge.detach()
    assert not {t.ident for t in threads()} - original


@pytest.mark.parametrize(
    "problem", ["missing", "unexpected extra frame", "ambiguous trigger"]
)
def test_failed_acquisition_has_no_cycle_judgments_or_fake_ng(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], problem: str
) -> None:
    engine = Engine()
    engine.release_warm.set()
    sources: list[Future[None]] = []
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_source=sources.append,
        observe_acquisition=lambda _: None,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.problem = problem
        source.release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        snapshot = composition.application.snapshot()
        assert snapshot.counters == ProductionCounters()
        assert snapshot.latest_production_result is None
        assert not composition.store.snapshot().frames
        assert len(engine.calls) == 2 and engine.closed
        assert isinstance(sources[0].exception(), BaslerAcquisitionError)
        assert source.cleaned.is_set()
    finally:
        cleanup(composition, [engine], acquisitions)


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.parametrize("late_group", [False, True])
def test_stop_close_wait_for_actual_source_exit_and_drop_late_groups(
    qapp: QApplication,
    tmp_path: Path,
    acquisitions: list[Acquisition],
    close: bool,
    late_group: bool,
) -> None:
    engine = Engine()
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.ignore_cancel = late_group
        stopped = app.close() if close else app.stop()
        assert app.snapshot().state is ApplicationState.STOPPING
        assert not stopped.done() and not source.cleaned.is_set()
        with pytest.raises(ApplicationStateError):
            app.start()
        source.release.set()
        stopped.result(WAIT)
        assert app.snapshot().state is (
            ApplicationState.CLOSED if close else ApplicationState.READY
        )
        assert app.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
        assert source.cleaned.is_set() and engine.closed and len(engine.calls) == 2
    finally:
        cleanup(composition, [engine], acquisitions)


def test_stop_after_acquisition_cleanup_before_admission_drops_group(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.release_warm.set()

    def observation(
        row: ArmedAcquisition | ContinuousArmedAcquisition | AcquisitionReport,
    ) -> None:
        if isinstance(row, AcquisitionReport):
            composition.application.stop()

    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=observation,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
    finally:
        cleanup(composition, [engine], acquisitions)


def test_generation_isolation_and_counters_retained_on_explicit_restart(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engines: list[Engine] = []

    def factory(_: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=factory,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        for generation in (1, 2):
            app.start().result(WAIT)
            until(lambda: acquisitions[-1].entered.is_set())
            acquisitions[-1].release.set()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert app.snapshot().generation == generation
            assert app.snapshot().counters.cycles_completed == generation
            assert all(
                row.generation == generation
                for row in composition.store.snapshot().frames
            )
            latest = app.snapshot().latest_production_result
            assert latest is not None and latest.cycle_id == 1
        assert len(engines) == 2 and all(engine.closed for engine in engines)
        assert sum(source.called for source in acquisitions) == 2
    finally:
        cleanup(composition, engines, acquisitions)


def test_unsafe_device_cleanup_remains_fault_owned(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        source = acquisitions[-1]
        source.unsafe = True
        source.release.set()
        until(lambda: app.snapshot().state is ApplicationState.FAULT)
        assert app.snapshot().runtime_owned and source.cleaned.is_set()
        assert app.snapshot().counters == ProductionCounters()
        with pytest.raises(ApplicationError, match="Stop failed"):
            app.stop().result(WAIT)
        assert app.snapshot().runtime_owned
    finally:
        cleanup(composition, [engine], acquisitions)


@pytest.mark.parametrize("close", [False, True])
def test_stop_close_after_admission_finishes_active_inspection(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], close: bool
) -> None:
    engine = Engine()
    engine.release_warm.set()
    engine.release_active.clear()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    app = composition.application
    try:
        app.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].release.set()
        until(engine.active.is_set)
        assert acquisitions[-1].cleaned.is_set()
        stopped = app.close() if close else app.stop()
        assert not stopped.done() and not engine.closed
        engine.release_active.set()
        stopped.result(WAIT)
        assert app.snapshot().counters.cycles_completed == 1
        assert len(engine.calls) == 8 and engine.closed
        assert len(composition.store.snapshot().frames) == 6
    finally:
        cleanup(composition, [engine], acquisitions)


def test_cli_requires_mapping_confirmation_and_rejects_plc_reporting() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/run_local_production_ui.py"
    base = [
        sys.executable,
        str(script),
        "--model-config",
        "unused",
        "--machine-config",
        "unused",
        "--recipe",
        "unused",
        "--basler-serials",
        *SERIALS,
    ]
    for extra, message in (
        ([], "--basler-mapping-confirmed"),
        (
            ["--basler-mapping-confirmed", "--report-first-station"],
            "cannot perform PLC reporting",
        ),
    ):
        result = subprocess.run(
            [*base, *extra], capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 2 and message in result.stderr


@pytest.mark.parametrize("option", ["replay", "reporting"])
def test_programmatic_composition_rejects_incompatible_modes(
    tmp_path: Path, option: str
) -> None:
    with pytest.raises(ValueError, match="cannot replay or report"):
        local_production.compose(
            *input_files(tmp_path),
            basler_serials=SERIALS,
            replay_controls=option == "replay",
            reporting=LocalReportingSettings() if option == "reporting" else None,
        )


@pytest.mark.parametrize("problem", ["order", "shape"])
def test_invalid_acquired_group_is_not_admitted(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition], problem: str
) -> None:
    engine = Engine()
    engine.release_warm.set()
    futures: list[Future[None]] = []
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_source=futures.append,
        observe_acquisition=lambda _: None,
    )
    try:
        composition.application.start().result(WAIT)
        until(lambda: acquisitions[-1].entered.is_set())
        acquisitions[-1].wrong_order = problem == "order"
        acquisitions[-1].wrong_shape = problem == "shape"
        acquisitions[-1].release.set()
        until(
            lambda: composition.application.snapshot().state is ApplicationState.READY
        )
        assert isinstance(futures[0].exception(), ValueError)
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
    finally:
        cleanup(composition, [engine], acquisitions)


def test_failed_warmup_never_calls_hardware_source(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    engine = Engine()
    engine.fail_warmup = True
    engine.release_warm.set()
    composition = local_production.compose(
        *input_files(tmp_path),
        engine_factory=lambda _: engine,
        basler_serials=SERIALS,
        observe_acquisition=lambda _: None,
    )
    try:
        ready = composition.application.start()
        until(ready.done)
        assert ready.exception() is not None
        assert composition.application.snapshot().state is ApplicationState.READY
        assert composition.application.snapshot().active is None
        assert composition.application.snapshot().counters == ProductionCounters()
        assert not composition.store.snapshot().frames
        assert not any(source.called for source in acquisitions)
        assert engine.closed
    finally:
        cleanup(composition, [engine], acquisitions)


def save_basler(path: Path) -> None:
    source = load_camera_sources(path)
    save_camera_sources(
        path,
        replace(
            source,
            mode="basler",
            cameras=tuple(
                replace(c, basler_serial=s)
                for c, s in zip(source.cameras, SERIALS, strict=True)
            ),
        ),
    )


class ContinuousAcquisition(ContinuousBaslerSource):
    def __init__(self, cameras: tuple[CameraId, ...], serials: tuple[str, ...]):
        super().__init__(cameras, serials)
        self.entered, self.release, self.cleaned = Event(), Event(), Event()
        self.problem = ""
        self.camera_count = 6
        self.ignore_cancel = False

    def run(
        self,
        stop: Event,
        *,
        begin: Callable[[CycleId], None],
        submit: Callable[[Frame], None],
        on_armed: Callable[[ContinuousArmedAcquisition], None],
        on_frame: Callable[[CycleId, AcquisitionMetadata], None] = lambda _c, _m: None,
    ) -> None:
        self.cleanup_safe = False
        now = perf_counter()
        try:
            on_armed(
                ContinuousArmedAcquisition(
                    self.serials,
                    now,
                    tuple(CameraReadiness(s, True, True, now) for s in self.serials),
                )
            )
            self.entered.set()
            assert self.release.wait(WAIT)
            if stop.is_set():
                return
            if self.problem:
                raise RuntimeError(self.problem)
            cycle = CycleId(1)
            begin(cycle)
            for index, camera in enumerate(self.cameras[: self.camera_count]):
                submit(
                    Frame(
                        FrameId(cycle, camera, 10 + index),
                        perf_counter(),
                        np.full((20, 60, 3), 100 + index, np.uint8),
                    )
                )
            assert stop.wait(WAIT)
        finally:
            self.cleanup_safe = True
            self.cleaned.set()


@pytest.fixture
def continuous_acquisitions(
    monkeypatch: pytest.MonkeyPatch,
) -> list[ContinuousAcquisition]:
    sources: list[ContinuousAcquisition] = []

    def factory(
        cameras: tuple[CameraId, ...], serials: tuple[str, ...]
    ) -> ContinuousAcquisition:
        source = ContinuousAcquisition(cameras, serials)
        sources.append(source)
        return source

    monkeypatch.setattr(local_production, "ContinuousBaslerSource", factory)
    return sources


@pytest.mark.parametrize(
    "problem",
    ["", "missing", "unexpected extra frame", "ambiguous trigger", "idle_stop"],
)
def test_normal_persistent_basler_start_one_cycle_or_explicit_source_failure(
    qapp: QApplication,
    tmp_path: Path,
    continuous_acquisitions: list[ContinuousAcquisition],
    problem: str,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    # Persisted entry ordering is not wiring or execution ordering.
    definition = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources, replace(definition, cameras=tuple(reversed(definition.cameras)))
    )
    original = data.sources.read_bytes()
    engine = Engine()
    delivery = FutureDelivery()
    startup = load_startup(
        data,
        engine_factory=lambda _: engine,
        observe_source=lambda f: delivery.watch("folder_source", f),
        observe_acquisition=lambda n: (
            local_production.SourceNotifications.watch_acquisition(
                delivery, startup.application.snapshot().generation, n
            )
        ),
    )
    window = create_window(startup, delivery)
    window.show()
    progress: list[str] = []

    def record_progress(_generation: int, phase: str) -> None:
        progress.append(phase)

    window.bridge.source_progress_changed.connect(record_progress)
    before = {t.ident for t in threads()}
    try:
        assert not startup.issues and not continuous_acquisitions and not engine.calls
        assert data.sources.read_bytes() == original
        assert not window.bridge.replay_enabled
        assert not window.buttons["secondary"].isVisible()
        window.buttons["operation"].click()
        until(engine.warm.is_set)
        assert window.snapshot.state is ApplicationState.STARTING
        assert not window.event_label.text()
        assert "Basler" in window.state_label.toolTip()
        assert (
            not any(source.entered.is_set() for source in continuous_acquisitions)
            and startup.application.snapshot().counters == ProductionCounters()
        )
        engine.release_warm.set()
        until(
            lambda: (
                bool(continuous_acquisitions)
                and continuous_acquisitions[-1].entered.is_set()
            )
        )
        source = continuous_acquisitions[-1]
        until(lambda: progress == ["basler_armed"])
        assert not window.event_label.text()
        assert window.state_label.toolTip().startswith("ARMED")
        assert source.serials == SERIALS
        assert startup.application.snapshot().counters == ProductionCounters()
        assert not startup.frames.snapshot().frames
        assert (
            not window.bridge.replay_enabled
            and not window.buttons["secondary"].isVisible()
        )
        assert (
            window.buttons["stop"].isEnabled() and window.buttons["pause"].isEnabled()
        )
        assert window.bridge.controls_enabled
        assert startup.local_replay is not None
        with pytest.raises(ApplicationStateError):
            startup.local_replay.step_replay()
        source.problem = "" if problem == "idle_stop" else problem
        if problem == "idle_stop":
            window.buttons["stop"].click()
            assert startup.application.snapshot().state is ApplicationState.STOPPING
            assert startup.application.snapshot().runtime_owned
            assert not source.cleaned.is_set()  # Still waiting for the owned SDK path.
        source.release.set()
        if not problem:
            until(lambda: startup.application.snapshot().counters.cycles_completed == 1)
            assert startup.application.snapshot().state is ApplicationState.RUNNING
            startup.application.stop().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        window.bridge.refresh()
        if problem:
            assert startup.application.snapshot().counters == ProductionCounters()
            assert not startup.frames.snapshot().frames
            if problem == "idle_stop":
                assert startup.application.snapshot().last_issue is None
                assert not startup.application.snapshot().runtime_owned
                assert not window.event_label.text()
            else:
                until(lambda: "RuntimeError" in window.event_label.accessibleName())
            assert progress == ["basler_armed"]
        else:
            snapshot = startup.application.snapshot()
            result = snapshot.latest_production_result
            assert result is not None and len(result.channels) == 36
            assert (
                snapshot.counters.cycles_started
                == snapshot.counters.cycles_completed
                == 1
            )
            assert [
                int(f.image[0, 0, 0]) for f in startup.frames.snapshot().frames
            ] == list(range(100, 106))
            for index, frame in enumerate(startup.frames.snapshot().frames):
                assert frame.frame_id == result.cameras[index].frame_id
                assert frame.source_key == f"basler:{SERIALS[index]}"
                assert matching_camera(frame, snapshot) is not None
            assert (
                not window.event_label.accessibleName()
            )  # ARMED is not stale after Stop.
            assert progress == ["basler_armed"]
        assert (
            source.cleaned.is_set()
            and engine.closed
            and len(continuous_acquisitions) == 1
        )
        assert data.sources.read_bytes() == original
    finally:
        engine.release_warm.set()
        engine.release_active.set()
        for source in continuous_acquisitions:
            source.release.set()
        window.close()
        until(lambda: window.bridge.closed)
        assert startup.application.join(0)
        delivery.detach()
    assert not {t.ident for t in threads()} - before


@pytest.mark.parametrize("close", [False, True])
def test_normal_basler_stop_close_waits_for_source_and_drops_late_group(
    qapp: QApplication,
    tmp_path: Path,
    continuous_acquisitions: list[ContinuousAcquisition],
    close: bool,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    try:
        startup.application.start().result(WAIT)
        until(
            lambda: (
                bool(continuous_acquisitions)
                and continuous_acquisitions[-1].entered.is_set()
            )
        )
        source = continuous_acquisitions[-1]
        source.ignore_cancel = True
        stopped = startup.application.close() if close else startup.application.stop()
        until(lambda: startup.application.snapshot().state is ApplicationState.STOPPING)
        assert not stopped.done()
        source.release.set()
        stopped.result(WAIT)
        assert startup.application.snapshot().state is (
            ApplicationState.CLOSED if close else ApplicationState.READY
        )
        assert startup.application.snapshot().counters == ProductionCounters()
        assert not startup.frames.snapshot().frames and source.cleaned.is_set()
        assert engine.closed
    finally:
        for source in continuous_acquisitions:
            source.release.set()
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_normal_basler_requires_folder_warmup_before_engine_or_hardware(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    source = load_camera_sources(data.sources)
    save_camera_sources(
        data.sources,
        replace(
            source, cameras=tuple(replace(c, folder="missing") for c in source.cameras)
        ),
    )
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    try:
        assert startup.application.snapshot().state is ApplicationState.READY
        assert any(i.key == "startup_source_unavailable" for i in startup.issues)
        with pytest.raises(ApplicationError):
            startup.application.start().result(WAIT)
        assert not acquisitions and not engine.calls
        assert startup.application.snapshot().state is ApplicationState.READY
        assert startup.application.snapshot().active is None
        assert startup.application.snapshot().counters == ProductionCounters()
    finally:
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_normal_continuous_stop_start_keeps_counters_and_replaces_owners(
    qapp: QApplication,
    tmp_path: Path,
    continuous_acquisitions: list[ContinuousAcquisition],
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    original = data.sources.read_bytes()
    engines: list[Engine] = []

    def factory(_: object) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engines.append(engine)
        return engine

    startup = load_startup(data, engine_factory=factory)
    app = startup.application
    try:
        for generation in (1, 2):
            app.start().result(WAIT)
            until(lambda: continuous_acquisitions[-1].entered.is_set())
            assert app.snapshot().counters.cycles_completed == generation - 1
            continuous_acquisitions[-1].release.set()
            until(
                lambda expected=generation: (
                    app.snapshot().counters.cycles_completed == expected
                )
            )
            assert app.snapshot().state is ApplicationState.RUNNING
            assert all(
                f.generation == generation for f in startup.frames.snapshot().frames
            )
            assert app.snapshot().latest_production_result is not None
            app.stop().result(WAIT)
            assert app.snapshot().state is ApplicationState.READY
            assert continuous_acquisitions[-1].cleaned.is_set() and engines[-1].closed
        assert len(engines) == len(continuous_acquisitions) == 2
        assert data.sources.read_bytes() == original
    finally:
        for source in continuous_acquisitions:
            source.release.set()
        app.close().result(WAIT)
        assert app.join(WAIT)


def test_normal_saved_source_switch_updates_controls_only_after_save_apply(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    data = saved_data(tmp_path)
    engine = Engine()
    startup = load_startup(data, engine_factory=lambda _: engine)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    try:
        assert window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        window.navigate(4)
        workspace = window.parameters.roi_workspace
        assert workspace is not None and workspace.sources is not None
        until(lambda: not window.parameters.busy)
        for i in range(1, 7):
            workspace.sources.set_serial(CameraId(f"CAM{i}"), SERIALS[i - 1])
        workspace.open_source_settings()
        assert workspace.source_dialog is not None
        workspace.source_dialog.mode.setCurrentIndex(1)
        workspace.source_dialog.accept()
        assert window.bridge.replay_enabled  # Draft is not the saved source selection.
        assert not window.buttons["operation"].isEnabled()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert (
            not window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        )
        assert not engine.calls and not acquisitions
        workspace.open_source_settings()
        workspace.source_dialog.mode.setCurrentIndex(0)
        workspace.source_dialog.accept()
        workspace.save_and_apply()
        until(lambda: not window.parameters.busy)
        window.bridge.refresh()
        assert window.bridge.replay_enabled and window.buttons["secondary"].isHidden()
        assert (
            workspace.sources.saved.camera(CameraId("CAM5")).basler_serial == SERIALS[4]
        )
        assert not engine.calls and not acquisitions
        window.navigate(0)
        assert window.buttons["secondary"].isHidden()
    finally:
        window.close()
        until(lambda: window.bridge.closed)
        assert startup.application.join(0)
        delivery.detach()


def test_normal_basler_optional_sdk_failure_is_source_error_without_folder_fallback(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vimo_demo.application import _basler_sdk

    data = saved_data(tmp_path)
    save_basler(data.sources)

    def missing(*, continuous: bool = False) -> Never:
        raise ModuleNotFoundError("pypylon deliberately unavailable")

    monkeypatch.setattr(_basler_sdk, "PylonBackend", missing)
    engine = Engine()
    engine.release_warm.set()
    completions: list[Future[None]] = []
    startup = load_startup(
        data, engine_factory=lambda _: engine, observe_source=completions.append
    )
    try:
        assert not engine.calls and not completions  # Restore does not touch SDK.
        startup.application.start().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        assert startup.application.snapshot().counters == ProductionCounters()
        assert not startup.frames.snapshot().frames and len(engine.calls) == 2
        assert isinstance(completions[0].exception(), ModuleNotFoundError)
        assert engine.closed
    finally:
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_normal_folder_runtime_needs_no_pypylon(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pypylon", None)

    def no_hardware(
        serials: Sequence[str], *, timeout: float = 30
    ) -> BaslerAcquisitionSource:
        raise AssertionError("Folder runtime must not construct a hardware source")

    monkeypatch.setattr(local_production, "BaslerAcquisitionSource", no_hardware)
    data = saved_data(tmp_path)
    engine = Engine()
    engine.release_warm.set()
    startup = load_startup(data, engine_factory=lambda _: engine)
    original = data.sources.read_bytes()
    try:
        startup.application.start().result(WAIT)
        until(lambda: startup.application.snapshot().state is ApplicationState.READY)
        assert startup.application.snapshot().counters.cycles_completed > 0
        assert engine.closed and data.sources.read_bytes() == original
        assert "pypylon.pylon" not in sys.modules
    finally:
        startup.application.close().result(WAIT)
        assert startup.application.join(WAIT)


def test_developer_fixed_images_override_saved_basler_without_false_status(
    qapp: QApplication, tmp_path: Path, acquisitions: list[Acquisition]
) -> None:
    from vimo_demo.application.folder_replay import folder_images
    from vimo_demo.ui.production_window import ProductionWindow

    data = saved_data(tmp_path)
    save_basler(data.sources)
    original = data.sources.read_bytes()
    engine = Engine()
    engine.release_active.clear()
    composition = local_production.compose(
        data.models / "general.json",
        data.machine,
        data.recipes / "local.recipe.json",
        folder_images(tmp_path / "frames")[:6],
        sources_file=data.sources,
        engine_factory=lambda _: engine,
        replay_controls=True,
    )
    bridge = QtProductionBridge(
        composition.application,
        composition.store,
        local_replay=composition.application,
        poll_ms=5,
    )
    window = ProductionWindow(
        bridge, (composition.choice,), recipe_editor=composition.editor
    )
    window.show()
    try:
        bridge.start()
        until(engine.warm.is_set)
        assert "Basler" not in window.event_label.accessibleName()
        engine.release_warm.set()
        until(engine.active.is_set)
        assert bridge.replay_enabled and not acquisitions
        assert "Basler" not in window.event_label.accessibleName()
        assert data.sources.read_bytes() == original
    finally:
        engine.release_warm.set()
        engine.release_active.set()
        window.close()
        until(lambda: bridge.closed)
        assert composition.application.join(0)


@pytest.mark.parametrize("mode", ["complete", "timeout", "shutdown", "ambiguous"])
def test_normal_basler_reports_full_and_partial_cycles_and_drains_stop(
    qapp: QApplication,
    tmp_path: Path,
    continuous_acquisitions: list[ContinuousAcquisition],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from tests.fins_tcp_support import TcpPeer

    from vimo_demo.communication import FinsTransport, first_station_reject_words
    from vimo_demo.communication.reporting import (
        ProductionReportingCommands,
        ReportingSelection,
    )

    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    if mode in ("shutdown", "ambiguous"):
        engine.release_active.clear()
    with TcpPeer(65534) as peer:
        startup = load_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, ProductionReportingCommands)
        try:
            assert app.reporting_selection().settings.host == "192.168.251.1"
            assert app.reporting_selection().settings.transport is FinsTransport.TCP
            assert not app.reporting_selection().enabled
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    LocalReportingSettings(port=peer.port, transport=FinsTransport.TCP),
                )
            )
            app.start().result(WAIT)
            until(
                lambda: (
                    bool(continuous_acquisitions)
                    and continuous_acquisitions[-1].entered.is_set()
                )
            )
            source = continuous_acquisitions[-1]
            source.camera_count = 6 if mode == "complete" else 1
            assert not peer.packets and app.snapshot().counters == ProductionCounters()
            source.release.set()
            assert engine.active.wait(WAIT)
            if mode in ("shutdown", "ambiguous"):
                if mode == "ambiguous":

                    def ambiguous(_cycle: CycleId) -> bool:
                        return False

                    monkeypatch.setattr(source, "attributable", ambiguous)
                stopped = app.stop()
                assert not stopped.done()
                engine.release_active.set()
                stopped.result(WAIT)
            else:
                until(lambda: app.snapshot().counters.cycles_completed == 1)
                until(
                    lambda: (
                        (r := app.reporting_snapshot()) is not None
                        and r.acknowledged_count == 1
                    )
                )
                app.stop().result(WAIT)
            report = app.reporting_snapshot()
            assert report is not None and not report.thread_alive
            assert (
                report.pending == 0
                and report.failed_count == report.rejected_count == 0
            )
            if mode == "ambiguous":
                assert (
                    report.skipped_count == 1
                    and report.acknowledged_count == 0
                    and not peer.packets
                )
            else:
                assert report.acknowledged_count == 1 and report.skipped_count == 0
                result = app.snapshot().latest_production_result
                assert result is not None
                words = first_station_reject_words(
                    (c.channel_id, c.business_status) for c in result.channels
                )
                assert peer.words(49, 4) == (65535, *words)
                if mode != "complete":
                    assert words[0] & 0xFFC0 == 0xFFC0 and words[1:] == (0xFFFF, 15)
                assert (
                    report.last_acknowledged is not None
                    and report.last_acknowledged.completion_to_ack_ms is not None
                )
            assert engine.closed and source.cleaned.is_set()
            assert app.snapshot().state is ApplicationState.READY
        finally:
            engine.release_active.set()
            for source in continuous_acquisitions:
                source.release.set()
            app.close().result(WAIT)
            assert app.join(WAIT)


class PassiveBackend:
    """SDK FIFO fake; exercises the real source/control/session implementation."""

    def __init__(self) -> None:
        self.condition = Condition()
        self.queues = {
            s: deque[tuple[AcquisitionMetadata, NDArray[np.uint8]]]() for s in SERIALS
        }
        self.armed = Event()
        self.closed = False
        self.prepared: list[str] = []

    def inventory(self, serials: tuple[str, ...]) -> None:
        assert serials == SERIALS

    def prepare(self, serial: str) -> None:
        self.prepared.append(serial)

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.armed.set()
        return tuple(CameraReadiness(s, True, True, perf_counter()) for s in SERIALS)

    def available(self, serial: str, timeout_ms: int) -> bool:
        with self.condition:
            if timeout_ms:
                self.condition.wait_for(
                    lambda: bool(self.queues[serial]), timeout_ms / 1000
                )
            return bool(self.queues[serial])

    def retrieve(self, serial: str) -> tuple[AcquisitionMetadata, NDArray[np.uint8]]:
        with self.condition:
            return self.queues[serial].popleft()

    def expose(self, ordinal: int, cameras: tuple[int, ...] = tuple(range(6))) -> None:
        with self.condition:
            for index in cameras:
                pixels = np.full((20, 60, 3), ordinal * 10 + index, dtype=np.uint8)
                pixels.setflags(write=False)
                self.queues[SERIALS[index]].append(
                    (
                        AcquisitionMetadata(
                            SERIALS[index],
                            perf_counter(),
                            True,
                            0,
                            "",
                            60,
                            20,
                            1,
                            3600,
                            ordinal - 1,
                            ordinal,
                            ordinal * 1000,
                            0,
                        ),
                        pixels,
                    )
                )
            self.condition.notify_all()

    def cleanup(self) -> tuple[CameraCleanup, ...]:
        self.closed = True
        return tuple(CameraCleanup(s, True, True, True, True) for s in self.prepared)


def test_normal_basler_player_drains_paused_frames_and_settles_reports(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backends: list[PassiveBackend] = []
    engines: list[Engine] = []

    def backend(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        value = PassiveBackend()
        backends.append(value)
        return value

    def factory(_prepared: object) -> Engine:
        value = Engine()
        value.release_warm.set()
        engines.append(value)
        return value

    monkeypatch.setattr(_basler_sdk, "PylonBackend", backend)
    delivery = FutureDelivery()
    startup = load_startup(data, engine_factory=factory)
    window = create_window(startup, delivery)
    window.show()
    app = startup.application
    assert startup.local_replay is not None

    def control() -> BaslerControlSnapshot:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value

    original_threads = {t.ident for t in threads()}
    with server() as peer:
        seed(peer, 99)
        panel = window.communication.reporting_panel
        assert panel is not None
        panel.controller.set_reporting_selection(
            ReportingSelection(True, LocalReportingSettings(port=peer.port, timeout=2))
        )
        try:
            window.buttons["operation"].click()
            until(lambda: bool(backends) and backends[0].armed.is_set())
            window.bridge.refresh()
            middle = window.buttons["pause"]
            window.buttons["pause"].click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            position, size = middle.pos(), middle.size()
            assert (
                middle.text() == text("replay_step", window.language)
                and not window.buttons["secondary"].isVisible()
            )
            for ordinal in (1, 2, 3):
                backends[0].expose(ordinal)
                until(lambda ordinal=ordinal: control().received_frames == ordinal * 6)
            assert app.snapshot().counters == ProductionCounters()
            assert len(engines[0].calls) == 2 and not engines[0].closed
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.queued_count == 0
            peer.hold_next(ReportStage.BITMAP)
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(4)
            until(lambda: app.snapshot().counters.cycles_completed == 1)
            assert peer.entered.wait(WAIT)
            assert control().state is ReplayState.STEPPING
            assert not engines[0].closed and not backends[0].closed
            backends[0].expose(5)
            until(lambda: control().received_frames == 30)
            assert app.snapshot().counters.cycles_completed == 1
            peer.release.set()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            assert (middle.pos(), middle.size()) == (position, size)
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 1
            result = app.snapshot().latest_production_result
            assert (
                result is not None
                and result.cycle_id == 4
                and len(result.channels) == 36
            )
            assert all(
                c.frame_id is not None and c.frame_id.sequence == 4
                for c in result.cameras
            )
            middle.click()
            until(lambda: control().admission_armed)
            backends[0].expose(6)
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 2
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(lambda: control().state is ReplayState.AUTOMATIC)
            for ordinal in (7, 8):
                backends[0].expose(ordinal)
                until(
                    lambda ordinal=ordinal: (
                        app.snapshot().counters.cycles_completed == ordinal - 4
                    )
                )
            backends[0].expose(9, (0,))
            until(lambda: app.snapshot().counters.cycles_started == 5)
            window.bridge.refresh()
            middle.click()
            assert control().state is ReplayState.PAUSING
            backends[0].expose(9, (5, 3, 4, 1, 2))
            until(lambda: control().state is ReplayState.PAUSED)
            assert app.snapshot().counters.cycles_completed == 5
            observed = panel.controller.reporting_snapshot()
            assert observed is not None and observed.acknowledged_count == 5
            window.bridge.refresh()
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert backends[0].closed and engines[0].closed
            window.bridge.refresh()
            window.buttons["operation"].click()
            until(lambda: len(backends) == 2 and backends[1].armed.is_set())
            window.bridge.refresh()
            middle.click()
            until(lambda: control().state is ReplayState.PAUSED)
            window.bridge.refresh()
            middle.click()  # An idle Step needs no fabricated cycle or timeout.
            until(lambda: control().admission_armed)
            window.buttons["stop"].click()
            until(lambda: app.snapshot().state is ApplicationState.READY)
            assert app.snapshot().counters.cycles_completed == 5
            assert app.snapshot().last_issue is None
            assert all(e.closed for e in engines) and all(b.closed for b in backends)
        finally:
            peer.release.set()
            for engine in engines:
                engine.release_active.set()
            window.close()
            until(lambda: window.bridge.closed)
            assert app.join(WAIT)
            delivery.detach()
    assert not any(
        t.name.startswith(
            (
                "EarlyCycleOwner-",
                "EarlyCameraInspector-",
                "VimoInferenceWorker-",
                "LocalProductionSource-",
                "FirstStationReporting-",
            )
        )
        for t in threads()
        if t.ident not in original_threads
    )


def test_basler_partial_step_waits_for_late_native_return_before_paused(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backend = PassiveBackend()
    engine = Engine()
    engine.release_warm.set()
    engine.release_active.clear()

    def sdk(*, continuous: bool = False) -> PassiveBackend:
        assert continuous
        return backend

    monkeypatch.setattr(_basler_sdk, "PylonBackend", sdk)
    startup = load_startup(data, engine_factory=lambda _: engine)
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.show()
    app = startup.application

    def state() -> ReplayState:
        value = window.bridge.control_snapshot()
        assert isinstance(value, BaslerControlSnapshot)
        return value.state

    try:
        window.buttons["operation"].click()
        until(backend.armed.is_set)
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(lambda: state() is ReplayState.PAUSED)
        window.bridge.refresh()
        window.buttons["pause"].click()
        until(
            lambda: (
                (value := window.bridge.control_snapshot()) is not None
                and isinstance(value, BaslerControlSnapshot)
                and value.admission_armed
            )
        )
        backend.expose(1, (0,))
        assert engine.active.wait(WAIT)
        until(lambda: app.snapshot().counters.cycles_completed == 1)
        result = app.snapshot().latest_production_result
        assert result is not None and len(result.channels) == 36
        assert result.cameras[0].frame_id is not None
        assert all(c.frame_id is None for c in result.cameras[1:])
        assert (
            state() is ReplayState.STEPPING
        )  # A deadline result is not native return.
        engine.release_active.set()
        until(lambda: state() is ReplayState.PAUSED)
        assert len(engine.calls) == 3 and app.snapshot().counters.cycles_started == 1
        window.bridge.refresh()
        window.buttons["stop"].click()
        until(lambda: app.snapshot().state is ApplicationState.READY)
        assert (
            app.snapshot().counters.cycles_completed == 1
            and engine.closed
            and backend.closed
        )
    finally:
        engine.release_active.set()
        window.close()
        until(lambda: window.bridge.closed)
        assert app.join(WAIT)
        delivery.detach()
