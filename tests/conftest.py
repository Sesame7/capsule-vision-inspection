from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def offscreen_app() -> QApplication:
    from PySide6.QtWidgets import QApplication

    # Headless platform is test-only; the real entry point uses native windows.
    app = QApplication(["qt-tests", "-platform", "offscreen"])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def qapp(offscreen_app: QApplication, monkeypatch: pytest.MonkeyPatch) -> QApplication:
    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        # Qt fixtures use synthetic model files. Metadata is an explicit fake
        # boundary like inference; normal tests must not open the real SDK.
        return ("黑点", "压痕", "破损", "变形")

    monkeypatch.setattr("capsule_vision.ui.recipe_page.read_model_labels", metadata)
    return offscreen_app
