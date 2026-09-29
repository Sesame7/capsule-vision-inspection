"""Minimal Qt integration; the inference/runtime packages remain Qt independent."""

from .inference_bridge import QtInferenceBridge

__all__ = ["QtInferenceBridge"]
