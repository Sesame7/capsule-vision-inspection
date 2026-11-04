"""Offline Size-body long-axis evaluation in original image coordinates."""

import argparse
import csv
import hashlib
import math
import platform
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns

import cv2
import numpy as np
from numpy.typing import NDArray
from visualize_inference import positive_int, save_png, write_json

from capsule_vision.application.folder_replay import folder_images
from capsule_vision.configuration import load_machine_layout
from capsule_vision.inference import BoundingRect
from capsule_vision.inference.models import Contour, Point
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import CameraLayout, ChannelId
from capsule_vision.inspection.production.capsule import (
    BodyMeasurement,
    CapsuleSettings,
    SizeSettings,
    ViewMeasurement,
    measure_capsule,
)

OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "outputs" / "LAX-2"
SIZE_ONLY = CapsuleSettings(size=SizeSettings(enabled=True))
AXIS_COLOR = (255, 80, 255)
ENDPOINT_COLOR = (0, 255, 255)


@dataclass(frozen=True, slots=True)
class LongAxis:
    major: float
    minor: float
    endpoints: tuple[Point, Point]
    corners: tuple[Point, ...]
    orientation_degrees: float


@dataclass(frozen=True, slots=True)
class EvaluatedView:
    channel_id: ChannelId
    view_rect: BoundingRect
    size: ViewMeasurement
    axis: LongAxis | None
    axis_issue: str | None
    contours: tuple[Contour, ...]
    size_pair_us: float
    axis_us: float | None


