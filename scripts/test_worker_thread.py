"""Validate a responsive Qt window using QThread + QObject Worker."""

import sys
import time

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget


class Worker(QObject):
    finished = Signal(float)

    @Slot()
    def run(self):
        started = time.perf_counter()
        thread = QThread.currentThread()
        # Short waits make closing during the simulated task interruptible.
        while time.perf_counter() - started < 3.0:
            if thread.isInterruptionRequested():
                break
            time.sleep(0.05)
        self.finished.emit(time.perf_counter() - started)


class WorkerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Worker Thread Test")
        self.resize(360, 160)
        self.worker_thread: QThread | None = None
        self.worker = None
        self.button = QPushButton("Start Test")
        self.status = QLabel("Ready")
        self.elapsed = QLabel("Elapsed: --")
        layout = QVBoxLayout(self)
        for widget in (self.button, self.status, self.elapsed):
            layout.addWidget(widget)
        self.button.clicked.connect(self.start_test)

    @Slot()
    def start_test(self):
        if self.worker_thread is not None:
            return
        self.button.setEnabled(False)
        self.status.setText("Running...")
        self.elapsed.setText("Elapsed: --")
        self.worker_thread = QThread(self)
        self.worker = Worker()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.complete)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.clean_up)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    @Slot(float)
    def complete(self, seconds: float):
        self.status.setText("Complete")
        self.elapsed.setText(f"Elapsed: {seconds:.3f} s")

    @Slot()
    def clean_up(self):
        self.worker = None
        self.worker_thread = None
        self.button.setEnabled(True)

    def closeEvent(self, event):
        if self.worker_thread is not None:
            self.worker_thread.requestInterruption()
            # quit() alone cannot stop run(); interruption stops its short waits.
            self.worker_thread.quit()
            self.worker_thread.wait()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = WorkerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
