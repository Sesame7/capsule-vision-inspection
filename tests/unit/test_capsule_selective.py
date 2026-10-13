"""Independent capability execution and intentional fast Color semantics."""

from dataclasses import replace

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from tests.capsule_support import ROI, SETTINGS, bodies

from vimo_demo.inference import BoundingRect
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus
from vimo_demo.inspection.production.capsule import (
    BodyMeasurement,
    CapsuleMeasurement,
    CapsuleSettings,
    ColorMeasurement,
    ViewMeasurement,
    judge_capsule,
    measure_capsule,
)


def selected(size: bool, color: bool) -> CapsuleSettings:
    return CapsuleSettings(
        replace(SETTINGS.size, enabled=size), replace(SETTINGS.color, enabled=color)
    )


@pytest.mark.parametrize(
    "size,color", [(False, False), (True, False), (False, True), (True, True)]
)
def test_only_requested_operations_execute(
    monkeypatch: pytest.MonkeyPatch, size: bool, color: bool
) -> None:
    import vimo_demo.inspection.production.capsule as module

    calls = {"hsv": 0, "components": 0, "hue": 0}
    hsv, components, hue = (
        cv2.cvtColor,
        cv2.connectedComponentsWithStats,
        # Private observation point; the assertion concerns selective execution.
        module._masked_pixel_hue,  # pyright: ignore[reportPrivateUsage]
    )
    image = bodies()

    def convert(image: NDArray[np.uint8], code: int):
        calls["hsv"] += 1
        return hsv(image, code)

    def geometry(mask: NDArray[np.uint8], connectivity: int):
        calls["components"] += 1
        return components(mask, connectivity=connectivity)

    def color_median(pixels: NDArray[np.uint8], usable: NDArray[np.uint8], count: int):
        calls["hue"] += 1
        return hue(pixels, usable, count)

    def forbidden(*args: object) -> None:
        pytest.fail("No body-interior sampling/erosion in selective paths")

    monkeypatch.setattr(cv2, "cvtColor", convert)
    monkeypatch.setattr(cv2, "connectedComponentsWithStats", geometry)
    monkeypatch.setattr(cv2, "erode", forbidden)
    monkeypatch.setattr(module, "_masked_pixel_hue", color_median)
    settings = selected(size, color)
    before = image.copy()
    measured = measure_capsule(image, ROI, settings)
    result = judge_capsule(measured, settings)
    assert calls == {
        "hsv": int(color),
        "components": 2 * int(size),
        "hue": 2 * int(color),
    }
    assert measured.size_measured is size and measured.color_measured is color
    assert np.array_equal(image, before)
    assert all(
        (v.body is not None) is size and (v.color is not None) is color
        for v in measured.views
    )
    assert sum(m.name.endswith(".width") for m in result.measurements) == 2 * int(size)
    assert sum(m.name.endswith(".height") for m in result.measurements) == 2 * int(size)
    assert sum(m.name.endswith(".hue") for m in result.measurements) == 2 * int(color)
    assert result.assessment.technical_status is (
        TechnicalStatus.SUCCESS if size or color else TechnicalStatus.DISABLED
    )