def body_axis(
    image: NDArray[np.uint8], body: BodyMeasurement
) -> tuple[LongAxis | None, str | None, tuple[Contour, ...]]:
    """Present Production's authoritative scalar geometry; recover contours only here."""
    rect = body.bounding_rect
    crop = image[rect.y : rect.y + rect.height, rect.x : rect.x + rect.width]
    # Visualization-only contour recovery; it never determines axis or validity.
    foreground = cv2.bitwise_not(cv2.inRange(crop, (0, 0, 0), (20, 20, 20)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        foreground, connectivity=8
    )
    if count < 2:
        return None, "component_unavailable", ()
    dominant = int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1
    if tuple(int(v) for v in stats[dominant, :4]) != (0, 0, rect.width, rect.height):
        return None, "component_mismatch", ()
    selected = cv2.inRange(labels, dominant, dominant)
    contours, _ = cv2.findContours(selected, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    original_contours = tuple(
        tuple(
            (float(p[0]) + rect.x, float(p[1]) + rect.y) for p in contour.reshape(-1, 2)
        )
        for contour in contours
    )
    start, end = body.endpoints
    dx, dy = end[0] - start[0], end[1] - start[1]
    nx, ny = (
        -dy / body.length * body.minor_length / 2,
        dx / body.length * body.minor_length / 2,
    )
    corners = (
        (start[0] - nx, start[1] - ny),
        (end[0] - nx, end[1] - ny),
        (end[0] + nx, end[1] + ny),
        (start[0] + nx, start[1] + ny),
    )
    orientation = math.degrees(math.atan2(dy, dx)) % 180
    return (
        LongAxis(body.length, body.minor_length, body.endpoints, corners, orientation),
        None,
        original_contours,
    )


def evaluate_frame(
    image: NDArray[np.uint8], camera: CameraLayout
) -> tuple[EvaluatedView, ...]:
    camera.validate_shape((image.shape[0], image.shape[1], image.shape[2]))
    rows: list[EvaluatedView] = []
    for slot in camera.slots:
        started = perf_counter_ns()
        measured = measure_capsule(image, slot.rect, SIZE_ONLY)
        pair_us = (perf_counter_ns() - started) / 1000
        middle = slot.rect.height // 2
        for view, offset, height in zip(
            measured.views,
            (0, middle),
            (middle, slot.rect.height - middle),
            strict=True,
        ):
            axis, issue, elapsed = None, None, None
            contours: tuple[Contour, ...] = ()
            if view.body is not None:
                started = perf_counter_ns()
                axis, issue, contours = body_axis(image, view.body)
                elapsed = (perf_counter_ns() - started) / 1000
            rows.append(
                EvaluatedView(
                    slot.channel_id,
                    BoundingRect(
                        slot.rect.x, slot.rect.y + offset, slot.rect.width, height
                    ),
                    view,
                    axis,
                    issue,
                    contours,
                    pair_us,
                    elapsed,
                )
            )
    return tuple(rows)


def record(image_id: str, camera_id: CameraId, row: EvaluatedView) -> dict[str, object]:
    body = row.size.body.bounding_rect if row.size.body is not None else None
    axis = row.axis
    return {
        "image": image_id,
        "camera": camera_id,
        "channel_id": row.channel_id,
        "view": row.size.name,
        "view_x": row.view_rect.x,
        "view_y": row.view_rect.y,
        "view_width": row.view_rect.width,
        "view_height": row.view_rect.height,
        "body_valid": body is not None,
        "body_reason": row.size.invalid_reason,
        "bbox_x": body.x if body else None,
        "bbox_y": body.y if body else None,
        "bbox_width": body.width if body else None,
        "bbox_height": body.height if body else None,
        "axis_reason": row.axis_issue,
        "major_px": axis.major if axis else None,
        "minor_px": axis.minor if axis else None,
        "start_x": axis.endpoints[0][0] if axis else None,
        "start_y": axis.endpoints[0][1] if axis else None,
        "end_x": axis.endpoints[1][0] if axis else None,
        "end_y": axis.endpoints[1][1] if axis else None,
        "orientation_deg_clockwise": axis.orientation_degrees if axis else None,
        "size_pair_us": row.size_pair_us,
        "axis_extra_us": row.axis_us,
    }


def fixed_point(point: Point) -> tuple[int, int]:
    """OpenCV shift=8; at most 1/512 pixel rasterization rounding per coordinate."""
    return round(point[0] * 256), round(point[1] * 256)


def render(
    image: NDArray[np.uint8],
    camera: CameraLayout,
    rows: tuple[EvaluatedView, ...],
    *,
    synthetic: bool,
) -> NDArray[np.uint8]:
    """Original image stays at (0,0), with readable measurements appended below."""
    width = max(image.shape[1], 960)
    canvas = np.full((image.shape[0] + 372, width, 3), 24, dtype=np.uint8)
    canvas[: image.shape[0], : image.shape[1]] = image
    for slot in camera.slots:
        rect = slot.rect
        cv2.rectangle(
            canvas,
            (rect.x, rect.y),
            (rect.x + rect.width - 1, rect.y + rect.height - 1),
            (210, 140, 60),
            1,
        )
        middle = rect.y + rect.height // 2
        cv2.line(
            canvas,
            (rect.x, middle),
            (rect.x + rect.width - 1, middle),
            (210, 140, 60),
            1,
        )
        cv2.putText(
            canvas,
            f"CH{slot.channel_id}",
            (rect.x + 3, rect.y + 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    for row in rows:
        for contour in row.contours:
            cv2.polylines(
                canvas,
                [np.rint(np.array(contour) * 256).astype(np.int32)],
                True,
                (80, 220, 80),
                1,
                cv2.LINE_AA,
                shift=8,
            )
        axis = row.axis
        if axis is None:
            continue
        cv2.polylines(
            canvas,
            [np.rint(np.array(axis.corners) * 256).astype(np.int32)],
            True,
            (0, 160, 255),
            1,
            cv2.LINE_AA,
            shift=8,
        )
        start, end = (fixed_point(point) for point in axis.endpoints)
        cv2.line(canvas, start, end, AXIS_COLOR, 2, cv2.LINE_AA, shift=8)
        for point in (start, end):
            cv2.circle(canvas, point, 3 * 256, ENDPOINT_COLOR, -1, cv2.LINE_AA, shift=8)
    lines = [
        f"{'SYNTHETIC' if synthetic else 'LOCAL IMAGE'}  {camera.camera_id}  {image.shape[1]}x{image.shape[0]}  Original pixel coordinates",
        "Blue: ROI/views   Green: body contour   Orange: rotated box   Magenta/yellow: axis/endpoints",
        "Each axis measures one view body. W/H count pixels; rotated lengths span pixel centers.",
    ]
    for row in rows:
        prefix = f"CH{row.channel_id:02d} {row.size.name:5s}"
        body, axis = row.size.body, row.axis
        if body is None:
            lines.append(f"{prefix}  UNAVAILABLE  {row.size.invalid_reason}")
        elif axis is None:
            lines.append(
                f"{prefix}  W/H={body.bounding_rect.width}/{body.bounding_rect.height}  AXIS UNAVAILABLE  {row.axis_issue}"
            )
        else:
            a, b = axis.endpoints
            lines.append(
                f"{prefix}  W/H={body.bounding_rect.width}/{body.bounding_rect.height}"
                f"  L/S={axis.major:.2f}/{axis.minor:.2f}  angle={axis.orientation_degrees:.2f}"
                f"  ({a[0]:.2f},{a[1]:.2f})-({b[0]:.2f},{b[1]:.2f})"
            )
    for i, line in enumerate(lines):
        cv2.putText(
            canvas,
            line,
            (12, image.shape[0] + 22 + i * 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (235, 235, 235),
            1,
            cv2.LINE_AA,
        )
    return canvas


def decode(path: Path) -> NDArray[np.uint8]:
    # UNCHANGED ignores EXIF orientation and preserves depth/channels for validation.
    image = cv2.imdecode(
        np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED
    )
    if (
        image is None
        or image.dtype != np.uint8
        or image.ndim != 3
        or image.shape[2] != 3
    ):
        raise ValueError(f"Expected a nonempty uint8 BGR image: {path}")
    image.setflags(write=False)
    return image.astype(np.uint8, copy=False)


def timing_summary(values: list[float]) -> dict[str, int | float]:
    return {
        "samples": len(values),
        "median_us": float(np.median(values)),
        "p95_us": float(np.percentile(values, 95)),
    }


def benchmark(
    image: NDArray[np.uint8], camera: CameraLayout, repeats: int
) -> dict[str, object]:
    """Separate paired trials, alternating order; exclude decode, drawing and IO."""

    def baseline() -> None:
        for slot in camera.slots:
            measure_capsule(image, slot.rect, SIZE_ONLY)

    for _ in range(3):
        baseline()
        evaluate_frame(image, camera)
    baseline_us: list[float] = []
    augmented_us: list[float] = []
    extra_view_us: list[float] = []
    delta_us: list[float] = []
    for iteration in range(repeats):
        for augmented in (False, True) if iteration % 2 == 0 else (True, False):
            started = perf_counter_ns()
            if augmented:
                measured = evaluate_frame(image, camera)
                augmented_us.append((perf_counter_ns() - started) / 1000)
                extra_view_us.extend(
                    row.axis_us for row in measured if row.axis_us is not None
                )
            else:
                baseline()
                baseline_us.append((perf_counter_ns() - started) / 1000)
        delta_us.append(augmented_us[-1] - baseline_us[-1])
    return {
        "baseline_frame": timing_summary(baseline_us),
        "augmented_frame": timing_summary(augmented_us),
        "paired_added_frame": timing_summary(delta_us),
        "additional_valid_view": timing_summary(extra_view_us)
        if extra_view_us
        else None,
        "trials_us": {
            "baseline_frame": baseline_us,
            "augmented_frame": augmented_us,
            "paired_added_frame": delta_us,
            "additional_valid_view": extra_view_us,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", type=Path, required=True)
    parser.add_argument(
        "--input",
        action="append",
        nargs=2,
        metavar=("CAMERA", "IMAGE_OR_FOLDER"),
        required=True,
    )
    parser.add_argument(
        "--sample",
        type=positive_int,
        default=3,
        help="Evenly spaced files per input folder (default 3)",
    )
    parser.add_argument(
        "--synthetic", action="store_true", help="Explicitly label generated inputs"
    )
    parser.add_argument(
        "--benchmark-repeats",
        type=positive_int,
        help="Optional paired timing trials per image",
    )
    args = parser.parse_args(argv)
    machine = Path(args.machine)
    machine_bytes = machine.read_bytes()
    layout = load_machine_layout(machine).layout
    cameras = {str(camera.camera_id): camera for camera in layout.cameras}
    inputs: list[tuple[CameraLayout, Path]] = []
    selections: list[dict[str, object]] = []
    for camera_id, value in args.input:
        if camera_id not in cameras:
            parser.error(f"Camera {camera_id} is absent from MachineLayout")
        path = Path(value).resolve(strict=True)
        paths = folder_images(path) if path.is_dir() else (path,)
        indexes = np.linspace(
            0, len(paths) - 1, min(args.sample, len(paths)), dtype=int
        )
        chosen = tuple(paths[int(i)] for i in indexes)
        inputs.extend((cameras[camera_id], p) for p in chosen)
        selections.append(
            {
                "camera": camera_id,
                "source": str(path),
                "available": len(paths),
                "selected": [str(p) for p in chosen],
            }
        )
    directory = OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "machine.tsv").write_bytes(machine_bytes)
    records: list[dict[str, object]] = []
    inventory: list[dict[str, object]] = []
    for index, (camera, path) in enumerate(inputs, 1):
        item: dict[str, object] = {"image": str(path), "camera": camera.camera_id}
        try:
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            image = decode(path)
            started = perf_counter_ns()
            rows = evaluate_frame(image, camera)
            item.update(
                {
                    "sha256": before,
                    "shape": image.shape,
                    "evaluation_us": (perf_counter_ns() - started) / 1000,
                }
            )
            records.extend(record(str(path), camera.camera_id, row) for row in rows)
            output = directory / f"{index:02d}-{camera.camera_id}.png"
            save_png(output, render(image, camera, rows, synthetic=args.synthetic))
            item["overlay"] = output.name
            if args.benchmark_repeats:
                item["benchmark"] = benchmark(image, camera, args.benchmark_repeats)
            if hashlib.sha256(path.read_bytes()).hexdigest() != before:
                raise ValueError("Source changed during evaluation")
        except (ValueError, OSError, cv2.error) as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        inventory.append(item)
    if records:
        with (directory / "measurements.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    machine_unchanged = machine.read_bytes() == machine_bytes
    write_json(
        directory / "summary.json",
        {
            "evidence": "synthetic" if args.synthetic else "existing_local_images",
            "machine": str(machine.resolve()),
            "machine_sha256": hashlib.sha256(machine_bytes).hexdigest(),
            "machine_unchanged": machine_unchanged,
            "geometry": {
                "coordinates": "Original image pixel centers, x right, y down",
                "orientation": "Undirected degrees clockwise from +x in [0,180)",
                "length": "Longer minAreaRect side over the selected component's pixel centers",
                "bbox": "Complete component occupied-pixel W/H, inclusive pixel counts",
                "endpoints": "Opposite shorter-edge midpoints, lexicographic order",
                "drawing": "Reported floating geometry rounded only at draw time to 1/256 pixel",
                "size_pair_us": "One integrated Size call for both logical views, repeated in each view row",
                "axis_extra_us": "Visualization-only component recovery and contour presentation; axis is already measured",
            },
            "selection": selections,
            "images": inventory,
            "views": len(records),
            "body_rejections": dict(
                Counter(str(r["body_reason"]) for r in records if not r["body_valid"])
            ),
            "axis_rejections": dict(
                Counter(str(r["axis_reason"]) for r in records if r["axis_reason"])
            ),
            "benchmark": {
                "repeats": args.benchmark_repeats,
                "warmup_pairs": 3,
                "order": "alternating",
                "units": "microseconds",
            },
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "processor": platform.processor(),
                "numpy": np.__version__,
                "opencv": cv2.__version__,
                "opencv_threads": cv2.getNumThreads(),
                "opencv_cpus": cv2.getNumberOfCPUs(),
            },
        },
    )
    print(directory)
    return (
        1 if not machine_unchanged or any("error" in item for item in inventory) else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
