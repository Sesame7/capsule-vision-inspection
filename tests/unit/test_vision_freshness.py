"""Saved Vision admission, immutable sessions and explicit source ownership."""

import json
from dataclasses import replace
from pathlib import Path
from threading import current_thread, main_thread
from typing import Never

import pytest
from tests.support.application import WAIT, ControlledEngine, FakeSession, close
from tests.support.source_inputs import folders, input_files, write_image
from tests.support.startup import saved_data

from capsule_vision.application import (
    ApplicationError,
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
    ProductionApplication,
    prepare_configuration,
)
from capsule_vision.application import composition as composition_module
from capsule_vision.application import editing as editing_module
from capsule_vision.application import vision_dependencies as dependencies_module
from capsule_vision.application.composition import compose
from capsule_vision.application.folder_replay import FolderSourceError
from capsule_vision.application.startup import ApplicationData, load_startup
from capsule_vision.application.vision_dependencies import (
    VisionDependencies,
    VisionPreparationRequired,
)
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    MachineLayoutDefinition,
    ResolvedInspectionConfiguration,
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
    save_machine_layout,
)


@pytest.fixture
def saved_candidate(tmp_path: Path) -> tuple[ApplicationData, PreparedConfiguration]:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    try:
        assert startup.prepared is not None
        assert startup.prepared.vision_dependencies is not None
        assert not startup.application.snapshot().candidate_requires_preparation
        return data, startup.prepared
    finally:
        close(startup.application)


def changed_machine(machine: MachineLayoutDefinition) -> MachineLayoutDefinition:
    first, *remaining = machine.layout.cameras
    slot, *slots = first.slots
    rect = replace(slot.rect, width=slot.rect.width - 1)
    camera = replace(first, slots=(replace(slot, rect=rect), *slots))
    return replace(
        machine, layout=replace(machine.layout, cameras=(camera, *remaining))
    )


@pytest.mark.parametrize("dependency", ["machine", "sources"])
@pytest.mark.parametrize("change", ["changed", "corrupt", "missing", "unreadable"])
def test_start_rejects_changed_or_unreadable_vision_before_factory(
    saved_candidate: tuple[ApplicationData, PreparedConfiguration],
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
    change: str,
) -> None:
    data, prepared = saved_candidate
    calls: list[PreparedConfiguration] = []

    def factory(candidate: PreparedConfiguration, generation: int) -> FakeSession:
        calls.append(candidate)
        return FakeSession()

    app = ProductionApplication(factory)
    path = data.machine if dependency == "machine" else data.sources
    if change == "changed":
        if dependency == "machine":
            save_machine_layout(path, changed_machine(load_machine_layout(path)))
        else:
            save_camera_sources(
                path, replace(load_camera_sources(path), target_fps=4.5)
            )
    elif change == "corrupt":
        path.write_text("invalid isolated definition", encoding="utf-8")
    elif change == "missing":
        path.unlink()
    else:
        original = load_machine_layout

        def unreadable(machine_path: Path) -> MachineLayoutDefinition:
            assert current_thread() is not main_thread()
            if dependency == "machine":
                raise PermissionError("isolated unreadable MachineLayout")
            return original(machine_path)

        def unreadable_sources(source_path: Path) -> None:
            assert current_thread() is not main_thread()
            raise PermissionError("isolated unreadable CameraSources")

        monkeypatch.setattr(dependencies_module, "load_machine_layout", unreadable)
        if dependency == "sources":
            monkeypatch.setattr(dependencies_module, "read_sources", unreadable_sources)
    try:
        app.configure(prepared)
        app.start_batch("Existing Batch").result(5)
        before = app.snapshot()
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        after = app.snapshot()
        assert after.state is ApplicationState.READY and not after.runtime_owned
        assert (
            after.candidate_prepared is prepared and after.candidate == before.candidate
        )
        assert after.active is None and after.candidate_requires_preparation
        assert after.batch_id == before.batch_id and after.counters == before.counters
        assert after.last_issue is not None
        assert after.last_issue.exception_type == VisionPreparationRequired.__name__
        assert "reselect" in after.last_issue.message and not calls
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
    finally:
        close(app)


