"""Shared synthetic two-body Capsule inputs, without production calibration."""

import cv2
import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection.production.capsule import (
    CapsuleSettings,
    ColorSettings,
    SizeSettings,
)

ROI = BoundingRect(0, 0, 80, 120)
SETTINGS = CapsuleSettings(SizeSettings(True, 20, 0, 30, 0), ColorSettings(True, 30, 0))


def bodies(hue: int = 30, brightness: int = 180) -> NDArray[np.uint8]:
    hsv = np.zeros((120, 80, 3), dtype=np.uint8)
    hsv[10:40, 20:40] = (hue, 240, brightness)
    hsv[70:100, 20:40] = (hue, 240, brightness)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR).astype(np.uint8, copy=False)
