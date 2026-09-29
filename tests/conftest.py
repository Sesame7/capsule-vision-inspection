import pytest
from PySide6.QtWidgets import QApplication

from vimo_demo.ui.app import DemoApplication


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    # Headless platform is test-only; the real entry point uses native windows.
    app = DemoApplication(["qt-tests", "-platform", "offscreen"])
    app.setQuitOnLastWindowClosed(False)
    return app


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-gpu", action="store_true", help="Run short local GPU regression"
    )
    parser.addoption(
        "--run-full-reference",
        action="store_true",
        help="Run the complete reference dataset",
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
        if "full_reference" in item.keywords:
            if not config.getoption("--run-full-reference"):
                item.add_marker(
                    pytest.mark.skip(
                        reason="Use --run-full-reference for the 400-image regression"
                    )
                )
        elif "gpu" in item.keywords and not config.getoption("--run-gpu"):
            item.add_marker(
                pytest.mark.skip(reason="Use --run-gpu for the short regression")
            )
