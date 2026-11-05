"""Normal composition, fake Basler SDK, real local TCP; no operational assets."""

import json
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import perf_counter

import pytest
from PySide6.QtWidgets import QApplication
from tests.integration.test_basler_production import (
    SERIALS,
    Engine,
    PassiveBackend,
    save_basler,
)
from tests.support.fins_tcp import TcpPeer
from tests.support.qt import close_window, until
from tests.support.startup import (
    TEST_PASSWORD,
    align_four_cycle,
    authenticated_startup,
    close,
    saved_data,
)

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    _basler_sdk,
)
from capsule_vision.application.identity_models import AuthorizationError, Role
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.communication import (
    FinsMemoryArea,
    FinsTcpEndpoint,
    FinsUdpEndpoint,
)
from capsule_vision.communication.batch_reporting import FourCycleReportingWorker
from capsule_vision.communication.operations import CommunicationRequest, RequestKind
from capsule_vision.communication.reporting import (
    ReportingMode,
    ReportingSelection,
    ReportingSettings,
    ReportingWriteProtectedError,
)
from capsule_vision.configuration import ImageArchiveSettings, load_recipe, save_recipe
from capsule_vision.configuration.communication_preferences import (
    CommunicationPreferences,
    load_communication_preferences,
    save_communication_preferences,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES
from capsule_vision.ui.bootstrap import create_window
from capsule_vision.ui.future_delivery import FutureDelivery


@pytest.mark.parametrize("before_start", [0, 1, 2, 3])
def test_running_trigger_stream_synchronizes_without_reassigning_startup_cycles(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_start: int,
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    archive = tmp_path / "archive"
    path = data.recipes / "local.json"
    save_recipe(
        path,
        replace(
            load_recipe(path),
            image_archive=ImageArchiveSettings(str(archive), True, True, True, True),
        ),
    )
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    engine = Engine()
    engine.release_warm.set()
    base = 149 if before_start % 2 else 49
    with TcpPeer(base_address=base) as peer:
        startup = authenticated_startup(
            data, engine_factory=lambda _: engine, select_basler_batch=False
        )
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        FinsTcpEndpoint(port=peer.port),
                        base,
                        mode=ReportingMode.FOUR_CYCLE,
                    ),
                )
            )
            app.connect_source()
            peer.set_words(base - 1, (65535,))
            for ordinal in range(1, before_start + 1):
                # Start during a partial SDK ordinal in one case. These peers
                # belong to the idle binding, even if they arrive after Start.
                partial = before_start == ordinal == 2
                backend.expose(ordinal, (0,) if partial else tuple(range(6)))
                received = (ordinal - 1) * 6 + (1 if partial else 6)
                until(
                    lambda received=received: (
                        app.source_snapshot().discarded_frames == received
                    )
                )
            app.start().result(3)
            until(
                lambda: bool(
                    (s := app.reporting_snapshot()) and s.initial_counter == 65535
                )
            )
            if before_start == 2:
                partial_control = app.basler_control_for(app.snapshot().generation)
                assert partial_control is not None
                # Deliberately present an unsafe transition with five peers
                # still missing. A second read proves the SDK rejected it.
                peer.set_words(base - 1, (23,))
                until(
                    lambda: bool(
                        (s := app.reporting_snapshot())
                        and s.startup_read
                        and s.startup_read.value == 23
                    )
                )
                after = perf_counter()
                until(
                    lambda: bool(
                        (s := app.reporting_snapshot())
                        and s.waiting_for_sync
                        and s.startup_read
                        and s.startup_read.started_at >= after
                    )
                )
                backend.expose(2, (1, 2, 3, 4, 5))
                until(
                    lambda: (
                        app.source_snapshot().discarded_frames == 12
                        and partial_control.snapshot().boundary_ready
                    )
                )
            skipped = 4 - before_start if before_start else 0

            def expose_and_archive(ordinal: int, completed: int) -> None:
                backend.expose(ordinal)
                until(lambda: app.snapshot().counters.cycles_completed == completed)
                # Pace the fixture's inputs to the bounded PNG writer. The
                # actual inspection/reporting paths never wait on this writer.
                until(
                    lambda: bool(
                        (a := app.archive_snapshot()) and a.written == completed * 6
                    )
                )

            for i in range(1, skipped + 1):
                expose_and_archive(before_start + i, i)
            snapshot = app.reporting_snapshot()
            assert (
                snapshot
                and snapshot.waiting_for_sync
                and not snapshot.batch_observations
            )
            assert not snapshot.group_progress and not startup.frames.snapshot().frames
            control = app.basler_control_for(app.snapshot().generation)
            assert control is not None
            until(lambda: control.snapshot().pending_cycles == 0)
            assert app.snapshot().pilot_counters.batches_completed == 0
            assert (
                app.history is not None
                and len(app.history.query().result(3).records) == skipped
            )
            assert all(p[10:12] == b"\x01\x01" for p in peer.packets)

            align_four_cycle(app, peer, 0)
            first = before_start + skipped + 1
            for i in range(4):
                expose_and_archive(first + i, skipped + i + 1)
                if i == 0:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.batch_observations
                            and s.batch_observations[0].reads
                        )
                    )
            until(
                lambda: bool(
                    (s := app.reporting_snapshot()) and s.acknowledged_count == 1
                )
            )
            app.stop().result(3)
            final = app.reporting_snapshot()
            assert (
                final and not final.phase_issue and len(final.batch_observations) == 1
            )
            assert final.batch_observations[0].cycles == tuple(range(first, first + 4))
            assert final.last_acknowledged and final.last_acknowledged.sequence == 0
            assert final.initial_counter == 65535
            assert app.snapshot().pilot_counters.batches_completed == 1
            assert [
                int.from_bytes(p[13:15])
                for p in peer.packets
                if p[10:12] == b"\x01\x02"
            ] == [base + 1, base]
            frames = startup.frames.snapshot().frames
            assert len(frames) == 6 and all(
                f.frame_id.cycle_id == first + 3 for f in frames
            )
            rows = app.history.query().result(3).records
            assert [(r.cycle_id, r.group_slot) for r in reversed(rows)] == [
                (c, c - first + 1 if c >= first else None)
                for c in range(before_start + 1, first + 4)
            ]
            images = list(archive.rglob("*.png"))
            assert len(images) == (skipped + 4) * 6
            assert {int(p.stem.split("_")[-1]) for p in images} == {
                r.cycle_id for r in rows
            }
        finally:
            close(startup)


