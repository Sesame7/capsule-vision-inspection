"""Installed application entry points."""

from pathlib import Path

from capsule_vision.ui.bootstrap import main as bootstrap_main


def app() -> int:
    """Start the application with configuration beside the working directory."""
    return bootstrap_main(Path.cwd())
