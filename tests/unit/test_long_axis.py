"""Offline geometry and artifact correctness, with no benchmark thresholds."""

import csv
import json
import math
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from evaluate_long_axis import (
    ENDPOINT_COLOR,
    SIZE_ONLY,
    decode,
    evaluate_frame,
    main,
    record,
    render,
)
from numpy.typing import NDArray
from tests.support.capsule_images import bodies
from tests.support.configuration import machine
from visualize_inference import save_png

from capsule_vision.configuration import save_machine_layout
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import CameraLayout, ChannelId, ChannelROI
from capsule_vision.inspection.production.capsule import measure_capsule


def camera(roi: BoundingRect) -> CameraLayout:
    # Explicit identical fixture ROIs exercise all six configured ChannelIds.
    return CameraLayout(
        CameraId("CAM1"), tuple(ChannelROI(i, ChannelId(i + 1), roi) for i in range(6))
    )


@pytest.mark.parametrize("horizontal", [False, True])
def test_known_axes_nonzero_origin_and_odd_view_height(horizontal: bool) -> None:
    image = np.zeros((135, 100, 3), dtype=np.uint8)
    roi = BoundingRect(7, 5, 80, 121)
    width, height = (30, 20) if horizontal else (20, 30)
    for y in (15, 75):
        image[y : y + height, 27 : 27 + width] = (0, 180, 180)
    before = image.copy()
    rows = evaluate_frame(image, camera(roi))
    accepted = measure_capsule(image, roi, SIZE_ONLY)
    for row, original in zip(rows[:2], accepted.views, strict=True):
        assert row.size == original and row.axis_issue is None
        axis = row.axis
        assert axis is not None
        assert axis.major == pytest.approx(29)
        assert axis.minor == pytest.approx(19)
        assert row.size.body is not None
        box = row.size.body.bounding_rect
        assert (box.width, box.height) == (width, height)
        expected = (
            ((27, box.y + 9.5), (56, box.y + 9.5))
            if horizontal
            else ((36.5, box.y), (36.5, box.y + 29))
        )
        np.testing.assert_allclose(sorted(axis.endpoints), expected, atol=1e-5)
        assert math.dist(*axis.endpoints) == pytest.approx(axis.major, abs=1e-5)
        assert axis.orientation_degrees == pytest.approx(
            0 if horizontal else 90, abs=1e-5
        )
        assert all(
            any(
                np.allclose(point, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
                for a, b in zip(
                    axis.corners, (*axis.corners[1:], axis.corners[0]), strict=True
                )
            )
            for point in axis.endpoints
        )
    assert rows[0].view_rect == BoundingRect(7, 5, 80, 60)
    assert rows[1].view_rect == BoundingRect(7, 65, 80, 61)
    np.testing.assert_array_equal(image, before)


def rotated_image() -> NDArray[np.uint8]:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    points = np.array(((20, 10), (50, 40), (44, 46), (14, 16)), dtype=np.int32)
    for y in (0, 60):
        cv2.fillConvexPoly(image, points + (0, y), (21, 0, 0))
    return image


def test_rotated_known_rectangle_and_small_competitor() -> None:
    image = rotated_image()
    layout = camera(BoundingRect(0, 0, 80, 120))
    original = evaluate_frame(image, layout)
    # This speck is inside the axis-aligned box, outside the dominant component.
    image[10:12, 48:50] = (255, 255, 255)
    measured = evaluate_frame(image, layout)
    for before, row in zip(original, measured, strict=True):
        axis = row.axis
        assert axis is not None and axis == before.axis
        assert row.contours == before.contours
        assert axis.major == pytest.approx(math.sqrt(1800), abs=1e-5)
        assert axis.minor == pytest.approx(math.sqrt(72), abs=1e-5)
        assert axis.orientation_degrees == pytest.approx(45, abs=1e-5)
        offset = 0 if row.size.name == "upper" else 60
        np.testing.assert_allclose(
            axis.endpoints, ((17, 13 + offset), (47, 43 + offset)), atol=1e-5
        )
        assert row.size.body is not None and row.size.body.bounding_rect.width == 37


@pytest.mark.parametrize("angle", [-75, -30, 15, 30, 60, 90])
def test_rotation_sweep_with_pixel_rasterization_tolerance(angle: int) -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    polygon = np.rint(cv2.boxPoints(((40, 30), (40, 12), angle))).astype(np.int32)
    cv2.fillConvexPoly(image, polygon, (80, 120, 200))
    row = evaluate_frame(image, camera(BoundingRect(0, 0, 80, 120)))[0]
    axis = row.axis
    assert axis is not None
    # Vertices are rounded to pixels, followed by discrete edge filling.
    assert axis.major == pytest.approx(40, abs=2)
    assert axis.minor == pytest.approx(12, abs=2)
    difference = (axis.orientation_degrees - angle + 90) % 180 - 90
    assert abs(difference) <= 2.5
    assert math.dist(*axis.endpoints) == pytest.approx(axis.major, abs=1e-5)


def test_exact_competitor_threshold_and_square_tie_are_deterministic() -> None:
    image = bodies()
    image[10:25, 50:60] = 180  # 150 pixels, exactly 25% of the 600-pixel body.
    layout = camera(BoundingRect(0, 0, 80, 120))
    assert evaluate_frame(image, layout)[0].size.invalid_reason == "competing_bodies"
    image[10, 50] = 0
    assert evaluate_frame(image, layout)[0].axis is not None
    image[:60] = 0
    image[10:30, 20:40] = 180
    first = evaluate_frame(image, layout)[0]
    assert first.axis is not None and first.axis.major == first.axis.minor == 19
    assert evaluate_frame(image, layout)[0].axis == first.axis


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing", "missing_body"),
        ("competing", "competing_bodies"),
        ("top", "truncated_body"),
        ("left", "truncated_body"),
        ("right", "truncated_body"),
        ("thin", "invalid_geometry"),
    ],
)
def test_accepted_invalidity_reason_is_preserved(kind: str, reason: str) -> None:
    image = bodies()
    if kind == "missing":
        image[:60] = 20  # The inclusive Size background threshold.
    elif kind == "competing":
        image[10:40, 50:70] = 180
    elif kind == "thin":
        image[:60] = 0
        image[10:40, 20:22] = 180
    else:
        slices = {
            "top": (slice(0, 10), slice(20, 40)),
            "bottom": (slice(40, 60), slice(20, 40)),
            "left": (slice(10, 40), slice(0, 20)),
            "right": (slice(10, 40), slice(40, 80)),
        }
        image[slices[kind]] = 180
    roi = BoundingRect(0, 0, 80, 120)
    original = measure_capsule(image, roi, SIZE_ONLY)
    row = evaluate_frame(image, camera(roi))[0]
    assert row.size == original.views[0]
    assert row.size.invalid_reason == reason
    assert row.axis is row.axis_issue is row.axis_us is None


