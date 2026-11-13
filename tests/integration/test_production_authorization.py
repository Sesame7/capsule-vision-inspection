"""Production command admission with real temporary accounts and synthetic sources."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtWidgets import QApplication
from tests.support.application import WAIT, ReplayEngine
from tests.support.qt import until
from tests.support.startup import (
    TEST_PASSWORD,
    authenticated_startup,
    close,
    provision_account,
    saved_data,
)

from capsule_vision.application import ApplicationState, ApplicationStateError
from capsule_vision.application.identity_models import (
    AuthorizationError,
    Role,
    StaleSessionError,
)
from capsule_vision.application.production_application import (
    SourceProductionApplication,
)
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.application.startup import load_startup
from capsule_vision.communication.reporting import ReportingMode
from capsule_vision.configuration import load_camera_sources, save_camera_sources


@pytest.mark.parametrize("role", list(Role))
def test_production_role_boundaries(
    qapp: QApplication, tmp_path: Path, role: Role
) -> None:
    data = saved_data(tmp_path)
    sources = load_camera_sources(data.sources)
    save_camera_sources(data.sources, replace(sources, loop=True))
    engines = iter((ReplayEngine(), ReplayEngine(fail="fatal")))
    startup = authenticated_startup(data, engine_factory=lambda _: next(engines))
    app, candidate = startup.application, startup.prepared
    assert isinstance(app, SourceProductionApplication) and candidate is not None
    owner = app.identity
    assert owner is not None
    try:
        if role is Role.GUEST:
            owner.logout()
        elif role is not Role.ADMINISTRATOR:
            provision_account(owner, "Actor", role, TEST_PASSWORD)
            owner.login("Actor", TEST_PASSWORD).result(WAIT)
        assert owner.snapshot().role is role
        before, connection = app.snapshot(), app.source_snapshot()
        original = app.reporting_selection()
        enabled = replace(original, enabled=True)
        saved: list[bool] = []
        if role < Role.ENGINEER:
            with pytest.raises(AuthorizationError):
                app.set_reporting_selection(enabled)
            with pytest.raises(AuthorizationError):
                app.persist_reporting_selection(enabled, lambda: saved.append(True))
            assert app.reporting_selection() == original and not saved
        else:
            app.persist_reporting_selection(enabled, lambda: saved.append(True))
            assert app.reporting_selection() == enabled and saved == [True]
            app.set_reporting_selection(original)
        # Saving independent preferences also publishes communication configuration.
        if role < Role.ENGINEER:
            with pytest.raises(AuthorizationError):
                app.persist_reporting_selection(original, lambda: saved.append(False))
            assert not saved
        else:
            app.persist_reporting_selection(original, lambda: saved.append(False))
            assert saved == [True, False]

        if role is Role.GUEST:
            for command in (
                app.start,
                app.step_source,
                app.begin_recipe_selection,
                lambda: app.configure(candidate),
                lambda: app.start_batch("changed"),
                app.clear_statistics,
                app.reset_fault,
                app.connect_source,
                app.disconnect_source,
                lambda: app.capture_image(candidate.configuration.layout.camera_ids[0]),
            ):
                with pytest.raises(AuthorizationError):
                    command()
            assert app.snapshot() == before and app.source_snapshot() == connection
            assert app.control_snapshot() is None and app.reporting_snapshot() is None
            return

        until(lambda: not app.snapshot().batch_pending)
        app.clear_statistics(revision=owner.snapshot().revision)
        revision = owner.snapshot().revision
        account = owner.snapshot().account
        assert account is not None
        owner.login(account.username, TEST_PASSWORD).result(WAIT)
        with pytest.raises(StaleSessionError):
            app.clear_statistics(revision=revision)
        first = app.begin_recipe_selection()
        second = app.begin_recipe_selection()
        with pytest.raises(StaleSessionError):
            app.install_recipe_selection(first, candidate)
        app.install_recipe_selection(second, candidate)
        app.start_batch("Batch").result(5)
        assert app.snapshot().batch_id == "Batch"
        with pytest.raises(ApplicationStateError):  # READY, no fault to reset
            app.reset_fault()
        app.start().result(WAIT)
        app.pause_source().result(WAIT)
        app.step_source().result(WAIT)
        app.resume_source().result(WAIT)
        assert app.snapshot().active_prepared is candidate
        app.stop().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY

        app.step_source()  # The next fake engine fails during its first replay cycle.
        until(
            lambda: (
                app.snapshot().state is ApplicationState.FAULT
                and not app.snapshot().runtime_owned
            )
        )
        app.reset_fault()
        assert app.snapshot().state is ApplicationState.READY
    finally:
        close(startup)


@pytest.mark.parametrize("reporting_enabled", [False, True])
def test_guest_cannot_start_or_step_prepared_folder_replay(
    tmp_path: Path, reporting_enabled: bool
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(
        data, engine_factory=lambda _: pytest.fail("unexpected engine")
    )
    app, candidate = startup.application, startup.prepared
    assert isinstance(app, SourceProductionApplication) and candidate is not None
    assert app.identity is not None
    try:
        selection = app.reporting_selection()
        app.set_reporting_selection(
            replace(
                selection,
                enabled=reporting_enabled,
                settings=replace(selection.settings, mode=ReportingMode.PER_CYCLE),
            )
        )
        app.identity.logout()
        before = app.snapshot()
        assert before.state is ApplicationState.READY and not app.basler_available
        for command in (app.start, app.step_source):
            with pytest.raises(AuthorizationError):
                command()
            assert app.snapshot() == before
            assert app.control_snapshot() is None and app.reporting_snapshot() is None
    finally:
        close(startup)


def test_admitted_replay_and_step_survive_logout_without_new_guest_admission(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    save_camera_sources(
        data.sources, replace(load_camera_sources(data.sources), loop=True)
    )
    engine = ReplayEngine()
    engine.hold_call = 1
    startup = authenticated_startup(data, engine_factory=lambda _: engine)
    app = startup.application
    assert isinstance(app, SourceProductionApplication)
    owner = app.identity
    assert owner is not None
    try:
        worker = provision_account(owner, "Worker", Role.OPERATOR, TEST_PASSWORD)
        owner.login(worker.username, TEST_PASSWORD).result(WAIT)
        starting = app.start()
        assert engine.entered.wait(WAIT)
        owner.logout()
        engine.release.set()
        starting.result(WAIT)
        running = app.snapshot()
        assert running.state is ApplicationState.RUNNING and running.run_id is not None
        for command in (app.pause_source, app.resume_source, app.step_source):
            with pytest.raises(AuthorizationError):
                command()
        owner.login(worker.username, TEST_PASSWORD).result(WAIT)
        app.pause_source().result(WAIT)
        paused = app.control_snapshot()
        assert paused is not None and paused.state is SourceControlState.PAUSED
        engine.hold_call = len(engine.calls) + 1
        engine.entered.clear()
        engine.release.clear()
        step = app.step_source()
        assert engine.entered.wait(WAIT) and not step.done()
        owner.logout()
        engine.release.set()
        step.result(WAIT)
        assert app.control_snapshot() == paused
        for command in (app.pause_source, app.resume_source, app.step_source):
            with pytest.raises(AuthorizationError):
                command()
        current = app.snapshot()
        assert (
            current.state is ApplicationState.RUNNING
            and current.run_id == running.run_id
        )
        assert current.generation == running.generation == 1
        app.stop().result(WAIT)
        assert app.snapshot().state is ApplicationState.READY
        with pytest.raises(AuthorizationError):
            app.step_source()
    finally:
        engine.release.set()
        close(startup)


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_accounts_restore_as_guest(tmp_path: Path, corrupt: bool) -> None:
    data = saved_data(tmp_path)
    if corrupt:
        data.security.write_bytes(b"invalid security storage")
    startup = load_startup(data)
    try:
        app = startup.application
        assert app.identity is not None and app.identity.snapshot().role is Role.GUEST
        assert app.snapshot().candidate_prepared is startup.prepared is not None
        assert isinstance(app, SourceProductionApplication)
        for command in (app.start, app.step_source, app.begin_recipe_selection):
            with pytest.raises(AuthorizationError):
                command()
        assert startup.editor is not None
        with pytest.raises(AuthorizationError):
            startup.editor.save()
        app.stop().result(WAIT)
    finally:
        close(startup)


def test_logout_revokes_start_admission_after_reporting_save(
    tmp_path: Path,
) -> None:
    data = saved_data(tmp_path)
    startup = authenticated_startup(
        data, engine_factory=lambda _: pytest.fail("unexpected engine")
    )
    app = startup.application
    assert isinstance(app, SourceProductionApplication) and app.identity is not None
    entered, release, starting = Event(), Event(), Event()
    selected = replace(app.reporting_selection(), enabled=True)

    def persist() -> None:
        entered.set()
        assert release.wait(WAIT)

    def guest_start() -> None:
        starting.set()
        app.start().result(WAIT)

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            saving = pool.submit(app.persist_reporting_selection, selected, persist)
            try:
                assert entered.wait(WAIT)
                logging_out = pool.submit(app.identity.logout)
                assert not logging_out.done()
            finally:
                release.set()
            saving.result(WAIT)
            logging_out.result(WAIT)
            attempt = pool.submit(guest_start)
            assert starting.wait(WAIT)
            with pytest.raises(AuthorizationError):
                attempt.result(WAIT)
        assert app.reporting_selection() == selected and app.snapshot().generation == 0
    finally:
        close(startup)
