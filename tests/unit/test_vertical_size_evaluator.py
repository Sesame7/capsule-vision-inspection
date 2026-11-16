"""Offline geometry and artifact correctness, with no benchmark thresholds."""

import csv
import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from evaluate_vertical_size import (
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
def test_known_spans_nonzero_origin_and_odd_view_height(horizontal: bool) -> None:
    image = np.zeros((135, 100, 3), dtype=np.uint8)
    roi = BoundingRect(7, 5, 80, 121)
    width, height = (30, 20) if horizontal else (20, 30)
    for y in (15, 75):
        image[y : y + height, 27 : 27 + width] = (0, 180, 180)
    before = image.copy()
    rows = evaluate_frame(image, camera(roi))
    accepted = measure_capsule(image, roi, SIZE_ONLY)
    for row, original in zip(rows[:2], accepted.views, strict=True):
        assert row.size == original
        body = row.size.body
        assert body is not None
        box = body.bounding_rect
        assert (box.width, box.height) == (width, height)
        x = 41.5 if horizontal else 36.5
        assert body.endpoints == ((x, box.y), (x, box.y + height - 1))
        assert body.length == height - 1
    assert rows[0].view_rect == BoundingRect(7, 5, 80, 60)
    assert rows[1].view_rect == BoundingRect(7, 65, 80, 61)
    np.testing.assert_array_equal(image, before)


def rotated_image() -> NDArray[np.uint8]:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    points = np.array(((20, 10), (50, 40), (44, 46), (14, 16)), dtype=np.int32)
    for y in (0, 60):
        cv2.fillConvexPoly(image, points + (0, y), (21, 0, 0))
    return image


def test_tilted_body_and_small_competitor() -> None:
    image = rotated_image()
    layout = camera(BoundingRect(0, 0, 80, 120))
    original = evaluate_frame(image, layout)
    # This speck is inside the axis-aligned box, outside the dominant component.
    image[10:12, 48:50] = (255, 255, 255)
    measured = evaluate_frame(image, layout)
    for before, row in zip(original, measured, strict=True):
        body = row.size.body
        assert body is not None and body == before.size.body
        offset = 0 if row.size.name == "upper" else 60
        assert body.bounding_rect == BoundingRect(14, 10 + offset, 37, 37)
        assert body.endpoints == ((32, 10 + offset), (32, 46 + offset))
        assert body.length == 36


@pytest.mark.parametrize("angle", [-75, -30, 15, 30, 60, 90])
def test_tilt_sweep_measures_rasterized_vertical_extent(angle: int) -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    polygon = np.rint(cv2.boxPoints(((40, 30), (40, 12), angle))).astype(np.int32)
    cv2.fillConvexPoly(image, polygon, (80, 120, 200))
    row = evaluate_frame(image, camera(BoundingRect(0, 0, 80, 120)))[0]
    body = row.size.body
    assert body is not None
    y, x = np.nonzero(image[:, :, 0])
    center_x = (int(x.min()) + int(x.max())) / 2
    assert body.endpoints == ((center_x, int(y.min())), (center_x, int(y.max())))
    assert body.length == int(y.max()) - int(y.min())


def test_exact_competitor_threshold_and_square_body_are_deterministic() -> None:
    image = bodies()
    image[10:25, 50:60] = 180  # 150 pixels, exactly 25% of the 600-pixel body.
    layout = camera(BoundingRect(0, 0, 80, 120))
    assert evaluate_frame(image, layout)[0].size.invalid_reason == "competing_bodies"
    image[10, 50] = 0
    assert evaluate_frame(image, layout)[0].size.body is not None
    image[:60] = 0
    image[10:30, 20:40] = 180
    first = evaluate_frame(image, layout)[0].size.body
    assert first is not None and first.length == 19
    assert evaluate_frame(image, layout)[0].size.body == first


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
    values = record("fixture", CameraId("CAM1"), row)
    assert values["vertical_length_px"] is values["start_y"] is values["end_y"] is None


def test_render_uses_the_exported_endpoints_and_preserves_pixels() -> None:
    image = rotated_image()
    layout = camera(BoundingRect(0, 0, 80, 120))
    rows = evaluate_frame(image, layout)
    overlay = render(image, layout, rows, synthetic=True)
    assert overlay.shape[0] > image.shape[0]
    np.testing.assert_array_equal(overlay[115, 70], image[115, 70])
    for row in rows[:2]:
        values = record("fixture", layout.camera_id, row)
        assert row.size.body is not None
        start, end = row.size.body.endpoints
        assert (values["start_x"], values["start_y"]) == start
        assert (values["end_x"], values["end_y"]) == end
        assert values["vertical_length_px"] == end[1] - start[1]
        for x, y in (start, end):
            np.testing.assert_array_equal(overlay[round(y), round(x)], ENDPOINT_COLOR)
    np.testing.assert_array_equal(image, rotated_image())


def test_cli_artifacts_sampling_and_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evaluate_vertical_size.OUTPUT_ROOT", tmp_path / "results")
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
    assert summary["geometry"]["vertical_length_px"] == (
        "Selected component's bounding height minus one"
    )
    assert all("benchmark" not in i for i in summary["images"])
    with (output / "measurements.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 36 and all(r["body_valid"] == "True" for r in exported)
    assert all(r["vertical_length_px"] == "29" for r in exported)
    assert (
        "major_px" not in exported[0] and "orientation_deg_clockwise" not in exported[0]
    )
    for item in summary["images"]:
        overlay = decode(output / item["overlay"])
        row = next(r for r in exported if r["image"] == item["image"])
        np.testing.assert_array_equal(
            overlay[round(float(row["start_y"])), round(float(row["start_x"]))],
            ENDPOINT_COLOR,
        )
    assert all(p.read_bytes() == content for p, content in before.items())
    assert main([*command, "--sample", "1", "--benchmark-repeats", "2"]) == 0
    timed = next(p for p in (tmp_path / "results").iterdir() if p != output)
    timed_summary = json.loads((timed / "summary.json").read_text(encoding="utf-8"))
    trial = timed_summary["images"][0]["benchmark"]
    assert trial["size_frame"]["samples"] == len(trial["trials_us"]) == 2
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"invalid")
    assert main(["--machine", str(path), "--input", "CAM1", str(corrupt)]) == 1
    with pytest.raises(SystemExit):
        main(["--machine", str(path), "--input", "CAM99", str(source)])


def test_saved_rois_are_never_clamped() -> None:
    with pytest.raises(ValueError, match="outside"):
        evaluate_frame(bodies(), camera(BoundingRect(1, 0, 80, 120)))
