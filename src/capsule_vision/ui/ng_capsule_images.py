"""Decode one selected capsule on the shared Qt pool. Only cropped pixels leave the job."""

from concurrent.futures import CancelledError, Future
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from PySide6.QtCore import QRect, QThreadPool
from PySide6.QtGui import QImage

from capsule_vision.application.history import HistoryFinding
from capsule_vision.application.ng_capsules import (
    CapsuleDetail,
    CapsuleExposure,
    FrozenROI,
)
from capsule_vision.inference import BoundingRect

from .history_image import read_archived_png


@dataclass(frozen=True, slots=True)
class ExposureImage:
    image: QImage = field(default_factory=QImage)
    findings: tuple[HistoryFinding, ...] = ()
    issue: str = ""


def exposure_image(
    exposure: CapsuleExposure, roi: FrozenROI | None, root: Path | None
) -> ExposureImage:
    if roi is None or exposure.path is None:
        return ExposureImage(issue="unavailable" if roi is None else "history_no_image")
    image, issue = read_archived_png(exposure.path, roi.camera, root, exposure.size)
    if issue:
        return ExposureImage(issue=issue)
    rect = roi.rect
    if (
        rect.height < 2
        or rect.x + rect.width > image.width()
        or rect.y + rect.height > image.height()
    ):
        return ExposureImage(issue="history_image_size_mismatch")
    area = QRect(rect.x, rect.y, rect.width, rect.height)
    local: list[HistoryFinding] = []
    for finding in exposure.findings if exposure.size is not None else ():
        r = finding.bounding_rect
        clipped = area.intersected(QRect(r.x, r.y, r.width, r.height))
        if not clipped.isEmpty():
            clipped.translate(-area.x(), -area.y())
            local.append(
                HistoryFinding(
                    finding.label,
                    BoundingRect(
                        clipped.x(), clipped.y(), clipped.width(), clipped.height()
                    ),
                    finding.channels,
                )
            )
    return ExposureImage(image.copy(area), tuple(local))


def load_images(
    detail: CapsuleDetail, root: Path | None, cancel: Event
) -> Future[tuple[ExposureImage, ...]]:
    result: Future[tuple[ExposureImage, ...]] = Future()

    def decode() -> None:
        try:
            columns: list[ExposureImage] = []
            for exposure in detail.exposures:
                if cancel.is_set():
                    raise CancelledError()
                columns.append(exposure_image(exposure, detail.roi, root))
            result.set_result(tuple(columns))
        except Exception as exc:
            result.set_exception(exc)

    QThreadPool.globalInstance().start(decode)
    return result
