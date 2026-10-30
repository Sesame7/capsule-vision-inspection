"""Local image-set ingress; no acquisition thread, queue or lifecycle ownership."""

from collections.abc import Sequence
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from capsule_vision.inference._validation import uint8_array
from capsule_vision.inspection import CycleId, Frame, FrameId
from capsule_vision.inspection.production import InspectionLayout

from .application import ProductionApplication
from .models import ApplicationState, ApplicationStateError
from .session import ProductionCycle


class FrameSetIngress:
    """One serial source for one running application generation.

    Supply the active configuration's layout. Create once after successful Start;
    do not mix other begin_cycle producers into this session. Calls belong to one
    non-GUI source context. Stop/drain that source before Stop/Start or replacement:
    existing application commands do not atomically bind a generation token.
    """

    def __init__(
        self, application: ProductionApplication, layout: InspectionLayout
    ) -> None:
        self._application = application
        self._cameras = layout.camera_ids
        self._generation = application.snapshot().generation
        self._next_cycle = 1
        self._require_session()

    def _require_session(self) -> None:
        snapshot = self._application.snapshot()
        if (
            snapshot.state is not ApplicationState.RUNNING
            or snapshot.generation != self._generation
        ):
            raise ApplicationStateError("Ingress requires its original running session")

    def submit(self, images: Sequence[NDArray[np.generic]]) -> ProductionCycle:
        """Copy one ordered BGR set, await admission, expose the existing result Future.

        Keep source arrays stable until this call returns/raises; afterward they
        may be reused even while inference runs. All images are validated/copied
        before begin, without conversion/resizing. Admission errors propagate;
        partial admission is not rolled back. The application still owns any
        accepted cycle until existing expiration/shutdown finalizes it.
        """
        self._require_session()
        if len(images) != len(self._cameras):
            raise ValueError(f"Expected {len(self._cameras)} images, got {len(images)}")
        cycle_id = CycleId(self._next_cycle)
        frames = tuple(
            Frame(
                FrameId(cycle_id, camera, 0),
                monotonic(),  # Local source metadata, never membership or deadlines.
                uint8_array(image).copy(order="C"),
            )
            for camera, image in zip(self._cameras, images, strict=True)
        )
        self._require_session()
        self._next_cycle += 1  # Admission rejection leaves a gap; never auto-retry.
        cycle = self._application.begin_cycle(cycle_id)
        cycle.accepted.result()
        for frame in frames:
            self._require_session()
            self._application.submit_frame(frame).result()
        return cycle
