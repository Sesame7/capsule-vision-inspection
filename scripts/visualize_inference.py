"""Render formal results in original coordinates; generated files only."""

import argparse
import hashlib
import json
import math
import os
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from io import TextIOWrapper
from pathlib import Path
from typing import cast

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import EngineInfo, ImageResult, VimoSegmentationEngine

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def image_paths(source: Path) -> list[Path]:
    source = source.resolve(strict=True)
    candidates = [source] if source.is_file() else source.iterdir()
    return sorted(
        (p for p in candidates if p.is_file() and p.suffix.lower() in EXTENSIONS),
        key=lambda p: (p.name.lower(), p.name),
    )


def decode(path: Path) -> NDArray[np.uint8]:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.dtype != np.uint8:
        raise ValueError(f"Cannot decode uint8 BGR image: {path}")
    # OpenCV's annotations do not narrow the dtype after the runtime check.
    return cast(NDArray[np.uint8], image)


def semantic_signature(result: ImageResult) -> dict[str, object]:
    return {
        "label_map": dict(result.label_map),
        "total_regions": len(result.regions),
        "regions_per_label": result.regions_per_label,
    }


def engine_metadata(info: EngineInfo) -> dict[str, object]:
    return {
        "model_sha256": info.model_sha256,
        "module_id": info.module_id,
        "module_name": info.module_name,
        "device_id": info.device_id,
        "area_thresholds": dict(info.area_thresholds),
        "python_version": sys.version,
    }


def result_metadata(result: ImageResult) -> dict[str, object]:
    return {
        **semantic_signature(result),
        "has_defects": result.has_defects,
        "image_shape": result.image_shape,
        "mask_shape": result.mask.shape,
        "regions": [
            {
                "label": r.label,
                "area": r.area,
                "bounding_rect": [
                    r.bounding_rect.x,
                    r.bounding_rect.y,
                    r.bounding_rect.width,
                    r.bounding_rect.height,
                ],
            }
            for r in result.regions
        ],
    }


