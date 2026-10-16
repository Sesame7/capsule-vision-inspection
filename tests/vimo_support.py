"""Local assets and semantic checks for the optional real inference regressions."""

import json
from collections import Counter
from pathlib import Path
from typing import NoReturn, TypedDict, cast

import numpy as np
import pytest

from vimo_demo.inference import ImageResult
from vimo_demo.inference._model_store import sha256

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "data/models/model_trt_fp16.vimosln"
TIMEOUT = 120.0


class ReferenceImage(TypedDict):
    filename: str
    sha256: str
    label_map: dict[str, int]
    regions_per_label: dict[str, int]


class LabelTotal(TypedDict):
    images: int
    regions: int


class Summary(TypedDict):
    total_images: int
    no_regions: int
    has_defects: int
    labels: dict[str, LabelTotal]


class Reference(TypedDict):
    model_sha256: str
    image_shape: list[int]
    summary: Summary
    images: list[ReferenceImage]


def unavailable(config: pytest.Config, reason: str) -> NoReturn:
    if config.getoption("--require-gpu"):
        pytest.fail(reason)
    pytest.skip(reason)


def short_cases(reference: Reference) -> list[ReferenceImage]:
    rows = reference["images"]
    chosen = [row for row in rows if not row["regions_per_label"]][:2]
    for label in sorted(reference["summary"]["labels"]):
        row = next(row for row in rows if label in row["regions_per_label"])
        if row not in chosen:
            chosen.append(row)
    for row in rows:
        if len(chosen) == 8:
            break
        if row not in chosen:
            chosen.append(row)
    return sorted(chosen, key=lambda row: (row["filename"].lower(), row["filename"]))


def verify_files(rows: list[ReferenceImage], config: pytest.Config) -> None:
    directory = ROOT / "images"
    for row in rows:
        path = directory / row["filename"]
        if not path.is_file() or sha256(path) != row["sha256"]:
            unavailable(config, f"Reference image missing/changed: {row['filename']}")


def verify_result(result: ImageResult, row: ReferenceImage, shape: list[int]) -> None:
    name = row["filename"]
    assert list(result.image_shape) == shape, name
    assert result.label_map == row["label_map"], name
    assert result.regions_per_label == row["regions_per_label"], name
    assert len(result.regions) == sum(row["regions_per_label"].values()), name
    assert result.has_defects == bool(row["regions_per_label"]), name
    assert (
        Counter(region.label for region in result.regions) == row["regions_per_label"]
    ), name
    assert result.mask.shape == tuple(shape[:2]) and result.mask.dtype == np.uint8, name
    assert not result.mask.flags.writeable and result.mask.flags.c_contiguous, name
    assert result.mask_id_to_label == {
        code: label for label, code in row["label_map"].items()
    }, name
    assert set(np.unique(result.mask).tolist()) <= {0, *row["label_map"].values()}, name
    for region in result.regions:
        assert (
            region.area >= 0
            and region.bounding_rect.width > 0
            and region.bounding_rect.height > 0
        ), name
        assert region.long_side >= 0 and region.short_side >= 0, name
        assert region.outer_contour, name


def load_reference(config: pytest.Config) -> Reference:
    reference = cast(
        Reference,
        json.loads((ROOT / "tests/reference/phase0.json").read_text(encoding="utf-8")),
    )
    if not MODEL.is_file() or not (ROOT / "images").is_dir():
        unavailable(config, "Local reference model/images unavailable")
    if sha256(MODEL) != reference["model_sha256"]:
        unavailable(config, "Model hash differs; golden semantics do not apply")
    return reference
