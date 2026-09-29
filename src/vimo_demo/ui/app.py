"""Launch from the repository root with python -m vimo_demo.ui.app MODEL."""

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from vimo_demo.inference import VimoSegmentationEngine
from vimo_demo.runtime import InferenceWorker

from .demo_window import DemoWindow


class DemoApplication(QApplication):
    """Route explicit Qt quit requests through the same deferred close policy."""

    window: DemoWindow | None = None

    def event(self, event: QEvent) -> bool:
        window = self.window
        if event.type() is QEvent.Type.Quit and window is not None:
            if not window.bridge.stopped:
                window.close()
                return True
        return super().event(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal ViMo / Qt lifecycle demo")
    parser.add_argument("model", type=Path)
    parser.add_argument("--image", default="", help="Optional local test image")
    args = parser.parse_args()
    model = Path(args.model)
    if not model.is_file():
        parser.error(f"Model does not exist: {model}")
    app = DemoApplication(sys.argv[:1])
    worker = InferenceWorker(lambda: VimoSegmentationEngine(model))
    window = DemoWindow(worker, str(args.image))
    app.window = window  # Strong ownership until exec returns after thread exit.
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