@pytest.mark.parametrize("ending", ["stop", "close", "plc_error"])
def test_startup_read_shutdown_and_failure_leave_no_pending_reporting(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    data = saved_data(tmp_path)
    save_basler(data.sources)
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    engine = Engine()
    engine.release_warm.set()
    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        FinsTcpEndpoint(port=peer.port, timeout=2),
                        mode=ReportingMode.FOUR_CYCLE,
                    ),
                )
            )
            peer.hold_next("read")
            if ending == "plc_error":
                peer.fail_next("read", "plc")
            app.start().result(3)
            assert peer.entered.wait(3)
            if ending == "plc_error":
                peer.release.set()
                until(
                    lambda: bool(
                        (s := app.reporting_snapshot())
                        and s.failure_issue
                        and s.phase_issue
                    )
                )
                backend.expose(1)
                until(lambda: app.snapshot().counters.cycles_completed == 1)
                assert app.snapshot().state is ApplicationState.RUNNING
                assert app.snapshot().pilot_counters.batches_completed == 0
                app.stop().result(3)
            else:
                stopped = app.stop() if ending == "stop" else app.close()
                assert not stopped.done()
                peer.release.set()
                stopped.result(3)
            report = app.reporting_snapshot()
            assert report and not report.thread_alive
            assert bool(report.phase_issue) == (ending == "plc_error")
            assert not report.waiting_for_sync and not report.batch_observations
            assert len(peer.packets) == 1 and peer.packets[0][10:12] == b"\x01\x01"
        finally:
            peer.release.set()
            close(startup)


