"""Small public inference API. Importing it does not load the ViMo runtime."""

from .errors import (
    VimoError,
    VimoInferenceError,
    VimoInitializationError,
    VimoResponseError,
)
from .models import (
    BoundingRect,
    DefectRegion,
    EngineInfo,
    ImageResult,
    RotatedRect,
    SegmentationModuleInfo,
)
from .vimo_adapter import VimoSegmentationEngine, read_model_labels, read_model_modules

__all__ = [
    "BoundingRect",
    "DefectRegion",
    "EngineInfo",
    "ImageResult",
    "RotatedRect",
    "SegmentationModuleInfo",
    "VimoError",
    "VimoInferenceError",
    "VimoInitializationError",
    "VimoResponseError",
    "VimoSegmentationEngine",
    "read_model_labels",
    "read_model_modules",
]
