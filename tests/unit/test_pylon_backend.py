"""Deterministic optional-SDK contract tests; no cameras, sleeps, Qt or PLC calls."""

import subprocess
import sys
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Event, Thread
from typing import Self

import numpy as np
import pytest
from numpy.typing import NDArray

from capsule_vision.application import _basler_sdk as sdk_boundary
from capsule_vision.application import basler
from capsule_vision.application.continuous_basler import (
    ContinuousArmedAcquisition,
    ContinuousBaslerSource,
)
from capsule_vision.inspection import CameraId

SERIALS = ("25334188", "25334173", "25334184", "25334174", "25334180", "25334178")
CAMERAS = tuple(CameraId(f"CAM{i + 1}") for i in range(6))


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Info:
    serial: str

    def GetSerialNumber(self) -> str:
        return self.serial

    def GetModelName(self) -> str:
        return "acA-test"


@dataclass
class Entry:
    symbol: str

    def GetSymbolic(self) -> str:
        return self.symbol


class Node:
    def __init__(self, camera: Camera, name: str) -> None:
        self.camera, self.name = camera, name

    def GetValue(self) -> basler.FeatureValue:
        if self.name.startswith("Trigger") and self.name != "TriggerSelector":
            selector = str(self.camera.features["TriggerSelector"])
            return self.camera.triggers[selector][self.name]
        return self.camera.features[self.name]

    def SetValue(self, value: basler.FeatureValue) -> None:
        # Any attempted trigger-mode/source/activation change fails this test.
        assert self.name in ("TriggerSelector", "AcquisitionStatusSelector")
        self.camera.features[self.name] = value

    def GetEntries(self) -> tuple[Entry, ...]:
        return tuple(Entry(s) for s in self.camera.triggers)


@dataclass
class Buffers:
    Value: int = 10


class Grab:
    def __init__(
        self, serial: str, *, succeeded: bool = True, skipped: int = 0
    ) -> None:
        self.serial, self.succeeded, self.skipped = serial, succeeded, skipped
        self.valid = True
        self.release_failures = 0
        self.pixels: NDArray[np.uint8] = np.full((2, 4, 3), int(serial[-2:]), np.uint8)
        self.pixels[0, 0] = (11, 22, 33)  # Already BGR; no additional RGB swap.

    def IsValid(self) -> bool:
        return self.valid

    def GrabSucceeded(self) -> bool:
        return self.succeeded

    def GetErrorCode(self) -> int:
        return 0 if self.succeeded else 42

    def GetErrorDescription(self) -> str:
        return "" if self.succeeded else "Incomplete transport frame"

    def GetWidth(self) -> int:
        return 4

    def GetHeight(self) -> int:
        return 2

    def GetPixelType(self) -> int:
        return 17301514

    def GetPayloadSize(self) -> int:
        return 8

    def GetBlockID(self) -> int:
        return 123

    def GetImageNumber(self) -> int:
        return 7

    def GetTimeStamp(self) -> int:
        return 998877

    def GetNumberOfSkippedImages(self) -> int:
        return self.skipped

    def Release(self) -> None:
        if self.release_failures:
            self.release_failures -= 1
            raise OSError("Release failed once")
        self.valid = False
        self.pixels.fill(239)  # Emulate reuse of vendor/conversion storage.