@pytest.mark.parametrize("mode", ["w48_observe", "w48_active"])
def test_obsolete_saved_mode_is_visible_and_preserved_on_normal_startup(
    tmp_path: Path, mode: str
) -> None:
    data = saved_data(tmp_path)
    with TcpPeer() as peer:
        data.communication.write_text(
            json.dumps(
                {
                    "reporting": {
                        "protocol": "fins_tcp",
                        "mode": mode,
                        "enabled": True,
                        "port": peer.port,
                        "base_address": 149,
                    }
                }
            ),
            encoding="utf-8",
        )
        original = data.communication.read_bytes()
        startup = authenticated_startup(data)
        assert isinstance(startup.application, SourceProductionApplication)
        try:
            assert mode in startup.communication_error
            assert not startup.communication.reporting.enabled
            assert startup.application.reporting_snapshot() is None
            assert peer.connections == 0 and not peer.packets
            assert data.communication.read_bytes() == original
        finally:
            close(startup)


@pytest.mark.parametrize("base", [49, 149])
@pytest.mark.parametrize("cameras", [5, 6])
@pytest.mark.parametrize("ending", ["stop", "close"])
def test_selected_register_range_protected_until_reporting_owner_exits(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: int,
    cameras: int,
    ending: str,
) -> None:
    backend = PassiveBackend()
    backend.serials = SERIALS[:cameras]
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path, camera_count=cameras)
    save_basler(data.sources)
    engine = Engine()
    with TcpPeer(base_address=base) as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        endpoint = FinsTcpEndpoint(port=peer.port, timeout=2)
        selection = ReportingSelection(
            True,
            ReportingSettings(
                endpoint,
                base,
                mode=ReportingMode.FOUR_CYCLE,
            ),
        )

        def write(address: int, count: int = 1) -> CommunicationRequest:
            request = CommunicationRequest(
                kind=RequestKind.WRITE,
                start=address,
                count=count,
                values=(7,) * count,
                area=FinsMemoryArea.W,
                endpoint=endpoint,
            )
            request.validate()
            return request

        def protected() -> None:
            last = base + (2 if cameras == 5 else 3)
            for address in range(base - 1, last + 1):
                with pytest.raises(ReportingWriteProtectedError):
                    app.validate_manual_request(write(address))
            with pytest.raises(ReportingWriteProtectedError):
                app.validate_manual_request(write(base - 2, 2))
            with pytest.raises(ReportingWriteProtectedError):
                app.validate_manual_request(
                    replace(write(base - 1), endpoint=FinsUdpEndpoint(port=peer.port))
                )
            for address in (base - 2, last + 1):
                app.validate_manual_request(write(address))
            other = 149 if base == 49 else 49
            app.validate_manual_request(write(other - 1, 5))
            app.validate_manual_request(replace(write(base), area=FinsMemoryArea.DM))
            app.validate_manual_request(
                replace(write(base), endpoint=replace(endpoint, port=peer.port + 1))
            )
            app.validate_manual_request(
                CommunicationRequest(
                    kind=RequestKind.READ,
                    start=base - 1,
                    count=1,
                    area=FinsMemoryArea.W,
                    endpoint=endpoint,
                )
            )

        try:
            app.set_reporting_selection(selection)
            started = app.start()
            assert engine.warm.wait(3)
            assert app.snapshot().state is ApplicationState.STARTING
            protected()
            assert not peer.packets and peer.connections == 0
            engine.release_warm.set()
            started.result(3)
            align_four_cycle(app, peer, 73)
            peer.hold_next("sequence")
            for cycle in range(1, 5):
                backend.expose(cycle, tuple(range(cameras)))
                until(
                    lambda cycle=cycle: (
                        app.snapshot().counters.cycles_completed == cycle
                    )
                )
                if cycle == 1:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.batch_observations
                            and s.batch_observations[0].reads
                        )
                    )
            assert peer.entered.wait(3)
            settled = app.stop() if ending == "stop" else app.close()
            assert not settled.done()
            if ending == "stop":
                protected()
            else:
                # Close rejects all new commands before the range guard. The
                # frozen reporting owner still owns the unfinished commit.
                assert (
                    app.reporting_for_generation(app.snapshot().generation)
                    == selection.settings
                )
                pending = app.reporting_snapshot()
                assert pending is not None and pending.thread_alive
                for address in range(base - 1, base + (3 if cameras == 5 else 4)):
                    with pytest.raises(
                        ApplicationStateError, match="Command unavailable"
                    ):
                        app.validate_manual_request(write(address))
            peer.release.set()
            settled.result(3)
            observed = app.reporting_snapshot()
            assert observed is not None and not observed.thread_alive
            assert peer.words(base) == (73,)
            if ending == "stop":
                app.validate_manual_request(write(base - 1, cameras // 6 + 4))
        finally:
            engine.release_warm.set()
            peer.release.set()
            close(startup)


@pytest.mark.parametrize("base", [49, 149])
def test_disabled_four_cycle_selection_has_no_plc_owner_or_group_attribution(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: int
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    with TcpPeer(base_address=base) as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    False,
                    ReportingSettings(
                        FinsTcpEndpoint(port=peer.port),
                        base,
                        mode=ReportingMode.FOUR_CYCLE,
                    ),
                )
            )
            app.start().result(3)
            for cycle in range(1, 5):
                backend.expose(cycle)
                until(
                    lambda cycle=cycle: (
                        app.snapshot().counters.cycles_completed == cycle
                    )
                )
            app.stop().result(3)
            assert app.reporting_snapshot() is None
            assert not peer.packets and peer.connections == 0
            assert not app.snapshot().pilot_counting
            assert app.snapshot().pilot_counters.batches_completed == 0
            assert app.snapshot().counters.cycles_completed == 4
        finally:
            close(startup)


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("base", [49, 149])
def test_normal_authorization_warmup_controls_and_stop(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    base: int,
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    with TcpPeer(base_address=base) as peer:
        selection = ReportingSelection(
            True,
            ReportingSettings(
                endpoint=FinsTcpEndpoint(port=peer.port),
                base_address=base,
                mode=ReportingMode.FOUR_CYCLE,
            ),
        )
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app, identity = startup.application, startup.application.identity
        assert isinstance(app, SourceProductionApplication) and identity is not None
        try:
            app.set_reporting_selection(selection)
            app.connect_source()
            if role is Role.GUEST:
                identity.logout()
            elif role is not Role.ADMINISTRATOR:
                identity.create_account("Actor", role, TEST_PASSWORD).result(3)
                identity.login("Actor", TEST_PASSWORD).result(3)
            if role < Role.ENGINEER:
                with pytest.raises(AuthorizationError):
                    app.persist_reporting_selection(
                        selection, lambda: pytest.fail("Unauthorized save")
                    )
            else:
                app.persist_reporting_selection(
                    selection,
                    lambda: save_communication_preferences(
                        data.communication,
                        CommunicationPreferences(reporting=selection),
                    ),
                )
            if role is Role.GUEST:
                with pytest.raises(AuthorizationError):
                    app.start()
                assert not engine.calls and not peer.packets
                return
            app.start().result(3)
            assert all(p[10:12] == b"\x01\x01" for p in peer.packets)
            for action in (app.pause_source, app.step_source, app.resume_source):
                with pytest.raises(ApplicationStateError, match="Four-cycle"):
                    action()
            if role >= Role.ENGINEER:
                for address in range(base - 1, base + 4):
                    with pytest.raises(ReportingWriteProtectedError):
                        app.validate_manual_request(
                            CommunicationRequest(
                                kind=RequestKind.WRITE,
                                start=address,
                                values=(7,),
                                area=FinsMemoryArea.W,
                                endpoint=selection.settings.endpoint,
                            )
                        )
            align_four_cycle(app, peer, 38)
            for cycle in range(1, 5):
                backend.expose(cycle)
                until(
                    lambda cycle=cycle: (
                        app.snapshot().counters.cycles_completed >= cycle
                    )
                )
                if cycle == 1:
                    until(
                        lambda: bool(
                            (s := app.reporting_snapshot())
                            and s.batch_observations
                            and s.batch_observations[0].reads
                        )
                    )
            until(lambda: bool((s := app.reporting_snapshot()) and s.last_outcome))
            snapshot = app.reporting_snapshot()
            assert snapshot is not None
            assert snapshot.acknowledged_count == 1
            counters = app.snapshot().counters
            assert counters.channel_ok == 144
            assert counters.business_ok + counters.business_empty == 144
            assert counters.business_ng == counters.business_unknown == 0
            physical = app.snapshot().pilot_counters
            assert app.snapshot().pilot_counting
            assert physical.batches_completed == 1
            assert physical.channel_ok + physical.channel_empty == 36
            assert physical.channel_ng == physical.channel_unknown == 0
            assert peer.words(base) == (38,)
            identity.logout()  # Stop/Close do not require a production role.
            app.stop().result(3)
            assert app.snapshot().state is ApplicationState.READY
            stopped = app.reporting_snapshot()
            assert stopped is not None and not stopped.thread_alive
            assert app.snapshot().pilot_counters == physical
        finally:
            close(startup)
        assert engine.closed and backend.closed


@pytest.mark.parametrize("base", [49, 149])
def test_folder_pilot_rejected_before_session_or_engine(
    tmp_path: Path, base: int
) -> None:
    data = saved_data(tmp_path)
    engine = Engine()
    startup = authenticated_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceProductionApplication)
    try:
        app.set_reporting_selection(
            ReportingSelection(
                True,
                ReportingSettings(
                    endpoint=FinsTcpEndpoint(),
                    base_address=base,
                    mode=ReportingMode.FOUR_CYCLE,
                ),
            )
        )
        before = app.snapshot()
        for command in (app.start, app.step_source):
            with pytest.raises(ApplicationStateError, match="requires Basler"):
                command()
        assert (
            app.snapshot() == before
            and not engine.calls
            and app.reporting_snapshot() is None
        )
    finally:
        close(startup)


def test_failed_warmup_never_reads_w48(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.fail_warmup = True
    engine.release_warm.set()
    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        endpoint=FinsTcpEndpoint(port=peer.port),
                        mode=ReportingMode.FOUR_CYCLE,
                    ),
                )
            )
            app.connect_source()
            active = app.snapshot().active
            with pytest.raises(ApplicationError):
                app.start().result(3)
            assert (
                app.snapshot().state is ApplicationState.READY
                and app.snapshot().active == active
            )
            assert peer.connections == 0 and not peer.packets
            assert not app.snapshot().pilot_counting
            assert app.snapshot().pilot_counters.batches_completed == 0
            snapshot = app.reporting_snapshot()
            assert snapshot is not None and not snapshot.thread_alive
        finally:
            close(startup)


