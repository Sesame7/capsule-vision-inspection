"""Saved Vision definitions captured by a stopped Production candidate."""

from dataclasses import dataclass
from pathlib import Path

from capsule_vision.configuration import (
    CameraSources,
    ConfigurationError,
    ConfigurationIOError,
    MachineLayoutDefinition,
    load_camera_sources,
    load_machine_layout,
)


class VisionPreparationRequired(ConfigurationError):
    """Saved Vision dependencies changed or can no longer be verified."""


def read_sources(path: Path) -> CameraSources | None:
    """An absent engineering source file is distinct from an unreadable one."""
    try:
        return load_camera_sources(path)
    except ConfigurationIOError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise
        return None


@dataclass(frozen=True, slots=True)
class VisionDependencies:
    machine_path: Path
    machine: MachineLayoutDefinition
    sources_path: Path | None = None
    sources: CameraSources | None = None

    def validate_current(self) -> None:
        """Blocking parsing, only on preparation or the Application lifecycle worker."""
        try:
            current_machine = load_machine_layout(self.machine_path)
            current_sources = (
                read_sources(self.sources_path)
                if self.sources_path is not None
                else None
            )
        except (ValueError, OSError) as exc:
            raise VisionPreparationRequired(
                "Saved Vision dependencies are unreadable; reselect the saved Recipe"
            ) from exc
        if current_machine != self.machine or (
            self.sources_path is not None and current_sources != self.sources
        ):
            raise VisionPreparationRequired(
                "Saved Vision definitions changed; reselect the saved Recipe"
            )
