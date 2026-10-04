"""Source-pixel rectangle gestures; no Qt, persistence or Recipe precedence."""

from dataclasses import dataclass
from math import floor
from typing import Literal

from vimo_demo.inference import BoundingRect

from .production_presentation import ImageTransform, image_transform

type Handle = Literal["nw", "n", "ne", "e", "se", "s", "sw", "w", "move"]
HANDLES: tuple[Handle, ...] = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


def pixel(value: float) -> int:
    """Nearest integer; exact halves go toward positive infinity."""
    return floor(value + 0.5)


def fits(rect: BoundingRect, size: tuple[int, int]) -> bool:
    return (
        min(rect.x, rect.y) >= 0
        and min(rect.width, rect.height) >= 1
        and rect.x + rect.width <= size[0]
        and rect.y + rect.height <= size[1]
    )


def handle_points(
    rect: BoundingRect, transform: ImageTransform
) -> tuple[tuple[float, float], ...]:
    x, y, w, h = transform.rectangle(rect)
    return (
        (x, y),
        (x + w / 2, y),
        (x + w, y),
        (x + w, y + h / 2),
        (x + w, y + h),
        (x + w / 2, y + h),
        (x, y + h),
        (x, y + h / 2),
    )


def drag_rect(
    rect: BoundingRect, handle: Handle, dx: float, dy: float, size: tuple[int, int]
) -> BoundingRect:
    """Always compute from the gesture's starting ROI, never accumulated rounding."""
    if not fits(rect, size):
        raise ValueError("ROI must fit the reference before dragging")
    x, y, right, bottom = rect.x, rect.y, rect.x + rect.width, rect.y + rect.height
    if handle == "move":
        return BoundingRect(
            min(max(pixel(x + dx), 0), size[0] - rect.width),
            min(max(pixel(y + dy), 0), size[1] - rect.height),
            rect.width,
            rect.height,
        )
    if "w" in handle:
        x = min(max(pixel(x + dx), 0), right - 1)
    if "e" in handle:
        right = min(max(pixel(right + dx), x + 1), size[0])
    if "n" in handle:
        y = min(max(pixel(y + dy), 0), bottom - 1)
    if "s" in handle:
        bottom = min(max(pixel(bottom + dy), y + 1), size[1])
    return BoundingRect(x, y, right - x, bottom - y)


@dataclass
class RoiInteractionModel:
    """Drawable projection plus presentation state; RecipeDraft is authoritative."""

    size: tuple[int, int] | None = None
    rect: BoundingRect | None = None
    editable: bool = False
    scale: float | None = None  # None = Fit; fixed zoom is logical pixels/source pixel.
    pan: tuple[float, float] = (0, 0)

    @property
    def can_drag(self) -> bool:
        return (
            self.editable
            and self.size is not None
            and self.rect is not None
            and fits(self.rect, self.size)
        )

    def transform(self, width: float, height: float) -> ImageTransform | None:
        if self.size is None or min(width, height) <= 0:
            return None
        return image_transform(
            *self.size, width, height, scale=self.scale, pan=self.pan
        )

    def hit(self, x: float, y: float, transform: ImageTransform) -> Handle | None:
        if not self.can_drag or self.rect is None or self.size is None:
            return None
        sx, sy = transform.source_point(x, y)
        if not (0 <= sx <= self.size[0] and 0 <= sy <= self.size[1]):
            return None  # Letterbox is never an ROI gesture.
        points = handle_points(self.rect, transform)
        distances = [max(abs(x - px), abs(y - py)) for px, py in points]
        index = min(range(len(points)), key=distances.__getitem__)
        if distances[index] <= 12:  # Logical-pixel hit radius, independent of zoom.
            return HANDLES[index]
        rect = self.rect
        if rect.x <= sx <= rect.x + rect.width and rect.y <= sy <= rect.y + rect.height:
            return "move"
        return None
