"""The only production module that imports ViMo; no dependency on spike scripts."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Self, cast

import numpy as np
from numpy.typing import NDArray

from ._model_store import stage_model
from ._validation import (
    normalize_mask,
    validate_device_id,
    validate_image,
    validate_label_map,
)
from .errors import (
    VimoError,
    VimoInferenceError,
    VimoInitializationError,
    VimoResponseError,
)
from .models import (
    BoundingRect,
    Contour,
    DefectRegion,
    EngineInfo,
    ImageResult,
    RotatedRect,
)

if TYPE_CHECKING:
    # SDK 3.20 has usable inline annotations but no py.typed marker/stub package.
    import smartmore.vimo.inference as vimo  # pyright: ignore[reportMissingTypeStubs]


# SDK __init__.py defines tuples in normal execution and empty geometry classes
# in VIMO_SDK_DOC_BUILDING mode. Pyright sees the latter. These local casts match
# the documented runtime tuples and the Phase 0 snapshot/visualization code.
def _rect(value: vimo.Rect) -> BoundingRect:
    return BoundingRect(*cast(tuple[int, int, int, int], value))


def _rotated_rect(value: vimo.RotatedRect) -> RotatedRect:
    return RotatedRect(*cast(tuple[float, float, float, float, float], value))


def _contour(value: list[vimo.Point2f]) -> Contour:
    return tuple(
        (float(x), float(y)) for x, y in cast(list[tuple[float, float]], value)
    )


def _convert_response(
    response: vimo.SegmentationResponse,
    shape: tuple[int, int, int],
    module_id: str,
) -> ImageResult:
    labels = {label: int(code) for label, code in response.label_map.items()}
    validate_label_map(labels)
    regions = tuple(
        DefectRegion(
            label=region.label,
            area=int(region.area),
            bounding_rect=_rect(region.bounding_rect),
            long_side=float(region.long_side),
            short_side=float(region.short_side),
            outer_contour=_contour(region.outer_contour),
            inner_contours=tuple(
                _contour(contour) for contour in region.inner_contours
            ),
            min_area_rect=_rotated_rect(region.min_area_rect),
        )
        for region in response.region_infos
    )
    return ImageResult(
        image_shape=shape,
        label_map=MappingProxyType(labels),
        regions=regions,
        mask=normalize_mask(response.mask, shape[:2]),
        roi=_rect(response.roi),
        rotated_roi=_rotated_rect(response.rotated_roi),
        module_id=module_id,
    )


class VimoSegmentationEngine:
    """One GPU module, called serially by one owning worker context.

    Construction loads the model. Use a context manager or close() explicitly.
    This object supplies no threading, retries, batching or acceptance policy.
    Relative cache_dir is relative to the process working directory.
    """

    def __init__(
        self,
        model_path: str | Path,
        device_id: int = 0,
        *,
        module_id: str | None = None,
        cache_dir: str | Path = "outputs/vimo_cache",
    ) -> None:
        self._stack = ExitStack()
        self._module: vimo.SegmentationModule | None = None
        self._info: EngineInfo
        try:
            validate_device_id(device_id)
            runtime_model, digest = stage_model(Path(model_path), Path(cache_dir))
            # Lazy import: pure models/tests remain usable without the SDK/runtime.
            import smartmore.vimo.inference as vimo  # pyright: ignore[reportMissingTypeStubs]

            solution = vimo.Solution(str(runtime_model))
            self._stack.enter_context(solution)
            candidates = [
                info
                for info in solution.module_info_list
                if info.type == vimo.ModuleType.SEGMENTATION
                and (module_id is None or info.id == module_id)
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"Need one segmentation module; candidates={[info.id for info in candidates]}"
                )
            info = candidates[0]
            module = solution.create_module(info.id, True, device_id, 1)
            self._stack.enter_context(module)
            # Runtime narrowing removes the vendor create_module return union.
            if not isinstance(module, vimo.SegmentationModule):
                raise TypeError(f"Expected SegmentationModule, got {type(module)}")
            if module.max_batch_size != 1:
                raise ValueError(
                    f"Expected max_batch_size=1, got {module.max_batch_size}"
                )
            params = module.get_params()
            # Validate C++ return values despite the claimed static return type.
            if not isinstance(params, vimo.SegmentationParams):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"Expected SegmentationParams, got {type(params)}")
            # Preserve the verified settings documented in
            # docs/architecture.md#inference:
            # every threshold, ROI, mask operation, score-map and other default.
            params.with_region_info = True
            flags = vimo.SegRegionInfoFlag
            params.region_info_flag |= (
                flags.AREA
                | flags.LONG_SIDE
                | flags.SHORT_SIDE
                | flags.BOUNDING_RECT
                | flags.MIN_AREA_RECT
                | flags.OUTER_CONTOUR
                | flags.INNER_CONTOURS
            )
            module.set_params(params)
            active = module.get_params()
            if not isinstance(active, vimo.SegmentationParams):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError("Unexpected active segmentation parameters")
            if (
                not active.with_mask
                or not active.with_region_info
                or active.with_score_map
            ):
                raise ValueError(
                    "Module defaults differ from the verified mask/region/score-map settings"
                )
            self._info = EngineInfo(
                model_sha256=digest,
                module_id=info.id,
                module_name=info.name,
                model_version=info.model_version,
                labels=tuple(info.features),
                device_id=device_id,
                area_thresholds=MappingProxyType(dict(active.area_thresholds)),
            )
            self._module = module
        except BaseException as exc:
            try:
                self._stack.close()
            except Exception as cleanup_error:
                exc.add_note(f"Cleanup also failed: {cleanup_error!r}")
            if isinstance(exc, Exception):
                raise VimoInitializationError(
                    f"Cannot initialize {model_path}: {exc}"
                ) from exc
            raise

    @property
    def info(self) -> EngineInfo:
        return self._info

    @property
    def closed(self) -> bool:
        return self._module is None

    def infer(self, image_bgr: NDArray[np.generic]) -> ImageResult:
        """Infer one uint8 HxWx3 BGR image; invalid inputs raise TypeError/ValueError."""
        module = self._module
        if module is None:
            raise VimoInferenceError("Engine is closed")
        image = validate_image(image_bgr)
        try:
            import smartmore.vimo.inference as vimo  # pyright: ignore[reportMissingTypeStubs]

            response = module.run(vimo.Request(image))
        except Exception as exc:
            raise VimoInferenceError(
                f"Module {self.info.module_id} inference failed: {exc}"
            ) from exc
        try:
            # A malformed runtime response must be a response error, not escape.
            if not isinstance(response, vimo.SegmentationResponse):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"Expected SegmentationResponse, got {type(response)}")
            return _convert_response(
                response, (image.shape[0], image.shape[1], 3), self.info.module_id
            )
        except Exception as exc:
            raise VimoResponseError(
                f"Module {self.info.module_id} response invalid: {exc}"
            ) from exc

    def close(self) -> None:
        """Release module before Solution through the SDK's real context managers."""
        self._module = None
        try:
            self._stack.close()
        except Exception as exc:
            raise VimoError(f"ViMo cleanup failed: {exc}") from exc

    def __enter__(self) -> Self:
        if self.closed:
            raise VimoError("Engine is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except VimoError as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"Cleanup also failed: {cleanup_error!r}")
