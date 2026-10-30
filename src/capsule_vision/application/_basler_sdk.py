"""Optional pypylon boundary for the continuous Basler owner.

Only this module knows SDK objects. pypylon is not a public dependency and is
absent in ordinary development. Its dynamic SWIG/GenApi interface is accessed
through local checked calls; values are narrowed before entering typed models.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from time import perf_counter
from types import MappingProxyType
from typing import cast

import numpy as np
from numpy.typing import NDArray

from .basler import (
    AcquisitionIssue,
    AcquisitionMetadata,
    BaslerDevice,
    CameraCleanup,
    CameraReadiness,
    CameraSettings,
    FeatureValue,
    acquisition_issue,
)

_FEATURES = (
    "AcquisitionMode",
    "TriggerSelector",
    "AcquisitionStatusSelector",
    "PixelFormat",
    "Width",
    "Height",
    "OffsetX",
    "OffsetY",
    "ReverseX",
    "ReverseY",
    "ExposureMode",
    "ExposureTime",
    "ExposureAuto",
    "Gain",
    "GainAuto",
    "AcquisitionFrameRateEnable",
    "AcquisitionFrameRate",
    "ChunkModeActive",
    "ChunkSelector",
    "ChunkEnable",
    "CounterSelector",
    "CounterEventSource",
    "CounterTriggerSource",
    "CounterResetSource",
)
_TRIGGER_FIELDS = ("TriggerMode", "TriggerSource", "TriggerActivation")


def _call(target: object, name: str, *args: object) -> object:
    method: object = getattr(target, name)
    if not callable(method):
        raise TypeError(f"SDK {name} is not callable")
    # Callable narrowing handles unannotated SWIG methods; each returned value
    # is subsequently narrowed before entering our typed data models.
    return method(*args)


def _attr(target: object, name: str) -> object:
    return getattr(target, name)


def _set_attr(target: object, name: str, value: object) -> None:
    # Dynamic SWIG properties, kept entirely inside the optional SDK boundary.
    setattr(target, name, value)


def _int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Expected SDK integer, got {type(value).__name__}")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Expected SDK boolean")
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected SDK string")
    return value


def _items(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("Expected SDK sequence")
    return tuple(cast(list[object] | tuple[object, ...], value))


def enumerate_devices() -> tuple[BaslerDevice, ...]:
    """Read only transport-layer metadata; never create/open a camera device."""
    pylon = import_module("pypylon.pylon")
    factory = _call(_attr(pylon, "TlFactory"), "GetInstance")
    return tuple(
        BaslerDevice(
            _str(_call(info, "GetSerialNumber")), _str(_call(info, "GetModelName"))
        )
        for info in _items(_call(factory, "EnumerateDevices"))
    )


@dataclass(slots=True)
class _Device:
    serial: str
    camera: object
    features: dict[str, FeatureValue] = field(default_factory=dict[str, FeatureValue])
    triggers: dict[str, dict[str, FeatureValue]] = field(
        default_factory=dict[str, dict[str, FeatureValue]]
    )
    max_buffers: int | None = None
    outstanding: object | None = None
    restored: bool = False


class PylonBackend:
    def __init__(self) -> None:
        # No pypylon import during package import, Folder startup or CLI --help.
        self.pylon = import_module("pypylon.pylon")
        self.genicam = import_module("pypylon.genicam")
        self.factory = _call(_attr(self.pylon, "TlFactory"), "GetInstance")
        self.devices: list[_Device] = []
        self.infos: dict[str, object] = {}
        self.converter: object | None = None

    def inventory(self, serials: tuple[str, ...]) -> None:
        infos = _items(_call(self.factory, "EnumerateDevices"))
        found = [_str(_call(info, "GetSerialNumber")) for info in infos]
        bad = [s for s in serials if found.count(s) != 1]
        if bad:
            raise RuntimeError(
                f"Required serials missing or duplicated: {bad}; found={found}"
            )
        self.infos = {s: infos[found.index(s)] for s in serials}
        for serial, info in self.infos.items():
            if not _bool(
                _call(
                    self.factory,
                    "IsDeviceAccessible",
                    info,
                    _attr(self.pylon, "DeviceAccessMode_Control"),
                )
            ):
                raise RuntimeError(f"Camera unavailable for control: {serial}")

    def _read(self, device: _Device, name: str) -> FeatureValue | None:
        node = _call(_call(device.camera, "GetNodeMap"), "GetNode", name)
        if node is None or not _bool(_call(self.genicam, "IsReadable", node)):
            return None
        value = _call(node, "GetValue")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"Invalid SDK feature {name}")
        return value

    def _select(self, device: _Device, name: str, setting: FeatureValue) -> None:
        if name not in ("TriggerSelector", "AcquisitionStatusSelector"):
            raise ValueError("Only selector cursors may be written")
        node = _call(_call(device.camera, "GetNodeMap"), "GetNode", name)
        if self._read(device, name) is None:
            raise RuntimeError(f"Cannot preserve {name}")
        if self._read(device, name) != setting:
            if not _bool(_call(self.genicam, "IsWritable", node)):
                raise RuntimeError(f"Cannot select {name}")
            _call(node, "SetValue", setting)
        if self._read(device, name) != setting:
            raise RuntimeError(f"Selector readback mismatch: {name}")

    def _triggers(self, device: _Device) -> dict[str, dict[str, FeatureValue]]:
        original = device.features["TriggerSelector"]
        node = _call(_call(device.camera, "GetNodeMap"), "GetNode", "TriggerSelector")
        rows: dict[str, dict[str, FeatureValue]] = {}
        try:
            for entry in _items(_call(node, "GetEntries")):
                if _bool(_call(self.genicam, "IsAvailable", entry)):
                    name = _str(_call(entry, "GetSymbolic"))
                    self._select(device, "TriggerSelector", name)
                    rows[name] = {}
                    for feature in _TRIGGER_FIELDS:
                        value = self._read(device, feature)
                        if value is not None:
                            rows[name][feature] = value
        finally:
            self._select(device, "TriggerSelector", original)
        return rows

    def prepare(self, serial: str) -> CameraSettings:
        camera = _call(self.pylon, "InstantCamera")
        device = _Device(serial, camera)
        self.devices.append(device)  # Own BEFORE any native attach/open/config work.
        _call(
            camera,
            "RegisterConfiguration",
            None,
            _attr(self.pylon, "RegistrationMode_ReplaceAll"),
            _attr(self.pylon, "Cleanup_None"),
        )
        _call(camera, "Attach", _call(self.factory, "CreateDevice", self.infos[serial]))
        _call(camera, "Open")
        info = _call(camera, "GetDeviceInfo")
        if _str(_call(info, "GetSerialNumber")) != serial:
            raise RuntimeError("Opened camera identity mismatch")
        for name in _FEATURES:
            value = self._read(device, name)
            if value is not None:
                device.features[name] = value
        status = _call(
            _call(camera, "GetNodeMap"), "GetNode", "AcquisitionStatusSelector"
        )
        if status is not None and _bool(_call(self.genicam, "IsWritable", status)):
            if "AcquisitionStatusSelector" not in device.features:
                raise RuntimeError("Cannot preserve writable AcquisitionStatusSelector")
        device.triggers = self._triggers(device)
        if device.features.get("AcquisitionMode") != "Continuous":
            raise RuntimeError(
                f"AcquisitionMode must already be Continuous; left unchanged: {device.features}"
            )
        expected = {
            "TriggerMode": "On",
            "TriggerSource": "Line1",
            "TriggerActivation": "RisingEdge",
        }
        if device.triggers.get("FrameStart") != expected or any(
            fields.get("TriggerMode") != "Off"
            for name, fields in device.triggers.items()
            if name != "FrameStart"
        ):
            raise RuntimeError(
                f"{serial}: incompatible hardware triggers; left unchanged: {device.triggers}"
            )
        device.max_buffers = _int(_attr(_attr(camera, "MaxNumBuffer"), "Value"))
        return CameraSettings(
            serial,
            MappingProxyType(device.features.copy()),
            MappingProxyType(
                {k: MappingProxyType(v.copy()) for k, v in device.triggers.items()}
            ),
            model=_str(_call(info, "GetModelName")),
        )

    def arm(self) -> tuple[CameraReadiness, ...]:
        self.converter = _call(self.pylon, "ImageFormatConverter")
        _set_attr(
            self.converter,
            "OutputPixelFormat",
            _attr(self.pylon, "PixelType_BGR8packed"),
        )
        for device in self.devices:
            _set_attr(_attr(device.camera, "MaxNumBuffer"), "Value", 3)
            _call(
                device.camera,
                "StartGrabbing",
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
            if not _bool(_call(device.camera, "IsGrabbing")):
                raise RuntimeError(f"Not grabbing: {device.serial}")
            rows.append(
                CameraReadiness(device.serial, supported, ready, perf_counter())
            )
        return tuple(rows)

    def _device(self, serial: str) -> _Device:
        return next(device for device in self.devices if device.serial == serial)

    def available(self, serial: str, timeout_ms: int) -> bool:
        camera = self._device(serial).camera
        return _bool(
            _call(_call(camera, "GetGrabResultWaitObject"), "Wait", timeout_ms)
        )

    def retrieve(
        self, serial: str
    ) -> tuple[AcquisitionMetadata, NDArray[np.uint8] | None]:
        device = self._device(serial)
        result = _call(
            device.camera,
            "RetrieveResult",
            1000,
            _attr(self.pylon, "TimeoutHandling_ThrowException"),
        )
        device.outstanding = result
        try:
            if not _bool(_call(result, "IsValid")):
                raise RuntimeError("Invalid grab result")
            actual = _str(
                _call(_call(device.camera, "GetDeviceInfo"), "GetSerialNumber")
            )
            if actual != serial:
                raise RuntimeError("Retrieved camera identity mismatch")

            def optional(name: str) -> int | None:
                return _int(_call(result, name)) if hasattr(result, name) else None

            row = AcquisitionMetadata(
                serial,
                perf_counter(),
                _bool(_call(result, "GrabSucceeded")),
                _int(_call(result, "GetErrorCode")),
                _str(_call(result, "GetErrorDescription")),
                _int(_call(result, "GetWidth")),
                _int(_call(result, "GetHeight")),
                _int(_call(result, "GetPixelType")),
                _int(_call(result, "GetPayloadSize")),
                optional("GetBlockID"),
                optional("GetImageNumber"),
                optional("GetTimeStamp"),
                optional("GetNumberOfSkippedImages"),
            )
            pixels: NDArray[np.uint8] | None = None
            if row.succeeded:
                converted = _call(self.converter, "ConvertToArray", result)
                if not isinstance(converted, np.ndarray):
                    raise TypeError("SDK conversion did not return an ndarray")
                array = cast(NDArray[np.generic], converted)
                if array.dtype != np.uint8 or array.shape != (row.height, row.width, 3):
                    raise ValueError("SDK BGR conversion changed shape/dtype")
                # Explicit independent copy BEFORE releasing any SDK/conversion storage.
                pixels = np.array(cast(NDArray[np.uint8], array), copy=True, order="C")
                pixels.setflags(write=False)
            return row, pixels
        finally:
            _call(result, "Release")
            if _bool(_call(result, "IsValid")):
                raise RuntimeError("SDK result still valid after Release")
            device.outstanding = None

    def _restore(self, device: _Device) -> None:
        if _bool(_call(device.camera, "IsGrabbing")):
            raise RuntimeError("Still grabbing; restoration unsafe")
        for name in ("AcquisitionStatusSelector", "TriggerSelector"):
            if name in device.features:
                self._select(device, name, device.features[name])
        if device.max_buffers is not None:
            node = _attr(device.camera, "MaxNumBuffer")
            _set_attr(node, "Value", device.max_buffers)
            if _int(_attr(node, "Value")) != device.max_buffers:
                raise RuntimeError("MaxNumBuffer restoration mismatch")
        if device.triggers and self._triggers(device) != device.triggers:
            raise RuntimeError("Trigger settings changed")
        for name, original in device.features.items():
            if name == "ExposureTime" and device.features.get("ExposureAuto") != "Off":
                continue
            if name == "Gain" and device.features.get("GainAuto") != "Off":
                continue
            if self._read(device, name) != original:
                raise RuntimeError(f"Settings changed: {name}")
        device.restored = True

    def cleanup(self) -> tuple[CameraCleanup, ...]:
        problems: dict[str, list[AcquisitionIssue]] = {
            d.serial: [] for d in self.devices
        }

        def action(
            device: _Device, label: str, operation: Callable[[], object]
        ) -> None:
            try:
                operation()
            except BaseException as exc:
                problems[device.serial].append(
                    acquisition_issue(label, device.serial, exc)
                )

        def observed(device: _Device, target: object, name: str) -> bool | None:
            try:
                return _bool(_call(target, name))
            except BaseException as exc:
                problems[device.serial].append(
                    acquisition_issue(f"verify_{name}", device.serial, exc)
                )
                return None

        # Stop ALL before restoring selector cursors on ANY camera.
        for device in self.devices:
            action(device, "stop", lambda d=device: _call(d.camera, "StopGrabbing"))
        rows: list[CameraCleanup] = []
        for device in self.devices:
            camera = device.camera
            if device.outstanding is not None:
                result = device.outstanding
                action(device, "release", lambda r=result: _call(r, "Release"))
                if observed(device, result, "IsValid") is False:
                    device.outstanding = None
            if observed(device, camera, "IsOpen") is True:
                action(device, "restore", lambda d=device: self._restore(d))
            elif not device.features:
                device.restored = (
                    True  # Open never succeeded; no selectors were changed.
                )
            action(device, "close", lambda c=camera: _call(c, "Close"))
            closed = observed(device, camera, "IsOpen") is False
            stopped = observed(device, camera, "IsGrabbing") is False
            if closed and stopped and device.outstanding is None:
                action(device, "detach", lambda c=camera: _call(c, "DestroyDevice"))
            detached = observed(device, camera, "IsPylonDeviceAttached") is False
            rows.append(
                CameraCleanup(
                    device.serial,
                    stopped,
                    device.restored and device.outstanding is None,
                    closed,
                    detached,
                    tuple(problems[device.serial]),
                )
            )
        if all(row.safe for row in rows):
            self.converter = None
        return tuple(rows)
