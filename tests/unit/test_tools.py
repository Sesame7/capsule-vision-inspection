"""Maintained tool behavior without SDK, GPU or reference assets."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from _tool_helpers import image_paths, load_pool, statistics
from run_production_ui import demo_choices
from stress_inference import drift, memory_value
from visualize_inference import render, save_png

from vimo_demo.configuration import load_machine_layout, save_machine_layout
from vimo_demo.inference import BoundingRect, ImageResult, RotatedRect


def test_pool_order_decode_and_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    pool = load_pool(tmp_path, 2)
    assert [p.name for p, _ in pool] == ["A.PNG", "b.png"]
    assert np.array_equal(pool[0][1], pixels)
    assert "0-corrupt.png" in capsys.readouterr().err
    with pytest.raises(ValueError, match="Need 3 usable images; found 2"):
        load_pool(tmp_path, 3)


def test_statistics_exclude_incomplete_calls() -> None:
    stats = statistics([1.0, 2.0, 3.0, float("nan")])
    assert stats["calls"] == 3
    assert stats["mean_ms"] == stats["p50_ms"] == 2.0
    assert stats["p95_ms"] == pytest.approx(2.9)
    assert stats["calls_per_second"] == 500
    assert statistics([])["mean_ms"] is None
    trend = drift(np.arange(1, 101, dtype=np.float64))
    assert trend["first_10_percent"] == statistics(list(range(1, 11)))
    assert trend["middle_10_percent"] == statistics(list(range(46, 56)))
    assert trend["last_10_percent"] == statistics(list(range(91, 101)))


@pytest.mark.parametrize("value", ["N/A", "[N/A]", "nan", "-1"])
def test_unavailable_gpu_memory_is_not_zero(value: str) -> None:
    with pytest.raises(ValueError):
        memory_value(value)


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
    machine = demo_choices(images, tmp_path)[0].prepared.configuration.layout
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
    demo_choices(images, tmp_path)
    path = tmp_path / "machine.json"
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
    choices = demo_choices(different_images, tmp_path)
    assert choices[0].prepared.configuration.layout == custom.layout
    assert path.read_bytes() == payload