@pytest.mark.parametrize("size", [False, True])
@pytest.mark.parametrize("strided", [False, True])
def test_readonly_views_preserve_complete_scalar_evidence(
    size: bool, strided: bool
) -> None:
    image = bodies()
    if strided:
        storage = np.zeros((120, 160, 3), dtype=np.uint8)
        storage[:, ::2] = image
        image = storage[:, ::2]
    image.flags.writeable = False
    before = image.copy()
    measured = measure_capsule(image, ROI, selected(size, True))
    assert measured == CapsuleMeasurement(
        (
            ViewMeasurement(
                "upper",
                BodyMeasurement(BoundingRect(20, 10, 20, 30)) if size else None,
                color=ColorMeasurement(30.0, 600, 240),
            ),
            ViewMeasurement(
                "lower",
                BodyMeasurement(BoundingRect(20, 70, 20, 30)) if size else None,
                color=ColorMeasurement(30.0, 600, 240),
            ),
        ),
        size,
        True,
    )
    result = judge_capsule(measured, selected(size, True))
    assert result.assessment.technical_status is TechnicalStatus.SUCCESS
    assert result.assessment.judgment is ProductJudgment.OK
    assert not result.reasons
    fields: list[tuple[str, float, str]] = [
        ("usable_pixels", 600, "px"),
        ("hue", 30.0, "opencv_hue"),
    ]
    if size:
        fields[:0] = [("width", 20, "px"), ("height", 30, "px")]
    expected = [
        (f"capsule.{view}.{name}", value, unit)
        for view in ("upper", "lower")
        for name, value, unit in fields
    ]
    assert [(m.name, m.value, m.unit) for m in result.measurements] == expected
    assert np.array_equal(image, before)


@pytest.mark.parametrize("color", [False, True])
def test_foreground_is_exact_including_dark_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    color: bool,
) -> None:
    rng = np.random.default_rng(20)
    image = rng.integers(0, 256, (21, 83, 3), dtype=np.uint8)
    image[:10] = rng.integers(0, 23, (10, 83, 3), dtype=np.uint8)
    original = cv2.connectedComponentsWithStats
    masks: list[NDArray[np.bool_]] = []

    def capture(mask: NDArray[np.uint8], connectivity: int):
        masks.append(mask != 0)
        return original(mask, connectivity=connectivity)

    expected = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 2] > 20
    monkeypatch.setattr(cv2, "connectedComponentsWithStats", capture)
    measure_capsule(image, BoundingRect(0, 0, 83, 21), selected(True, color))
    assert np.array_equal(np.concatenate(masks), expected)


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("empty", "insufficient_foreground"),
        ("gray", "insufficient_foreground"),
        ("dark", "insufficient_foreground"),
        ("ambiguous", "unusable"),
        ("wrong", "tolerance"),
    ],
)
def test_color_content_rejection_is_success_ng(kind: str, reason: str) -> None:
    image = bodies()
    if kind == "empty":
        image[:] = 0
    elif kind == "gray":
        image[image.max(axis=2) > 0] = 100
    elif kind == "dark":
        image = bodies(brightness=30)
    elif kind == "ambiguous":
        hsv = np.zeros_like(image)
        hsv[:, :40] = (0, 240, 180)
        hsv[:, 40:] = (90, 240, 180)
        image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.uint8, copy=False)
    else:
        image = bodies(60)
    settings = selected(False, True)
    measured = measure_capsule(image, ROI, settings)
    result = judge_capsule(measured, settings)
    assert result.assessment.technical_status is TechnicalStatus.SUCCESS
    assert result.assessment.judgment is ProductJudgment.NG
    assert [r.code for r in result.reasons] == [
        f"capsule.color.{view}.{reason}" for view in ("upper", "lower")
    ]
    assert all(v.body is None for v in measured.views)
    assert not any(m.name.endswith((".width", ".height")) for m in result.measurements)
    assert all(r.bounding_rect is None for r in result.reasons)


@pytest.mark.parametrize("pixels,expected", [(239, False), (240, True)])
def test_color_relative_sufficiency_is_inclusive(pixels: int, expected: bool) -> None:
    image = np.zeros((120, 80, 3), dtype=np.uint8)
    for offset in (0, 60):
        view = image[offset : offset + 60].reshape(-1, 3)
        view[:pixels] = (0, 180, 180)
    settings = selected(False, True)
    measured = measure_capsule(image, ROI, settings)
    assert all(
        v.color and v.color.minimum_pixels == 240 and v.color.usable_pixels == pixels
        for v in measured.views
    )
    assert (
        judge_capsule(measured, settings).assessment.judgment is ProductJudgment.OK
    ) == expected


