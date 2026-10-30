"""Equipment-free production UI: real application/runtime, explicitly fake engine.

All simulation below is launcher-only; no business state is copied into widgets.
"""

import argparse
import hashlib
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from typing import Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray
from PySide6.QtWidgets import QApplication

from capsule_vision.application.composition import EngineeringComposition
from capsule_vision.application.composition import compose as compose_application
from capsule_vision.configuration import (
    CheckConfiguration,
    ModelDefinition,
    ModelRegistry,
    RecipeDefinition,
    load_machine_layout,
    load_model_definition,
    load_recipe,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from capsule_vision.configuration.drafts import MachineLayoutDraft
from capsule_vision.inference import (
    BoundingRect,
    DefectRegion,
    ImageResult,
    RotatedRect,
)
from capsule_vision.inference._validation import uint8_array
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow
from capsule_vision.ui.production_bridge import QtProductionBridge
from capsule_vision.ui.source_notifications import SourceNotifications

type Scenario = Literal["normal", "ng", "warning", "fault"]


class DemoTerminalFailure(BaseException):
    """Explicit fake worker-terminal failure for FAULT presentation review."""


def demo_recipes() -> tuple[RecipeDefinition, ...]:
    defaults = CheckConfiguration()
    return (
        RecipeDefinition("demo-A", "Demo A", defaults, model_id="synthetic"),
        RecipeDefinition(
            "demo-B",
            "Demo B",
            defaults,
            model_id="synthetic",
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


def demo_files(
    images: tuple[NDArray[np.uint8], ...], folder: Path
) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    """Seed only absent fixtures; existing geometry and user definitions stay intact."""
    if len(images) != 6:
        raise ValueError("Six demo images are required")
    images = tuple(uint8_array(image) for image in images)
    folder.mkdir(parents=True, exist_ok=True)
    payload = b"ViMo UI demo fixture; not a ViMo model.\n"
    digest = hashlib.sha256(payload).hexdigest()
    for identity, name in (
        ("synthetic", "Synthetic"),
        ("synthetic-alt", "Synthetic alternative"),
    ):
        directory = folder / identity
        directory.mkdir(exist_ok=True)
        artifact = directory / "model.vimosln"
        if not artifact.exists():
            with artifact.open("xb") as stream:
                stream.write(payload)
        elif artifact.read_bytes() != payload:
            raise ValueError(
                f"Unexpected demo artifact; refusing to overwrite {artifact}"
            )
        metadata = directory / "model.json"
        if not metadata.exists():
            save_model_definition(
                metadata,
                ModelDefinition(
                    identity, name, str(artifact.resolve()), digest, "demo"
                ),
            )
        model = load_model_definition(metadata)
        if model.expected_sha256 != digest or model.module_id != "demo":
            raise ValueError(f"Unexpected demo model definition: {metadata}")

    machine_path = folder / "machine.tsv"
    if not machine_path.exists():
        draft = MachineLayoutDraft(None, machine_path)
        for camera, image in zip(draft.camera_ids, images, strict=True):
            draft.initialize(camera, image.shape[1], image.shape[0])
        save_machine_layout(machine_path, draft.definition)
    machine = load_machine_layout(machine_path)
    directory = folder / "recipes"
    directory.mkdir(exist_ok=True)
    for recipe in demo_recipes():
        path = directory / f"{recipe.recipe_id}.json"
        if not path.exists():
            save_recipe(path, recipe, exclusive=True)
    recipe_file = directory / "demo-A.json"
    selected = load_recipe(recipe_file).model_id
    if selected not in ("synthetic", "synthetic-alt"):
        raise ValueError("Demo Recipes require a synthetic Model")

    frames = folder / "frames"
    frames.mkdir(exist_ok=True)
    paths: list[Path] = []
    for camera, image in zip(
        machine.layout.camera_ids, images[: len(machine.layout.cameras)], strict=True
    ):
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("Cannot encode demo input")
        pixels = encoded.tobytes()
        path = frames / f"{camera}-{hashlib.sha256(pixels).hexdigest()}.png"
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(pixels)
        elif path.read_bytes() != pixels:
            raise ValueError(f"Unexpected demo image; refusing to overwrite {path}")
        paths.append(path)
    return folder / selected / "model.json", machine_path, recipe_file, tuple(paths)


def demo_model_labels(
    path: Path, *, module_id: str, expected_sha256: str
) -> tuple[str, ...]:
    if (
        module_id != "demo"
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
    ):
        raise ValueError("Demo model identity changed")
    return ("demo-defect",)


def compose(
    images: tuple[NDArray[np.uint8], ...],
    folder: Path,
    scenario: Scenario,
    delay: float,
    *,
    observe_source: Callable[[Future[None]], None] = lambda _: None,
) -> EngineeringComposition:
    model, machine, recipe, paths = demo_files(images, folder)
    shapes = {image.shape for image in images[: len(paths)]}
    composition = compose_application(
        model,
        machine,
        recipe,
        paths,
        engine_factory=lambda _: DemoEngine(scenario, 2 * len(shapes), delay),
        preparation_factory=lambda _: DemoEngine("normal", 0, delay),
        observe_source=observe_source,
    )
    try:
        composition.editor.registry = ModelRegistry(
            tuple(
                load_model_definition(folder / identity / "model.json")
                for identity in ("synthetic", "synthetic-alt")
            )
        )
        return composition
    except BaseException:
        composition.application.close().result()
        composition.application.join()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Production UI with simulated inference; no GPU, camera or PLC"
    )
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    parser.add_argument("--size", default="1080x1920")
    parser.add_argument("--frames-dir", type=Path)
    parser.add_argument(
        "--scenario", choices=("normal", "ng", "warning", "fault"), default="normal"
    )
    args = parser.parse_args(argv)
    try:
        width, height = (int(n) for n in args.size.lower().split("x"))
        if width < 880 or height < 720:
            raise ValueError("Minimum size is 880x720 logical pixels")
        images = demo_images(args.frames_dir)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    language: Language = "zh" if args.language == "zh" else "en"
    scenario = cast(Scenario, args.scenario)  # argparse choices validated above.
    delivery: FutureDelivery | None = None

    def source_finished(future: Future[None]) -> None:
        assert delivery is not None
        delivery.watch("folder_source", future)

    composition = compose(
        images,
        Path("outputs/production_ui_demo"),
        scenario,
        0.3,
        observe_source=source_finished,
    )
    window: MainWindow | None = None
    try:
        application = QApplication(sys.argv[:1])
        delivery = FutureDelivery()
        bridge = QtProductionBridge(composition.application, composition.store)
        SourceNotifications(bridge, delivery)
        window = MainWindow(
            bridge,
            language=language,
            recipe_editor=composition.editor,
            label_reader=demo_model_labels,
        )

        def identify_demo(_language: str = "") -> None:
            window.setWindowTitle(
                f"Capsule Vision Inspection · {text('simulation', window.language)}"
            )

        window.language_changed.connect(identify_demo)
        identify_demo()
        window.resize(width, height)
        window.show()
        return application.exec()
    finally:
        try:
            if window is not None:
                window.finish_close()
        finally:
            # The shared Application owns its source and native-worker termination.
            try:
                composition.application.close().result()
                composition.application.join()
            finally:
                if delivery is not None:
                    delivery.detach()


if __name__ == "__main__":
    raise SystemExit(main())
