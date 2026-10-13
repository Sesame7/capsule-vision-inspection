"""Real ViMo Production UI: saved per-camera folders or explicit CLI images."""

import sys
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import cv2
import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Slot

from vimo_demo.application import (
    ApplicationState,
    ApplicationStateError,
    LocalCycleIngress,
    PreparedConfiguration,
    ProductionApplication,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.application.folder_cache import (
    CacheState,
    FolderCacheStatus,
    FolderReplayCache,
)
from vimo_demo.application.folder_replay import (
    FolderReplay,
    FolderSourceError,
    start_interval_wait,
)
from vimo_demo.application.session import EngineFactory
from vimo_demo.configuration import (
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
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inference._validation import uint8_array
from vimo_demo.inspection import CycleId, Frame, FrameId
from vimo_demo.runtime import CoordinatorStoppedError, InferenceEngine
from vimo_demo.ui.camera_source_editor import CameraSourceDraft
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeEditorSession


def real_engine(prepared: PreparedConfiguration) -> InferenceEngine:
    return VimoSegmentationEngine(
        prepared.model_path, module_id=prepared.configuration.model.module_id
    )


def _cli_image(path: Path) -> NDArray[np.uint8]:
    # Preserve the explicit-path launcher's ValueError/OSError decoding contract.
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode uint8 BGR image: {path}")
    return uint8_array(image)


class LocalRuntimeSession(ProductionRuntimeSession):
    """Launcher composition only: the existing session also joins its local source.

    No engine calls here: construction, warm-up, inference and close remain on
    InferenceWorker. Source waits never run on Qt. READY cannot precede source exit.
    """

    def __init__(
        self,
        prepared: PreparedConfiguration,
        generation: int,
        application: ProductionApplication,
        images: tuple[NDArray[np.uint8], ...],
        store: LatestDisplayFrameStore,
        engine_factory: EngineFactory = real_engine,
        *,
        replay: FolderReplay | None = None,
        sources: CameraSources | None = None,
        source_base: Path = Path("."),
    ) -> None:
        def warmup(config: PreparedConfiguration) -> Sequence[NDArray[np.generic]]:
            for camera, image in zip(
                config.configuration.layout.cameras, self._images, strict=True
            ):
                camera.validate_shape((image.shape[0], image.shape[1], 3))
            return self._images

        super().__init__(
            prepared,
            generation,
            warmup=warmup,
            engine_factory=engine_factory,
            cycle_timeout=30,
            max_collecting=1,
            max_ready=1,
        )
        self._application, self._images, self._store = application, images, store
        self._layout = prepared.configuration.layout
        self._replay, self._sources, self._source_base = replay, sources, source_base
        self._source_stop = Event()
        self.source_done: Future[None] = Future()
        self.source_done.set_running_or_notify_cancel()
        self._source = Thread(
            target=self._produce,
            name=f"LocalProductionSource-{generation}",
            daemon=False,
        )

    def start(self) -> Future[None]:
        ready = super().start()
        self._source.start()
        return ready

    def request_stop(self) -> None:
        self._source_stop.set()
        super().request_stop()

    def join(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + timeout
        if not super().join(timeout):
            return False
        if self._source.ident is not None:
            remaining = None if deadline is None else max(0, deadline - monotonic())
            self._source.join(remaining)
        joined = not self._source.is_alive()
        if (
            joined
            and self.cleanup_safe
            and self._replay is not None
            and self._replay.cached
        ):
            # No owner can read source pixels now. The application cache survives
            # Stop, but an old disposable session must not retain the full dataset.
            self._images = ()
            self._replay = None
        return joined

    @property
    def thread_names(self) -> tuple[str, ...]:
        return (*super().thread_names, self._source.name)

    def _produce(self) -> None:
        try:
            while not self._source_stop.is_set():
                snapshot = self._application.snapshot()
                if snapshot.state is ApplicationState.RUNNING:
                    if snapshot.generation != self.generation:
                        raise ApplicationStateError("Local source generation mismatch")
                    break
                if self._source_stop.wait(0.05):
                    return
            if self._source_stop.is_set():
                return
            ingress = LocalCycleIngress(self._application, self._layout)
            started: float | None = None
            first = True
            while not self._source_stop.is_set():
                fps = self._sources.target_fps if self._sources else 8.0
                if started is not None and self._source_stop.wait(
                    start_interval_wait(started, monotonic(), fps)
                ):
                    return
                started = monotonic()
                if self._replay is not None and not first:
                    group = self._replay.next_group()
                    if group is None:
                        self._application.stop()  # Normal exhaustion, never FAULT.
                        return
                    self._images = group
                first = False
                if self._source_stop.is_set():
                    return
                captured = monotonic()  # Local submission time, not camera exposure.
                result = ingress.submit(self._images).result.result()
                for camera, image in zip(result.cameras, self._images, strict=True):
                    if camera.frame_id is not None:
                        # Reuse the actual admitted identity; these source pixels
                        # equal the ingress copy. Store owns a separate display copy.
                        self._store.publish_pixels(
                            camera.frame_id,
                            captured,
                            image,
                            generation=self.generation,
                            source_key=self._sources.camera(camera.camera_id).key(
                                self._source_base
                            )
                            if self._sources
                            else None,
                        )
                del result  # Keep no cycle/result history.
        except BaseException as exc:
            if not (
                (
                    self._source_stop.is_set()
                    or self._application.snapshot().state
                    in (ApplicationState.STOPPING, ApplicationState.CLOSED)
                )
                and isinstance(exc, (ApplicationStateError, CoordinatorStoppedError))
            ):
                self.source_done.set_exception(exc)
                # Stop a partially admitted cycle; never retry or fabricate NG.
                self._application.stop()
        finally:
            if not self.source_done.done():
                self.source_done.set_result(None)


class _LocalApplication(ProductionApplication):
    """Local composition resource only; core lifecycle/activation stays unchanged."""

    def __init__(
        self,
        factory: Callable[[PreparedConfiguration, int], LocalRuntimeSession],
        cache: FolderReplayCache,
    ) -> None:
        self.replay_cache = cache
        super().__init__(factory)

    def close(self) -> Future[None]:
        completed = super().close()
        completed.add_done_callback(self._release_cache)
        return completed

    def _release_cache(self, completed: Future[None]) -> None:
        # Close success follows safe source/native/runtime joins. A failed Close
        # keeps ownership/cache; application.join still proves lifecycle-thread exit.
        if completed.exception(timeout=0) is None:
            self.replay_cache.clear()


def _cache_notice(status: FolderCacheStatus) -> None:
    if status.state in (
        CacheState.LOADING,
        CacheState.READY,
        CacheState.HIT,
        CacheState.FALLBACK,
    ):
        print(
            f"FolderReplay cache {status.state.name}: {status.loaded_images}/{status.image_count} images, "
            f"{status.decoded_bytes} bytes, load={status.load_seconds:.3f}s"
            + (f"; on-demand fallback: {status.reason}" if status.reason else ""),
            file=sys.stderr,
        )


@dataclass
class LocalComposition:
    application: _LocalApplication
    store: LatestDisplayFrameStore
    choice: RecipeChoice
    editor: RecipeEditorSession


def compose(
    model_file: Path,
    machine_file: Path,
    recipe_file: Path,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    sources_file: Path | None = None,
    observe_cache: Callable[[FolderCacheStatus], None] = _cache_notice,
) -> LocalComposition:
    """Parse/hash/decode before entering Qt; no fallback images or invented ROIs.

    Relative model paths are resolved against the model-description directory.
    Selected files follow configured camera order, not filename camera guesses.
    """
    if paths and len(paths) != 6:
        raise ValueError("Exactly six local image paths are required")
    model = load_model_definition(model_file)
    machine = load_machine_layout(machine_file)
    recipe = load_recipe(recipe_file)
    registry = ModelRegistry((model,))
    prepared = prepare_configuration(
        resolve_configuration(machine, recipe, registry, model.model_id),
        base_directory=model_file.resolve().parent,
    )
    if len(prepared.configuration.layout.cameras) != 6:
        raise ValueError("Local Production UI requires exactly six configured cameras")
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
    editor = RecipeEditorSession(
        RecipeCatalog(recipe_file.resolve().parent),
        machine,
        registry,
        model.model_id,
        model_file.resolve().parent,
        machine_path=machine_file.resolve(),
        camera_sources=CameraSourceDraft(sources, sources_file),
        cli_sources=bool(paths),
    )
    app, store = local_application(
        editor,
        paths,
        engine_factory=engine_factory,
        observe_source=observe_source,
        observe_cache=observe_cache,
    )
    app.configure(prepared)
    return LocalComposition(
        app, store, RecipeChoice(prepared, recipe.display_name), editor
    )


def local_application(
    editor: RecipeEditorSession,
    paths: Sequence[Path] = (),
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
    observe_cache: Callable[[FolderCacheStatus], None] = _cache_notice,
) -> tuple[_LocalApplication, LatestDisplayFrameStore]:
    """Shared serial composition; no session/source/engine is created until Start."""
    if paths and len(paths) != 6:
        raise ValueError("Exactly six local image paths are required")
    if editor.camera_sources is None:
        raise ValueError("Local production requires camera sources")
    sources_file = editor.camera_sources.path
    sources = editor.camera_sources.saved
    images = tuple(_cli_image(path) for path in paths)
    store = LatestDisplayFrameStore(editor.machine.layout.camera_ids)
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

    def factory(config: PreparedConfiguration, generation: int) -> LocalRuntimeSession:
        try:
            saved_sources = load_camera_sources(sources_file) if not paths else sources
            saved_sources.validate_cameras(config.configuration.layout.camera_ids)
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
        session = LocalRuntimeSession(
            config,
            generation,
            app,
            group,
            store,
            engine_factory,
            replay=replay,
            sources=snapshot,
            source_base=sources_file.parent,
        )
        observe_source(session.source_done)
        return session

    app = _LocalApplication(factory, cache)
    return app, store


class SourceNotifications(QObject):
    """Source Future -> existing queued Qt command-error presentation."""

    def __init__(self, bridge: QtProductionBridge, delivery: FutureDelivery) -> None:
        super().__init__(bridge)
        self._bridge = bridge
        delivery.emitter.completed.connect(
            self.completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(delivery.detach, Qt.ConnectionType.DirectConnection)

    @Slot(str, object, object)
    def completed(self, name: str, _value: object, error: object) -> None:
        if isinstance(error, BaseException):
            traceback.print_exception(error)
            self._bridge.command_failed.emit(name, error)