def test_unchanged_saves_and_serialization_do_not_invalidate_candidate(
    saved_candidate: tuple[ApplicationData, PreparedConfiguration],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, prepared = saved_candidate
    machine, sources = (
        load_machine_layout(data.machine),
        load_camera_sources(data.sources),
    )
    save_machine_layout(data.machine, machine)
    values = json.loads(data.sources.read_text(encoding="utf-8"))
    data.sources.write_text(
        json.dumps(dict(reversed(values.items()))), encoding="utf-8"
    )
    checked: list[str] = []
    validate = VisionDependencies.validate_current

    def checked_off_dispatch(dependencies: VisionDependencies) -> None:
        assert current_thread() is not main_thread()
        checked.append(current_thread().name)
        validate(dependencies)

    monkeypatch.setattr(VisionDependencies, "validate_current", checked_off_dispatch)
    app = ProductionApplication(lambda _p, _g: FakeSession())
    try:
        app.configure(prepared)
        app.observe_saved_vision(machine, sources)
        assert not app.snapshot().candidate_requires_preparation
        app.start().result(WAIT)
        assert app.snapshot().active_prepared is prepared and checked == [
            app.thread_name
        ]
        app.stop().result(WAIT)
    finally:
        close(app)


def test_saved_changes_and_failed_restart_preserve_active_and_candidate(
    saved_candidate: tuple[ApplicationData, PreparedConfiguration],
) -> None:
    data, prepared = saved_candidate
    sessions: list[FakeSession] = []

    def factory(_candidate: PreparedConfiguration, _generation: int) -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    app = ProductionApplication(factory)
    try:
        app.configure(prepared)
        app.start().result(WAIT)
        active = app.snapshot().active_prepared
        new_machine = changed_machine(load_machine_layout(data.machine))
        save_machine_layout(data.machine, new_machine)
        app.observe_saved_vision(new_machine, None, sources_known=False)
        running = app.snapshot()
        assert running.state is ApplicationState.RUNNING and running.runtime_owned
        assert running.active_prepared is active is prepared
        assert (
            running.candidate_prepared is prepared
            and running.candidate_requires_preparation
        )
        assert prepared.configuration.layout != new_machine.layout
        assert len(sessions) == 1 and not sessions[0].stop_requested.is_set()
        app.stop().result(WAIT)
        with pytest.raises(ApplicationStateError, match="re-preparation"):
            app.start()
        # Even direct installation of the old value cannot bypass disk preflight.
        app.configure(prepared)
        with pytest.raises(ApplicationError):
            app.start().result(WAIT)
        assert app.snapshot().active_prepared is active and len(sessions) == 1
    finally:
        close(app)


def test_preparation_rejects_layout_changed_during_model_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    original = prepare_configuration

    def changed_during_hash(
        configuration: ResolvedInspectionConfiguration, *, base_directory: Path
    ) -> PreparedConfiguration:
        result = original(configuration, base_directory=base_directory)
        save_machine_layout(
            data.machine, changed_machine(load_machine_layout(data.machine))
        )
        return result

    try:
        assert startup.editor is not None and startup.prepared is not None
        monkeypatch.setattr(
            editing_module, "prepare_configuration", changed_during_hash
        )
        preparation = startup.editor.production_preparation()
        with pytest.raises(VisionPreparationRequired, match="changed"):
            preparation.prepare_saved(startup.prepared.configuration.recipe_id)
        assert startup.application.snapshot().candidate_prepared is startup.prepared
    finally:
        close(startup.application)


@pytest.mark.parametrize("reprepare", [False, True])
def test_fixed_image_launcher_does_not_adopt_saved_camera_sources(
    tmp_path: Path,
    reprepare: bool,
) -> None:
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = compose(*input_files(tmp_path), engine_factory=lambda _: engine)
    app = composition.application
    try:
        dependencies = composition.prepared.vision_dependencies
        assert dependencies is not None and dependencies.sources_path is None
        assert dependencies.sources is not None
        sources = tmp_path / "camera_sources.json"
        sources.write_text("invalid independent source file", encoding="utf-8")
        candidate = composition.prepared
        if reprepare:
            candidate = composition.editor.production_preparation().prepare_saved(
                candidate.configuration.recipe_id
            )
            assert candidate.vision_dependencies is not None
            assert candidate.vision_dependencies.sources is dependencies.sources
            assert candidate.vision_dependencies.sources_path is None
            app.configure(candidate)
        app.start().result(WAIT)
        assert app.snapshot().active_prepared is candidate
        assert not app.snapshot().candidate_requires_preparation
        app.stop().result(WAIT)
    finally:
        close(app)


def test_startup_restores_updated_dependencies_without_start(
    saved_candidate: tuple[ApplicationData, PreparedConfiguration],
) -> None:
    data, before = saved_candidate
    preferences = data.preferences.read_bytes()
    save_machine_layout(
        data.machine, changed_machine(load_machine_layout(data.machine))
    )
    sources = load_camera_sources(data.sources)
    save_camera_sources(data.sources, replace(sources, loop=not sources.loop))
    startup = load_startup(data)
    try:
        current = startup.application.snapshot()
        assert current.state is ApplicationState.READY and current.active is None
        assert not current.runtime_owned and not current.candidate_requires_preparation
        assert current.candidate_prepared is startup.prepared
        assert (
            startup.prepared is not None
            and startup.prepared.vision_dependencies is not None
        )
        assert (
            startup.prepared.configuration.recipe_id == before.configuration.recipe_id
        )
        assert (
            startup.prepared.configuration.layout
            == load_machine_layout(data.machine).layout
        )
        assert startup.prepared.vision_dependencies.sources == load_camera_sources(
            data.sources
        )
        assert data.preferences.read_bytes() == preferences
    finally:
        close(startup.application)


def test_session_factory_uses_captured_sources_if_files_change_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = saved_data(tmp_path)
    startup = load_startup(data)
    assert (
        startup.prepared is not None
        and startup.prepared.vision_dependencies is not None
    )
    captured = startup.prepared.vision_dependencies.sources
    assert captured is not None
    validate = VisionDependencies.validate_current
    seen: list[object] = []

    def validated_then_changed(value: VisionDependencies) -> None:
        assert current_thread() is not main_thread()
        validate(value)
        save_camera_sources(
            data.sources, replace(captured, target_fps=captured.target_fps + 1)
        )

    def observe_factory(*args: object, **kwargs: object) -> Never:
        seen.append(kwargs["sources"])
        raise ValueError("isolated constructor observation; no runtime created")

    def forbidden_disk_read(_path: Path) -> Never:
        raise AssertionError("Session factory must use captured CameraSources")

    monkeypatch.setattr(VisionDependencies, "validate_current", validated_then_changed)
    monkeypatch.setattr(composition_module, "load_camera_sources", forbidden_disk_read)
    monkeypatch.setattr(composition_module, "SourceRuntimeSession", observe_factory)
    try:
        with pytest.raises(ApplicationError):
            startup.application.start().result(WAIT)
        assert seen == [captured]
        assert seen[0] is captured
        assert seen[0] != load_camera_sources(data.sources)
        assert startup.application.snapshot().active is None
    finally:
        close(startup.application)


@pytest.mark.parametrize("created_after_preflight", [False, True])
def test_missing_captured_sources_never_fall_back_and_repreparation_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created_after_preflight: bool
) -> None:
    model, machine, recipe, _paths = input_files(tmp_path)
    sources_file = tmp_path / "camera_sources.json"
    available = folders(tmp_path, loop=False)
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    effects: list[str] = []
    disk_reads: list[Path] = []

    def create_engine(_prepared: PreparedConfiguration) -> ControlledEngine:
        effects.append("engine")
        return engine

    def forbidden_session(*args: object, **kwargs: object) -> Never:
        effects.append("session")
        raise AssertionError("Missing captured sources must not construct a session")

    def read_from_disk(path: Path) -> CameraSources:
        disk_reads.append(path)
        return load_camera_sources(path)

    composition = compose(model, machine, recipe, engine_factory=create_engine)
    app = composition.application
    candidate = composition.prepared
    dependencies = candidate.vision_dependencies
    assert dependencies is not None and dependencies.sources is None
    assert dependencies.sources_path == sources_file and not sources_file.exists()
    validate = VisionDependencies.validate_current
    preflights: list[str] = []

    def checked_then_created(value: VisionDependencies) -> None:
        assert current_thread() is not main_thread() and value is dependencies
        validate(value)
        preflights.append(current_thread().name)
        if created_after_preflight:
            save_camera_sources(sources_file, available)

    try:
        app.start_batch("Existing Batch").result(5)
        before = app.snapshot()
        with monkeypatch.context() as patch:
            patch.setattr(VisionDependencies, "validate_current", checked_then_created)
            patch.setattr(composition_module, "load_camera_sources", read_from_disk)
            patch.setattr(composition_module, "SourceRuntimeSession", forbidden_session)
            with pytest.raises(
                ApplicationError, match="captured CameraSources"
            ) as error:
                app.start().result(WAIT)
        assert isinstance(error.value.__cause__, FolderSourceError)
        assert isinstance(error.value.__cause__.__cause__, ConfigurationError)
        after = app.snapshot()
        assert after.state is ApplicationState.READY and not after.runtime_owned
        assert after.candidate_prepared is candidate and after.active_prepared is None
        assert after.batch_id == before.batch_id and after.counters == before.counters
        assert after.last_issue is not None
        assert after.last_issue.exception_type == FolderSourceError.__name__
        assert preflights == [app.thread_name] and not disk_reads and not effects
        assert sources_file.exists() == created_after_preflight

        if not sources_file.exists():
            save_camera_sources(sources_file, available)
        recovered = composition.editor.production_preparation().prepare_saved(
            candidate.configuration.recipe_id
        )
        assert recovered.vision_dependencies is not None
        assert recovered.vision_dependencies.sources == available
        app.configure(recovered)
        app.start().result(WAIT)
        assert engine.active.wait(WAIT)
        app.stop().result(WAIT)
        assert app.snapshot().active_prepared is recovered and effects == ["engine"]
        assert engine.closed
        before_missing_again = app.snapshot()
        sources_file.unlink()
        missing_again = composition.editor.production_preparation().prepare_saved(
            candidate.configuration.recipe_id
        )
        assert missing_again.vision_dependencies is not None
        assert missing_again.vision_dependencies.sources is None
        app.configure(missing_again)
        with monkeypatch.context() as patch:
            patch.setattr(composition_module, "SourceRuntimeSession", forbidden_session)
            with pytest.raises(ApplicationError, match="captured CameraSources"):
                app.start().result(WAIT)
        failed_restart = app.snapshot()
        assert failed_restart.active_prepared is recovered
        assert failed_restart.candidate_prepared is missing_again
        assert failed_restart.counters == before_missing_again.counters
        assert failed_restart.state is ApplicationState.READY
        assert not failed_restart.runtime_owned and effects == ["engine"]
    finally:
        close(app)


