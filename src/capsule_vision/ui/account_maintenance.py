"""Native, offline account maintenance. The CLI and GUI use the same leased operations."""

import sqlite3
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout

from capsule_vision.application.account_maintenance import (
    MaintenanceParser,
    initialize_administrator,
    recover_administrator,
)
from capsule_vision.application.identity_models import (
    AccountConflictError,
    SecurityBusyError,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import LANGUAGES, Language

from .future_delivery import FutureDelivery
from .identity_controls import CredentialDialog, identity_feedback
from .style import Style, apply_theme


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    feedback: str
    audit_incomplete: bool


class MaintenanceWindow(QDialog):
    def __init__(
        self, entry: Path, language: Language, worker: ThreadPoolExecutor
    ) -> None:
        super().__init__()
        self.path = entry.resolve() / "data" / "security.sqlite3"
        self.language: Language = language
        self.worker = worker
        self.form: CredentialDialog | None = None
        self.pending = False
        self.delivery = FutureDelivery()
        self.delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(self.delivery.detach, Qt.ConnectionType.DirectConnection)
        self.setWindowTitle(text("identity_maintenance", language))
        apply_theme(
            self,
            "dark"
            if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
            else "light",
        )
        layout = QVBoxLayout(self)
        instruction = QLabel(text("identity_stop_app", language))
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        path = QLabel(str(self.path))
        path.setTextFormat(Qt.TextFormat.PlainText)
        path.setWordWrap(True)
        layout.addWidget(path)
        self.setup = QPushButton(text("identity_setup", language))
        self.recover = QPushButton(
            f"{text('identity_reset', language)} · {text('identity_administrator', language)}"
        )
        for button in (self.setup, self.recover):
            button.setMinimumHeight(Style.touch)
            layout.addWidget(button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setup.clicked.connect(lambda: self.open_form("setup"))
        self.recover.clicked.connect(lambda: self.open_form("recover"))
        self.resize(460, self.sizeHint().height())

    def open_form(self, kind: Literal["setup", "recover"]) -> None:
        if self.pending or self.form is not None:
            return
        form = self.form = CredentialDialog(kind, None, self)
        form.present(self.language)
        form.setWindowModality(Qt.WindowModality.WindowModal)
        form.buttons.accepted.connect(self._submit)
        form.finished.connect(self._dismiss)
        form.show()

    @Slot()
    def _dismiss(self) -> None:
        if self.form:
            self.form.deleteLater()
            self.form = None

    @Slot()
    def _submit(self) -> None:
        form = self.form
        if self.pending or form is None:
            return
        values = form.submission(self.language)
        if values is None:
            return
        operation = (
            initialize_administrator if form.kind == "setup" else recover_administrator
        )
        username, password = (
            values["identity_username"],
            values["identity_new_password"],
        )
        path = self.path
        form.accept()
        self.pending = True
        self.setup.setEnabled(False)
        self.recover.setEnabled(False)
        self.status.setText(text("identity_working", self.language))

        def run() -> MaintenanceResult:
            incomplete = False

            def warn() -> None:
                nonlocal incomplete
                incomplete = True

            try:
                operation(path, username, password, audit_warning=warn)
            except AccountConflictError:
                key = "identity_denied"
            except SecurityBusyError:
                key = "identity_busy"
            except OSError, sqlite3.Error:
                key = "identity_storage"
            except Exception as exc:
                key = identity_feedback(exc)
            else:
                key = "identity_done"
            return MaintenanceResult(key, incomplete)

        self.delivery.watch("offline", self.worker.submit(run))

    @Slot(str, object, object)
    def _completed(self, _key: str, value: object, error: object) -> None:
        self.pending = False
        self.setup.setEnabled(True)
        self.recover.setEnabled(True)
        result = (
            value
            if isinstance(value, MaintenanceResult)
            else MaintenanceResult(identity_feedback(error), False)
        )
        message = text(result.feedback, self.language)
        if result.audit_incomplete:
            message += " · " + text("identity_audit_incomplete", self.language)
        self.status.setText(message)
        self.status.setAccessibleName(message)

    def reject(self) -> None:
        if not self.pending:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.pending:
            event.ignore()
        else:
            super().closeEvent(event)


def main(argv: Sequence[str] | None = None) -> int:
    parser = MaintenanceParser(
        description="Offline local Administrator setup and recovery"
    )
    parser.add_argument(
        "--entry-directory",
        type=Path,
        default=Path.cwd(),
        help="Installation root containing data/ (default: working directory)",
    )
    parser.add_argument("--language", choices=LANGUAGES, default="zh")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="AccountMaintenance"
    ) as worker:
        window = MaintenanceWindow(args.entry_directory, args.language, worker)
        window.show()
        try:
            return app.exec()
        finally:
            window.delivery.detach()


if __name__ == "__main__":
    raise SystemExit(main())
