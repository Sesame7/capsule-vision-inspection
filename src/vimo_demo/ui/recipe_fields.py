"""Typed controls for existing check leaves; no domain validation or precedence."""

from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.configuration import BlobCheckConfig, CheckConfig, ViMoCheckConfig

from .production_style import Language, Style, text
from .recipe_editor import CheckDraft, Group


class CheckFields(QGroupBox):
    changed = Signal()

    def __init__(self, group: Group) -> None:
        super().__init__()
        self.group = group
        self.language: Language = "zh"
        self.root_mode = True
        self._loading = False
        self._row = CheckDraft()
        self._choices: tuple[bool | None, ...] = (True, False)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.labels: dict[str, QLabel] = {}
        self.booleans: dict[str, QComboBox] = {}
        for key in ("enabled", "required"):
            label, combo = QLabel(), QComboBox()
            combo.setMinimumHeight(Style.touch)
            self.labels[key] = label
            self.booleans[key] = combo
            form.addRow(label, combo)
            combo.currentIndexChanged.connect(self._changed)
        self.bounds: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        if group == "blob":
            for key in ("min_area", "max_area"):
                label, enabled, value = QLabel(), QCheckBox(), QLineEdit()
                enabled.setMinimumHeight(Style.touch)
                value.setMinimumHeight(Style.touch)
                row = QHBoxLayout()
                row.addWidget(enabled)
                row.addWidget(value, 1)
                self.labels[key] = label
                self.bounds[key] = (enabled, value)
                form.addRow(label, row)
                enabled.toggled.connect(self._changed)
                value.textEdited.connect(self._changed)
        self.label_override = QCheckBox(self)
        self.label_values = QPlainTextEdit(self)
        if group == "vimo":
            self.label_override.setMinimumHeight(Style.touch)
            self.label_values.setFixedHeight(88)
            self.labels["disabled_labels"] = QLabel()
            self.labels["disabled_labels"].setWordWrap(True)
            box = QWidget()
            inner = QVBoxLayout(box)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.addWidget(self.label_override)
            inner.addWidget(self.label_values)
            form.addRow(self.labels["disabled_labels"], box)
            self.label_override.toggled.connect(self._changed)
            self.label_values.textChanged.connect(self._changed)
        else:
            self.label_override.hide()
            self.label_values.hide()
        layout.addLayout(form)
        self.effective = QLabel()
        self.effective.setWordWrap(True)
        layout.addWidget(self.effective)

    def _changed(self) -> None:
        if self._loading:
            return
        self._refresh_modes()
        self.changed.emit()

    def _refresh_modes(self) -> None:
        for enabled, value in self.bounds.values():
            value.setEnabled(enabled.isChecked())
            key = (
                ("set_bound" if enabled.isChecked() else "not_set")
                if self.root_mode
                else ("override" if enabled.isChecked() else "inherit")
            )
            enabled.setText(text(key, self.language))
        self.label_override.setText(
            text(
                "override" if self.label_override.isChecked() else "inherit",
                self.language,
            )
        )
        self.label_values.setEnabled(self.root_mode or self.label_override.isChecked())

    def show_row(self, row: CheckDraft, *, root: bool, language: Language) -> None:
        self._loading = True
        self._row, self.root_mode, self.language = row, root, language
        self.setTitle(text(self.group, language))
        for key, label in self.labels.items():
            label.setText(text(key, language))
        self._choices = (True, False) if root else (None, True, False)
        for key, value in (("enabled", row.enabled), ("required", row.required)):
            combo = self.booleans[key]
            combo.clear()
            combo.addItems(
                [
                    text("inherit" if v is None else "true" if v else "false", language)
                    for v in self._choices
                ]
            )
            combo.setCurrentIndex(self._choices.index(value))
        for key, value in (("min_area", row.min_area), ("max_area", row.max_area)):
            if key not in self.bounds:
                continue
            toggle, line = self.bounds[key]
            toggle.setText(text("set_bound" if root else "override", language))
            toggle.setToolTip(text("not_set" if root else "inherit", language))
            toggle.setChecked(value is not None)
            line.setText("" if value is None else value)
            line.setEnabled(value is not None)
        self.label_override.setText(text("override", language))
        self.label_override.setVisible(not root and self.group == "vimo")
        self.label_override.setChecked(row.disabled_labels is not None)
        self.label_values.setPlainText("\n".join(row.disabled_labels or ()))
        self.label_values.setEnabled(root or row.disabled_labels is not None)
        self._refresh_modes()
        self._loading = False

    def read(self) -> CheckDraft:
        row = replace(
            self._row,
            enabled=self._choices[self.booleans["enabled"].currentIndex()],
            required=self._choices[self.booleans["required"].currentIndex()],
        )
        if self.group == "blob":
            toggle, line = self.bounds["min_area"]
            row.min_area = line.text() if toggle.isChecked() else None
            toggle, line = self.bounds["max_area"]
            row.max_area = line.text() if toggle.isChecked() else None
        if self.group == "vimo":
            value = self.label_values.toPlainText()
            row.disabled_labels = (
                (() if value == "" else tuple(value.split("\n")))
                if self.root_mode or self.label_override.isChecked()
                else None
            )
        return row

    def show_effective(self, check: CheckConfig | None) -> None:
        label = text("effective", self.language)
        if check is None:
            self.effective.setText(f"{label}: --")
            return
        value = f"{text('enabled', self.language)}={text('true' if check.enabled else 'false', self.language)}, {text('required', self.language)}={text('true' if check.required else 'false', self.language)}"
        if isinstance(check, BlobCheckConfig):
            value += f" · {text('min_area', self.language)}: {check.min_area if check.min_area is not None else text('not_set', self.language)} · {text('max_area', self.language)}: {check.max_area if check.max_area is not None else text('not_set', self.language)}"
        if isinstance(check, ViMoCheckConfig):
            value += " · " + repr(list(check.disabled_labels))
        self.effective.setText(f"{label}: {value}")
