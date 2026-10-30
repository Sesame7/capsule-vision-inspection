"""Single-owner configuration working copies; persistence is separate from activation."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from capsule_vision.inference import BoundingRect
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import (
    CameraLayout,
    ChannelId,
    ChannelROI,
    InspectionLayout,
)
from capsule_vision.inspection.production.capsule import CapsuleSettings

from .errors import ConfigurationError, ConfigurationIOError
from .models import (
    CheckConfiguration,
    ImageArchiveSettings,
    MachineLayoutDefinition,
    RecipeDefinition,
)
from .persistence import (
    load_camera_sources,
    load_machine_layout,
    save_camera_sources,
    save_machine_layout,
)
from .roi_geometry import fits, pixel
from .sources import CameraSources


@dataclass
class ImageArchiveDraft:
    root_directory: str = ""
    save_ok: bool = False
    save_ng: bool = False
    save_error: bool = False
    save_empty: bool = False

    @classmethod
    def from_settings(cls, value: ImageArchiveSettings) -> ImageArchiveDraft:
        return cls(
            value.root_directory,
            value.save_ok,
            value.save_ng,
            value.save_error,
            value.save_empty,
        )

    def to_settings(self) -> ImageArchiveSettings:
        return ImageArchiveSettings(
            self.root_directory,
            self.save_ok,
            self.save_ng,
            self.save_error,
            self.save_empty,
        )


@dataclass
class RecipeDraft:
    recipe_id: str
    display_name: str
    product_code: str | None = None
    model_id: str | None = None
    defaults: CheckConfiguration = field(default_factory=CheckConfiguration)

    capsule: CapsuleSettings = field(default_factory=CapsuleSettings)
    image_archive: ImageArchiveDraft = field(default_factory=ImageArchiveDraft)

    @classmethod
    def from_recipe(cls, recipe: RecipeDefinition) -> RecipeDraft:
        return cls(
            recipe.recipe_id,
            recipe.display_name,
            recipe.product_code,
            recipe.model_id,
            recipe.defaults,
            recipe.capsule,
            ImageArchiveDraft.from_settings(recipe.image_archive),
        )

    def to_recipe(self) -> RecipeDefinition:
        return RecipeDefinition(
            self.recipe_id,
            self.display_name,
            self.defaults,
            self.product_code,
            self.model_id,
            capsule=self.capsule,
            image_archive=self.image_archive.to_settings(),
        )


class MachineLayoutDraft:
    def __init__(
        self, machine: MachineLayoutDefinition | None, path: Path | None
    ) -> None:
        self.path = path
        self.bounds: dict[CameraId, tuple[int, int]] = {}
        self._file_present = path is not None and path.exists()
        self._saved = machine
        self.reset(machine)

    def reset(self, machine: MachineLayoutDefinition | None) -> None:
        self._saved = machine
        self._camera_ids = (
            machine.layout.camera_ids
            if machine
            else tuple(CameraId(f"CAM{i}") for i in range(1, 7))
        )
        self._geometry = (
            {c.camera_id: c for c in machine.layout.cameras} if machine else {}
        )

    @property
    def persisted(self) -> bool:
        return self._saved is not None

    @property
    def saved_definition(self) -> MachineLayoutDefinition | None:
        return self._saved

    @property
    def saved(self) -> MachineLayoutDefinition:
        if self._saved is None:
            raise ConfigurationError("Save a complete MachineLayout before preparation")
        return self._saved

    @saved.setter
    def saved(self, machine: MachineLayoutDefinition) -> None:
        self._saved = machine

    @property
    def camera_ids(self) -> tuple[CameraId, ...]:
        return self._camera_ids

    @property
    def complete(self) -> bool:
        return all(c in self._geometry for c in self.camera_ids)

    @property
    def definition(self) -> MachineLayoutDefinition:
        if not self.complete:
            raise ConfigurationError(
                "ROI geometry is not initialized for every selected camera"
            )
        return MachineLayoutDefinition(
            InspectionLayout(tuple(self.camera(c) for c in self.camera_ids))
        )

    @definition.setter
    def definition(self, value: MachineLayoutDefinition) -> None:
        self._camera_ids = value.layout.camera_ids
        self._geometry = {c.camera_id: c for c in value.layout.cameras}

    @property
    def dirty(self) -> bool:
        if self._saved is None:
            return bool(self._geometry) or len(self.camera_ids) != 6
        return not self.complete or self.definition != self._saved

    def rois(self, identity: CameraId) -> tuple[ChannelROI, ...]:
        camera = self._geometry.get(identity)
        return camera.slots if camera else ()

    def camera(self, identity: CameraId) -> CameraLayout:
        if identity not in self._geometry:
            raise ConfigurationError(f"{identity}: ROI geometry is not initialized")
        return self._geometry[identity]

    def initialize(self, camera: CameraId, width: int, height: int) -> bool:
        """Use real dimensions once; references never repair existing geometry."""
        if camera not in self.camera_ids or camera in self._geometry:
            return False
        if type(width) is not int or type(height) is not int or width < 6 or height < 1:
            raise ConfigurationError(
                "ROI initialization requires width >= 6 and height > 0"
            )
        index = self.camera_ids.index(camera)
        self._geometry[camera] = CameraLayout(
            camera,
            tuple(
                ChannelROI(
                    i,
                    ChannelId(index * 6 + i + 1),
                    BoundingRect(
                        i * width // 6, 0, (i + 1) * width // 6 - i * width // 6, height
                    ),
                )
                for i in range(6)
            ),
        )
        self.bounds[camera] = width, height
        return True

    def replace_rects(self, camera: CameraId, rects: tuple[BoundingRect, ...]) -> None:
        current = self.camera(camera)
        size = self.bounds.get(camera)
        if len(rects) != len(current.slots):
            raise ValueError("Every configured slot is required")
        if size is not None and any(not fits(r, size) for r in rects):
            raise ValueError("ROI exceeds reference bounds; operation rejected")
        self._geometry[camera] = replace(
            current,
            slots=tuple(
                replace(slot, rect=rect)
                for slot, rect in zip(current.slots, rects, strict=True)
            ),
        )

    def remove_camera_six(self) -> None:
        if len(self.camera_ids) != 6:
            raise ValueError("CAM6 is not configured")
        self._camera_ids = self.camera_ids[:5]
        self._geometry.pop(CameraId("CAM6"), None)

    def restore_camera_six(self) -> None:
        if len(self.camera_ids) != 5:
            raise ValueError("CAM6 is already configured")
        self._camera_ids = (*self.camera_ids, CameraId("CAM6"))
        # Existing saved machines retain their explicit CAM5 template operation.
        # First-run placeholders wait for CAM6's own actual dimensions.
        if self.persisted:
            self._geometry[CameraId("CAM6")] = CameraLayout(
                CameraId("CAM6"),
                tuple(
                    ChannelROI(slot.slot, ChannelId(31 + slot.slot), replace(slot.rect))
                    for slot in self.camera(CameraId("CAM5")).slots
                ),
            )

    def common_size(self, camera: CameraId, width: int, height: int) -> None:
        self.replace_rects(
            camera,
            tuple(
                BoundingRect(
                    pixel(s.rect.x + (s.rect.width - width) / 2),
                    pixel(s.rect.y + (s.rect.height - height) / 2),
                    width,
                    height,
                )
                for s in self.camera(camera).slots
            ),
        )

    def distribute(self, camera: CameraId) -> None:
        rects = [s.rect for s in self.camera(camera).slots]
        first, last = rects[0], rects[-1]
        ax, ay = first.x + first.width / 2, first.y + first.height / 2
        bx, by = last.x + last.width / 2, last.y + last.height / 2
        for index in range(1, len(rects) - 1):
            r = rects[index]
            t = index / (len(rects) - 1)
            rects[index] = BoundingRect(
                pixel(ax + (bx - ax) * t - r.width / 2),
                pixel(ay + (by - ay) * t - r.height / 2),
                r.width,
                r.height,
            )
        self.replace_rects(camera, tuple(rects))

    def save(self) -> None:
        if self.path is None:
            raise ValueError("No machine layout persistence path configured")
        if self._file_present:
            if not self.persisted or load_machine_layout(self.path) != self.saved:
                raise ConfigurationError(
                    "MachineLayout changed/invalid on disk; reload before Save"
                )
        elif self.path.exists():
            raise ConfigurationError(
                "MachineLayout appeared on disk; reload before Save"
            )
        definition = self.definition
        for camera in definition.layout.cameras:
            size = self.bounds.get(camera.camera_id)
            if size is not None:
                try:
                    camera.validate_shape((size[1], size[0], 3))
                except ValueError as exc:
                    raise ConfigurationError(f"{camera.camera_id}: {exc}") from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationIOError(f"Machine directory: {exc}") from exc
        save_machine_layout(self.path, definition)
        self.saved = definition
        self._file_present = True

    def discard(self) -> None:
        self.reset(self._saved)

    def load_saved(self, machine: MachineLayoutDefinition | None) -> None:
        self.reset(machine)
        self._file_present = self.path is not None and self.path.exists()


class CameraSourceDraft:
    def __init__(self, sources: CameraSources | None, path: Path) -> None:
        self.saved = self.definition = (
            sources if sources is not None else CameraSources()
        )
        self.persisted = sources is not None
        self.path = path
        self._file_present = path.exists()

    @property
    def dirty(self) -> bool:
        return self.definition != self.saved

    def set_folder(self, camera: CameraId, folder: str) -> None:
        row = replace(self.definition.camera(camera), folder=folder)
        self.definition = replace(
            self.definition,
            cameras=tuple(c for c in self.definition.cameras if c.camera_id != camera)
            + (row,),
        )

    def set_loop(self, enabled: bool) -> None:
        self.definition = replace(self.definition, loop=enabled)

    def set_fps(self, value: float) -> None:
        self.definition = replace(self.definition, target_fps=value)

    def set_mode(self, mode: Literal["folder", "basler"]) -> None:
        self.definition = replace(self.definition, mode=mode)

    def set_serial(self, camera: CameraId, serial: str) -> None:
        row = replace(self.definition.camera(camera), basler_serial=serial)
        self.definition = replace(
            self.definition,
            cameras=tuple(c for c in self.definition.cameras if c.camera_id != camera)
            + (row,),
        )

    def save(self) -> None:
        if self._file_present:
            if not self.persisted or load_camera_sources(self.path) != self.saved:
                raise ConfigurationError(
                    "Sources changed/invalid on disk; reload before Save"
                )
        elif self.path.exists():
            raise ConfigurationError("Sources appeared on disk; reload before Save")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationIOError(f"Sources directory: {exc}") from exc
        save_camera_sources(self.path, self.definition)
        self.saved = self.definition
        self.persisted = self._file_present = True

    def discard(self) -> None:
        self.definition = self.saved

    def load_saved(self, sources: CameraSources | None) -> None:
        self.saved = self.definition = (
            sources if sources is not None else CameraSources()
        )
        self.persisted = sources is not None
        self._file_present = self.path.exists()
