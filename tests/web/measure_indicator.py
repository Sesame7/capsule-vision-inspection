"""Measure an observer indicator from the Node fixture with the bundled Qt WebEngine."""

import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile


def main() -> int:
    app = QGuiApplication([])
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile, None)

    def loaded(ok: bool) -> None:
        app.exit(0 if ok else 1)

    page.loadFinished.connect(loaded)
    page.setHtml(sys.stdin.read(), QUrl("about:blank"))
    result = app.exec()
    print(page.title())
    return result


if __name__ == "__main__":
    sys.exit(main())
