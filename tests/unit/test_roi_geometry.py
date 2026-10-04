import pytest

from vimo_demo.inference import BoundingRect
from vimo_demo.ui.production_presentation import image_transform
from vimo_demo.ui.roi_geometry import (
    HANDLES,
    Handle,
    RoiInteractionModel,
    drag_rect,
    fits,
    handle_points,
    pixel,
)


@pytest.mark.parametrize("size", [(1056, 960), (2000, 500), (500, 2000)])
@pytest.mark.parametrize("viewport", [(500, 900), (900, 500)])
def test_fit_letterbox_and_round_trip(
    size: tuple[int, int], viewport: tuple[int, int]
) -> None:
    t = image_transform(*size, *viewport)
    assert t.scale == min(viewport[0] / size[0], viewport[1] / size[1])
    assert t.x >= -1e-10 and t.y >= -1e-10  # Binary float boundary roundoff.
    assert min(t.x, t.y) == pytest.approx(0)
    rect = BoundingRect(23, 31, 100, 99)
    for _ in range(100):
        x, y, w, h = t.rectangle(rect)
        sx, sy = t.source_point(x, y)
        rect = BoundingRect(
            pixel(sx), pixel(sy), pixel(w / t.scale), pixel(h / t.scale)
        )
    assert rect == BoundingRect(23, 31, 100, 99)


def test_zoom_pan_resize_and_invalid_dimensions() -> None:
    rect = BoundingRect(23, 31, 100, 99)
    model = RoiInteractionModel((1056, 960), rect, True)
    for scale in (None, 0.5, 1, 2):
        model.scale = scale
        model.pan = (17, -23)
        for width, height in ((500, 900), (900, 500)):
            t = model.transform(width, height)
            assert t is not None
            if scale is not None:
                assert t.scale == scale
            x, y, _, _ = t.rectangle(rect)
            assert t.source_point(x, y) == pytest.approx((23, 31))
    assert model.rect == rect
    assert model.transform(0, 100) is None
    for args in ((0, 1, 100, 100), (1, 1, 0, 100)):
        with pytest.raises(ValueError):
            image_transform(*args)
    with pytest.raises(ValueError):
        image_transform(1, 1, 100, 100, scale=0)
    with pytest.raises(ValueError):
        image_transform(1, 1, 100, 100, pan=(float("nan"), 0))


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("move", (25, 37, 40, 50)),
        ("n", (20, 37, 40, 43)),
        ("e", (20, 30, 45, 50)),
        ("s", (20, 30, 40, 57)),
        ("w", (25, 30, 35, 50)),
        ("nw", (25, 37, 35, 43)),
        ("ne", (20, 37, 45, 43)),
        ("se", (20, 30, 45, 57)),
        ("sw", (25, 30, 35, 57)),
    ],
)
def test_move_and_each_handle(
    handle: Handle, expected: tuple[int, int, int, int]
) -> None:
    assert drag_rect(
        BoundingRect(20, 30, 40, 50), handle, 5, 7, (100, 100)
    ) == BoundingRect(*expected)


@pytest.mark.parametrize("handle", HANDLES)
def test_clamping_minimum_size_no_flipping(handle: Handle) -> None:
    rect = BoundingRect(20, 30, 40, 50)
    for delta in (-1000, 1000):
        result = drag_rect(rect, handle, delta, delta, (100, 100))
        assert fits(result, (100, 100))
        if "w" in handle:
            assert result.x + result.width == 60
        if "e" in handle:
            assert result.x == 20
        if "n" in handle:
            assert result.y + result.height == 80
        if "s" in handle:
            assert result.y == 30
        if handle in ("e", "w"):
            assert (result.y, result.height) == (30, 50)
        if handle in ("n", "s"):
            assert (result.x, result.width) == (20, 40)
    collapse_x = 1000 if "w" in handle else -1000 if "e" in handle else 0
    collapse_y = 1000 if "n" in handle else -1000 if "s" in handle else 0
    collapsed = drag_rect(rect, handle, collapse_x, collapse_y, (100, 100))
    assert collapsed.width == (1 if "w" in handle or "e" in handle else 40)
    assert collapsed.height == (1 if "n" in handle or "s" in handle else 50)


def test_move_boundaries_rounding_and_invalid_start() -> None:
    rect = BoundingRect(20, 30, 40, 50)
    assert drag_rect(rect, "move", -1000, -1000, (100, 100)) == BoundingRect(
        0, 0, 40, 50
    )
    assert drag_rect(rect, "move", 1000, 1000, (100, 100)) == BoundingRect(
        60, 50, 40, 50
    )
    assert drag_rect(rect, "move", 0.5, -0.5, (100, 100)) == BoundingRect(
        21, 30, 40, 50
    )
    assert [pixel(v) for v in (-0.5, 0.49, 0.5, 1.5)] == [0, 0, 1, 2]
    with pytest.raises(ValueError):
        drag_rect(rect, "move", 1, 1, (40, 40))


def test_hit_targets_letterbox_and_read_only() -> None:
    model = RoiInteractionModel((200, 100), BoundingRect(20, 20, 80, 60), True)
    t = model.transform(800, 800)
    assert t is not None and t.y == 200
    assert model.rect is not None
    for name, (x, y) in zip(HANDLES, handle_points(model.rect, t), strict=True):
        assert model.hit(x + 3, y + 3, t) == name
    assert model.hit(240, 400, t) == "move"
    assert model.hit(240, 190, t) is None
    model.editable = False
    assert model.hit(240, 400, t) is None
    model.size = None
    assert model.transform(800, 800) is None
