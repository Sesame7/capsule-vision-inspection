"""Bounded preparation ownership with real worker threads and fake engines."""

from concurrent.futures import Future, TimeoutError
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from threading import Barrier, Event, Thread, get_ident
from threading import enumerate as threads

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.application_support import WAIT, FakeSession, close, prepared
from tests.cycle_support import image_result

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    ModelPreparationRequest,
    ModelPreparationResult,
    ModelPreparationState,
    ProductionApplication,
)
from vimo_demo.application import model_preparation as implementation
from vimo_demo.inference import ImageResult, VimoInitializationError


class Engine:
    def __init__(self) -> None:
        self.entered, self.release = Event(), Event()
        self.close_entered, self.close_release = Event(), Event()
        self.release.set()
        self.close_release.set()
        self.trace: list[tuple[str, int]] = []
        self.inputs: list[NDArray[np.generic]] = []
        self.error: Exception | None = None
        self.close_error: Exception | None = None

    def factory(self, _request: ModelPreparationRequest) -> Engine:
        self.trace.append(("create", get_ident()))
        self.entered.set()
        assert self.release.wait(WAIT)
        if self.error is not None and not _request.sample_shape:
            raise self.error
        return self

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self.trace.append(("infer", get_ident()))
        self.inputs.append(image_bgr)
        if self.error:
            raise self.error
        return image_result(np.zeros(image_bgr.shape, dtype=np.uint8))

    def close(self) -> None:
        self.trace.append(("close", get_ident()))
        self.close_entered.set()
        assert self.close_release.wait(WAIT)
        if self.close_error:
            raise self.close_error


def request(tmp_path: Path, *, sample: bool = False) -> ModelPreparationRequest:
    config = prepared(tmp_path)
    return ModelPreparationRequest(
        config.configuration.model,
        config.model_path,
        sample_shape=(2, 3, 3) if sample else None,
    )


@pytest.mark.parametrize("sample", [False, True])
def test_offline_initialization_or_two_samples_have_no_production_side_effects(
    tmp_path: Path, sample: bool
) -> None:
    engine = Engine()
    session_calls: list[int] = []

    def session_factory(_p: object, _g: int) -> FakeSession:
        session_calls.append(_g)
        raise AssertionError(
            "Preparation must not construct a production/source/report session"
        )

    app = ProductionApplication(session_factory, preparation_factory=engine.factory)
    req = request(tmp_path, sample=sample)
    before = app.snapshot()
    try:
        future = app.prepare_model_cache(req)
        assert not future.cancel()
        result = future.result(WAIT)
        assert result == ModelPreparationResult(req, True, 2 if sample else 0, True)
        assert app.snapshot() == before
        assert not session_calls
        assert app.preparation_snapshot().state is ModelPreparationState.SUCCEEDED
        assert not app.preparation_snapshot().owns_resources
        assert len({owner for _, owner in engine.trace}) == 1
        assert engine.trace[0][1] != get_ident()
        assert [event for event, _ in engine.trace] == (
            ["create", "infer", "infer", "close"] if sample else ["create", "close"]
        )
        for image in engine.inputs:
            assert image.shape == (2, 3, 3) and image.dtype == np.uint8
            assert not image.flags.writeable and not image.any()
        with pytest.raises(FrozenInstanceError):
            req.__setattr__("device_id", 1)
    finally:
        close(app)
    assert not any(t.name == app.thread_name for t in threads())


@pytest.mark.parametrize("sample", [False, True])
def test_initialization_and_sample_failures_preserve_active_configuration(
    tmp_path: Path, sample: bool
) -> None:
    engine = Engine()
    engine.error = ValueError("sample failed" if sample else "init failed")
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    config = prepared(tmp_path)
    try:
        app.configure(config)
        app.start().result(WAIT)
        app.stop().result(WAIT)
        before = app.snapshot()
        with pytest.raises(ApplicationError, match="failed") as failure:
            app.prepare_model_cache(request(tmp_path, sample=sample)).result(WAIT)
        assert failure.value.__cause__ is engine.error
        assert app.snapshot() == before
        assert app.preparation_snapshot().state is ModelPreparationState.FAILED
        result = app.preparation_snapshot().result
        assert result is not None and result.cleanup_complete
        engine.error = None
        assert app.prepare_model_cache(request(tmp_path)).result(WAIT).initialized
        app.start().result(WAIT)  # Preparation did not replace the Production path.
    finally:
        close(app)


