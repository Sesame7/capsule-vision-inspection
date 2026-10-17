from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    # Headless platform is test-only; the real entry point uses native windows.
    app = QApplication(["qt-tests", "-platform", "offscreen"])
    app.setQuitOnLastWindowClosed(False)

    def metadata(
        path: Path, *, module_id: str, expected_sha256: str
    ) -> tuple[str, ...]:
        # Qt fixtures use synthetic model files. Metadata is an explicit fake
        # boundary like inference; normal tests must not open the real SDK.
        return ("黑点", "压痕", "破损", "变形")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("vimo_demo.ui.parameters_page.read_model_labels", metadata)
        yield app


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-gpu", action="store_true", help="Run short local GPU regression"
    )
    parser.addoption(
        "--require-gpu",
        action="store_true",
        help="Fail rather than skip missing GPU prerequisites",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if "gpu" in item.keywords and not config.getoption("--run-gpu"):
            item.add_marker(
                pytest.mark.skip(reason="Use --run-gpu for the short regression")
            )
