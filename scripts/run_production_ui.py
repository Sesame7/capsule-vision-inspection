"""Equipment-free production UI: real application/runtime, explicitly fake engine.

All simulation below is launcher-only; no business state is copied into widgets.
"""

import argparse
import hashlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication

from vimo_demo.application import (
    ApplicationState,
    PreparedConfiguration,
    ProductionApplication,
    ProductionRuntimeSession,
    prepare_configuration,
)
from vimo_demo.configuration import (
    CameraOverride,
    ChannelOverride,
    CheckConfiguration,
    CheckOverrides,
    MachineLayoutDefinition,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    ViMoOverride,
    load_machine_layout,
    resolve_configuration,
    save_machine_layout,
    save_recipe,
)
from vimo_demo.inference import BoundingRect, DefectRegion, ImageResult, RotatedRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.production_bridge import QtProductionBridge, RecipeChoice
from vimo_demo.ui.production_style import Language
from vimo_demo.ui.production_window import ProductionWindow
from vimo_demo.ui.recipe_catalog import RecipeCatalog
from vimo_demo.ui.recipe_editor import RecipeEditorSession

type Scenario = Literal["normal", "ng", "warning", "fault"]


class DemoTerminalFailure(BaseException):
    """Explicit fake worker-terminal failure for FAULT presentation review."""


def demo_recipes() -> tuple[RecipeDefinition, ...]:
    defaults = CheckConfiguration()
    return (
        RecipeDefinition("demo-A", "Demo A", defaults),
        RecipeDefinition(
            "demo-B",
            "Demo B",
            defaults,
            camera_overrides=(
                CameraOverride(
                    CameraId("CAM3"), CheckOverrides(vimo=ViMoOverride(required=False))
                ),
            ),
            channel_overrides=(
                ChannelOverride(
                    ChannelId(17), CheckOverrides(vimo=ViMoOverride(required=True))
                ),
            ),
        ),
    )


class DemoEngine:
    def __init__(self, scenario: Scenario, warmup_calls: int, delay: float) -> None:
        self._scenario = scenario
        self._warmup_calls = warmup_calls
        self._calls = 0
        self._delay = delay
        self._wait = Event()

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        self._calls += 1
        # Deliberate fake latency makes STARTING/STOPPING inspectable; no GUI wait.
        self._wait.wait(self._delay)
        production_call = self._calls - self._warmup_calls
        if production_call > 0 and self._scenario == "fault":
            raise DemoTerminalFailure("Simulated terminal engine failure")
        if (
            production_call > 0
            and production_call % 6 == 1
            and self._scenario == "warning"
        ):
            raise ValueError("Simulated image processing failure")
        height, width = image_bgr.shape[:2]
        rect = BoundingRect(
            width // 3, height // 3, max(2, width // 16), max(2, height // 12)
        )
        rotated = RotatedRect(width / 2, height / 2, width, height, 0)
        regions = ()
        if production_call > 0 and production_call % 6 == 3 and self._scenario == "ng":
            regions = (
                DefectRegion(
                    "demo-defect",
                    rect.width * rect.height,
                    rect,
                    rect.height,
                    rect.width,
                    (),
                    (),
                    rotated,
                ),
            )
        mask = np.zeros((height, width), dtype=np.uint8)
        mask.setflags(write=False)
        return ImageResult(
            (height, width, 3),
            {"demo-defect": 1},
            regions,
            mask,
            BoundingRect(0, 0, width, height),
            rotated,
            "demo",
        )

    def close(self) -> None:
        self._wait.wait(self._delay)