def test_long_native_initialization_and_close_keep_ownership_observable(
    tmp_path: Path,
) -> None:
    engine = Engine()
    engine.release.clear()
    engine.close_release.clear()
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    app.configure(prepared(tmp_path))
    req = request(tmp_path)
    try:
        future = app.prepare_model_cache(req)
        assert engine.entered.wait(WAIT)
        assert app.preparation_snapshot().state is ModelPreparationState.PREPARING
        with pytest.raises(TimeoutError):
            future.result(0)
        with pytest.raises(ApplicationStateError):
            app.prepare_model_cache(req)
        with pytest.raises(ApplicationStateError):
            app.start()
        closed = app.close()
        assert app.close() is closed and not closed.done()
        assert not app.join(0)
        assert app.preparation_snapshot().owns_resources
        with pytest.raises(ApplicationStateError):
            app.prepare_model_cache(req)
        engine.release.set()
        assert engine.close_entered.wait(WAIT)
        assert not closed.done() and app.preparation_snapshot().owns_resources
        engine.close_release.set()
        closed.result(WAIT)
        with pytest.raises(ApplicationError):
            future.result(WAIT)  # Close interrupted readiness; no fabricated success.
        assert app.join(WAIT) and app.snapshot().state is ApplicationState.CLOSED
    finally:
        engine.release.set()
        engine.close_release.set()
        close(app)


def test_close_before_worker_start_never_creates_an_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = implementation.ModelPreparation.run

    def delayed(
        owner: implementation.ModelPreparation,
    ) -> tuple[ModelPreparationResult, BaseException | None]:
        entered.set()
        assert release.wait(WAIT)
        return original(owner)

    monkeypatch.setattr(implementation.ModelPreparation, "run", delayed)
    engine = Engine()
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    try:
        future = app.prepare_model_cache(request(tmp_path))
        assert entered.wait(WAIT)
        closed = app.close()
        release.set()
        closed.result(WAIT)
        with pytest.raises(ApplicationError):
            future.result(WAIT)
        assert not engine.trace
    finally:
        release.set()
        close(app)


def test_success_is_not_delivered_until_actual_cleanup_and_join(tmp_path: Path) -> None:
    engine = Engine()
    engine.close_release.clear()
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    app.configure(prepared(tmp_path))
    try:
        future = app.prepare_model_cache(request(tmp_path))
        assert engine.close_entered.wait(WAIT)
        assert not future.done()
        assert app.preparation_snapshot().owns_resources
        with pytest.raises(ApplicationStateError):
            app.start()
        engine.close_release.set()
        assert future.result(WAIT).cleanup_complete
        assert not app.preparation_snapshot().owns_resources
    finally:
        engine.close_release.set()
        close(app)


@pytest.mark.parametrize("stage", ["starting", "running", "stopping"])
def test_preparation_rejected_through_entire_production_lifetime(
    tmp_path: Path, stage: str
) -> None:
    session = FakeSession(automatic_start=False)
    session.join_gate = Event()
    engine = Engine()
    app = ProductionApplication(
        lambda _p, _g: session, preparation_factory=engine.factory
    )
    try:
        app.configure(prepared(tmp_path))
        started = app.start()
        assert session.start_entered.wait(WAIT)
        if stage != "starting":
            session.ready.set_result(None)
            started.result(WAIT)
        if stage == "stopping":
            app.stop()
            assert session.join_entered.wait(WAIT)
        with pytest.raises(ApplicationStateError):
            app.prepare_model_cache(request(tmp_path))
        assert not engine.trace
    finally:
        if not session.ready.done():
            session.ready.set_result(None)
        session.join_gate.set()
        close(app)


def test_start_and_preparation_race_admits_only_one_owner(tmp_path: Path) -> None:
    engine = Engine()
    engine.release.clear()
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    app.configure(prepared(tmp_path))
    req = request(tmp_path)
    barrier = Barrier(3)
    accepted: list[Future[None] | Future[ModelPreparationResult]] = []
    rejected: list[BaseException] = []

    def attempt(prepare: bool) -> None:
        barrier.wait(WAIT)
        try:
            accepted.append(app.prepare_model_cache(req) if prepare else app.start())
        except ApplicationStateError as error:
            rejected.append(error)

    callers = [Thread(target=attempt, args=(prepare,)) for prepare in (False, True)]
    try:
        for caller in callers:
            caller.start()
        barrier.wait(WAIT)
        for caller in callers:
            caller.join(WAIT)
            assert not caller.is_alive()
        assert len(accepted) == len(rejected) == 1
        engine.release.set()
        accepted[0].result(WAIT)
    finally:
        engine.release.set()
        close(app)


