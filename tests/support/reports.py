"""Reopen the published document with Qt's PDF reader, including Chinese glyph text."""

from pathlib import Path

from PySide6.QtPdf import QPdfDocument


def pdf_text(path: Path) -> str:
    document = QPdfDocument()
    assert document.load(str(path)) == QPdfDocument.Error.None_
    assert document.pageCount() > 0
    try:
        return "\n".join(
            document.getAllText(i).text() for i in range(document.pageCount())
        )
    finally:
        document.close()