class Camera:
    def __init__(self, sdk: SDK) -> None:
        self.sdk = sdk
        self.serial = "unattached"
        self.attached = self.opened = self.grabbing = self.registered = False
        self.MaxNumBuffer = Buffers()
        self.features: dict[str, basler.FeatureValue] = {
            "AcquisitionMode": "Continuous",
            "TriggerSelector": "FrameStart",
            "AcquisitionStatusSelector": "AcquisitionActive",
            "PixelFormat": "BayerGB8",
            "Width": 4,
            "Height": 2,
            "OffsetX": 144,
            "OffsetY": 64,
            "ReverseX": True,
            "ReverseY": False,
            "ExposureMode": "Timed",
            "ExposureTime": 500,
            "ExposureAuto": "Off",
            "Gain": 0,
            "GainAuto": "Off",
        }
        self.triggers: dict[str, dict[str, basler.FeatureValue]] = {
            name: {
                "TriggerMode": mode,
                "TriggerSource": "Line1",
                "TriggerActivation": "RisingEdge",
            }
            for name, mode in (("FrameStart", "On"), ("FrameBurstStart", "Off"))
        }
        self.queue: list[Grab] = []

    def RegisterConfiguration(self, config: object, mode: int, cleanup: int) -> None:
        assert not self.attached and not self.opened and config is None
        assert (mode, cleanup) == (1, 2)
        self.registered = True

    def Attach(self, info: Info) -> None:
        assert self.registered
        self.serial, self.attached = info.serial, True
        if self.serial == self.sdk.wrong_source:
            self.triggers["FrameStart"]["TriggerSource"] = "Line2"

    def Open(self) -> None:
        assert self.registered and self.attached
        if self.serial == self.sdk.open_failure:
            raise OSError("Camera open failed")
        self.opened = True

    def GetDeviceInfo(self) -> Info:
        return Info(
            "different" if self.serial == self.sdk.wrong_identity else self.serial
        )

    def GetNodeMap(self) -> Self:
        return self

    def GetNode(self, name: str) -> Node | None:
        if name in self.features or name in self.triggers["FrameStart"]:
            return Node(self, name)
        return None

    def StartGrabbing(self, strategy: int, loop: int) -> None:
        assert self.MaxNumBuffer.Value == 3
        assert (strategy, loop) == (3, 4)
        self.sdk.events.append((self.serial, "start"))
        self.grabbing = True
        if self.sdk.pre_arm == self.serial:
            self.sdk.deliver(self.serial)

    def CanWaitForFrameTriggerReady(self) -> bool:
        return self.serial != self.sdk.unsupported_ready

    def WaitForFrameTriggerReady(self, timeout: int, handling: int) -> bool:
        assert (timeout, handling) == (2000, 5)
        self.features["AcquisitionStatusSelector"] = "FrameTriggerWait"
        return self.serial != self.sdk.not_ready

    def IsGrabbing(self) -> bool:
        return self.grabbing

    def GetGrabResultWaitObject(self) -> Self:
        return self

    def Wait(self, milliseconds: int) -> bool:
        assert 0 <= milliseconds <= 25
        if self.queue:
            return True
        self.sdk.clock.now += milliseconds / 1000
        return bool(self.queue)

    def RetrieveResult(self, timeout: int, handling: int) -> Grab:
        assert (timeout, handling) == (1000, 5)
        return self.queue.pop(0)

    def StopGrabbing(self) -> None:
        self.sdk.events.append((self.serial, "stop"))
        self.grabbing = False
        for grab in self.queue:
            grab.Release()
        self.queue.clear()

    def IsOpen(self) -> bool:
        return self.opened

    def Close(self) -> None:
        self.sdk.events.append((self.serial, "close"))
        if self.serial == self.sdk.close_failure:
            raise OSError("Camera close failed")
        self.opened = self.grabbing = False

    def DestroyDevice(self) -> None:
        assert not self.opened and not self.grabbing
        self.attached = False

    def IsPylonDeviceAttached(self) -> bool:
        return self.attached


class Converter:
    OutputPixelFormat: int = 0

    def ConvertToArray(self, grab: Grab) -> NDArray[np.uint8]:
        assert self.OutputPixelFormat == 6
        return grab.pixels


class Genicam:
    @staticmethod
    def IsReadable(node: object) -> bool:
        return node is not None

    @staticmethod
    def IsWritable(node: object) -> bool:
        return node is not None

    @staticmethod
    def IsAvailable(node: object) -> bool:
        return node is not None


