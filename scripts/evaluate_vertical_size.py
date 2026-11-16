"""Offline capsule Size evaluation using vertical pixel-center spans."""

import argparse
import csv
import hashlib
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
from capsule_vision.inference.models import Point
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import CameraLayout, ChannelId
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    SizeSettings,
    ViewMeasurement,
    measure_capsule,
)

OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "outputs" / "vertical-size"
SIZE_ONLY = CapsuleSettings(size=SizeSettings(enabled=True))
LENGTH_COLOR = (255, 80, 255)
ENDPOINT_COLOR = (0, 255, 255)


@dataclass(frozen=True, slots=True)
class EvaluatedView:
    channel_id: ChannelId
    view_rect: BoundingRect
    size: ViewMeasurement
    size_pair_us: float


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
            rows.append(
                EvaluatedView(
                    slot.channel_id,
                    BoundingRect(
                        slot.rect.x, slot.rect.y + offset, slot.rect.width, height
                    ),
                    view,
                    pair_us,
                )
            )
    return tuple(rows)


def record(image_id: str, camera_id: CameraId, row: EvaluatedView) -> dict[str, object]:
    body = row.size.body
    box = body.bounding_rect if body is not None else None
    start, end = body.endpoints if body is not None else ((None, None), (None, None))
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
        "bbox_x": box.x if box else None,
        "bbox_y": box.y if box else None,
        "bbox_width": box.width if box else None,
        "bbox_height": box.height if box else None,
        "vertical_length_px": body.length if body else None,
        "start_x": start[0],
        "start_y": start[1],
        "end_x": end[0],
        "end_y": end[1],
        "size_pair_us": row.size_pair_us,
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
        body = row.size.body
        if body is None:
            continue
        box = body.bounding_rect
        cv2.rectangle(
            canvas,
            (box.x, box.y),
            (box.x + box.width - 1, box.y + box.height - 1),
            (0, 160, 255),
            1,
            cv2.LINE_AA,
        )
        start, end = (fixed_point(point) for point in body.endpoints)
        cv2.line(canvas, start, end, LENGTH_COLOR, 2, cv2.LINE_AA, shift=8)
        for point in (start, end):
            cv2.circle(canvas, point, 3 * 256, ENDPOINT_COLOR, -1, cv2.LINE_AA, shift=8)
    lines = [
        f"{'SYNTHETIC' if synthetic else 'LOCAL IMAGE'}  {camera.camera_id}  {image.shape[1]}x{image.shape[0]}  Original pixel coordinates",
        "Blue: ROI/views   Orange: body bounds   Magenta/yellow: vertical length/endpoints",
        "W/H count pixels. Vertical length spans the top and bottom pixel centers (H - 1).",
    ]
    for row in rows:
        prefix = f"CH{row.channel_id:02d} {row.size.name:5s}"
        body = row.size.body
        if body is None:
            lines.append(f"{prefix}  UNAVAILABLE  {row.size.invalid_reason}")
        else:
            a, b = body.endpoints
            lines.append(
                f"{prefix}  W/H={body.bounding_rect.width}/{body.bounding_rect.height}"
                f"  Vertical length={body.length} px"
                f"  ({a[0]:.1f},{a[1]})-({b[0]:.1f},{b[1]})"
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
    """Time production Size over the frame's ROIs; exclude decode, drawing and IO."""

    def measure_frame() -> None:
        for slot in camera.slots:
            measure_capsule(image, slot.rect, SIZE_ONLY)

    for _ in range(3):
        measure_frame()
    trials_us: list[float] = []
    for _ in range(repeats):
        started = perf_counter_ns()
        measure_frame()
        trials_us.append((perf_counter_ns() - started) / 1000)
    return {"size_frame": timing_summary(trials_us), "trials_us": trials_us}


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
        help="Optional production Size timing trials per image",
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
                "vertical_length_px": "Selected component's bounding height minus one",
                "bbox": "Complete component occupied-pixel W/H, inclusive pixel counts",
                "endpoints": "Top and bottom pixel centers at the horizontal midpoint of the bounds",
                "size_pair_us": "One integrated Size call for both logical views, repeated in each view row",
            },
            "selection": selections,
            "images": inventory,
            "views": len(records),
            "body_rejections": dict(
                Counter(str(r["body_reason"]) for r in records if not r["body_valid"])
            ),
            "benchmark": {
                "repeats": args.benchmark_repeats,
                "warmup_frames": 3,
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
