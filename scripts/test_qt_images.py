"""Display up to six local images in a resizable 2-row, 3-column grid."""

import argparse
from pathlib import Path
import sys

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QGridLayout, QLabel, QSizePolicy, QWidget


def load_pixmap(path: Path) -> QPixmap:
    # fromfile + imdecode also supports Windows paths containing Chinese text.
    bgr = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("OpenCV could not decode the image")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    height, width, _ = rgb.shape
    image = QImage(
        rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888
    ).copy()  # Own the pixels after the local NumPy array is released.
    return QPixmap.fromImage(image)


class ImageSlot(QLabel):
    def __init__(self, pixmap: QPixmap | None = None):
        super().__init__("Empty")
        self.original = pixmap
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setStyleSheet("background: #202020; color: #cccccc;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.original is not None:
            self.setPixmap(self.original.scaled(
                self.contentsRect().size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class ImageWindow(QWidget):
    def __init__(self, directory: Path):
        super().__init__()
        self.setWindowTitle("Qt Image Test (2 x 3)")
        self.resize(960, 640)
        paths = sorted(
            (p for p in directory.iterdir()
             if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}),
            key=lambda p: p.name.lower(),
        )[:6]
        layout = QGridLayout(self)
        for index in range(6):
            pixmap = None
            if index < len(paths):
                try:
                    pixmap = load_pixmap(paths[index])
                except (OSError, ValueError, cv2.error) as exc:
                    print(f"Cannot read {paths[index]}: {exc}", file=sys.stderr)
            slot = ImageSlot(pixmap)
            if index < len(paths):
                slot.setToolTip(str(paths[index]))
                if pixmap is None:
                    slot.setText("Cannot read image")
            layout.addWidget(slot, index // 3, index % 3)
        for row in range(2):
            layout.setRowStretch(row, 1)
        for column in range(3):
            layout.setColumnStretch(column, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_dir", type=Path, help="Directory containing PNG/JPG/JPEG files")
    args = parser.parse_args()
    if not args.image_dir.is_dir():
        parser.error(f"Not a directory: {args.image_dir}")
    app = QApplication(sys.argv[:1])
    window = ImageWindow(args.image_dir)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
