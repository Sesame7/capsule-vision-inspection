"""Exercise the SDK boundary with stand-ins, never import the real SDK/GPU."""

import hashlib
import sys
from dataclasses import dataclass
from enum import IntFlag
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from numpy.typing import NDArray

from vimo_demo.inference import (
    BoundingRect,
    VimoError,
    VimoInferenceError,
    VimoInitializationError,
    VimoResponseError,
    VimoSegmentationEngine,
    read_model_labels,
)


class Flags(IntFlag):
    AREA = 1
    LONG_SIDE = 2
    SHORT_SIDE = 4
    BOUNDING_RECT = 8
    MIN_AREA_RECT = 16
    OUTER_CONTOUR = 32
    INNER_CONTOURS = 64


@dataclass
class StandIn:
    model: Path
    cache: Path
    solution: MagicMock
    module: MagicMock
    params: SimpleNamespace
    response: SimpleNamespace
    exits: list[str]

    def engine(self) -> VimoSegmentationEngine:
        return VimoSegmentationEngine(self.model, cache_dir=self.cache)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StandIn:
    model = tmp_path / "model.vimosln"
    model.write_bytes(b"unit-test model")
    params = SimpleNamespace(
        with_region_info=False,
        with_mask=True,
        with_score_map=False,
        region_info_flag=Flags.AREA,
        area_thresholds={"dent": 100},
        roi=(0, 0, 0, 0),
        mask_operation=0,
        width_thresholds={"dent": 0},
    )
    region = SimpleNamespace(
        label="dent",
        area=2,
        bounding_rect=(0, 0, 2, 1),
        long_side=2.0,
        short_side=1.0,
        min_area_rect=(1.0, 0.5, 2.0, 1.0, 0.0),
        outer_contour=[(0.0, 0.0), (1.0, 0.0)],
        inner_contours=[[(0.5, 0.5)]],
    )
    response = SimpleNamespace(
        label_map={"dent": 7},
        mask=np.full((2, 3, 1), 7, np.uint8),
        region_infos=[region],
        roi=(0, 0, 3, 2),
        rotated_roi=(1.5, 1.0, 3.0, 2.0, 0.0),
    )
    module = MagicMock()
    module.max_batch_size = 1
    module.get_params.return_value = params
    module.run.return_value = response
    solution = MagicMock()
    solution.create_module.return_value = module
    solution.module_info_list = [
        SimpleNamespace(
            id="2", name="seg", type=1, features=["dent"], model_version="V2"
        )
    ]
    exits: list[str] = []

    def exit_module(*_args: object) -> None:
        exits.append("module")

    def exit_solution(*_args: object) -> None:
        exits.append("solution")

    def request(image: NDArray[np.uint8]) -> SimpleNamespace:
        return SimpleNamespace(image=image)

    module.__exit__.side_effect = exit_module
    solution.__exit__.side_effect = exit_solution
    vendor = ModuleType("smartmore.vimo.inference")
    for name, value in {
        "Solution": MagicMock(return_value=solution),
        "SegmentationModule": MagicMock,
        "SegmentationParams": SimpleNamespace,
        "SegmentationResponse": SimpleNamespace,
        "ModuleType": SimpleNamespace(SEGMENTATION=1),
        "SegRegionInfoFlag": Flags,
        "Request": request,
    }.items():
        setattr(vendor, name, value)
    root, package = ModuleType("smartmore"), ModuleType("smartmore.vimo")
    vars(root)["vimo"] = package
    vars(package)["inference"] = vendor
    for name, value in {
        "smartmore": root,
        "smartmore.vimo": package,
        "smartmore.vimo.inference": vendor,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    return StandIn(model, tmp_path / "cache", solution, module, params, response, exits)


@pytest.mark.parametrize(
    "failure", [None, "hash", "module", "duplicate", "empty", "load", "close"]
)
def test_complete_label_metadata_never_creates_an_engine(
    sdk: StandIn, failure: str | None
) -> None:
    digest = hashlib.sha256(sdk.model.read_bytes()).hexdigest()
    if failure == "module":
        sdk.solution.module_info_list = []
    elif failure == "duplicate":
        sdk.solution.module_info_list[0].features = ["Dent", "Dent"]
    elif failure == "empty":
        sdk.solution.module_info_list[0].features = []
    else:
        sdk.solution.module_info_list[0].features = ["Dent", "dent", "压痕"]
    if failure == "load":
        sdk.solution.load_from_memory.side_effect = RuntimeError("bad metadata")
    if failure == "close":
        sdk.solution.__exit__.side_effect = RuntimeError("cleanup failed")
    if failure:
        with pytest.raises(VimoInitializationError) as error:
            read_model_labels(
                sdk.model,
                module_id="2",
                expected_sha256="0" * 64 if failure == "hash" else digest,
            )
        assert error.value.__cause__ is not None
    else:
        assert read_model_labels(sdk.model, module_id="2", expected_sha256=digest) == (
            "Dent",
            "dent",
            "压痕",
        )
        sdk.solution.load_from_memory.assert_called_once_with(
            bytearray(sdk.model.read_bytes())
        )
    sdk.solution.create_module.assert_not_called()
    sdk.module.run.assert_not_called()
    if failure != "hash":
        sdk.solution.__exit__.assert_called_once()
    assert not sdk.cache.exists()


def test_reuses_module_preserves_settings_and_detaches_response(sdk: StandIn) -> None:
    with sdk.engine() as engine:
        image = np.zeros((2, 3, 3), np.uint8)
        first = engine.infer(image)
        sdk.response.mask[...] = 0
        sdk.response.label_map.clear()
        sdk.response.region_infos[0].outer_contour.clear()
        second = engine.infer(image)
        assert first.mask[0, 0] == 7 and second.mask[0, 0] == 0
        assert first.label_map == {"dent": 7}
        assert first.regions[0].outer_contour == ((0.0, 0.0), (1.0, 0.0))
        assert first.regions[0].inner_contours == (((0.5, 0.5),),)
        assert first.regions[0].bounding_rect == BoundingRect(0, 0, 2, 1)
        sdk.solution.create_module.assert_called_once_with("2", True, 0, 1)
        sdk.module.set_params.assert_called_once_with(sdk.params)
        assert sdk.params.area_thresholds == {"dent": 100}
        assert sdk.params.width_thresholds == {"dent": 0}
        assert sdk.params.roi == (0, 0, 0, 0) and sdk.params.mask_operation == 0
        assert sdk.params.with_mask and not sdk.params.with_score_map
        assert sdk.params.with_region_info and sdk.params.region_info_flag == Flags(127)
    assert sdk.exits == ["module", "solution"]
    engine.close()
    assert sdk.exits == ["module", "solution"]
    assert first.mask[0, 0] == 7  # Still valid after module/Solution release.
    with pytest.raises(VimoInferenceError, match="closed"):
        engine.infer(image)


def test_initialization_failure_closes_both_objects_and_chains(sdk: StandIn) -> None:
    original = RuntimeError("vendor configuration failure")
    sdk.module.set_params.side_effect = original
    with pytest.raises(VimoInitializationError) as caught:
        sdk.engine()
    assert caught.value.__cause__ is original
    assert sdk.exits == ["module", "solution"]


def test_sdk_inference_error_preserves_cause_and_message(sdk: StandIn) -> None:
    original = RuntimeError("runtime DLL failed")
    sdk.module.run.side_effect = original
    with (
        sdk.engine() as engine,
        pytest.raises(VimoInferenceError, match="runtime DLL failed") as caught,
    ):
        engine.infer(np.zeros((2, 3, 3), np.uint8))
    assert caught.value.__cause__ is original


def test_bad_mask_is_response_error(sdk: StandIn) -> None:
    sdk.response.mask = np.zeros((2, 3, 3), np.uint8)
    with (
        sdk.engine() as engine,
        pytest.raises(VimoResponseError, match="mask") as caught,
    ):
        engine.infer(np.zeros((2, 3, 3), np.uint8))
    assert isinstance(caught.value.__cause__, ValueError)


def test_cleanup_failure_does_not_hide_callers_error(sdk: StandIn) -> None:
    sdk.module.__exit__.side_effect = RuntimeError("cleanup failed")
    original = ValueError("caller failed")
    with pytest.raises(ValueError) as caught, sdk.engine():
        raise original
    assert caught.value is original
    assert any("cleanup failed" in note for note in original.__notes__)
    assert sdk.exits == ["solution"]


def test_explicit_cleanup_failure_is_reported(sdk: StandIn) -> None:
    engine = sdk.engine()
    sdk.module.__exit__.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(VimoError, match="cleanup failed"):
        engine.close()
    assert engine.closed