class SDK:
    RegistrationMode_ReplaceAll = 1
    Cleanup_None = 2
    GrabStrategy_OneByOne = 3
    GrabLoop_ProvidedByUser = 4
    TimeoutHandling_ThrowException = 5
    PixelType_BGR8packed = 6
    DeviceAccessMode_Control = 7
    ImageFormatConverter = Converter

    def __init__(self) -> None:
        self.clock = Clock()
        self.infos = [Info(s) for s in sorted(SERIALS)]  # Different enumeration order.
        self.cameras: list[Camera] = []
        self.grabs: list[Grab] = []
        self.events: list[tuple[str, str]] = []
        self.inaccessible = self.wrong_identity = self.wrong_source = ""
        self.not_ready = self.unsupported_ready = self.pre_arm = ""
        self.open_failure = self.close_failure = ""

    @property
    def TlFactory(self) -> Self:
        return self

    def GetInstance(self) -> Self:
        return self

    def EnumerateDevices(self) -> tuple[Info, ...]:
        return tuple(self.infos)

    def IsDeviceAccessible(self, info: Info, access: int) -> bool:
        assert access == 7
        return info.serial != self.inaccessible

    def InstantCamera(self) -> Camera:
        camera = Camera(self)
        self.cameras.append(camera)
        return camera

    def CreateDevice(self, info: Info) -> Info:
        return info

    def deliver(self, serial: str, *, succeeded: bool = True, skipped: int = 0) -> Grab:
        grab = Grab(serial, succeeded=succeeded, skipped=skipped)
        self.grabs.append(grab)
        next(c for c in self.cameras if c.serial == serial).queue.append(grab)
        return grab


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> SDK:
    instance = SDK()
    modules: dict[str, object] = {"pypylon.pylon": instance, "pypylon.genicam": Genicam}

    def load(name: str) -> object:
        return modules[name]

    monkeypatch.setattr(sdk_boundary, "import_module", load)
    monkeypatch.setattr(sdk_boundary, "perf_counter", instance.clock)
    return instance


def test_read_only_device_discovery_never_creates_or_opens_cameras(sdk: SDK) -> None:
    assert basler.discover_basler_devices() == tuple(
        basler.BaslerDevice(s, "acA-test") for s in sorted(SERIALS)
    )
    assert not sdk.cameras and not sdk.events and not sdk.grabs


def test_device_discovery_preserves_optional_sdk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ModuleNotFoundError("pypylon unavailable")

    def unavailable(name: str) -> object:
        raise error

    monkeypatch.setattr(sdk_boundary, "import_module", unavailable)
    with pytest.raises(ModuleNotFoundError) as observed:
        basler.discover_basler_devices()
    assert observed.value is error


def assert_closed(sdk: SDK) -> None:
    assert all(not c.opened and not c.attached and not c.grabbing for c in sdk.cameras)
    assert all(c.MaxNumBuffer.Value == 10 for c in sdk.cameras)
    assert all(c.features["TriggerSelector"] == "FrameStart" for c in sdk.cameras)
    assert all(
        c.features["AcquisitionStatusSelector"] == "AcquisitionActive"
        for c in sdk.cameras
    )
    assert all(not grab.valid for grab in sdk.grabs)
    first_close = next(i for i, (_, event) in enumerate(sdk.events) if event == "close")
    assert sum(event == "stop" for _, event in sdk.events[:first_close]) == len(
        sdk.cameras
    )


def armed_backend() -> sdk_boundary.PylonBackend:
    backend = sdk_boundary.PylonBackend()
    backend.inventory(SERIALS)
    for serial in SERIALS:
        backend.prepare(serial)
    backend.arm()
    return backend


def test_complete_order_metadata_and_owned_pixels(sdk: SDK) -> None:
    backend = sdk_boundary.PylonBackend()
    try:
        backend.inventory(SERIALS)
        settings = tuple(backend.prepare(s) for s in SERIALS)
        assert tuple(row.serial for row in settings) == SERIALS
        assert all(row.model == "acA-test" for row in settings)
        readiness = backend.arm()
        assert tuple(row.serial for row in readiness) == SERIALS
        assert all(row.supported and row.ready for row in readiness)
        for serial in reversed(SERIALS):
            sdk.deliver(serial)
        for serial in SERIALS:
            assert backend.available(serial, 0)
            metadata, pixels = backend.retrieve(serial)
            assert metadata.serial == serial and pixels is not None
            assert pixels.shape == (2, 4, 3) and pixels.dtype == np.uint8
            assert pixels.flags.owndata and pixels.flags.c_contiguous
            assert not pixels.flags.writeable and pixels.base is None
            assert np.array_equal(pixels[0, 0], (11, 22, 33))
            assert int(pixels[1, 1, 0]) == int(serial[-2:])
            assert (metadata.block_id, metadata.image_number) == (123, 7)
            assert metadata.camera_timestamp == 998877
            assert all(not np.shares_memory(pixels, grab.pixels) for grab in sdk.grabs)
    finally:
        assert all(row.safe for row in backend.cleanup())
    assert_closed(sdk)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "inaccessible"])
def test_inventory_failure_never_opens_or_arms(sdk: SDK, problem: str) -> None:
    if problem == "missing":
        sdk.infos.pop()
    elif problem == "duplicate":
        sdk.infos.append(sdk.infos[0])
    else:
        sdk.inaccessible = SERIALS[0]
    backend = sdk_boundary.PylonBackend()
    with pytest.raises(RuntimeError, match="missing or duplicated|unavailable"):
        backend.inventory(SERIALS)
    assert not sdk.cameras and not sdk.events and backend.cleanup() == ()


