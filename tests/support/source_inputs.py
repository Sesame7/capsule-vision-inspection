"""Qt-independent synthetic input files shared by local composition tests."""

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from capsule_vision.configuration import (
    CameraFolder,
    CameraSources,
    MachineLayoutDefinition,
    RecipeDefinition,
    save_camera_sources,
    save_machine_layout,
    save_model_definition,
    save_recipe,
)
from capsule_vision.inspection import CameraId
from tests.support.application import prepared


def input_files(
    folder: Path, *, camera_count: int = 6
) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    assert camera_count in (5, 6)
    config = prepared(folder).configuration
    model, machine, recipe = (
        folder / name for name in ("general/model.json", "machine.tsv", "local.json")
    )
    save_model_definition(model, config.model)
    save_machine_layout(
        machine,
        MachineLayoutDefinition(
            replace(config.layout, cameras=config.layout.cameras[:camera_count])
        ),
    )
    save_recipe(
        recipe, RecipeDefinition("local", "Local test", model_id=config.model.model_id)
    )
    paths: list[Path] = []
    for index in range(camera_count):
        path = folder / f"图像-{index}.png"
        ok, encoded = cv2.imencode(
            ".png", np.full((20, 60, 3), index + 1, dtype=np.uint8)
        )
        assert ok
        encoded.tofile(path)
        paths.append(path)
    return model, machine, recipe, tuple(paths)


def write_image(path: Path, value: int, shape: tuple[int, int] = (20, 60)) -> None:
    ok, data = cv2.imencode(".png", np.full((*shape, 3), value, dtype=np.uint8))
    assert ok
    data.tofile(path)


def folders(base: Path, *, loop: bool = True) -> CameraSources:
    rows: list[CameraFolder] = []
    for index in range(6):
        directory = base / f"independent-{index}"
        directory.mkdir()
        for number in range(1, 3 + index % 2):
            write_image(directory / f"unrelated{number}.png", 10 * index + number)
        rows.append(CameraFolder(CameraId(f"CAM{index + 1}"), directory.name))
    return CameraSources(tuple(reversed(rows)), loop=loop)


def replay_files(
    folder: Path, *, loop: bool = True, first_camera_images: int = 1
) -> tuple[Path, Path, Path]:
    model, machine, recipe, _ = input_files(folder)
    assignments: list[CameraFolder] = []
    for camera, length in enumerate((first_camera_images, 2, 3, 4, 2, 3), 1):
        directory = folder / f"CAM{camera}"
        directory.mkdir()
        for index in range(length):
            ok, encoded = cv2.imencode(
                ".png", np.full((20, 60, 3), camera * 10 + index, dtype=np.uint8)
            )
            assert ok
            encoded.tofile(directory / f"{index}.png")
        assignments.append(CameraFolder(CameraId(f"CAM{camera}"), directory.name))
    save_camera_sources(
        folder / "camera_sources.json",
        CameraSources(tuple(assignments), loop=loop, target_fps=1),
    )
    return model, machine, recipe
