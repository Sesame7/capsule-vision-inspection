import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    # Headless platform is test-only; the real entry point uses native windows.
    app = QApplication(["qt-tests", "-platform", "offscreen"])
    app.setQuitOnLastWindowClosed(False)
    return app


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