@pytest.mark.parametrize(
    "problem,error,message",
    [
        ("wrong_identity", RuntimeError, "identity mismatch"),
        ("wrong_source", RuntimeError, "incompatible hardware triggers"),
        ("open_failure", OSError, "Camera open failed"),
        ("not_ready", RuntimeError, "camera readiness"),
        ("pre_arm", RuntimeError, "Pre-ARMED"),
    ],
)
def test_preparation_and_arming_failures_cleanup_without_invitation(
    sdk: SDK, problem: str, error: type[Exception], message: str
) -> None:
    setattr(sdk, problem, SERIALS[2])
    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    with pytest.raises(error, match=message):
        source.run(
            Event(),
            begin=lambda _: pytest.fail("No cycle may begin"),
            submit=lambda _: pytest.fail("No frame may be admitted"),
            on_armed=lambda _: pytest.fail("Failed preparation cannot report ARMED"),
        )
    assert_closed(sdk)
    assert source.cleanup_safe
    if problem in ("wrong_identity", "wrong_source", "open_failure"):
        assert all(event != "start" for _, event in sdk.events)


def test_unsupported_readiness_is_reported_without_fabrication(sdk: SDK) -> None:
    sdk.unsupported_ready = SERIALS[0]
    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    stop = Event()

    def armed(event: ContinuousArmedAcquisition) -> None:
        assert event.readiness[0].ready is None
        assert not event.readiness[0].supported
        stop.set()

    source.run(stop, begin=lambda _: None, submit=lambda _: None, on_armed=armed)
    assert stop.is_set() and source.cleanup_safe
    assert_closed(sdk)


@pytest.mark.parametrize("skipped", [False, True])
def test_failed_or_skipped_result_is_explicit_and_released(
    sdk: SDK, skipped: bool
) -> None:
    backend = armed_backend()
    try:
        sdk.deliver(SERIALS[0], succeeded=skipped, skipped=int(skipped))
        row, pixels = backend.retrieve(SERIALS[0])
        assert row.serial == SERIALS[0]
        assert row.succeeded == skipped and row.skipped_images == int(skipped)
        assert row.error_code == (0 if skipped else 42)
        assert (pixels is not None) == skipped
        assert all(not grab.valid for grab in sdk.grabs)
    finally:
        assert all(row.safe for row in backend.cleanup())
    assert_closed(sdk)


def test_release_error_remains_explicit_and_cleanup_releases_buffer(sdk: SDK) -> None:
    backend = armed_backend()
    try:
        grab = sdk.deliver(SERIALS[0])
        grab.release_failures = 1
        with pytest.raises(OSError, match="Release failed once"):
            backend.retrieve(SERIALS[0])
        assert grab.valid
    finally:
        assert all(row.safe for row in backend.cleanup())
    assert_closed(sdk)


def test_conversion_failure_releases_sdk_buffers(sdk: SDK) -> None:
    backend = armed_backend()
    try:
        sdk.deliver(SERIALS[0]).pixels = np.zeros((2, 4, 4), dtype=np.uint8)
        with pytest.raises(ValueError, match="conversion changed shape/dtype"):
            backend.retrieve(SERIALS[0])
        assert all(not grab.valid for grab in sdk.grabs)
    finally:
        assert all(row.safe for row in backend.cleanup())
    assert_closed(sdk)


def test_changed_camera_settings_are_reported_without_silent_repair(sdk: SDK) -> None:
    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    stop = Event()

    def armed(_event: ContinuousArmedAcquisition) -> None:
        sdk.cameras[0].features["PixelFormat"] = "Unexpected external change"
        stop.set()

    source.run(stop, begin=lambda _: None, submit=lambda _: None, on_armed=armed)
    assert not source.cleanup_safe and not source.cleanup[0].settings_restored
    assert any(
        "Settings changed" in issue.message for issue in source.cleanup[0].issues
    )
    assert sdk.cameras[0].features["PixelFormat"] == "Unexpected external change"
    assert_closed(sdk)


