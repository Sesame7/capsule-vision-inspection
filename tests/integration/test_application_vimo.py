"""Opt-in application startup/warm-up/stop with the already verified local SDK."""

from dataclasses import replace
from pathlib import Path
from threading import enumerate as threads
from threading import get_ident

import pytest
from tests.integration.test_reference import unavailable
from tests.integration.test_worker_vimo import (
    MODEL,
    TIMEOUT,
    RecordedEngine,
    reference_image,
)
from tests.unit.test_production_application import prepared

from vimo_demo.application import (
    ApplicationError,
    ApplicationState,
    ProductionApplication,
    ProductionCounters,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.inference import VimoInitializationError


@pytest.mark.gpu
def test_real_application_warmup_and_cleanup(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    reference, _, image = reference_image(pytestconfig)
    synthetic = prepared(tmp_path).configuration
    configuration = replace(
        synthetic,
        model=replace(
            synthetic.model,
            model_path=str(MODEL),
            expected_sha256=reference["model_sha256"],
        ),
    )
    candidate = prepare_configuration(configuration, base_directory=MODEL.parent)
    trace: list[tuple[str, int]] = []
    session = ProductionRuntimeSession(
        candidate,
        1,
        warmup=lambda _: (image,),
        engine_factory=lambda _: RecordedEngine(trace),
    )
    app = ProductionApplication(lambda _prepared, _generation: session)
    try:
        app.configure(candidate)
        try:
            app.start().result(TIMEOUT)
        except ApplicationError as exc:
            cause = exc.__cause__
            if isinstance(cause, VimoInitializationError):
                vendor = cause.__cause__
                if isinstance(vendor, (ImportError, OSError)) or (
                    type(vendor).__name__ == "VimoException"
                    and getattr(vendor, "code", None) == 40000
                ):
                    unavailable(
                        pytestconfig, f"ViMo runtime/license unavailable: {cause}"
                    )
            raise
        snapshot = app.snapshot()
        assert snapshot.state is ApplicationState.RUNNING
        assert snapshot.counters == ProductionCounters()
        assert snapshot.latest_production_result is None
        assert [name for name, _ in trace] == ["construct", "infer", "infer"]
        app.stop().result(TIMEOUT)
        assert app.snapshot().state is ApplicationState.READY
    finally:
        app.close().result(TIMEOUT)
        assert app.join(TIMEOUT)
    assert [name for name, _ in trace] == ["construct", "infer", "infer", "close"]
    assert len({ident for _, ident in trace}) == 1 and trace[0][1] != get_ident()
    assert not ({app.thread_name, *session.thread_names} & {t.name for t in threads()})
    print("Application GPU: 2 warm-ups, zero production counters, owner-thread cleanup")