def test_degenerate_rotated_body_is_explicitly_invalid() -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    cv2.line(image, (10, 10), (39, 39), (255, 255, 255), 1)
    row = evaluate_frame(image, camera(BoundingRect(0, 0, 80, 120)))[0]
    assert row.size.body is None and row.size.invalid_reason == "invalid_geometry"
    assert row.axis is row.axis_issue is None
    assert not row.contours


def test_render_uses_the_exported_endpoints_and_preserves_pixels() -> None:
    image = rotated_image()
    layout = camera(BoundingRect(0, 0, 80, 120))
    rows = evaluate_frame(image, layout)
    overlay = render(image, layout, rows, synthetic=True)
    assert overlay.shape[0] > image.shape[0]
    np.testing.assert_array_equal(overlay[115, 70], image[115, 70])
    for row in rows[:2]:
        values = record("fixture", layout.camera_id, row)
        assert row.axis is not None
        assert (values["start_x"], values["start_y"]) == row.axis.endpoints[0]
        assert (values["end_x"], values["end_y"]) == row.axis.endpoints[1]
        for x, y in row.axis.endpoints:
            np.testing.assert_array_equal(overlay[round(y), round(x)], ENDPOINT_COLOR)
    np.testing.assert_array_equal(image, rotated_image())


def test_cli_artifacts_sampling_and_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evaluate_long_axis.OUTPUT_ROOT", tmp_path / "results")
    model = machine()
    layout = replace(
        model.layout,
        cameras=tuple(
            replace(
                c,
                slots=tuple(
                    replace(slot, rect=BoundingRect(0, 0, 80, 120)) for slot in c.slots
                ),
            )
            for c in model.layout.cameras
        ),
    )
    path = tmp_path / "machine.tsv"
    save_machine_layout(path, replace(model, layout=layout))
    source = tmp_path / "images"
    source.mkdir()
    for i in range(5):
        save_png(source / f"{i}.png", bodies())
    before = {p: p.read_bytes() for p in source.iterdir()}
    command = ["--machine", str(path), "--input", "CAM1", str(source), "--synthetic"]
    assert main(command) == 0
    output = next((tmp_path / "results").iterdir())
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["evidence"] == "synthetic"
    assert [Path(i["image"]).name for i in summary["images"]] == [
        "0.png",
        "2.png",
        "4.png",
    ]
    assert summary["views"] == 36
    assert all("benchmark" not in i for i in summary["images"])
    with (output / "measurements.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 36 and all(r["body_valid"] == "True" for r in exported)
    for item in summary["images"]:
        overlay = decode(output / item["overlay"])
        row = next(r for r in exported if r["image"] == item["image"])
        np.testing.assert_array_equal(
            overlay[round(float(row["start_y"])), round(float(row["start_x"]))],
            ENDPOINT_COLOR,
        )
    assert all(p.read_bytes() == content for p, content in before.items())
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"invalid")
    assert main(["--machine", str(path), "--input", "CAM1", str(corrupt)]) == 1
    with pytest.raises(SystemExit):
        main(["--machine", str(path), "--input", "CAM99", str(source)])


def test_saved_rois_are_never_clamped() -> None:
    with pytest.raises(ValueError, match="outside"):
        evaluate_frame(bodies(), camera(BoundingRect(1, 0, 80, 120)))
