"""A searchable historical-account popup on the existing audit reader."""

from typing import cast

from PySide6.QtCore import QPoint, QSignalBlocker, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from capsule_vision.application.audit import AuditAccount
from capsule_vision.application.identity import LocalIdentity
from capsule_vision.application.identity_models import IdentityError, Role
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .future_delivery import FutureDelivery
from .image_tools import image_icon
from .style import Style, theme_colors


class AuditAccounts(QPushButton):
    changed = Signal()

    def __init__(self, identity: LocalIdentity | None) -> None:
        super().__init__()
        self.identity = identity
        self.selected: AuditAccount | None = None
        self.language: Language = "zh"
        self._token = 0
        self._busy = self._pending = self._more = False
        self._revision: int | None = None
        self._cursor: tuple[str, str] | None = None
        self._delivery = FutureDelivery()
        self._delivery.emitter.completed.connect(
            self._completed, Qt.ConnectionType.QueuedConnection
        )
        self.destroyed.connect(
            self._delivery.detach, Qt.ConnectionType.DirectConnection
        )
        self.setFixedSize(208, Style.touch)
        self.popup = QMenu(self)
        panel = QWidget()
        panel.setFixedWidth(360)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(Style.small, Style.small, Style.small, Style.small)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(Style.touch)
        self.choices = QListWidget()
        self.choices.setObjectName("reportBatches")
        self.choices.setMinimumHeight(6 * Style.touch)
        layout.addWidget(self.search)
        layout.addWidget(self.choices)
        action = QWidgetAction(self.popup)
        action.setDefaultWidget(panel)
        self.popup.addAction(action)
        self.clicked.connect(self._open)
        self.search.textChanged.connect(self._search)
        self.choices.itemClicked.connect(self._choose)
        self.choices.itemActivated.connect(self._choose)
        self.choices.verticalScrollBar().valueChanged.connect(self._scroll)
        self.set_language(self.language)

    def _open(self) -> None:
        self.popup.popup(self.mapToGlobal(QPoint(0, self.height())))
        self._search()
        self.search.setFocus()

    def _search(self) -> None:
        self._cursor = None
        self._more = False
        self.choices.clear()
        self._request()

    def _scroll(self, value: int) -> None:
        if (
            self._more
            and not self._busy
            and value == self.choices.verticalScrollBar().maximum()
        ):
            self._request()

    def _request(self) -> None:
        self._token += 1
        self._pending = True
        self.choices.setEnabled(False)
        if not self._busy:
            self._submit()

    def _submit(self) -> None:
        self._pending = False
        if self.identity is None:
            self.clear()
            return
        self._revision = self.identity.snapshot().revision
        self._busy = True
        try:
            future = self.identity.audit.accounts(
                self.search.text(), self._cursor, self._revision
            )
        except RuntimeError as exc:
            self._completed(str(self._token), None, exc)
            return
        self._delivery.watch(str(self._token), future)

    @Slot(str, object, object)
    def _completed(self, token: str, result: object, error: object) -> None:
        self._busy = False
        if token != str(self._token):
            if self._pending:
                self._submit()
            return
        session = self.identity.snapshot() if self.identity else None
        if (
            session is None
            or session.revision != self._revision
            or session.role < Role.ENGINEER
            or isinstance(error, IdentityError)
        ):
            self.clear()
            return
        self._more = False
        if self._cursor is None:
            self.choices.clear()
            self._add(text("records_all_accounts", self.language), None)
        if error is not None or not isinstance(result, tuple):
            self._add(text("history_unavailable", self.language), None)
            self.choices.item(self.choices.count() - 1).setFlags(
                Qt.ItemFlag.NoItemFlags
            )
        else:
            rows = cast(tuple[AuditAccount, ...], result)
            for account in rows[:50]:
                self._add(account.username, account)
            if rows:
                self._cursor = rows[min(49, len(rows) - 1)].cursor
            self._more = len(rows) > 50
        self.choices.setEnabled(True)

    def _add(self, caption: str, account: AuditAccount | None) -> None:
        self.choices.addItem(caption)
        item = self.choices.item(self.choices.count() - 1)
        item.setData(Qt.ItemDataRole.UserRole, account)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, caption)
        item.setToolTip(caption)
        item.setSizeHint(QSize(300, Style.touch))
        if account == self.selected:
            self.choices.setCurrentItem(item)

    def _choose(self) -> None:
        if self.choices.currentRow() < 0:
            return
        item = self.choices.currentItem()
        if not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        account: object = item.data(Qt.ItemDataRole.UserRole)
        self.selected = account if isinstance(account, AuditAccount) else None
        self.set_language(self.language)
        self.popup.hide()
        self.setFocus()
        self.changed.emit()

    def set_language(self, language: Language) -> None:
        self.language = language
        caption = (
            self.selected.username
            if self.selected
            else text("records_all_accounts", language)
        )
        visible = self.fontMetrics().elidedText(
            caption, Qt.TextElideMode.ElideRight, self.width() - 48
        )
        self.setText(visible.replace("&", "&&"))
        self.setToolTip(caption)
        self.setAccessibleName(text("records_user", language) + " · " + caption)
        self.setIcon(image_icon("discover", theme_colors(self).primary))
        self.setIconSize(QSize(Style.icon, Style.icon))
        self.search.setPlaceholderText(text("records_find_account", language))
        self.search.setAccessibleName(self.search.placeholderText())
        self.choices.setAccessibleName(text("records_user", language))
        if self.choices.count():
            item = self.choices.item(0)
            caption = text("records_all_accounts", language)
            item.setText(caption)
            item.setToolTip(caption)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, caption)

    def clear(self) -> None:
        self._token += 1
        self._pending = self._more = False
        self._cursor = None
        self._revision = None
        self.selected = None
        self.popup.hide()
        self.choices.clear()
        with QSignalBlocker(self.search):
            self.search.clear()
        self.set_language(self.language)

    def dispose(self) -> None:
        self.clear()
        self._delivery.detach()
