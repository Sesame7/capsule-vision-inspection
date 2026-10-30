"""Pure canonical MachineLayout and Model definition builders."""

from capsule_vision.configuration import (
    MachineLayoutDefinition,
    ModelDefinition,
    ModelRegistry,
)
from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)


def machine(*, mirrored: bool = False, count: int = 6) -> MachineLayoutDefinition:
    return MachineLayoutDefinition(
        InspectionLayout(
            tuple(
                CameraLayout(
                    CameraId(f"CAM{camera + 1}"),
                    tuple(
                        ChannelROI(
                            slot,
                            ChannelId(camera * 6 + slot + 1),
                            BoundingRect(
                                (5 - slot if mirrored else slot) * 10, 0, 8, 10
                            ),
                        )
                        for slot in range(6)
                    ),
                )
                for camera in range(count)
            )
        )
    )


def model(model_id: str = "general") -> ModelDefinition:
    return ModelDefinition(model_id, "通用模型", "model.vimosln", "0" * 64, "2")


def registry() -> ModelRegistry:
    return ModelRegistry((model("special"), model()))
