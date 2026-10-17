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

from vimo_demo.application import _basler_sdk as sdk_boundary
from vimo_demo.application import basler
from vimo_demo.application.basler import (
    AcquisitionGroup,
    BaslerAcquisitionCancelled,
    BaslerAcquisitionError,
    BaslerAcquisitionSource,
)

SERIALS = ("25334188", "25334173", "25334184", "25334174", "25334180", "25334178")


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

    def StartGrabbingMax(self, count: int, strategy: int, loop: int) -> None:
        assert count == 3 and self.MaxNumBuffer.Value == 3
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
        self.sdk.release_due()
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
        self.scheduled: list[tuple[float, str]] = []
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

    def trigger(self, _: basler.ArmedAcquisition) -> None:
        for serial in reversed(SERIALS):
            self.deliver(serial)

    def release_due(self) -> None:
        due = [row for row in self.scheduled if row[0] <= self.clock.now]
        self.scheduled = [row for row in self.scheduled if row[0] > self.clock.now]
        for _, serial in due:
            self.deliver(serial)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> SDK:
    instance = SDK()
    modules: dict[str, object] = {"pypylon.pylon": instance, "pypylon.genicam": Genicam}

    def load(name: str) -> object:
        return modules[name]

    monkeypatch.setattr(sdk_boundary, "import_module", load)
    monkeypatch.setattr(basler, "perf_counter", instance.clock)
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


def test_complete_order_metadata_and_owned_pixels(sdk: SDK) -> None:
    source = BaslerAcquisitionSource(SERIALS, timeout=0.5)
    group = source.acquire(on_armed=sdk.trigger)
    assert tuple(image.serial for image in group.images) == SERIALS
    assert tuple(row.serial for row in group.report.settings) == SERIALS
    assert all(row.model == "acA-test" for row in group.report.settings)
    assert group.report.armed is not None
    assert all(row.ready for row in group.report.armed.readiness)
    assert group.report.finished_at >= group.report.armed.deadline
    assert not group.report.issues and group.report.cleanup_safe
    assert not group.report.missing_serials and not group.report.extra_serials
    for image in group.images:
        assert image.pixels.shape == (2, 4, 3) and image.pixels.dtype == np.uint8
        assert image.pixels.flags.owndata and image.pixels.flags.c_contiguous
        assert not image.pixels.flags.writeable and image.pixels.base is None
        assert np.array_equal(image.pixels[0, 0], (11, 22, 33))
        assert int(image.pixels[1, 1, 0]) == int(image.serial[-2:])
        assert (image.metadata.block_id, image.metadata.image_number) == (123, 7)
        assert image.metadata.camera_timestamp == 998877
        assert all(
            not np.shares_memory(image.pixels, grab.pixels) for grab in sdk.grabs
        )
    assert_closed(sdk)
    assert source.close() == ()


@pytest.mark.parametrize("problem", ["missing", "duplicate", "inaccessible"])
def test_inventory_failure_never_opens_or_arms(sdk: SDK, problem: str) -> None:
    if problem == "missing":
        sdk.infos.pop()
    elif problem == "duplicate":
        sdk.infos.append(sdk.infos[0])
    else:
        sdk.inaccessible = SERIALS[0]
    with pytest.raises(BaslerAcquisitionError) as caught:
        BaslerAcquisitionSource(SERIALS).acquire()
    assert caught.value.report.armed is None and not sdk.cameras
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(
    "problem",
    ["wrong_identity", "wrong_source", "open_failure", "not_ready", "pre_arm"],
)
def test_preparation_and_arming_failures_cleanup_without_invitation(
    sdk: SDK, problem: str
) -> None:
    setattr(sdk, problem, SERIALS[2])
    with pytest.raises(BaslerAcquisitionError) as caught:
        BaslerAcquisitionSource(SERIALS).acquire(on_armed=sdk.trigger)
    assert caught.value.report.armed is None
    assert_closed(sdk)
    assert caught.value.report.cleanup_safe
    if problem in ("wrong_identity", "wrong_source", "open_failure"):
        assert all(event != "start" for _, event in sdk.events)


def test_unsupported_readiness_is_reported_without_fabrication(sdk: SDK) -> None:
    sdk.unsupported_ready = SERIALS[0]
    result = BaslerAcquisitionSource(SERIALS, timeout=0.1).acquire(on_armed=sdk.trigger)
    assert result.report.armed is not None
    assert result.report.armed.readiness[0].ready is None
    assert not result.report.armed.readiness[0].supported
    assert_closed(sdk)


