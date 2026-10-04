"""Layout-managed bilingual production shell; application snapshots are authoritative."""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Slot
from PySide6.QtGui import QCloseEvent, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import ApplicationState, ProductionApplicationSnapshot
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus

from .camera_card import CameraCard
from .display_frames import DisplayFrame, DisplaySnapshot
from .parameters_page import ParametersPage
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_presentation import controls, grid_columns
from .production_style import Language, Style, navigation_icon, stylesheet, text
from .recipe_editor import RecipeEditorSession

PAGES = ("production", "parameters", "diagnostics", "history", "settings")


class ProductionWindow(QMainWindow):
    def __init__(
        self,
        bridge: QtProductionBridge,
        choices: tuple[RecipeChoice, ...],
        *,
        language: Language = "zh",
        simulation: bool = False,
        recipe_editor: RecipeEditorSession | None = None,
    ) -> None:
        super().__init__()
        if (
            not choices
            or len(bridge.frames.cameras) != 6
            or any(
                c.prepared.configuration.layout.camera_ids != bridge.frames.cameras
                for c in choices
            )
        ):
            raise ValueError(
                "Production screen requires six consistent camera identities"
            )
        self.bridge = bridge
        bridge.setParent(self)
        self.choices = choices
        self.language: Language = language
        self.simulation = simulation
        self.snapshot = bridge.application.snapshot()
        self._frames: dict[CameraId, DisplayFrame] = {}
        self._columns = 0
        self._command_issue = False
        self._closing = False
        self.setMinimumSize(*Style.minimum)
        self.setWindowTitle("ViMo · Production")
        font = QFont()
        font.setFamilies(["Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        self.setFont(font)
        self.setStyleSheet(stylesheet())
        shell = QWidget()
        shell.setObjectName("shell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.space)
        nav = QHBoxLayout()
        nav.setSpacing(Style.small)
        brand = QLabel("ViMo")
        brand.setProperty("role", "state")
        nav.addWidget(brand)
        self.navigation: list[QPushButton] = []
        for index, _ in enumerate(PAGES):
            button = QPushButton()
            button.setIcon(navigation_icon(index))
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.setCheckable(True)
            button.setMinimumHeight(Style.touch)
            button.clicked.connect(lambda _checked=False, i=index: self.navigate(i))
            nav.addWidget(button, 1)
            self.navigation.append(button)
        self.language_button = QPushButton()
        self.language_button.setMinimumHeight(Style.touch)
        self.language_button.clicked.connect(self._switch_language)
        nav.addWidget(self.language_button)
        root.addLayout(nav)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        production = QWidget()
        page = QVBoxLayout(production)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(Style.space)
        self.pages.addWidget(production)
        summary = QFrame()
        summary.setObjectName("summary")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(
            Style.space, Style.space, Style.space, Style.space
        )
        header = QHBoxLayout()
        self.state_label = QLabel()
        self.state_label.setProperty("role", "state")
        header.addWidget(self.state_label)
        header.addStretch()
        self.mode_label = QLabel()
        self.mode_label.setProperty("role", "secondary")
        header.addWidget(self.mode_label)
        summary_layout.addLayout(header)
        self.identity_label = QLabel()
        self.identity_label.setWordWrap(True)
        self.identity_label.setProperty("role", "secondary")
        summary_layout.addWidget(self.identity_label)
        metric_row = QHBoxLayout()
        self.metrics: dict[str, tuple[QLabel, QLabel]] = {}
        for key in ("cycles", "channel_ok", "channel_ng", "processing"):
            metric = QVBoxLayout()
            metric.setSpacing(0)
            value = QLabel("0")
            value.setProperty("role", "number")
            caption = QLabel()
            caption.setProperty("role", "secondary")
            metric.addWidget(value)
            metric.addWidget(caption)
            metric_row.addLayout(metric, 1)
            self.metrics[key] = (value, caption)
        summary_layout.addLayout(metric_row)
        actions = QHBoxLayout()
        actions.setSpacing(Style.small)
        self.recipe = QComboBox()
        for choice in choices:
            self.recipe.addItem(choice.label)
        self.recipe.setMinimumHeight(Style.touch)
        self.recipe.setMinimumWidth(130)
        self.recipe.activated.connect(self._select_recipe)
        actions.addWidget(self.recipe, 1)
        self.buttons: dict[str, QPushButton] = {}
        for key in ("batch", "reset", "stop", "start"):
            button = QPushButton()
            button.setObjectName(key)
            button.setMinimumHeight(Style.touch)
            actions.addWidget(button)
            self.buttons[key] = button
        self.buttons["start"].clicked.connect(bridge.start)
        self.buttons["stop"].clicked.connect(bridge.stop)
        self.buttons["reset"].clicked.connect(bridge.reset)
        self.buttons["batch"].clicked.connect(self._batch_dialog)
        summary_layout.addLayout(actions)
        page.addWidget(summary)
        workspace = QWidget()
        self.grid = QGridLayout(workspace)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Style.space)
        self.cards = tuple(
            CameraCard(camera)
            for camera in choices[0].prepared.configuration.layout.cameras
        )
        page.addWidget(workspace, 1)
        self.placeholders: dict[str, QLabel] = {}
        self.parameters = ParametersPage(bridge, recipe_editor, simulation=simulation)
        self.parameters.applied.connect(self._recipe_applied)
        for name in PAGES[1:]:
            if name == "parameters":
                self.pages.addWidget(self.parameters)
                continue
            placeholder = QWidget()
            container = QVBoxLayout(placeholder)
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            container.addWidget(label)
            self.placeholders[name] = label
            self.pages.addWidget(placeholder)
        event = QFrame()
        event.setObjectName("event")
        event_layout = QVBoxLayout(event)
        self.event_label = QLabel()
        self.event_label.setWordWrap(True)
        event_layout.addWidget(self.event_label)
        # Fixed bounded presentation space prevents warning-induced grid jumps.
        event.setFixedHeight(76)
        root.addWidget(event)
        bridge.snapshot_changed.connect(self._snapshot_changed)
        bridge.frames_changed.connect(self._frames_changed)
        bridge.command_failed.connect(self._command_failed)
        bridge.command_finished.connect(self._command_finished)
        bridge.shutdown_finished.connect(self._shutdown_finished)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self.navigate(0)
        self.set_language(language)
        self._arrange()
        bridge.refresh()

    @property
    def columns(self) -> int:
        return self._columns

    def _arrange(self) -> None:
        columns = grid_columns(self.width(), self.height())
        if columns == self._columns:
            return
        self._columns = columns
        for card in self.cards:
            self.grid.removeWidget(card)
        for i in range(3):
            self.grid.setRowStretch(i, 1 if i < 6 // columns else 0)
            self.grid.setColumnStretch(i, 1 if i < columns else 0)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange()
        super().resizeEvent(event)

    def navigate(self, index: int) -> None:
        if (
            self.pages.currentWidget() is self.parameters
            and index != 1
            and self.parameters.roi_workspace is not None
        ):
            self.parameters.leave_workspace(lambda: self.navigate(index))
            return
        self.pages.setCurrentIndex(index)
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)

    @Slot()
    def _switch_language(self) -> None:
        self.set_language("en" if self.language == "zh" else "zh")

    def set_language(self, language: Language) -> None:
        self.language = language
        self.parameters.set_language(language)
        self.language_button.setText("English" if language == "zh" else "中文")
        for key, button in zip(PAGES, self.navigation, strict=True):
            button.setText(text(key, language))
        for key, button in self.buttons.items():
            button.setText(text(key, language))
        for key, (_, caption) in self.metrics.items():
            caption.setText(text(key, language))
        for key, label in self.placeholders.items():
            label.setText(
                f"{text(key, language)}\n\n{text('placeholder', language)}\n{text(key + '_note', language)}"
            )
        self.recipe.setAccessibleName(text("recipe", language))
        self._render()

    @Slot(int)
    def _select_recipe(self, index: int) -> None:
        self.bridge.select_recipe(self.choices[index])

    @Slot(object)
    def _recipe_applied(self, choice: object) -> None:
        if not isinstance(choice, RecipeChoice):
            return
        identity = choice.prepared.configuration.recipe_id
        choices = [
            c
            for c in self.choices
            if c.prepared.configuration.recipe_id != identity
            and c.prepared.configuration.layout == choice.prepared.configuration.layout
        ]
        choices.append(choice)
        self.choices = tuple(choices)
        self.recipe.clear()
        self.recipe.addItems([c.label for c in self.choices])
        self._render()

    @Slot()
    def _batch_dialog(self) -> None:
        value, accepted = QInputDialog.getText(
            self,
            text("batch", self.language),
            text("batch_prompt", self.language),
            text=self.snapshot.batch_id or "",
        )
        if accepted:
            self.bridge.start_batch(value.strip() or None)

    @Slot(object)
    def _snapshot_changed(self, snapshot: object) -> None:
        if isinstance(snapshot, ProductionApplicationSnapshot):
            self.snapshot = snapshot
            self._render()

    @Slot(object)
    def _frames_changed(self, frames: object) -> None:
        if isinstance(frames, DisplaySnapshot):
            self._frames = {frame.frame_id.camera_id: frame for frame in frames.frames}
            self._render_cards()

    @Slot(str, object)
    def _command_failed(self, command: str, error: object) -> None:
        if command == "close" and not self.bridge.closing:
            self._closing = False
        self._command_issue = (
            True  # One bounded presentation fact, no exception retained.
        )
        self._render()

    @Slot(str)
    def _command_finished(self, command: str) -> None:
        self._command_issue = False
        self._render()

    def _render_cards(self) -> None:
        identity = (
            self.snapshot.active
            if self.snapshot.state is ApplicationState.RUNNING
            else self.snapshot.candidate
        )
        choice = next(
            (
                c
                for c in self.choices
                if identity
                and c.prepared.configuration.recipe_id == identity.recipe_id
                and c.prepared.configuration.model.model_id == identity.model_id
            ),
            None,
        )
        for index, card in enumerate(self.cards):
            if choice:
                card.camera_layout = choice.prepared.configuration.layout.cameras[index]
            card.present(
                self._frames.get(card.camera_layout.camera_id),
                self.snapshot,
                self.language,
            )

    def _render(self) -> None:
        snapshot, language = self.snapshot, self.language
        availability = controls(snapshot)
        for key, enabled in (
            ("start", availability.start),
            ("stop", availability.stop),
            ("reset", availability.reset),
            ("batch", availability.batch),
        ):
            self.buttons[key].setEnabled(enabled and not self._closing)
        self.recipe.setEnabled(availability.recipe and not self._closing)
        self.state_label.setText(
            text("closing" if self._closing else snapshot.state.name, language)
        )
        color = (
            Style.fault if snapshot.state is ApplicationState.FAULT else Style.accent
        )
        self.state_label.setStyleSheet(f"color: {color};")
        self.mode_label.setText(text("simulation", language) if self.simulation else "")
        selected = snapshot.candidate
        active = snapshot.active
        self.identity_label.setText(
            f"{text('selected', language)}: {selected.recipe_id if selected else '--'} "
            f"[{text('model', language)}: {selected.model_id if selected else '--'}]  ·  "
            f"{text('active', language)}: {active.recipe_id if active else '--'} "
            f"[{text('model', language)}: {active.model_id if active else '--'}]  ·  "
            f"{text('batch_id', language)}: {snapshot.batch_id or '--'}"
        )
        if selected:
            index = next(
                (
                    i
                    for i, c in enumerate(self.choices)
                    if c.prepared.configuration.recipe_id == selected.recipe_id
                ),
                -1,
            )
            if index >= 0:
                self.recipe.setCurrentIndex(index)
        counts = snapshot.counters
        for key, count in (
            ("cycles", counts.cycles_completed),
            ("channel_ok", counts.channel_ok),
            ("channel_ng", counts.channel_ng),
        ):
            self.metrics[key][0].setText(f"{count:,}")
        result = snapshot.latest_production_result
        duration = (
            "--"
            if result is None or result.inspection_started_at is None
            else f"{(result.result_ready_at - result.inspection_started_at) * 1000:.0f} ms"
        )
        self.metrics["processing"][0].setText(duration)
        event_key = (
            "running_note"
            if snapshot.state is ApplicationState.RUNNING
            else snapshot.state.name
        )
        if snapshot.state is ApplicationState.READY:
            event_key = "ready_note"
        if result and result.assessment.judgment is ProductJudgment.NG:
            event_key = "ng_note"
        if result and result.assessment.technical_status is TechnicalStatus.ERROR:
            event_key = "technical_note"
        if snapshot.last_issue:
            event_key = "issue_note"
        if self._command_issue:
            event_key = "command_error"
        if snapshot.state is ApplicationState.FAULT:
            event_key = "fault_note"
        if self._closing:
            event_key = "close_wait" if self._command_issue else "closing"
        message = text(event_key, language)
        if counts.channel_unknown or counts.technical_error_cycles:
            message += f"\n{text('unknown_count', language)}: {counts.channel_unknown:,} · {text('error_cycles', language)}: {counts.technical_error_cycles:,}"
        self.event_label.setText(message)
        self.event_label.setStyleSheet(
            f"color: {Style.fault if snapshot.state is ApplicationState.FAULT else Style.warning if counts.technical_error_cycles or self._command_issue or snapshot.last_issue else Style.secondary};"
        )
        self._render_cards()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() is QEvent.Type.Quit and not self.bridge.closed:
            self.close()
            return True
        return super().eventFilter(watched, event)

    @Slot()
    def _shutdown_finished(self) -> None:
        if self._closing:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.bridge.closed:
            application = QApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
            self.bridge.detach()
            event.accept()
        else:
            event.ignore()
            if not self._closing:
                self.parameters.request_close(self._begin_close)

    def _begin_close(self) -> None:
        self._closing = True
        self._render()
        self.bridge.close()
