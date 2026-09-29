"""Standard widgets for lifecycle exploration, not a production inspection UI."""

from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.inference import ImageResult
from vimo_demo.runtime import InferenceWorker, WorkerState

from .inference_bridge import QtInferenceBridge


class DemoWindow(QWidget):
    def __init__(
        self,
        worker: InferenceWorker,
        image_path: str = "",
        *,
        shutdown_timeout_ms: int = 30_000,
    ) -> None:
        super().__init__()
        self.setWindowTitle("ViMo / Qt lifecycle exploration")
        self.resize(560, 330)
        self.bridge = QtInferenceBridge(
            worker, self, shutdown_timeout_ms=shutdown_timeout_ms
        )
        self._closing = False
        self._busy = False
        self._image: NDArray[np.generic] | None = None
        self.status = QLabel(worker.state.name)
        self.start_button = QPushButton("Start Worker")
        self.image_path = QLineEdit(image_path)
        self.browse_button = QPushButton("Select Image")
        self.run_button = QPushButton("Run Inference")
        self.summary = QLabel("No result")
        self.summary.setWordWrap(True)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.close_button = QPushButton("Close")
        layout = QVBoxLayout(self)
        for widget in (
            self.status,
            self.start_button,
            self.image_path,
            self.browse_button,
            self.run_button,
            self.summary,
            self.error,
            self.close_button,
        ):
            layout.addWidget(widget)
        self.start_button.clicked.connect(self.bridge.start)
        self.browse_button.clicked.connect(self._browse)
        self.run_button.clicked.connect(self._run)
        self.close_button.clicked.connect(self.close)
        self.bridge.state_changed.connect(self._state_changed)
        self.bridge.startup_succeeded.connect(self._ready)
        self.bridge.startup_failed.connect(self._show_error)
        self.bridge.result_ready.connect(self._result)
        self.bridge.inference_failed.connect(self._task_error)
        self.bridge.submission_failed.connect(self._task_error)
        self.bridge.shutdown_failed.connect(self._show_error)
        self.bridge.shutdown_finished.connect(self._shutdown_finished)
        self._update_buttons()

    def _update_buttons(self) -> None:
        state = self.bridge.worker.state
        self.start_button.setEnabled(not self._closing and state is WorkerState.NEW)
        self.run_button.setEnabled(
            not self._closing and not self._busy and state is WorkerState.RUNNING
        )
        self.image_path.setEnabled(not self._closing and not self._busy)
        self.browse_button.setEnabled(not self._closing and not self._busy)

    @Slot(object)
    def _state_changed(self, state: object) -> None:
        if isinstance(state, WorkerState):
            self.status.setText("READY" if state is WorkerState.RUNNING else state.name)
        self._update_buttons()

    @Slot()
    def _ready(self) -> None:
        if not self._closing:
            self.status.setText("READY")
            self._update_buttons()

    @Slot()
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select a BGR test image")
        if path:
            self.image_path.setText(path)

    @Slot()
    def _run(self) -> None:
        if self._closing or self._busy:
            return
        self.error.clear()
        try:
            image = cv2.imdecode(
                np.fromfile(Path(self.image_path.text()), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if image is None:
                raise ValueError("Cannot decode the selected image")
        except Exception as exc:
            self._show_error(exc)
            return
        self._image = image  # Owned, unmodified until the task finishes.
        self._busy = True
        self._update_buttons()
        self.bridge.submit(image)

    @Slot(object)
    def _result(self, result: object) -> None:
        self._busy = False
        self._image = None
        if self._closing:
            return
        if isinstance(result, ImageResult):
            self.summary.setText(
                f"has_defects: {result.has_defects}\n"
                f"label_map: {dict(result.label_map)}\n"
                f"regions: {len(result.regions)} / {result.regions_per_label}\n"
                f"mask shape: {result.mask.shape}"
            )
        self._update_buttons()

    @Slot(object)
    def _task_error(self, error: object) -> None:
        self._busy = False
        self._image = None
        self._show_error(error)
        self._update_buttons()

    @Slot(object)
    def _show_error(self, error: object) -> None:
        if isinstance(error, BaseException):
            self.error.setText(f"{type(error).__name__}: {error}")

    @Slot()
    def _shutdown_finished(self) -> None:
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.bridge.stopped:
            self.bridge.detach()
            event.accept()
            return
        event.ignore()
        self._closing = True
        self._update_buttons()
        self.bridge.request_stop()
