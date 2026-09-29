"""Opt-in regressions against frozen Phase 0 semantics, never model accuracy."""

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TypedDict, cast

import cv2
import numpy as np
import pytest

from vimo_demo.inference import (
    ImageResult,
    VimoInitializationError,
    VimoSegmentationEngine,
)
from vimo_demo.inference._model_store import sha256

ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}


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


@pytest.fixture(scope="module")
def reference() -> Reference:
    # This is a tracked, reviewed fixture schema, not arbitrary external input.
    return cast(
        Reference,
        json.loads((ROOT / "tests/reference/phase0.json").read_text(encoding="utf-8")),
    )


def unavailable(config: pytest.Config, reason: str) -> NoReturn:
    if config.getoption("--require-gpu"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
def engine(
    reference: Reference, pytestconfig: pytest.Config
) -> Iterator[VimoSegmentationEngine]:
    model = ROOT / "models/model_trt_fp16.vimosln"
    if not model.is_file() or not (ROOT / "images").is_dir():
        unavailable(pytestconfig, "Local reference model/images unavailable")
    if sha256(model) != reference["model_sha256"]:
        unavailable(
            pytestconfig, "Model hash differs; Phase 0 golden counts do not apply"
        )
    try:
        active = VimoSegmentationEngine(model, cache_dir=ROOT / "outputs/vimo_cache")
    except VimoInitializationError as exc:
        cause = exc.__cause__
        # Missing DLL/SDK or license are prerequisites; configuration/adapter
        # programming errors must fail, not masquerade as an unavailable GPU.
        if isinstance(cause, (ImportError, OSError)) or (
            type(cause).__name__ == "VimoException"
            and getattr(cause, "code", None) == 40000
        ):
            unavailable(pytestconfig, f"ViMo runtime/license unavailable: {exc}")
        raise
    with active:
        assert active.info.module_id == "2"
        assert active.info.area_thresholds == {
            "黑点": 30,
            "压痕": 100,
            "破损": 30,
            "变形": 1500,
        }
        yield active


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


def verify_files(
    rows: list[ReferenceImage], config: pytest.Config, *, full: bool
) -> None:
    directory = ROOT / "images"
    if full:
        actual = {
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in EXTENSIONS
        }
        if actual != {row["filename"] for row in rows}:
            unavailable(config, "Dataset filenames differ from the 400-image reference")
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


def run_reference(
    engine: VimoSegmentationEngine,
    reference: Reference,
    rows: list[ReferenceImage],
    name: str,
) -> Summary:
    image_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    defects = 0
    first: ImageResult | None = None
    first_mask_hash: str | None = None
    for index, row in enumerate(rows, 1):
        image = cv2.imdecode(
            np.fromfile(ROOT / "images" / row["filename"], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert image is not None, row["filename"]
        result = engine.infer(image)
        verify_result(result, row, reference["image_shape"])
        if first is None:
            first = result
            first_mask_hash = hashlib.sha256(result.mask.tobytes()).hexdigest()
        defects += int(result.has_defects)
        image_counts.update(result.regions_per_label.keys())
        region_counts.update(result.regions_per_label)
        if len(rows) > 12 and index % 100 == 0:
            print(f"Reference regression: {index}/{len(rows)}")
    assert first is not None
    # Retained results survive subsequent calls and real SDK context cleanup.
    engine.close()
    assert hashlib.sha256(first.mask.tobytes()).hexdigest() == first_mask_hash
    observed: Summary = {
        "total_images": len(rows),
        "no_regions": len(rows) - defects,
        "has_defects": defects,
        "labels": {
            label: {"images": image_counts[label], "regions": region_counts[label]}
            for label in sorted(image_counts)
        },
    }
    output = ROOT / "outputs/phase1a_regression"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "model_sha256": engine.info.model_sha256,
        "reference_manifest_sha256": sha256(ROOT / "tests/reference/phase0.json"),
        "per_image_semantics_matched": len(rows),
        "summary": observed,
    }
    (output / f"{name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    return observed


@pytest.mark.gpu
def test_short_reference(
    engine: VimoSegmentationEngine, reference: Reference, pytestconfig: pytest.Config
) -> None:
    rows = short_cases(reference)
    verify_files(rows, pytestconfig, full=False)
    observed = run_reference(engine, reference, rows, "short")
    assert observed["no_regions"] > 0 and observed["has_defects"] > 0
    assert set(observed["labels"]) == set(reference["summary"]["labels"])


@pytest.mark.gpu
@pytest.mark.full_reference
def test_full_reference(
    engine: VimoSegmentationEngine, reference: Reference, pytestconfig: pytest.Config
) -> None:
    verify_files(reference["images"], pytestconfig, full=True)
    observed = run_reference(engine, reference, reference["images"], "full_reference")
    assert observed == reference["summary"]
