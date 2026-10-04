"""Opt-in real ViMo Production UI with one fixed, ordered six-image set."""

import argparse
import sys
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import numpy as np
from _tool_helpers import decode, image_paths
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QApplication

from vimo_demo.application import (
    ApplicationState,
    ApplicationStateError,
    LocalCycleIngress,
    PreparedConfiguration,
    ProductionApplication,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.application.session import EngineFactory
from vimo_demo.configuration import (
    ModelRegistry,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    resolve_configuration,
)
from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.inspection import CycleId, Frame, FrameId
from vimo_demo.runtime import CoordinatorStoppedError, InferenceEngine
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.inference_bridge import FutureDelivery
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeEditorSession


def real_engine(prepared: PreparedConfiguration) -> InferenceEngine:
    return VimoSegmentationEngine(
        prepared.model_path, module_id=prepared.configuration.model.module_id
    )


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
    ) -> None:
        def warmup(config: PreparedConfiguration) -> Sequence[NDArray[np.generic]]:
            for camera, image in zip(
                config.configuration.layout.cameras, images, strict=True
            ):
                camera.validate_shape((image.shape[0], image.shape[1], 3))
            return images

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
        return not self._source.is_alive()

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
            while not self._source_stop.is_set():
                captured = monotonic()  # Local submission time, not camera exposure.
                result = ingress.submit(self._images).result.result()
                for camera, image in zip(result.cameras, self._images, strict=True):
                    if camera.frame_id is not None:
                        # Reuse the actual admitted identity; these fixed pixels
                        # equal the ingress copy. Store owns a separate display copy.
                        self._store.publish(
                            Frame(camera.frame_id, captured, image),
                            generation=self.generation,
                        )
                del result  # Keep no cycle/result history.
                if self._source_stop.wait(0.25):
                    return  # Human-visible cadence, not a throughput guarantee.
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


@dataclass
class LocalComposition:
    application: ProductionApplication
    store: LatestDisplayFrameStore
    choice: RecipeChoice
    editor: RecipeEditorSession


def compose(
    model_file: Path,
    machine_file: Path,
    recipe_file: Path,
    paths: Sequence[Path],
    *,
    engine_factory: EngineFactory = real_engine,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
) -> LocalComposition:
    """Parse/hash/decode before entering Qt; no fallback images or invented ROIs.

    Relative model paths are resolved against the model-description directory.
    Selected files follow configured camera order, not filename camera guesses.
    """
    if len(paths) != 6:
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
    images = tuple(decode(path) for path in paths)
    store = LatestDisplayFrameStore(prepared.configuration.layout.camera_ids)
    for camera, image in zip(
        prepared.configuration.layout.cameras, images, strict=True
    ):
        camera.validate_shape((image.shape[0], image.shape[1], 3))
        image.setflags(write=False)
        store.publish(
            Frame(FrameId(CycleId(0), camera.camera_id, 0), monotonic(), image),
            generation=0,
        )
    editor = RecipeEditorSession(
        RecipeCatalog(recipe_file.resolve().parent),
        machine,
        registry,
        model.model_id,
        model_file.resolve().parent,
    )

    def factory(config: PreparedConfiguration, generation: int) -> LocalRuntimeSession:
        session = LocalRuntimeSession(
            config, generation, app, images, store, engine_factory
        )
        observe_source(session.source_done)
        return session

    app = ProductionApplication(factory)
    app.configure(prepared)
    return LocalComposition(
        app, store, RecipeChoice(prepared, recipe.display_name), editor
    )


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--machine-config", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--frames-dir", type=Path, help="First six sorted files; no bad-file skipping"
    )
    selection.add_argument(
        "--images", nargs=6, type=Path, help="Six paths in configured camera order"
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--size", default="1080x1920")
    args = parser.parse_args()
    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
        if width < 880 or height < 720:
            raise ValueError("Minimum size is 880x720 logical pixels")
        paths = image_paths(args.frames_dir)[:6] if args.frames_dir else args.images
        composition = compose(
            args.model_config,
            args.machine_config,
            args.recipe,
            paths,
            observe_source=lambda future: delivery.watch("local_source", future),
        )
    except Exception:
        traceback.print_exc()
        return 1
    # Start cannot be requested until the gate/window exist. Initial artifact IO
    # above precedes Qt creation, and subsequent preparation uses the existing UI.
    qt = QApplication(sys.argv[:1])
    delivery = FutureDelivery()
    try:
        bridge = QtProductionBridge(composition.application, composition.store)
        SourceNotifications(bridge, delivery)  # QObject parent retains the receiver.
        window = ProductionWindow(
            bridge,
            (composition.choice,),
            language="zh" if args.language == "zh" else "en",
            recipe_editor=composition.editor,
        )
        window.setWindowTitle(
            "ViMo · Local files / real inference · 本地图片 / 真实推理"
        )
        window.resize(width, height)
        window.show()
        return qt.exec()
    finally:
        # Outside the GUI loop; never release ownership while native work lives.
        composition.application.close().result()
        composition.application.join()
        delivery.detach()


if __name__ == "__main__":
    raise SystemExit(main())