@pytest.mark.parametrize("mode", list(ReportingMode))
def test_preferences_roundtrip_explicit_pilot(
    tmp_path: Path, mode: ReportingMode
) -> None:
    path = tmp_path / "communication.json"
    value = CommunicationPreferences(
        reporting=ReportingSelection(
            True, ReportingSettings(endpoint=FinsTcpEndpoint(), mode=mode)
        )
    )
    save_communication_preferences(path, value)
    assert load_communication_preferences(path) == value
    payload = json.loads(path.read_text())
    assert payload["reporting"].get("mode", "per_cycle") == mode.value


def test_four_cycle_ui_draft_save_localization_and_permission(
    qapp: QApplication, tmp_path: Path
) -> None:
    startup = authenticated_startup(saved_data(tmp_path))
    delivery = FutureDelivery()
    window = create_window(startup, delivery)
    window.resize(1080, 900)
    window.show()
    try:
        window.navigate(2)
        page = window.communication
        panel = page.reporting_panel
        assert panel is not None
        assert panel.mode_buttons[ReportingMode.FOUR_CYCLE].isChecked()
        assert tuple(panel.mode_buttons) == tuple(ReportingMode)
        assert panel.mode_group.exclusive()
        assert not panel.enabled.isChecked() and not hasattr(panel, "evidence")
        panel.mode_buttons[ReportingMode.PER_CYCLE].click()
        panel.base_address.setValue(120)
        panel.mode_buttons[ReportingMode.FOUR_CYCLE].click()
        assert panel.base_address.value() == 120  # No implicit address replacement.
        assert panel.base_choices.currentIndex() == -1
        with pytest.raises(ValueError, match="W49 or W149"):
            panel.selection()
        panel.base_choices.setCurrentIndex(1)
        panel.enabled.setChecked(True)
        assert panel.controller.reporting_selection().settings.base_address == 49
        assert not panel.controller.reporting_selection().enabled
        page.save_settings.click()
        until(
            lambda: (
                panel.controller.reporting_selection().enabled
                and panel.controller.reporting_selection().settings.base_address == 149
            )
        )
        assert (
            load_communication_preferences(startup.data.communication).reporting
            == panel.selection()
        )
        for language in LANGUAGES:
            window.set_language(language)
            qapp.processEvents()
            assert panel.mode_buttons[ReportingMode.FOUR_CYCLE].isChecked()
            assert panel.mode_control.accessibleName()
            for mode, button in panel.mode_buttons.items():
                assert button.accessibleName() == text(
                    "plc_mode_" + mode.value, language
                )
                assert button.accessibleName() in button.toolTip()
            assert panel.base_choices.currentText() == "W149"
            assert panel.base_field.rect().contains(panel.base_choices.geometry())
        identity = startup.application.identity
        assert identity is not None
        identity.logout()
        window.identity_controls.refresh()
        assert not panel.mode_control.isEnabled() and not panel.enabled.isEnabled()
        # A phase fence is an error even with no failed socket request.
        worker = FourCycleReportingWorker(1, panel.selection().settings)
        window.bridge.reporting_snapshot = replace(
            worker.snapshot(), phase_issue="W48 changed"
        )
        window.set_language("en")
        assert window.event_frame.plc.text() == f"PLC · {text('technical_error', 'en')}"
    finally:
        close_window(window)
        delivery.detach()


