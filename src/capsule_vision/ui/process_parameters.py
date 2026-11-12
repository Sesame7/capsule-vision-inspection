"""Recipe editors and temporary read-only observations; no I/O or persistence."""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QHideEvent, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.configuration.process_parameters import (
    BUILTIN_PROCESS_CATALOG,
    ProcessCatalog,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language

from .image_tools import image_icon
from .style import Style, button_width, set_button_caption, shell_icon, theme_colors

# Presentation order only. The catalog retains electrical ordering and owns IDs.
_MECHANISMS = (
    "lift_valve",
    "drop_valve",
    "extend_valve",
    "speed",
    "reject",
    "photo",
)
_GROUP_CAPTIONS = {
    "lift_valve": "process_lift_short",
    "drop_valve": "process_drop_short",
    "extend_valve": "process_extend_short",
}
_FIELD_CAPTIONS = {
    "process_running_speed": "process_running_short",
    "process_rotation_speed": "process_rotation_short",
    "process_open_compensation": "process_open_comp_short",
    "process_close_compensation": "process_close_comp_short",
}


class ProcessParametersCard(QFrame):
    edited = Signal()
    plc_requested = Signal()
    plc_ended = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("recipeSection")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.language: Language = "zh"
        self.catalog = BUILTIN_PROCESS_CATALOG
        self.fields: dict[str, QLineEdit] = {}
        self.labels: dict[str, QLabel] = {}
        self.groups: dict[str, QWidget] = {}
        self.group_titles: dict[str, QLabel] = {}
        self.readings: dict[str, QLabel] = {}
        self._value_stacks: dict[str, QStackedLayout] = {}
        self._plc_state = ""
        self._plc_values: dict[str, int | None] = {}
        self._plc_detail = ""
        self._plc_allowed = False
        self._plc_window: QWidget | None = None
        self._unshown: dict[str, str] = {}
        self._columns = 0
        self._editable = False
        root = QVBoxLayout(self)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.small)
        header = QHBoxLayout()
        header.setSpacing(Style.small)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.override_count = QLabel()
        header.addWidget(self.title)
        header.addWidget(self.override_count)
        header.addStretch()
        self.plc_status = QLabel()
        header.addWidget(self.plc_status)
        self.view_plc = QPushButton()
        self.view_plc.setMinimumHeight(Style.touch)
        self.view_plc.setAutoRepeat(False)
        self.view_plc.installEventFilter(self)
        self.view_plc.pressed.connect(self._begin_plc_preview)
        self.view_plc.released.connect(self.end_plc_preview)
        header.addWidget(self.view_plc)
        self.expand = QPushButton()
        self.expand.setObjectName("recipeDisclosure")
        self.expand.setCheckable(True)
        self.expand.setFixedSize(Style.touch, Style.touch)
        self.expand.setIconSize(QSize(Style.icon, Style.icon))
        self.expand.setProperty("compactAction", True)
        header.addWidget(self.expand)
        root.addLayout(header)
        self.details = QWidget()
        self.grid = QGridLayout(self.details)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Style.space)
        root.addWidget(self.details)
        self.details.hide()
        self.expand.toggled.connect(self._expanded)
        self._build()

    def _build(self) -> None:
        self.end_plc_preview()
        for widget in self.groups.values():
            self.grid.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self.fields.clear()
        self.labels.clear()
        self.groups.clear()
        self.group_titles.clear()
        self.readings.clear()
        self._value_stacks.clear()
        groups = tuple(g for g in _MECHANISMS if g in self.catalog.groups) + tuple(
            g for g in self.catalog.groups if g not in _MECHANISMS
        )
        for group in groups:
            widget = QWidget()
            layout = QGridLayout(widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setHorizontalSpacing(Style.small)
            layout.setVerticalSpacing(4)
            layout.setColumnStretch(0, 1)
            layout.setColumnStretch(1, 1)
            title = QLabel()
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            font = title.font()
            font.setBold(True)
            title.setFont(font)
            layout.addWidget(title, 0, 0, 1, 2)
            self.groups[group], self.group_titles[group] = widget, title
            parameters = tuple(p for p in self.catalog.parameters if p.group == group)
            for index, parameter in enumerate(parameters):
                label = QLabel()
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                field = QLineEdit()
                field.setMinimumWidth(0)
                field.setMinimumHeight(Style.touch)
                field.setPlaceholderText("—")
                field.textEdited.connect(self._edited)
                value = QWidget()
                stack = QStackedLayout(value)
                stack.setContentsMargins(0, 0, 0, 0)
                reading = QLabel()
                reading.setObjectName("processReading")
                reading.setTextFormat(Qt.TextFormat.PlainText)
                reading.setSizePolicy(
                    QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
                )
                stack.addWidget(field)
                stack.addWidget(reading)
                label.setBuddy(field)
                row, column = divmod(index, 2)
                layout.addWidget(label, 1 + row * 2, column)
                layout.addWidget(value, 2 + row * 2, column)
                self.fields[parameter.id], self.labels[parameter.id] = field, label
                self.readings[parameter.id], self._value_stacks[parameter.id] = (
                    reading,
                    stack,
                )
        self._columns = 0
        self._arrange()
        self.set_language(self.language)
        self.set_editable(self._editable)

    def show_values(self, catalog: ProcessCatalog, values: dict[str, str]) -> None:
        if catalog != self.catalog:
            self.catalog = catalog
            self._build()
        self._unshown = {
            key: value for key, value in values.items() if key not in self.fields
        }
        for key, field in self.fields.items():
            field.setText(values.get(key, ""))
        self._caption()

    def values(self) -> dict[str, str]:
        return {
            **self._unshown,
            **{
                key: field.text()
                for key, field in self.fields.items()
                if field.text().strip() not in ("", "-")
            },
        }

    def _edited(self) -> None:
        self._caption()
        self.edited.emit()

    def _expanded(self, expanded: bool) -> None:
        if not expanded:
            self.end_plc_preview()
        self.details.setVisible(expanded)
        self._arrange()
        self._caption()
        self.set_plc_available(self._plc_allowed)
        self.updateGeometry()

    @property
    def plc_previewing(self) -> bool:
        return bool(self._plc_state)

    def set_plc_available(self, allowed: bool) -> None:
        self._plc_allowed = allowed
        if not allowed:
            self.end_plc_preview()
        self.view_plc.setEnabled(allowed and self.expand.isChecked())
        self.view_plc.setToolTip(
            text(
                "process_plc_hold"
                if allowed and self.expand.isChecked()
                else "process_plc_expand"
                if allowed
                else "permission_engineer",
                self.language,
            )
        )

    def _begin_plc_preview(self) -> None:
        if not self.view_plc.isEnabled() or self.plc_previewing:
            return
        self._plc_state, self._plc_detail = "process_plc_loading", ""
        self._plc_values.clear()
        self._plc_window = self.window()
        self._plc_window.installEventFilter(self)
        self._render_plc()
        self.plc_requested.emit()

    def show_plc_values(
        self,
        values: dict[str, int | None] | None,
        *,
        detail: str = "",
        error_key: str = "process_plc_unavailable",
    ) -> None:
        if not self.plc_previewing:
            return
        self._plc_values = values or {}
        self._plc_state = "process_plc_values" if values is not None else error_key
        self._plc_detail = detail
        self._render_plc()

    def end_plc_preview(self) -> None:
        if not self.plc_previewing:
            return
        self._plc_state, self._plc_detail = "", ""
        self._plc_values.clear()
        if self._plc_window is not None:
            self._plc_window.removeEventFilter(self)
            self._plc_window = None
        self.view_plc.setDown(False)
        self._render_plc()
        self.plc_ended.emit()

    def _render_plc(self) -> None:
        state = text(self._plc_state, self.language) if self._plc_state else ""
        self.plc_status.setText(state)
        self.plc_status.setToolTip(self._plc_detail)
        self.plc_status.setVisible(
            self.plc_previewing and self._plc_state != "process_plc_values"
        )
        self.view_plc.setAccessibleDescription(
            "\n".join(
                part
                for part in (
                    text("process_plc_hold", self.language),
                    state,
                    self._plc_detail,
                )
                if part
            )
        )
        for key, stack in self._value_stacks.items():
            value = self._plc_values.get(key)
            reading = self.readings[key]
            reading.setText(
                str(value)
                if value is not None
                else "…"
                if self._plc_state == "process_plc_loading"
                else "—"
            )
            full = self.fields[key].accessibleName()
            reading.setAccessibleName(
                f"{full} · {text('process_plc_values', self.language)}"
            )
            reading.setToolTip(
                "\n".join(
                    part
                    for part in (
                        full,
                        state,
                        text("process_plc_unmapped", self.language)
                        if self._plc_state == "process_plc_values" and value is None
                        else "",
                        self._plc_detail,
                    )
                    if part
                )
            )
            reading.setAccessibleDescription(reading.toolTip())
            # Recipe editors and their text are never replaced by observations.
            stack.setCurrentWidget(reading if self.plc_previewing else self.fields[key])

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self.plc_previewing and (
            event.type()
            in (QEvent.Type.WindowDeactivate, QEvent.Type.Close, QEvent.Type.Hide)
            or watched is self.view_plc
            and event.type()
            in (QEvent.Type.FocusOut, QEvent.Type.UngrabMouse, QEvent.Type.TouchCancel)
        ):
            self.end_plc_preview()
        if (
            watched is self.view_plc
            and isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.end_plc_preview()
            return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.end_plc_preview()
        super().hideEvent(event)

    def _caption(self) -> None:
        title = text("process_parameters", self.language)
        count = text("process_override_count", self.language).format(
            count=len(self.values())
        )
        self.title.setText(text("process_title", self.language))
        self.title.setToolTip(title)
        self.title.setAccessibleName(title)
        self.override_count.setText(
            text("process_set_count", self.language).format(count=len(self.values()))
        )
        self.override_count.setToolTip(count)
        self.override_count.setAccessibleName(count)
        self.expand.setToolTip(f"{title} · {count}")
        self.expand.setAccessibleName(f"{title} · {count}")
        self.refresh_theme()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable
        for field in self.fields.values():
            field.setReadOnly(not editable)

    def set_language(self, language: Language) -> None:
        self.language = language
        set_button_caption(
            self.view_plc,
            text("process_view_plc", language),
            button_width(
                self.view_plc,
                ("process_view_plc",),
                languages=("zh", "en"),
            ),
        )
        self.view_plc.setAccessibleDescription(text("process_plc_hold", language))
        self.set_plc_available(self._plc_allowed)
        for group, label in self.group_titles.items():
            full_group = self.catalog.group_label(group, language)
            short = _GROUP_CAPTIONS.get(group)
            label.setText(
                text(short, language)
                if short and group not in dict(self.catalog.group_labels)
                else full_group
            )
            label.setToolTip(full_group)
            label.setAccessibleName(full_group)
        for parameter in self.catalog.parameters:
            label = parameter.label(language)
            full = f"{self.catalog.group_label(parameter.group, language)} · {label}"
            short = _FIELD_CAPTIONS.get(parameter.label_key)
            self.labels[parameter.id].setText(
                text(short, language) if short and not parameter.labels else label
            )
            self.labels[parameter.id].setAccessibleName(full)
            field = self.fields[parameter.id]
            field.setAccessibleName(full)
            hint = text("process_value_hint", language).format(
                name=full,
                minimum=parameter.minimum,
                maximum=parameter.maximum,
                addresses=f"D{parameter.address}–D{parameter.address + 1}",
            )
            field.setToolTip(hint)
            field.setAccessibleDescription(hint)
            self.labels[parameter.id].setToolTip(hint)
        self._caption()
        self._render_plc()
        self._arrange()

    def refresh_theme(self) -> None:
        colors = theme_colors(self)
        self.override_count.setStyleSheet(f"color: {colors.secondary};")
        self.plc_status.setStyleSheet(f"color: {colors.secondary};")
        self.expand.setIcon(
            shell_icon("chevron", colors.primary)
            if self.expand.isChecked()
            else image_icon("next", colors.primary)
        )

    def _arrange(self) -> None:
        width = max(1, self.contentsRect().width() - 2 * Style.space)
        field_width = max(
            4 * Style.touch, self.fontMetrics().horizontalAdvance("000000000000000000")
        )
        group_width = 2 * field_width + Style.small
        columns = min(3, max(1, (width + Style.space) // (group_width + Style.space)))
        if columns == self._columns:
            return
        self._columns = columns
        for widget in self.groups.values():
            self.grid.removeWidget(widget)
        for index, widget in enumerate(self.groups.values()):
            self.grid.addWidget(
                widget, index // columns, index % columns, Qt.AlignmentFlag.AlignTop
            )
        for column in range(3):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._arrange()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "grid"):
            self._arrange()
