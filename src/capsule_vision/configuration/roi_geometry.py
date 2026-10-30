"""Pure stored-ROI rounding and bounds shared by drafts and UI gestures."""

from math import floor

from capsule_vision.inference import BoundingRect


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