@pytest.mark.parametrize("received", [0, 5])
def test_timeout_missing_does_not_return_partial_success(
    sdk: SDK, received: int
) -> None:
    def trigger(_: basler.ArmedAcquisition) -> None:
        for serial in SERIALS[:received]:
            sdk.deliver(serial)

    with pytest.raises(BaslerAcquisitionError) as caught:
        BaslerAcquisitionSource(SERIALS, timeout=0.2).acquire(on_armed=trigger)
    report = caught.value.report
    assert report.missing_serials == SERIALS[received:]
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert len(report.observations) == received
    assert_closed(sdk)


def test_extra_frame_after_complete_set_is_not_missed(sdk: SDK) -> None:
    def trigger(armed: basler.ArmedAcquisition) -> None:
        sdk.trigger(armed)
        sdk.scheduled.append((armed.armed_at + 0.15, SERIALS[2]))

    with pytest.raises(BaslerAcquisitionError, match="extra frame") as caught:
        BaslerAcquisitionSource(SERIALS, timeout=0.5).acquire(on_armed=trigger)
    assert len(caught.value.report.observations) == 7
    assert caught.value.report.extra_serials == (SERIALS[2],)
    assert_closed(sdk)


@pytest.mark.parametrize("skipped", [False, True])
def test_failed_or_skipped_result_is_explicit_and_released(
    sdk: SDK, skipped: bool
) -> None:
    def trigger(_: basler.ArmedAcquisition) -> None:
        sdk.deliver(SERIALS[0], succeeded=skipped, skipped=int(skipped))

    with pytest.raises(BaslerAcquisitionError) as caught:
        BaslerAcquisitionSource(SERIALS, timeout=0.2).acquire(on_armed=trigger)
    row = caught.value.report.observations[0]
    assert row.serial == SERIALS[0]
    assert row.error_code == (0 if skipped else 42)
    assert_closed(sdk)


def test_release_error_retried_during_cleanup_and_cause_preserved(sdk: SDK) -> None:
    def trigger(_: basler.ArmedAcquisition) -> None:
        sdk.deliver(SERIALS[0]).release_failures = 1

    with pytest.raises(BaslerAcquisitionError, match="Release failed once") as caught:
        BaslerAcquisitionSource(SERIALS, timeout=0.2).acquire(on_armed=trigger)
    assert isinstance(caught.value.__cause__, OSError)
    assert_closed(sdk)


def test_conversion_failure_releases_sdk_buffers(sdk: SDK) -> None:
    def trigger(_: basler.ArmedAcquisition) -> None:
        sdk.deliver(SERIALS[0]).pixels = np.zeros((2, 4, 4), dtype=np.uint8)

    with pytest.raises(BaslerAcquisitionError, match="conversion changed shape"):
        BaslerAcquisitionSource(SERIALS, timeout=0.2).acquire(on_armed=trigger)
    assert_closed(sdk)


def test_changed_camera_settings_are_reported_without_silent_repair(sdk: SDK) -> None:
    def trigger(armed: basler.ArmedAcquisition) -> None:
        sdk.trigger(armed)
        sdk.cameras[0].features["PixelFormat"] = "Unexpected external change"

    source = BaslerAcquisitionSource(SERIALS, timeout=0.2)
    with pytest.raises(BaslerAcquisitionError, match="Settings changed") as caught:
        source.acquire(on_armed=trigger)
    assert not caught.value.report.cleanup_safe
    assert sdk.cameras[0].features["PixelFormat"] == "Unexpected external change"
    assert_closed(sdk)
    with pytest.raises(RuntimeError, match="cleanup unresolved"):
        source.acquire()


def test_reentrant_attempt_is_rejected_without_losing_first_owner(sdk: SDK) -> None:
    source = BaslerAcquisitionSource(SERIALS, timeout=0.2)

    def trigger(armed: basler.ArmedAcquisition) -> None:
        with pytest.raises(RuntimeError, match="Acquisition active"):
            source.acquire()
        with pytest.raises(RuntimeError, match="acquisition is active"):
            source.close()
        sdk.trigger(armed)

    assert len(source.acquire(on_armed=trigger).images) == 6
    assert_closed(sdk)


def test_uncertain_close_blocks_reuse_until_actual_cleanup(sdk: SDK) -> None:
    sdk.close_failure = SERIALS[2]
    source = BaslerAcquisitionSource(SERIALS, timeout=0.1)
    with pytest.raises(BaslerAcquisitionError) as caught:
        source.acquire(on_armed=sdk.trigger)
    assert not caught.value.report.cleanup_safe
    assert sdk.cameras[2].opened and sdk.cameras[2].attached
    assert all(not camera.opened for i, camera in enumerate(sdk.cameras) if i != 2)
    with pytest.raises(RuntimeError, match="cleanup unresolved"):
        source.acquire()
    sdk.close_failure = ""
    assert all(row.safe for row in source.close())
    assert_closed(sdk)