def output_directory(kind: str) -> Path:
    directory = (
        ROOT / "outputs" / kind / datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def write_json(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def run_cli(main: Callable[[], int]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        return main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 1


# Reference model display colors, keyed by NAME, never by assumed mask ID.
# Other models get deterministic display colors; these do not change inference.
COLORS = {"黑点": "D40005", "压痕": "CCC900", "破损": "69CC00", "变形": "00CC85"}


def color_bgr(label: str) -> tuple[int, int, int]:
    color = COLORS.get(label)
    if color is not None:
        return int(color[4:6], 16), int(color[2:4], 16), int(color[:2], 16)
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return 64 + digest[0] % 192, 64 + digest[1] % 192, 64 + digest[2] % 192


def render(image: NDArray[np.uint8], result: ImageResult) -> NDArray[np.uint8]:
    """The image plane stays at (0,0); a legend is appended BELOW the image."""
    overlay = image.copy()
    mapping = result.mask_id_to_label
    legend: list[tuple[str, str, tuple[int, int, int]]] = []
    for code in np.unique(result.mask).tolist():
        if code == 0 and code not in mapping:  # Documented SDK background.
            continue
        label = mapping.get(code, f"Unmapped mask ID {code}")
        color = color_bgr(label)
        pixels = result.mask == code
        overlay[pixels] = np.rint(overlay[pixels] * 0.6 + np.array(color) * 0.4).astype(
            np.uint8
        )
        legend.append((f"ID {code}: {label}", f"ID {code} (see JSON for label)", color))
    for index, region in enumerate(result.regions, 1):
        color = color_bgr(region.label)
        rect = region.bounding_rect
        if region.outer_contour:
            points = np.rint(np.array(region.outer_contour)).astype(np.int32)
            cv2.polylines(overlay, [points], True, color, 2, cv2.LINE_AA)
        cv2.rectangle(
            overlay,
            (rect.x, rect.y),
            (rect.x + rect.width - 1, rect.y + rect.height - 1),
            color,
            2,
        )
        cv2.putText(
            overlay,
            f"#{index}",
            (rect.x, max(16, rect.y - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            color,
            2,
            cv2.LINE_AA,
        )
        legend.append(
            (
                f"#{index} {region.label}  area={region.area}",
                f"#{index} ID={result.label_map.get(region.label)} area={region.area}",
                color,
            )
        )
    if not legend:
        legend.append(
            (
                "No returned defect regions",
                "No returned defect regions",
                (220, 220, 220),
            )
        )
    # Pillow is optional and already present on the verified workstation.
    # Without a Chinese font, numbered ASCII legend entries link to exact JSON labels.
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        Image = ImageDraw = ImageFont = None
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    if (
        Image is not None
        and ImageDraw is not None
        and ImageFont is not None
        and font_path.is_file()
    ):
        font = ImageFont.truetype(str(font_path), 22)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        width = max(
            image.shape[1],
            *(
                math.ceil(probe.textbbox((0, 0), line, font=font)[2]) + 24
                for line, _, _ in legend
            ),
        )
        canvas = Image.new(
            "RGB", (width, image.shape[0] + 34 * len(legend) + 12), (24, 24, 24)
        )
        canvas.paste(Image.fromarray(cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)), (0, 0))
        draw = ImageDraw.Draw(canvas)
        for index, (line, _, bgr) in enumerate(legend):
            draw.text(
                (12, image.shape[0] + 34 * index),
                line,
                font=font,
                fill=tuple(reversed(bgr)),
            )
        return np.asarray(canvas, dtype=np.uint8)[:, :, ::-1].copy()
    width = max(
        image.shape[1],
        *(
            cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)[0][0] + 24
            for _, line, _ in legend
        ),
    )
    fallback = np.full(
        (image.shape[0] + 34 * len(legend) + 12, width, 3), 24, dtype=np.uint8
    )
    fallback[: image.shape[0], : image.shape[1]] = overlay
    for index, (_, line, color) in enumerate(legend):
        cv2.putText(
            fallback,
            line,
            (12, image.shape[0] + 34 * index + 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            1,
            cv2.LINE_AA,
        )
    return fallback


def save_png(path: Path, image: NDArray[np.uint8]) -> None:
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise OSError(f"PNG encoding failed: {path}")
    encoded.tofile(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument(
        "images", type=Path, help="One image or a non-recursive directory"
    )
    parser.add_argument(
        "--filenames", nargs="+", help="Exact filenames in the directory"
    )
    parser.add_argument("--limit", type=positive_int)
    args = parser.parse_args()
    paths = image_paths(args.images)
    if args.filenames:
        selected = set(args.filenames)
        missing = selected - {path.name for path in paths}
        if missing:
            parser.error(f"Selected filenames not found: {sorted(missing)}")
        paths = [path for path in paths if path.name in selected]
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        parser.error("No supported images selected")
    destination = output_directory("visualization")
    with VimoSegmentationEngine(args.model) as engine:
        for path in paths:
            image = decode(path)
            result = engine.infer(image)
            # Include the source extension in the directory to avoid stem collisions.
            folder = destination / path.name
            folder.mkdir()
            save_png(folder / "overlay.png", render(image, result))
            save_png(folder / "mask.png", result.mask)
            write_json(
                folder / "result.json",
                {
                    "filename": path.name,
                    **result_metadata(result),
                    "mask_id_to_label": result.mask_id_to_label,
                    "display_colors_bgr": {
                        label: color_bgr(label) for label in result.label_map
                    },
                    "note": "Original coordinates, no resizing; legend below image. Raw mask IDs in mask.png.",
                },
            )
        write_json(
            destination / "summary.json",
            {
                **engine_metadata(engine.info),
                "images": [p.name for p in paths],
                "palette": "Reference colors by label name; deterministic colors for unexpected labels.",
            },
        )
    print(f"Saved {len(paths)} visualizations: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