def test_sha_is_rechecked_before_any_engine_is_created(tmp_path: Path) -> None:
    engine = Engine()
    req = request(tmp_path)
    req.model_path.write_bytes(b"changed after selection")
    app = ProductionApplication(
        lambda _p, _g: FakeSession(), preparation_factory=engine.factory
    )
    try:
        with pytest.raises(ApplicationError, match="SHA-256"):
            app.prepare_model_cache(req).result(WAIT)
        assert not engine.trace and not app.preparation_snapshot().owns_resources
    finally:
        close(app)


def test_actual_worker_close_failure_is_not_clean_termination(tmp_path: Path) -> None:
    engine = Engine()
    engine.close_error = ValueError("native close failed")
    owner = implementation.ModelPreparation(request(tmp_path), engine.factory)
    result, error = owner.run()
    assert error is engine.close_error and result.initialized
    assert not result.cleanup_complete and not owner.settle()
    assert not owner.worker.is_alive
    assert (
        owner.engine is not None
    )  # Retain the uncertain SDK owner, even after thread exit.


def test_incomplete_cleanup_blocks_start_reset_and_successful_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = Event()

    def incomplete(
        owner: implementation.ModelPreparation,
    ) -> tuple[ModelPreparationResult, BaseException | None]:
        return ModelPreparationResult(owner.request, False, 0, False), ValueError(
            "still owned"
        )

    monkeypatch.setattr(implementation.ModelPreparation, "run", incomplete)

    def settle(_owner: implementation.ModelPreparation) -> bool:
        return released.is_set()

    monkeypatch.setattr(implementation.ModelPreparation, "settle", settle)
    app = ProductionApplication(lambda _p, _g: FakeSession())
    try:
        app.configure(prepared(tmp_path))
        with pytest.raises(ApplicationError, match="still owned"):
            app.prepare_model_cache(request(tmp_path)).result(WAIT)
        assert app.snapshot().state is ApplicationState.FAULT
        assert app.preparation_snapshot().owns_resources
        with pytest.raises(ApplicationStateError):
            app.reset_fault()
        with pytest.raises(ApplicationStateError):
            app.start()
        with pytest.raises(ApplicationError, match="incomplete"):
            app.close().result(WAIT)
        assert not app.join(0) and app.preparation_snapshot().owns_resources
    finally:
        released.set()  # Explicitly release only this fake resource before test cleanup.
        close(app)


def test_constructor_cleanup_failure_note_is_preserved(tmp_path: Path) -> None:
    original = ValueError("native initialization failed")
    original.add_note("Cleanup also failed: uncertain native resource")

    def broken(_request: ModelPreparationRequest) -> Engine:
        raise VimoInitializationError("initialization") from original

    owner = implementation.ModelPreparation(request(tmp_path), broken)
    result, error = owner.run()
    assert isinstance(error, VimoInitializationError)
    assert not result.initialized and not result.cleanup_complete
    assert not owner.worker.is_alive


def test_real_factory_preserves_production_default_staging_without_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int, str | None]] = []
    engine = Engine()

    def make(path: Path, *, device_id: int = 0, module_id: str | None = None) -> Engine:
        calls.append((path, device_id, module_id))
        return engine

    monkeypatch.setattr(implementation, "VimoSegmentationEngine", make)
    req = request(tmp_path)
    assert implementation.real_preparation_engine(req) is engine
    assert calls == [(req.model_path, 0, req.model.module_id)]
    # No alternate cache_dir, SDK settings, source input or image inference supplied.


@pytest.mark.parametrize("shape", [(0, 3, 3), (2, 3, 1), (2, -1, 3)])
def test_invalid_sample_shapes_are_rejected(
    tmp_path: Path, shape: tuple[int, int, int]
) -> None:
    with pytest.raises(ValueError, match="sample_shape"):
        replace(request(tmp_path), sample_shape=shape)