@pytest.mark.parametrize("control", [False, True])
def test_armed_callback_failure_and_control_exit_cleanup(
    sdk: SDK, control: bool
) -> None:
    def fail(_: basler.ArmedAcquisition) -> None:
        raise KeyboardInterrupt if control else ValueError("Notification failed")

    with pytest.raises(KeyboardInterrupt if control else BaslerAcquisitionError):
        BaslerAcquisitionSource(SERIALS).acquire(on_armed=fail)
    assert_closed(sdk)


@pytest.mark.parametrize(
    "timeout", [0.0, -1.0, float("nan"), float("inf"), 121.0, True]
)
def test_invalid_timeout_before_sdk_import(timeout: float) -> None:
    with pytest.raises(ValueError):
        BaslerAcquisitionSource(SERIALS, timeout=timeout)


def test_explicit_unique_six_identities_required() -> None:
    for serials in (SERIALS[:5], (*SERIALS[:5], SERIALS[0]), (*SERIALS[:5], " CAM6")):
        with pytest.raises(ValueError):
            BaslerAcquisitionSource(serials)


def test_default_folder_import_does_not_load_optional_sdk() -> None:
    code = """
import sys
sys.modules['pypylon'] = None
from vimo_demo.application.folder_replay import FolderReplay
from vimo_demo.application.basler import BaslerAcquisitionSource
assert 'pypylon.pylon' not in sys.modules
assert 'PySide6' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_missing_optional_sdk_is_a_chained_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_: str) -> object:
        raise ModuleNotFoundError("pypylon is an external prerequisite")

    monkeypatch.setattr(sdk_boundary, "import_module", missing)
    with pytest.raises(BaslerAcquisitionError) as caught:
        BaslerAcquisitionSource(SERIALS).acquire()
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    assert caught.value.report.issues[0].stage == "initialization"
    assert caught.value.report.armed is None


def test_pre_cancelled_attempt_does_not_open_or_arm(sdk: SDK) -> None:
    cancel = Event()
    cancel.set()
    source = BaslerAcquisitionSource(SERIALS)
    with pytest.raises(BaslerAcquisitionCancelled) as caught:
        source.acquire(cancel=cancel)
    assert not sdk.cameras and caught.value.report.armed is None
    assert source.cleanup_safe


def test_cancel_after_armed_discards_complete_queued_group(sdk: SDK) -> None:
    cancel = Event()

    def triggered(armed: basler.ArmedAcquisition) -> None:
        sdk.trigger(armed)
        cancel.set()

    with pytest.raises(BaslerAcquisitionCancelled):
        BaslerAcquisitionSource(SERIALS).acquire(on_armed=triggered, cancel=cancel)
    assert_closed(sdk)


def test_cancel_during_cleanup_never_returns_a_late_group(
    sdk: SDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = Event()
    original = Camera.Close

    def close(camera: Camera) -> None:
        original(camera)
        cancel.set()

    monkeypatch.setattr(Camera, "Close", close)
    with pytest.raises(BaslerAcquisitionCancelled) as caught:
        BaslerAcquisitionSource(SERIALS, timeout=0.1).acquire(
            on_armed=sdk.trigger, cancel=cancel
        )
    assert len(caught.value.report.observations) == 6
    assert caught.value.report.cleanup_safe
    assert_closed(sdk)


def test_cancel_cannot_force_a_blocked_sdk_wait(
    sdk: SDK, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, cancel = Event(), Event(), Event()
    original = Camera.Wait

    def wait(camera: Camera, milliseconds: int) -> bool:
        if milliseconds:
            entered.set()
            assert release.wait(5)
        return original(camera, milliseconds)

    monkeypatch.setattr(Camera, "Wait", wait)
    source = BaslerAcquisitionSource(SERIALS)
    result: Future[AcquisitionGroup] = Future()

    def acquire() -> None:
        try:
            result.set_result(source.acquire(cancel=cancel))
        except BaseException as exc:
            result.set_exception(exc)

    owner = Thread(target=acquire, name="BaslerCancellationTest")
    owner.start()
    try:
        assert entered.wait(5)
        cancel.set()
        assert not result.done() and not source.cleanup_safe
        assert all(camera.opened for camera in sdk.cameras)
    finally:
        release.set()
        owner.join(5)
    assert not owner.is_alive()
    with pytest.raises(BaslerAcquisitionCancelled):
        result.result(1)
    assert source.cleanup_safe
    assert_closed(sdk)