def test_uncertain_close_retains_native_resources_until_explicit_cleanup(
    sdk: SDK,
) -> None:
    sdk.close_failure = SERIALS[2]
    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    stop = Event()
    source.run(
        stop, begin=lambda _: None, submit=lambda _: None, on_armed=lambda _: stop.set()
    )
    assert not source.cleanup_safe
    assert sdk.cameras[2].opened and sdk.cameras[2].attached
    assert not source.cleanup[2].closed and not source.cleanup[2].detached
    assert all(not camera.opened for i, camera in enumerate(sdk.cameras) if i != 2)
    sdk.close_failure = ""
    source.retry_cleanup()
    assert source.cleanup_safe and len(sdk.cameras) == 6
    assert_closed(sdk)


@pytest.mark.parametrize("control", [False, True])
def test_armed_callback_failure_and_control_exit_cleanup(
    sdk: SDK, control: bool
) -> None:
    def fail(_: ContinuousArmedAcquisition) -> None:
        raise KeyboardInterrupt if control else ValueError("Notification failed")

    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    with pytest.raises(KeyboardInterrupt if control else ValueError):
        source.run(Event(), begin=lambda _: None, submit=lambda _: None, on_armed=fail)
    assert source.cleanup_safe
    assert_closed(sdk)


def test_explicit_unique_five_or_six_identities_required() -> None:
    assert ContinuousBaslerSource(CAMERAS[:5], SERIALS[:5]).serials == SERIALS[:5]
    for serials in (
        SERIALS[:4],
        (*SERIALS, "7"),
        (*SERIALS[:5], SERIALS[0]),
        (*SERIALS[:5], " CAM6"),
        (*SERIALS[:5], ""),
        (*SERIALS[:5], "１２３"),
        (*SERIALS[:5], "123a"),
    ):
        with pytest.raises(ValueError, match="unique, explicit numeric serials"):
            ContinuousBaslerSource(CAMERAS, serials)


def test_default_folder_import_does_not_load_optional_sdk() -> None:
    code = """
import sys
sys.modules['pypylon'] = None
from capsule_vision.application.folder_replay import FolderReplay
from capsule_vision.application.continuous_basler import ContinuousBaslerSource
from capsule_vision.inspection import CameraId
ContinuousBaslerSource(tuple(CameraId(f'CAM{i+1}') for i in range(6)), tuple(str(i+1) for i in range(6)))
assert 'pypylon.pylon' not in sys.modules
assert 'PySide6' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_missing_optional_sdk_is_explicit_before_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ModuleNotFoundError("pypylon is an external prerequisite")

    def missing(_: str) -> object:
        raise error

    monkeypatch.setattr(sdk_boundary, "import_module", missing)
    source = ContinuousBaslerSource(CAMERAS, SERIALS)
    with pytest.raises(ModuleNotFoundError) as caught:
        source.run(
            Event(),
            begin=lambda _: None,
            submit=lambda _: None,
            on_armed=lambda _: None,
        )
    assert caught.value is error and source.cleanup_safe


def test_stopped_source_does_not_open_or_arm(sdk: SDK) -> None:
    stop = Event()
    stop.set()
    source = ContinuousBaslerSource(CAMERAS, SERIALS)
    source.run(
        stop,
        begin=lambda _: None,
        submit=lambda _: None,
        on_armed=lambda _: pytest.fail("Stopped"),
    )
    assert not sdk.cameras and source.cleanup_safe


def test_stop_cannot_force_a_blocked_sdk_wait(
    sdk: SDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, stop = Event(), Event(), Event()
    original = Camera.Wait

    def wait(camera: Camera, milliseconds: int) -> bool:
        if milliseconds:
            entered.set()
            assert release.wait(5)
        return original(camera, milliseconds)

    monkeypatch.setattr(Camera, "Wait", wait)
    source = ContinuousBaslerSource(CAMERAS, SERIALS, clock=sdk.clock)
    result: Future[None] = Future()

    def run() -> None:
        try:
            source.run(
                stop,
                begin=lambda _: None,
                submit=lambda _: None,
                on_armed=lambda _: None,
            )
            result.set_result(None)
        except BaseException as exc:
            result.set_exception(exc)

    owner = Thread(target=run, name="BaslerStopTest")
    owner.start()
    try:
        assert entered.wait(5)
        stop.set()
        assert not result.done() and not source.cleanup_safe
        assert all(camera.opened for camera in sdk.cameras)
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    result.result(1)
    assert source.cleanup_safe
    assert_closed(sdk)
