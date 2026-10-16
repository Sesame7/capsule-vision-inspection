"""Optional small real-SDK semantic and output-ownership regression."""

import hashlib
from collections.abc import Iterator

import cv2
import numpy as np
import pytest
from tests.vimo_support import (
    MODEL,
    ROOT,
    Reference,
    load_reference,
    short_cases,
    unavailable,
    verify_files,
    verify_result,
)

from vimo_demo.inference import VimoInitializationError, VimoSegmentationEngine
from vimo_demo.inference._model_store import sha256


@pytest.fixture
def reference(pytestconfig: pytest.Config) -> Reference:
    return load_reference(pytestconfig)


@pytest.fixture
def engine(
    reference: Reference, pytestconfig: pytest.Config
) -> Iterator[VimoSegmentationEngine]:
    model = MODEL
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


@pytest.mark.gpu
def test_short_reference(
    engine: VimoSegmentationEngine, reference: Reference, pytestconfig: pytest.Config
) -> None:
    rows = short_cases(reference)
    verify_files(rows, pytestconfig)
    first = None
    first_mask_hash = None
    observed_labels: set[str] = set()
    has_defects: list[bool] = []
    for row in rows:
        image = cv2.imdecode(
            np.fromfile(ROOT / "images" / row["filename"], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert image is not None, row["filename"]
        result = engine.infer(image)
        verify_result(result, row, reference["image_shape"])
        observed_labels.update(result.regions_per_label)
        has_defects.append(result.has_defects)
        if first is None:
            first = result
            first_mask_hash = hashlib.sha256(result.mask.tobytes()).hexdigest()
    assert first is not None
    engine.close()
    assert hashlib.sha256(first.mask.tobytes()).hexdigest() == first_mask_hash
    assert any(has_defects) and not all(has_defects)
    assert observed_labels == set(reference["summary"]["labels"])
