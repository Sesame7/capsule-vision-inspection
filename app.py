"""Normal Capsule Vision Inspection startup, with persistent data anchored to this entry location."""

from pathlib import Path

from capsule_vision.ui.bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main(Path(__file__).resolve().parent))
