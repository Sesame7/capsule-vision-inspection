"""Display ownership and presentation calculations, independent of Qt rendering."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from weakref import ref

import numpy as np
import pytest
from tests.unit.test_production_application import production
from tests.unit.test_production_inspection import source_frame

from vimo_demo.application import (
    ApplicationState,
    ProductionApplicationSnapshot,
    ProductionCounters,
)
from vimo_demo.inference import BoundingRect
from vimo_demo.inspection import CameraId, CycleId, Frame, FrameId
from vimo_demo.inspection.production import Assessment, ProductJudgment, TechnicalStatus
from vimo_demo.ui.display_frames import LatestDisplayFrameStore
from vimo_demo.ui.production_presentation import (
    Indicator,
    controls,
    grid_columns,
    image_transform,
    indicator,
    matching_camera,
)
from vimo_demo.ui.production_style import STRINGS, text


def snapshot(
    state: ApplicationState = ApplicationState.READY,
) -> ProductionApplicationSnapshot:
    return ProductionApplicationSnapshot(
        state, 1, None, None, None, ProductionCounters(), None, False, None
    )


@pytest.mark.parametrize(
    "width,height,columns",
    [
        (1080, 1920, 2),
        (900, 1200, 2),
        (1920, 1080, 3),
        (1199, 1000, 2),
        (1200, 1000, 3),
    ],
)
def test_grid_breakpoint(width: int, height: int, columns: int) -> None:
    assert grid_columns(width, height) == columns


@pytest.mark.parametrize(
    "viewport,expected",
    [
        ((400, 400), (0, 100, 400, 200)),
        ((600, 200), (100, 0, 400, 200)),
        ((800, 400), (0, 0, 800, 400)),
    ],
)
def test_letterbox_and_overlay(
    viewport: tuple[int, int], expected: tuple[int, ...]
) -> None:
    transform = image_transform(200, 100, *viewport)
    assert (transform.x, transform.y, transform.width, transform.height) == expected
    box = transform.rectangle(BoundingRect(20, 10, 40, 30))
    assert box == (
        transform.x + 20 * transform.scale,
        transform.y + 10 * transform.scale,
        40 * transform.scale,
        30 * transform.scale,
    )


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_viewport(value: float) -> None:
    with pytest.raises(ValueError):
        image_transform(200, 100, value, 100)


def test_display_store_owned_immutable_latest_only() -> None:
    camera = CameraId("CAM1")
    store = LatestDisplayFrameStore((camera,))
    frame = source_frame()
    assert store.publish(frame, generation=1)
    first = store.snapshot().frames[0]
    assert not np.shares_memory(first.image, frame.image)
    # Producer may reuse pixels after its separate inspection lease; display is safe.
    frame.image.setflags(write=True)
    frame.image[:] = 200
    assert not first.image.any()
    with pytest.raises(ValueError):
        first.image.setflags(write=True)
    assert not store.publish(frame, generation=1)
    old = ref(first.image)
    del first
    for cycle in range(2, 20):
        assert store.publish(source_frame(cycle=cycle), generation=1)
        assert len(store.snapshot().frames) == 1
    assert old() is None
    assert not store.publish(source_frame(cycle=1), generation=0)
    assert store.publish(source_frame(cycle=0), generation=2)
    with pytest.raises(ValueError, match="Unknown"):
        store.publish(source_frame(CameraId("unknown")), generation=2)


def test_concurrent_store_identity_and_bounded_count() -> None:
    cameras = tuple(CameraId(f"CAM{i}") for i in range(1, 7))
    store = LatestDisplayFrameStore(cameras)

    def publish(index: int) -> None:
        for cycle in range(10):
            image = np.full((20, 60, 3), index, dtype=np.uint8)
            store.publish(
                Frame(FrameId(CycleId(cycle), cameras[index], cycle), 0, image),
                generation=1,
            )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(publish, range(6)))
    result = store.snapshot()
    assert len(result.frames) == 6 and result.revision == 60
    assert tuple(f.frame_id.camera_id for f in result.frames) == cameras
    assert all(int(f.image[0, 0, 0]) == index for index, f in enumerate(result.frames))


@pytest.mark.parametrize(
    "state,start,stop,reset,recipe,batch",
    [
        (ApplicationState.UNCONFIGURED, False, False, False, True, False),
        (ApplicationState.READY, True, False, False, True, True),
        (ApplicationState.STARTING, False, True, False, False, False),
        (ApplicationState.RUNNING, False, True, False, False, False),
        (ApplicationState.STOPPING, False, False, False, False, False),
        (ApplicationState.FAULT, False, False, True, False, False),
        (ApplicationState.CLOSED, False, False, False, False, False),
    ],
)
def test_controls(
    state: ApplicationState,
    start: bool,
    stop: bool,
    reset: bool,
    recipe: bool,
    batch: bool,
) -> None:
    result = controls(snapshot(state))
    assert (result.start, result.stop, result.reset, result.recipe, result.batch) == (
        start,
        stop,
        reset,
        recipe,
        batch,
    )
    assert not controls(
        replace(snapshot(ApplicationState.FAULT), runtime_owned=True)
    ).reset


def test_indicator_distinguishes_pending_disabled_error_and_ng() -> None:
    assert indicator(None) is Indicator.PENDING
    assert (
        indicator(Assessment(TechnicalStatus.DISABLED, ProductJudgment.UNKNOWN))
        is Indicator.DISABLED
    )
    assert (
        indicator(Assessment(TechnicalStatus.ERROR, ProductJudgment.NG))
        is Indicator.ERROR_NG
    )
    assert (
        indicator(Assessment(TechnicalStatus.ERROR, ProductJudgment.UNKNOWN))
        is Indicator.ERROR
    )
    assert (
        indicator(Assessment(TechnicalStatus.SUCCESS, ProductJudgment.NG))
        is Indicator.NG
    )
    assert (
        indicator(Assessment(TechnicalStatus.SUCCESS, ProductJudgment.OK))
        is Indicator.OK
    )


def test_result_frame_identity_includes_session_and_sequence() -> None:
    store = LatestDisplayFrameStore((CameraId("CAM1"),))
    store.publish(source_frame(), generation=1)
    display = store.snapshot().frames[0]
    current = replace(
        snapshot(ApplicationState.RUNNING), latest_production_result=production()
    )
    assert matching_camera(display, current) is not None
    assert matching_camera(display, replace(current, generation=2)) is None
    assert (
        matching_camera(
            replace(display, frame_id=replace(display.frame_id, sequence=8)), current
        )
        is None
    )
    store.publish(source_frame(cycle=2), generation=1)
    assert matching_camera(store.snapshot().frames[0], current) is None


def test_bilingual_catalog_and_factual_labels() -> None:
    for key in STRINGS:
        assert text(key, "zh") and text(key, "en")
    for key in (
        "production",
        "parameters",
        "diagnostics",
        "history",
        "settings",
        "start",
        "stop",
        "batch",
        "cycles",
        "channel_ok",
        "channel_ng",
    ):
        assert key in STRINGS
    assert "次数" in text("channel_ok", "zh")
    assert "occurrences" in text("channel_ng", "en")
