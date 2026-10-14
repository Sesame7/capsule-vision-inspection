"""Minimal Qt integration; the inference/runtime packages remain Qt independent."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inference_bridge import QtInferenceBridge

__all__ = ["QtInferenceBridge"]


def __getattr__(name: str) -> type[QtInferenceBridge]:
    # Pure display/identity consumers must not import Qt as a package side effect.
    if name == "QtInferenceBridge":
        from .inference_bridge import QtInferenceBridge

        return QtInferenceBridge
    raise AttributeError(name)
