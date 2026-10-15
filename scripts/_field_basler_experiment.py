"""S1-E2A-only finite continuous grab; reuse the verified SDK boundary/cleanup.

Private checked SDK helpers are intentionally reused locally in this experiment.
No device settings are written. Host buffer count/selector cursors are restored
by the existing backend; SDK pixels are independently copied before Release.
"""

from time import perf_counter

from vimo_demo.application._basler_sdk import (
    PylonBackend,
    _attr,  # pyright: ignore[reportPrivateUsage]
    _bool,  # pyright: ignore[reportPrivateUsage]
    _call,  # pyright: ignore[reportPrivateUsage]
    _set_attr,  # pyright: ignore[reportPrivateUsage]
)
from vimo_demo.application.basler import CameraReadiness


class ConversionTimer:
    """Observe the unchanged converter call, without touching SDK buffers."""

    def __init__(self, converter: object) -> None:
        self.converter = converter
        self.elapsed_ms = 0.0

    def ConvertToArray(self, result: object) -> object:
        start = perf_counter()
        converted = _call(self.converter, "ConvertToArray", result)
        self.elapsed_ms = (perf_counter() - start) * 1000
        return converted


class FiniteBackend(PylonBackend):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.conversion_timer: ConversionTimer | None = None

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.converter = _call(self.pylon, "ImageFormatConverter")
        _set_attr(
            self.converter,
            "OutputPixelFormat",
            _attr(self.pylon, "PixelType_BGR8packed"),
        )
        self.conversion_timer = ConversionTimer(self.converter)
        self.converter = self.conversion_timer
        for device in self.devices:
            _set_attr(_attr(device.camera, "MaxNumBuffer"), "Value", 3)
            # Same verified finite API, a session-wide bound instead of three.
            # One extra frame remains observable rather than auto-stopping at N.
            _call(
                device.camera,
                "StartGrabbingMax",
                self.limit,
                _attr(self.pylon, "GrabStrategy_OneByOne"),
                _attr(self.pylon, "GrabLoop_ProvidedByUser"),
            )
        rows: list[CameraReadiness] = []
        for device in self.devices:
            supported = _bool(_call(device.camera, "CanWaitForFrameTriggerReady"))
            ready = (
                _bool(
                    _call(
                        device.camera,
                        "WaitForFrameTriggerReady",
                        2000,
                        _attr(self.pylon, "TimeoutHandling_ThrowException"),
                    )
                )
                if supported
                else None
            )
            if not _bool(_call(device.camera, "IsGrabbing")) or ready is False:
                raise RuntimeError(f"Camera not armed: {device.serial}")
            rows.append(
                CameraReadiness(device.serial, supported, ready, perf_counter())
            )
        return tuple(rows)