@pytest.mark.parametrize("fixed_images", [False, True])
def test_launchers_without_dependency_snapshots_keep_source_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_images: bool
) -> None:
    model, machine, recipe, paths = input_files(tmp_path)
    initial = folders(tmp_path, loop=False)
    sources_file = tmp_path / "camera_sources.json"
    save_camera_sources(sources_file, initial)
    engine = ControlledEngine()
    engine.release_warm.set()
    engine.release_active.set()
    composition = compose(
        model,
        machine,
        recipe,
        paths if fixed_images else (),
        engine_factory=lambda _: engine,
    )
    app = composition.application
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    write_image(replacement / "first.png", 99)
    latest = replace(
        initial,
        cameras=(
            CameraFolder(initial.cameras[0].camera_id, replacement.name),
            *initial.cameras[1:],
        ),
    )
    save_camera_sources(sources_file, latest)
    disk_reads: list[Path] = []

    def read_from_disk(path: Path) -> CameraSources:
        assert not fixed_images
        disk_reads.append(path)
        return load_camera_sources(path)

    monkeypatch.setattr(composition_module, "load_camera_sources", read_from_disk)
    legacy = replace(composition.prepared, vision_dependencies=None)
    try:
        app.configure(legacy)
        app.start().result(WAIT)
        assert engine.active.wait(WAIT)
        app.stop().result(WAIT)
        assert app.snapshot().active_prepared is legacy
        assert disk_reads == ([] if fixed_images else [sources_file])
        assert engine.calls[2][0] == (1 if fixed_images else 99)
    finally:
        close(app)
