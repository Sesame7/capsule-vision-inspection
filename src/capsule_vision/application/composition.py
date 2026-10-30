"""Qt-free production factories and explicit-path engineering input setup."""

from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.application.basler_connection import (
    ConnectionState,
)
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.folder_cache import (
    FolderCacheStatus,
    FolderReplayCache,
)
from capsule_vision.application.folder_replay import (
    FolderSourceError,
)
from capsule_vision.application.history import ProductionHistory
from capsule_vision.application.session import EngineFactory, real_engine
from capsule_vision.communication.reporting import (
    FirstStationReportingWorker,
    ReportingSettings,
)
from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    ConfigurationError,
    ModelRegistry,
    load_camera_sources,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from capsule_vision.configuration.catalog import RecipeCatalog
from capsule_vision.configuration.drafts import CameraSourceDraft
from capsule_vision.display import LatestDisplayFrameStore
from capsule_vision.inference._validation import uint8_array
from capsule_vision.inspection import CycleId, Frame, FrameId

from .application import ProductionApplication
from .model_preparation import ModelPreparationFactory, real_preparation_engine
from .models import (
    ApplicationState,
    ApplicationStateError,
    PreparedConfiguration,
    prepare_configuration,
)
from .production_application import SourceProductionApplication
from .production_session import SourceRuntimeSession


def _cli_image(path: Path) -> NDArray[np.uint8]:
    # Preserve the explicit-path launcher's ValueError/OSError decoding contract.
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode uint8 BGR image: {path}")
    return uint8_array(image)


@dataclass
class EngineeringComposition:
    application: SourceProductionApplication
    store: LatestDisplayFrameStore
    prepared: PreparedConfiguration
    editor: RecipeEditorSession


def compose(
    model_file: Path,
    machine_file: Path,
    recipe_file: Path,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    preparation_factory: ModelPreparationFactory = real_preparation_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    sources_file: Path | None = None,
    observe_cache: Callable[[FolderCacheStatus], None] = lambda _: None,
    reporting: ReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
) -> EngineeringComposition:
    """Parse/hash/decode explicit inputs; no fallback images or invented ROIs.

    Relative model paths are resolved against the model-description directory.
    Selected files follow configured camera order, not filename camera guesses.
    """
    model = load_model_definition(model_file)
    machine = load_machine_layout(machine_file)
    recipe = load_recipe(recipe_file)
    registry = ModelRegistry((model,))
    prepared = prepare_configuration(
        resolve_configuration(machine, recipe, registry),
        base_directory=model_file.resolve().parent,
    )
    if paths and len(paths) != len(prepared.configuration.layout.cameras):
        raise ValueError("One local image path per configured camera is required")
    sources_file = (
        sources_file or machine_file.parent / "camera_sources.json"
    ).resolve()
    sources = (
        load_camera_sources(sources_file)
        if sources_file.exists()
        else CameraSources(
            tuple(
                CameraFolder(
                    camera, str(paths[index].resolve().parent) if paths else ""
                )
                for index, camera in enumerate(prepared.configuration.layout.camera_ids)
            )
        )
    )
    sources.validate_cameras(prepared.configuration.layout.camera_ids)
    sources.validate_mapping()
    editor = RecipeEditorSession(
        RecipeCatalog(recipe_file.resolve().parent),
        machine,
        registry,
        model_file.resolve().parent,
        machine_path=machine_file.resolve(),
        camera_sources=CameraSourceDraft(sources, sources_file),
        cli_sources=bool(paths),
    )
    app, store = create_production_application(
        editor,
        paths,
        engine_factory=engine_factory,
        preparation_factory=preparation_factory,
        observe_source=observe_source,
        observe_cache=observe_cache,
        reporting=reporting,
        replay_controls=replay_controls,
        reporting_editable=reporting_editable,
    )
    try:
        app.configure(prepared)
    except BaseException:
        app.close().result()
        app.join()
        raise
    return EngineeringComposition(app, store, prepared, editor)