def demo_images(directory: Path | None = None) -> tuple[NDArray[np.uint8], ...]:
    if directory is not None:
        if not directory.is_dir():
            raise ValueError("Image directory does not exist")
        images: list[NDArray[np.uint8]] = []
        for path in sorted(
            directory.iterdir(), key=lambda p: (p.name.casefold(), p.name)
        ):
            if path.is_file() and path.suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".bmp",
                ".tif",
                ".tiff",
            }:
                image = cv2.imdecode(
                    np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if image is None or image.shape[0] < 20 or image.shape[1] < 60:
                    raise ValueError(f"Cannot use image: {path.name}")
                images.append(cast(NDArray[np.uint8], image))
                if len(images) == 6:
                    return tuple(images)
        raise ValueError("Six usable images are required")
    result: list[NDArray[np.uint8]] = []
    for index in range(6):
        image = np.zeros((580, 640, 3), dtype=np.uint8)
        # Two synthetic views per channel, solely for equipment-free tuning review.
        for slot in range(6):
            left, right = slot * 640 // 6, (slot + 1) * 640 // 6
            center = (left + right) // 2
            for y in (145, 435):
                cv2.ellipse(
                    image,
                    (center, y),
                    (30 + slot, 90),
                    0,
                    0,
                    360,
                    (15, 180 + index * 5, 200),
                    -1,
                )
        result.append(image)
    return tuple(result)


def demo_choices(
    images: tuple[NDArray[np.uint8], ...], folder: Path
) -> tuple[RecipeChoice, ...]:
    """Tiny known fake artifact supports real Phase 2C validation without an SDK."""
    folder.mkdir(parents=True, exist_ok=True)
    artifact = folder / "synthetic-model.fixture"
    payload = b"Phase 2D UI demo only; not a ViMo model.\n"
    if not artifact.exists():
        artifact.write_bytes(payload)
    elif artifact.read_bytes() != payload:
        raise ValueError("Unexpected demo artifact; refusing to overwrite")
    model = ModelDefinition(
        "synthetic",
        "Synthetic",
        str(artifact.resolve()),
        hashlib.sha256(payload).hexdigest(),
        "demo",
    )
    machine_path = folder / "machine.json"
    if machine_path.exists():
        machine = load_machine_layout(machine_path)
    else:
        # Initialization only: saved machine geometry is always authoritative.
        cameras: list[CameraLayout] = []
        for index, image in enumerate(images):
            height, width = image.shape[:2]
            cameras.append(
                CameraLayout(
                    CameraId(f"CAM{index + 1}"),
                    tuple(
                        ChannelROI(
                            slot,
                            ChannelId(index * 6 + slot + 1),
                            BoundingRect(
                                slot * width // 6,
                                0,
                                (slot + 1) * width // 6 - slot * width // 6,
                                height,
                            ),
                        )
                        for slot in range(6)
                    ),
                )
            )
        machine = MachineLayoutDefinition(InspectionLayout(tuple(cameras)))
        save_machine_layout(machine_path, machine)
    return tuple(
        RecipeChoice(
            prepare_configuration(
                resolve_configuration(
                    machine,
                    recipe,
                    ModelRegistry((model,)),
                    model.model_id,
                ),
                base_directory=folder,
            ),
            recipe.display_name,
        )
        for recipe in demo_recipes()
    )


class DemoSource:
    """One bounded source cycle at a time. UI rendering never drives admission."""

    def __init__(
        self,
        app: ProductionApplication,
        store: LatestDisplayFrameStore,
        images: tuple[NDArray[np.uint8], ...],
    ) -> None:
        self._app, self._store, self._images = app, store, images
        self._stop = Event()
        self._thread = Thread(
            target=self._run, name="ProductionDemoSource", daemon=False
        )
        self._thread.start()

    def _run(self) -> None:
        cycle = 0
        while not self._stop.wait(0.25):
            snapshot = self._app.snapshot()
            if snapshot.state is ApplicationState.CLOSED:
                return
            if snapshot.state is not ApplicationState.RUNNING:
                continue
            cycle += 1
            try:
                receipt = self._app.begin_cycle(CycleId(cycle))
                receipt.accepted.result()
                for camera, image in zip(
                    self._store.cameras, self._images, strict=True
                ):
                    frame = Frame(
                        FrameId(CycleId(cycle), camera, cycle),
                        monotonic(),
                        image.copy(),
                    )
                    self._app.submit_frame(frame).result()
                    self._store.publish(frame, generation=snapshot.generation)
                receipt.result.result()
            except BaseException:
                # Expected demo stop/overload/fault. State/error stays application-owned.
                # No retry of this cycle and no widgets/signals from this source.
                continue

    def close(self) -> None:
        self._stop.set()
        self._thread.join()  # Launcher finally, after application/thread exit; not GUI work.


@dataclass
class DemoComposition:
    application: ProductionApplication
    store: LatestDisplayFrameStore
    choices: tuple[RecipeChoice, ...]
    source: DemoSource
    editor: RecipeEditorSession


def compose(
    images: tuple[NDArray[np.uint8], ...],
    folder: Path,
    scenario: Scenario,
    delay: float,
) -> DemoComposition:
    choices = demo_choices(images, folder)
    shapes = {image.shape for image in images}

    def factory(
        prepared: PreparedConfiguration, generation: int
    ) -> ProductionRuntimeSession:
        return ProductionRuntimeSession(
            prepared,
            generation,
            warmup=lambda _: images,
            engine_factory=lambda _: DemoEngine(scenario, 2 * len(shapes), delay),
            cycle_timeout=30,
        )

    store = LatestDisplayFrameStore(choices[0].prepared.configuration.layout.camera_ids)
    for camera, image in zip(store.cameras, images, strict=True):
        store.publish(
            Frame(FrameId(CycleId(0), camera, 0), monotonic(), image.copy()),
            generation=0,
        )
    directory = folder / "recipes"
    directory.mkdir(parents=True, exist_ok=True)
    for recipe in demo_recipes():
        path = directory / f"{recipe.recipe_id}.recipe.json"
        if not path.exists():
            save_recipe(path, recipe)
    model = choices[0].prepared.configuration.model
    editor = RecipeEditorSession(
        RecipeCatalog(directory),
        MachineLayoutDefinition(choices[0].prepared.configuration.layout),
        ModelRegistry(
            (
                model,
                replace(
                    model,
                    model_id="synthetic-alt",
                    display_name="Synthetic alternative",
                ),
            )
        ),
        model.model_id,
        folder,
        machine_path=folder / "machine.json",
    )
    app = ProductionApplication(factory)
    app.configure(choices[0].prepared)
    return DemoComposition(app, store, choices, DemoSource(app, store, images), editor)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Production UI with simulated inference; no GPU, camera or PLC"
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--size", default="1080x1920")
    parser.add_argument("--frames-dir", type=Path)
    parser.add_argument(
        "--scenario", choices=("normal", "ng", "warning", "fault"), default="normal"
    )
    args = parser.parse_args()
    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
        if width < 880 or height < 720:
            raise ValueError("Minimum size is 880x720 logical pixels")
        images = demo_images(args.frames_dir)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    language: Language = "zh" if args.language == "zh" else "en"
    scenario = cast(Scenario, args.scenario)  # argparse choices validated above.
    composition = compose(images, Path("outputs/production_ui_demo"), scenario, 0.3)
    application = QApplication(sys.argv[:1])
    bridge = QtProductionBridge(composition.application, composition.store)
    window = ProductionWindow(
        bridge,
        composition.choices,
        language=language,
        simulation=True,
        recipe_editor=composition.editor,
    )
    window.resize(width, height)
    window.show()
    try:
        return application.exec()
    finally:
        # Qt normally exits only after deferred Close; retain safe ownership even
        # if an external caller exits the event loop directly.
        composition.application.close().result()
        composition.application.join()
        composition.source.close()


if __name__ == "__main__":
    raise SystemExit(main())
