"""Image I/O and compact reporting helpers for the inference visualizer."""

import argparse
import json
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

from vimo_demo.inference import EngineInfo, ImageResult

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