@pytest.mark.parametrize("pixels,expected", [(11, False), (12, True)])
def test_color_absolute_floor(pixels: int, expected: bool) -> None:
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    for offset in (0, 5):
        image[offset : offset + 5].reshape(-1, 3)[:pixels] = (0, 180, 180)
    settings = replace(
        selected(False, True), color=replace(SETTINGS.color, target_hue=30)
    )
    measured = measure_capsule(image, BoundingRect(0, 0, 10, 10), settings)
    assert all(v.color and v.color.minimum_pixels == 12 for v in measured.views)
    assert (
        judge_capsule(measured, settings).assessment.judgment is ProductJudgment.OK
    ) == expected


def test_color_can_pass_truncated_geometry_and_combined_retains_both_evidence() -> None:
    image = np.tile(bodies()[10, 20], (120, 80, 1))
    for size in (False, True):
        settings = selected(size, True)
        measured = measure_capsule(image, ROI, settings)
        result = judge_capsule(measured, settings)
        assert all(v.color and v.color.hue == 30 for v in measured.views)
        assert all(v.body is None for v in measured.views)
        assert result.assessment.judgment is (
            ProductJudgment.NG if size else ProductJudgment.OK
        )
        assert [r.code for r in result.reasons] == (
            ["capsule.upper.truncated_body", "capsule.lower.truncated_body"]
            if size
            else []
        )
        assert sum(m.name.endswith(".hue") for m in result.measurements) == 2
        assert not any(m.name.endswith(".width") for m in result.measurements)


def test_color_independent_lower_view_wraparound_and_inclusive_tolerance() -> None:
    image = bodies(179)
    image[60:] = bodies(3)[60:]
    for tolerance, expected in ((2, ProductJudgment.OK), (1, ProductJudgment.NG)):
        settings = replace(
            selected(False, True),
            color=replace(SETTINGS.color, target_hue=1, hue_tolerance=tolerance),
        )
        result = judge_capsule(measure_capsule(image, ROI, settings), settings)
        assert result.assessment.judgment is expected
        assert len(result.reasons) == (0 if tolerance == 2 else 2)
    image[:60] = bodies(1)[:60]
    settings = replace(
        settings, color=replace(settings.color, target_hue=1, hue_tolerance=1)
    )
    result = judge_capsule(measure_capsule(image, ROI, settings), settings)
    assert [r.code for r in result.reasons] == ["capsule.color.lower.tolerance"]


def test_enabling_unmeasured_capability_is_technical_contract_error() -> None:
    measured = measure_capsule(bodies(), ROI, selected(False, True))
    with pytest.raises(ValueError, match="no measured evidence"):
        judge_capsule(measured, SETTINGS)


@pytest.mark.parametrize("strided", [False, True])
def test_color_filter_matches_exact_saturation_value_boundaries(strided: bool) -> None:
    from vimo_demo.inspection.production.capsule import median_pixel_hue

    rng = np.random.default_rng(40)
    hsv = rng.integers(0, 256, (51, 83, 3), dtype=np.uint8)
    hsv[:, :, 0] %= 180
    hsv[:12, :, 1] = 39
    hsv[12:24, :, 1] = 40
    hsv[24:36, :, 2] = 39
    hsv[36:, :, 2] = 40
    image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.uint8, copy=False)
    if strided:
        image = image[::-1, ::2]
    image.flags.writeable = False
    before = image.copy()
    actual = cv2.cvtColor(image, cv2.COLOR_BGR2HSV).astype(np.uint8, copy=False)
    measured = measure_capsule(
        image, BoundingRect(0, 0, image.shape[1], 51), selected(False, True)
    )
    for view, pixels in zip(measured.views, (actual[:25], actual[25:]), strict=True):
        mask = (pixels[:, :, 1] >= 40) & (pixels[:, :, 2] >= 40)
        assert view.color and view.color.usable_pixels == int(np.count_nonzero(mask))
        assert view.color.hue == median_pixel_hue(pixels[:, :, 0][mask])
    assert np.array_equal(image, before)