def create_production_application(
    editor: RecipeEditorSession,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    preparation_factory: ModelPreparationFactory = real_preparation_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    observe_cache: Callable[[FolderCacheStatus], None] = lambda _: None,
    reporting: ReportingSettings | None = None,
    replay_controls: bool = False,
    reporting_editable: bool = False,
    history: ProductionHistory | None = None,
) -> tuple[SourceProductionApplication, LatestDisplayFrameStore]:
    """Shared serial composition; no session/source/engine is created until Start."""
    if paths and len(paths) != len(editor.machine.layout.cameras):
        raise ValueError("One local image path per configured camera is required")
    if editor.camera_sources is None:
        raise ValueError("Local production requires camera sources")
    source_draft = editor.camera_sources
    sources_file = source_draft.path
    sources = source_draft.saved
    images = tuple(_cli_image(path) for path in paths)
    store = LatestDisplayFrameStore(
        editor.machine_editor.camera_ids if editor.machine_editor.persisted else ()
    )
    cache = FolderReplayCache(notify=observe_cache)
    for camera, image in zip(
        editor.machine.layout.cameras if images else (), images, strict=True
    ):
        camera.validate_shape((image.shape[0], image.shape[1], 3))
        image.setflags(write=False)
        store.publish(
            Frame(FrameId(CycleId(0), camera.camera_id, 0), monotonic(), image),
            generation=0,
            source_key=CameraFolder(
                camera.camera_id,
                str(
                    paths[editor.machine.layout.camera_ids.index(camera.camera_id)]
                    .resolve()
                    .parent
                ),
            ).key(sources_file.parent),
        )

    def factory(config: PreparedConfiguration, generation: int) -> SourceRuntimeSession:
        try:
            saved_sources = load_camera_sources(sources_file) if not paths else sources
            saved_sources.validate_cameras(config.configuration.layout.camera_ids)
            if not paths and saved_sources.mode != source_draft.saved.mode:
                raise ConfigurationError(
                    "Source mode changed on disk; apply in Vision or restart"
                )
        except ConfigurationError as exc:
            cache.clear()
            raise FolderSourceError(f"Camera sources {sources_file}: {exc}") from exc
        snapshot = (
            saved_sources
            if not paths
            else CameraSources(
                tuple(
                    CameraFolder(camera, str(path.resolve().parent))
                    for camera, path in zip(
                        config.configuration.layout.camera_ids, paths, strict=True
                    )
                )
            )
        )
        snapshot.validate_cameras(config.configuration.layout.camera_ids)
        serials = (
            snapshot.basler_serials(config.configuration.layout.camera_ids)
            if snapshot.mode == "basler"
            else None
        )
        continuous_source = app.connection if serials is not None else None
        if continuous_source is not None:
            assert serials is not None
            connected = continuous_source.snapshot()
            if connected.state is not ConnectionState.CONNECTED or tuple(
                (row.camera_id, row.serial) for row in connected.cameras
            ) != tuple(
                zip(config.configuration.layout.camera_ids, serials, strict=True)
            ):
                raise ApplicationStateError(
                    "Connected cameras differ from the saved source"
                )
            staged: list[NDArray[np.uint8]] = []
            for camera, actual in zip(
                config.configuration.layout.cameras, connected.cameras, strict=True
            ):
                camera.validate_shape((actual.height, actual.width, 3))
                image = np.zeros((actual.height, actual.width, 3), dtype=np.uint8)
                image.setflags(write=False)
                staged.append(image)
            group = tuple(staged)
            replay = None
        else:
            replay = (
                None
                if paths
                else cache.replay(
                    config.configuration.layout,
                    snapshot,
                    sources_file.parent,
                    cancelled=lambda: (
                        app.snapshot().state
                        in (
                            ApplicationState.STOPPING,
                            ApplicationState.CLOSED,
                            ApplicationState.FAULT,
                        )
                    ),
                )
            )
            group = images if replay is None else replay.next_group()
            if group is None:
                raise ValueError("No complete image group")
        session_reporting = app.reporting_for_generation(generation)
        report_worker = (
            FirstStationReportingWorker(
                generation,
                session_reporting,
                attributable=lambda identity: (
                    continuous_source is None
                    or continuous_source.attributable(identity.cycle_id)
                ),
            )
            if session_reporting is not None
            else None
        )
        session = SourceRuntimeSession(
            config,
            generation,
            app,
            group,
            store,
            engine_factory,
            replay=replay,
            sources=snapshot,
            source_base=sources_file.parent,
            reporting=report_worker,
            replay_control=app.folder_control_for(generation),
            continuous_source=continuous_source,
            basler_control=app.basler_control_for(generation)
            if continuous_source is not None
            else None,
        )
        app.set_reporting_owner(report_worker)
        app.set_archive_owner(session.archive)
        observe_source(session.source_done)
        return session

    app = SourceProductionApplication(
        factory,
        cache,
        store,
        preparation_factory=preparation_factory,
        replay_controls=replay_controls,
        reporting=reporting,
        reporting_editable=reporting_editable,
        history=history,
        source_draft=source_draft if not paths else None,
        source_cameras=editor.machine_editor.camera_ids,
        source_camera_ids=lambda: (
            editor.machine.layout.camera_ids
            if editor.machine_editor.persisted
            else editor.machine_editor.camera_ids
        ),
    )
    return app, store


def unconfigured_application(
    history: ProductionHistory | None = None,
) -> ProductionApplication:
    """Normal stopped shell can edit session preferences without inventing inputs."""

    def unavailable(
        _config: PreparedConfiguration, _generation: int
    ) -> SourceRuntimeSession:
        raise ConfigurationError("Persistent configuration is unavailable")

    return SourceProductionApplication(
        unavailable, FolderReplayCache(), reporting_editable=True, history=history
    )