@pytest.mark.parametrize("hold_native", [False, True])
def test_four_trigger_burst_latency_and_unchanged_native_ownership(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hold_native: bool,
) -> None:
    backend = PassiveBackend()
    monkeypatch.setattr(_basler_sdk, "PylonBackend", lambda: backend)
    data = saved_data(tmp_path)
    save_basler(data.sources)
    engine = Engine()
    engine.release_warm.set()
    if hold_native:
        engine.release_active.clear()
    with TcpPeer() as peer:
        startup = authenticated_startup(data, engine_factory=lambda _: engine)
        app = startup.application
        assert isinstance(app, SourceProductionApplication)
        try:
            app.set_reporting_selection(
                ReportingSelection(
                    True,
                    ReportingSettings(
                        endpoint=FinsTcpEndpoint(port=peer.port),
                        mode=ReportingMode.FOUR_CYCLE,
                    ),
                )
            )
            app.connect_source()
            app.start().result(3)
            align_four_cycle(app, peer, 44)
            start = perf_counter()
            for index in range(4):
                Event().wait(max(0, start + index * 0.060 - perf_counter()))
                backend.expose(index + 1)
            until(lambda: bool((s := app.reporting_snapshot()) and s.last_outcome))
            snapshot = app.reporting_snapshot()
            assert snapshot is not None and snapshot.acknowledged_count == 1
            batch = snapshot.batch_observations[-1]
            assert batch.report is not None and batch.report.acknowledged_at is not None
            elapsed = (batch.report.acknowledged_at - start) * 1000
            evidence = {
                "held_native": hold_native,
                "elapsed_ms": elapsed,
                "completion_to_ack_ms": batch.report.completion_to_ack_ms,
                "terminal_outcomes": batch.terminal_outcomes,
                "missed_assumed_240ms_window": elapsed >= 240,
            }
            print(json.dumps(evidence))
            assert len(batch.terminal_outcomes) == 4
            if hold_native:
                assert all(
                    status == "TIMED_OUT" for _, status in batch.terminal_outcomes
                )
                assert not engine.closed
                stopping = app.stop()
                assert not stopping.done()  # Deadline never releases the SDK owner.
                engine.release_active.set()
                stopping.result(3)
            else:
                assert all(
                    status == "COMPLETE" for _, status in batch.terminal_outcomes
                )
                app.stop().result(3)
        finally:
            engine.release_active.set()
            close(startup)
