"""Maintained tool behavior without SDK, GPU or reference assets."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from run_demo import compose, demo_files
from visualize_inference import decode, image_paths, render, save_png

from capsule_vision.configuration import (
    load_machine_layout,
    load_recipe,
    save_machine_layout,
    save_recipe,
)
from capsule_vision.inference import BoundingRect, ImageResult, RotatedRect


def test_pool_order_decode_and_failure(tmp_path: Path) -> None:
    pixels = np.zeros((5, 7, 3), dtype=np.uint8)
    save_png(tmp_path / "b.png", pixels)
    save_png(tmp_path / "A.PNG", pixels)
    (tmp_path / "0-corrupt.png").write_bytes(b"not an image")
    (tmp_path / "ignored.txt").write_text("ignored")
    assert [p.name for p in image_paths(tmp_path)] == [
        "0-corrupt.png",
        "A.PNG",
        "b.png",
    ]
    assert np.array_equal(decode(tmp_path / "A.PNG"), pixels)
    with pytest.raises(ValueError, match="Cannot decode"):
        decode(tmp_path / "0-corrupt.png")


def test_overlay_uses_actual_mask_id_without_moving_image() -> None:
    image = np.full((8, 9, 3), 100, dtype=np.uint8)
    mask = np.zeros((8, 9), dtype=np.uint8)
    mask[3, 4] = 17  # Intentionally not the reference model's ID.
    result = ImageResult(
        image_shape=(8, 9, 3),
        label_map={"黑点": 17},
        regions=(),
        mask=mask,
        roi=BoundingRect(0, 0, 9, 8),
        rotated_roi=RotatedRect(4, 3, 9, 8, 0),
        module_id="test",
    )
    overlay = render(image, result)
    assert overlay.shape[0] > image.shape[0]  # Legend below, no resizing.
    assert np.array_equal(overlay[3, 4], [62, 60, 145])  # 60% BGR + 40% (5,0,212).
    assert np.array_equal(overlay[2, 4], image[2, 4])
    assert np.all(image == 100)
    assert mask[3, 4] == 17


@pytest.mark.parametrize("width,height", [(1056, 960), (641, 37), (7, 3), (6, 1)])
def test_initial_machine_partitions_cover_every_pixel(
    tmp_path: Path, width: int, height: int
) -> None:
    # Distinct dimensions ensure each camera uses its own reference, not CAM1's.
    images = tuple(
        np.zeros((height + i, width + i, 3), dtype=np.uint8) for i in range(6)
    )
    machine = load_machine_layout(demo_files(images, tmp_path)[1]).layout
    assert machine.camera_ids == tuple(f"CAM{i}" for i in range(1, 7))
    for index, (camera, image) in enumerate(zip(machine.cameras, images, strict=True)):
        h, w = image.shape[:2]
        camera.validate_shape((h, w, 3))
        coverage = np.zeros((h, w), dtype=np.uint8)
        for slot, roi in enumerate(camera.slots):
            rect = roi.rect
            assert roi.slot == slot and roi.channel_id == index * 6 + slot + 1
            assert rect.y == 0 and rect.height == h and rect.width > 0
            assert rect.x >= 0 and rect.x + rect.width <= w
            coverage[:, rect.x : rect.x + rect.width] += 1
        assert np.all(coverage == 1)  # No missing or multiply covered pixels.
        assert (
            max(r.rect.width for r in camera.slots)
            - min(r.rect.width for r in camera.slots)
            <= 1
        )
    if width == 1056:
        assert [r.rect for r in machine.cameras[0].slots] == [
            BoundingRect(x, 0, 176, 960) for x in (0, 176, 352, 528, 704, 880)
        ]


def test_demo_seed_preserves_saved_machine_geometry(tmp_path: Path) -> None:
    images = tuple(np.zeros((20, 61, 3), dtype=np.uint8) for _ in range(6))
    demo_files(images, tmp_path)
    path = tmp_path / "machine.tsv"
    saved = load_machine_layout(path)
    first, *others = saved.layout.cameras
    slots = (replace(first.slots[0], rect=BoundingRect(3, 2, 4, 7)), *first.slots[1:])
    custom = replace(
        saved,
        layout=replace(saved.layout, cameras=(replace(first, slots=slots), *others)),
    )
    save_machine_layout(path, custom)
    payload = path.read_bytes()
    # A later launch with different reference dimensions must not re-seed it.
    different_images = tuple(np.zeros((40, 120, 3), dtype=np.uint8) for _ in range(6))
    files = demo_files(different_images, tmp_path)
    assert load_machine_layout(files[1]).layout == custom.layout
    assert path.read_bytes() == payload


def test_demo_seed_preserves_recipe_edits_and_alternate_model(tmp_path: Path) -> None:
    images = tuple(np.zeros((20, 60, 3), dtype=np.uint8) for _ in range(6))
    _, _, recipe, _ = demo_files(images, tmp_path)
    save_recipe(
        recipe,
        replace(
            load_recipe(recipe), display_name="Edited demo", model_id="synthetic-alt"
        ),
    )
    before = recipe.read_bytes()
    composition = compose(images, tmp_path, "normal", 0)
    try:
        assert composition.prepared.configuration.display_name == "Edited demo"
        assert composition.prepared.configuration.model.model_id == "synthetic-alt"
        assert len(composition.editor.catalog.discover().entries) == 2
        assert len(composition.editor.registry.models) == 2
        assert recipe.read_bytes() == before
    finally:
        composition.application.close().result(5)
        assert composition.application.join(5)


def test_demo_seed_rejects_changed_artifact_without_overwriting(tmp_path: Path) -> None:
    images = tuple(np.zeros((20, 60, 3), dtype=np.uint8) for _ in range(6))
    model, machine, _, _ = demo_files(images, tmp_path)
    artifact = model.with_name("model.vimosln")
    original_geometry = machine.read_bytes()
    artifact.write_bytes(b"Changed input; never repair it")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        demo_files(images, tmp_path)
    assert artifact.read_bytes() == b"Changed input; never repair it"
    assert machine.read_bytes() == original_geometry
