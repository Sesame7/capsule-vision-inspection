"""Optional logical sizing through the normal bootstrap, without production work."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QWidget
from tests.support.qt import close_window, ensure_test_fonts

from capsule_vision.application import ApplicationState
from capsule_vision.application.startup import Startup
from capsule_vision.ui import bootstrap
from capsule_vision.ui.future_delivery import FutureDelivery
from capsule_vision.ui.main_window import MainWindow


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "1080",
        "1080:1920",
        "1080×1920",
        "1080x",
        "x1920",
        "1080.0x1920",
        "+1080x1920",
        "-1080x1920",
        "1080x-1920",
        " 1080x1920",
        "1080x1920 ",
        "１０８０x１９２０",
        "0x0",
        "879x720",
        "880x719",
        "16777216x720",
        "880x16777216",
        "999999999999999999999x1920",
    ],
)
def test_invalid_size_is_rejected_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    def unexpected_load(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Invalid arguments reached application startup")

    monkeypatch.setattr(bootstrap, "load_startup", unexpected_load)
    entry = tmp_path / "unused"
    with pytest.raises(SystemExit) as error:
        bootstrap.main(
            entry, ["--window-size"] if value is None else [f"--window-size={value}"]
        )
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "--window-size" in message and "error:" in message
    assert not entry.exists()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("880x720", (880, 720)),
        ("1080X1920", (1080, 1920)),
        ("01920x01080", (1920, 1080)),
        ("16777215x16777215", (16777215, 16777215)),
    ],
)
def test_valid_logical_dimensions(value: str, expected: tuple[int, int]) -> None:
    assert bootstrap.parse_window_size(value) == expected


@pytest.mark.parametrize("size", [None, (880, 720), (1080, 1920), (1600, 900)])
def test_normal_bootstrap_window_state_size_and_close(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int] | None,
) -> None:
    ensure_test_fonts()
    windows: list[MainWindow] = []
    original_create = bootstrap.create_window

    def application(_arguments: Sequence[str]) -> QApplication:
        return qapp

    def create(startup: Startup, delivery: FutureDelivery) -> MainWindow:
        result = original_create(startup, delivery)
        windows.append(result)
        return result

    def execute() -> int:
        (window,) = windows
        qapp.processEvents()
        assert window.isVisible()
        setup = window.identity_controls.form
        assert setup is not None and setup.kind == "setup" and setup.isVisible()
        setup.reject()
        assert not (tmp_path / "data/security.sqlite3").exists()
        assert window.isMaximized() == (size is None)
        if size is not None:
            assert window.size() == QSize(*size)
            assert window.maximumSize() == QSize(16_777_215, 16_777_215)
            assert window.size().expandedTo(window.minimumSize()) == window.size()
            available = window.screen().availableGeometry()
            frame = window.frameGeometry()
            if (
                available.width() >= frame.width()
                and available.height() >= frame.height()
            ):
                assert available.contains(frame)
        assert (
            window.bridge.application.snapshot().state is ApplicationState.UNCONFIGURED
        )
        return 0

    monkeypatch.setattr(bootstrap, "QApplication", application)
    monkeypatch.setattr(bootstrap, "create_window", create)
    monkeypatch.setattr(qapp, "exec", execute)
    arguments = [] if size is None else ["--window-size", f"{size[0]}x{size[1]}"]
    try:
        assert bootstrap.main(tmp_path, arguments) == 0
        assert windows[0].bridge.application.join(0)
        assert windows[0].bridge.application.snapshot().state is ApplicationState.CLOSED
    finally:
        for window in windows:
            close_window(window)


def test_sizing_preserves_actual_widget_constraints(qapp: QApplication) -> None:
    window = QWidget()
    window.setMinimumSize(1000, 800)
    try:
        with pytest.raises(ValueError, match="current window minimum 1000x800"):
            bootstrap.show_sized_window(window, (880, 720))
        assert not window.isVisible()
        window.setMaximumSize(1200, 900)
        with pytest.raises(ValueError, match="cannot apply requested 1600x900"):
            bootstrap.show_sized_window(window, (1600, 900))
        assert window.minimumSize() == QSize(1000, 800)
        assert window.maximumSize() == QSize(1200, 900)
        bootstrap.show_sized_window(window, (1100, 850))
        assert window.minimumSize() == QSize(1000, 800)
        assert window.maximumSize() == QSize(1200, 900)
        window.resize(1150, 875)
        qapp.processEvents()
        assert window.size() == QSize(1150, 875) and not window.isMaximized()
    finally:
        window.close()
        window.deleteLater()


def test_current_minimum_error_drains_normal_startup(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    windows: list[MainWindow] = []
    original_create = bootstrap.create_window

    def application(_arguments: Sequence[str]) -> QApplication:
        return qapp

    def create(startup: Startup, delivery: FutureDelivery) -> MainWindow:
        window = original_create(startup, delivery)
        window.setMinimumWidth(2000)
        windows.append(window)
        return window

    monkeypatch.setattr(bootstrap, "QApplication", application)
    monkeypatch.setattr(bootstrap, "create_window", create)
    try:
        with pytest.raises(SystemExit) as error:
            bootstrap.main(tmp_path, ["--window-size", "1080x1920"])
        assert error.value.code == 2
        assert "current window minimum 2000x720" in capsys.readouterr().err
        assert not windows[0].isVisible()
        assert windows[0].bridge.application.join(0)
        assert windows[0].bridge.application.snapshot().state is ApplicationState.CLOSED
    finally:
        for window in windows:
            close_window(window)
