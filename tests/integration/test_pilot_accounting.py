"""Application and Qt pilot accounting; isolated files, fake SDK, local TCP only."""

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_basler_production import (
    SERIALS,
    Engine,
    PassiveBackend,
    save_basler,
)
from tests.support.application import FakeSession
from tests.support.business import mixed_result
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import screen as screen
from tests.support.qt import until
from tests.support.startup import authenticated_startup, close, saved_data

from capsule_vision.application import ApplicationError, ProductionCounters, _basler_sdk
from capsule_vision.application.models import PilotCounters, PreparedConfiguration
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import FinsTcpEndpoint
from capsule_vision.communication.reporting import (
    CompletedReportingBatch,
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.main_window import MainWindow


@pytest.mark.parametrize("cameras", [5, 6])
def test_retention_reset_stale_completion_and_separate_mode_units(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cameras: int
) -> None:
    backend = PassiveBackend()
    backend.serials = SERIALS[:cameras]
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path, camera_count=cameras)
    save_basler(data.sources)
    fail_warmup = False

    def engine_factory(_: PreparedConfiguration) -> Engine:
        engine = Engine()
        engine.release_warm.set()
        engine.fail_warmup = fail_warmup
        return engine

    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=engine_factory)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        events: list[CompletedReportingBatch] = []
        observe = app.observe_pilot_batch

        def capture(batch: CompletedReportingBatch) -> None:
            events.append(batch)
            observe(batch)

        monkeypatch.setattr(app, "observe_pilot_batch", capture)
        settings = ReportingSettings(
            endpoint=FinsTcpEndpoint(port=peer.port), mode=ReportingMode.FOUR_CYCLE
        )
        ordinal = 0

        def expose(count: int, *, pilot: bool) -> None:
            nonlocal ordinal
            raw = app.snapshot().counters.cycles_completed
            for index in range(count):
                ordinal += 1
                backend.expose(ordinal, tuple(range(cameras)))
                until(
                    lambda index=index: (
                        app.snapshot().counters.cycles_completed == raw + index + 1
                    )
                )
                if pilot and index == 0:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.batch_observations
                            and s.batch_observations[-1].reads
                        )
                    )

        try:
            app.set_reporting_selection(ReportingSelection(True, settings))
            app.connect_source()
            app.start().result(3)
            expose(3, pilot=True)
            assert app.snapshot().pilot_counters == PilotCounters()
            app.stop().result(3)
            assert not events  # Partial batches never resume in the next session.
            app.start().result(3)
            expose(4, pilot=True)
            until(lambda: app.snapshot().pilot_counters.batches_completed == 1)
            counts = app.snapshot().pilot_counters
            assert counts.channel_empty + counts.channel_ok == cameras * 6
            assert counts.channel_unknown == counts.channel_ng == 0
            assert app.snapshot().counters.cycles_completed == 7
            observe(events[0])  # Duplicate current-generation event.
            assert app.snapshot().pilot_counters == counts
            app.stop().result(3)
            observe(events[0])  # Stopped owner cannot publish again.
            assert app.snapshot().pilot_counters == counts
            assert app.history is not None
            records = app.history.query().result(3).records
            assert len(records) == 7 and all(
                len(row.channels) == cameras * 6 for row in records
            )

            prepared = app.snapshot().candidate_prepared
            assert prepared is not None
            app.configure(
                replace(
                    prepared,
                    configuration=replace(
                        prepared.configuration,
                        recipe_id="second",
                        display_name="Second",
                    ),
                )
            )
            assert app.snapshot().pilot_counters == counts
            app.set_reporting_selection(
                ReportingSelection(
                    True, replace(settings, mode=ReportingMode.PER_CYCLE)
                )
            )
            assert (
                app.snapshot().pilot_counting
            )  # Draft settings cannot relabel retained facts.
            fail_warmup = True
            with pytest.raises(ApplicationError):
                app.start().result(3)
            assert (
                app.snapshot().pilot_counting
                and app.snapshot().pilot_counters == counts
            )
            fail_warmup = False
            app.start().result(3)
            until(lambda: not app.snapshot().pilot_counting)
            expose(1, pilot=False)
            observe(events[0])
            assert app.snapshot().pilot_counters == counts
            app.stop().result(3)

            app.set_reporting_selection(
                ReportingSelection(
                    True, replace(settings, mode=ReportingMode.FOUR_CYCLE)
                )
            )
            peer.set_words(48, (12,))
            peer.fail_next("sequence", "drop_after")
            app.start().result(3)
            observe(events[0])  # Stale generation in a new pilot session.
            assert app.snapshot().pilot_counters == counts
            expose(4, pilot=True)
            until(lambda: bool((s := app.reporting_snapshot()) and s.failed_count))
            assert app.snapshot().pilot_counters.batches_completed == 2
            assert app.snapshot().counters.cycles_completed == 12
            app.stop().result(3)

            app.start_batch("New operator batch")
            assert app.snapshot().pilot_counters == PilotCounters()
            assert app.snapshot().counters == ProductionCounters()
            observe(events[-1])
            assert app.snapshot().pilot_counters == PilotCounters()
            assert len(app.history.query().result(3).records) == 12
            app.set_reporting_selection(ReportingSelection(False, settings))
            app.start().result(3)
            until(lambda: not app.snapshot().pilot_counting)
            expose(1, pilot=False)
            assert app.snapshot().counters.cycles_completed == 1
            assert app.snapshot().pilot_counters == PilotCounters()
            app.stop().result(3)
        finally:
            close(startup)


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080)])
def test_pilot_metrics_keep_raw_duration_and_all_language_accessibility(
    screen: tuple[MainWindow, FakeSession], size: tuple[int, int]
) -> None:
    window, _ = screen
    window.resize(*size)
    raw = ProductionCounters(
        cycles_completed=8,
        business_ok=24,
        business_ng=96,
        business_empty=48,
        business_unknown=120,
    )
    snapshot = replace(
        window.snapshot,
        pilot_counting=True,
        pilot_counters=PilotCounters(2, 6, 24, 12, 30),
        counters=raw,
        latest_production_result=mixed_result(),
    )
    window.bridge.snapshot_changed.emit(snapshot)
    for language in LANGUAGES:
        window.set_language(language)
        QApplication.processEvents()
        assert len(window.metrics) == 5
        assert window.metrics["cycles"][0].text() == "2"
        assert window.metrics["cycles"][1].text() == text("pilot_batches", language)
        assert (
            text("pilot_raw_cycles", language).format(count="8")
            in window.metrics["cycles"][0].toolTip()
        )
        assert window.metrics["channel_ok"][0].text() == "6"
        assert window.metrics["channel_ng"][0].text() == "24"
        assert window.metrics["channel_empty"][0].text() == "12"
        unknown = text("pilot_unknown", language).format(count="30")
        assert unknown in window.metrics["channel_ng"][1].text()
        assert unknown in window.metrics["channel_ng"][0].accessibleName()
        assert window.metrics["processing"][0].text() == "25 ms"
        for value, caption in window.metrics.values():
            assert (
                value.isVisible()
                and value.accessibleName()
                and caption.accessibleName()
            )
            assert (
                caption.fontMetrics().horizontalAdvance(caption.text())
                <= caption.width()
            )
        assert window.size().toTuple() == size
    window.bridge.snapshot_changed.emit(replace(snapshot, pilot_counting=False))
    assert window.metrics["cycles"][0].text() == "8"
    assert window.metrics["channel_ok"][0].text() == "24"
    assert window.metrics["channel_ng"][1].text() == "NG"
