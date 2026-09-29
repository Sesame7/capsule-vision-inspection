"""Maintained tool behavior without SDK, GPU or reference assets."""

from pathlib import Path

import numpy as np
import pytest
from _tool_helpers import image_paths, load_pool, statistics
from stress_inference import drift, memory_value
from visualize_inference import render, save_png

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
