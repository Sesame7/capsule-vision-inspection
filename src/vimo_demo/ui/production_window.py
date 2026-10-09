"""Layout-managed bilingual production shell; application snapshots are authoritative."""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Slot
from PySide6.QtGui import QCloseEvent, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from vimo_demo.application import (
    ApplicationState,
    ConfigurationIdentity,
    ProductionApplicationSnapshot,
)
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import ProductJudgment, TechnicalStatus

from .camera_card import CameraCard
from .display_frames import DisplayFrame, DisplaySnapshot
from .parameters_page import ParametersPage
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_presentation import controls, grid_columns, primary_action
from .production_style import (
    Language,
    Style,
    navigation_icon,
    production_icon,
    stylesheet,
    text,
)
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
        self._active_choice: RecipeChoice | None = None
        self._source_issue = ""
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
        summary = self.summary = QFrame()
        summary.setObjectName("summary")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(
            Style.space, Style.small, Style.space, Style.small
        )
        summary_layout.setSpacing(Style.small)
        header = QHBoxLayout()
        header.setSpacing(Style.space)
        self.state_icon = QLabel()
        self.state_icon.setFixedSize(Style.icon, Style.icon)
        header.addWidget(self.state_icon)
        self.state_label = QLabel()
        self.state_label.setStyleSheet("font-weight: 600;")
        header.addWidget(self.state_label)
        self.identity_label = QLabel()
        self.identity_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.identity_label.setMinimumWidth(2 * Style.touch)
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.identity_label, 1)
        self.pending_label = QLabel()
        self.pending_label.setFixedWidth(4 * Style.touch)
        self.pending_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pending_label.setStyleSheet(f"color: {Style.secondary};")
        header.addWidget(self.pending_label)
        self.batch_label = QLabel()
        self.batch_label.setFixedWidth(3 * Style.touch)
        self.batch_label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.batch_label)
        batch = QPushButton()
        batch.setFixedSize(Style.touch, Style.touch)
        batch.setIcon(production_icon("batch"))
        batch.setIconSize(QSize(Style.icon, Style.icon))
        batch.clicked.connect(self._batch_dialog)
        header.addWidget(batch)
        operation = QPushButton()
        operation.setObjectName("start")
        operation.setFixedSize(4 * Style.touch, Style.touch)
        operation.setIconSize(QSize(Style.icon, Style.icon))
        operation.clicked.connect(self._operate)
        header.addWidget(operation)
        self.buttons = {"batch": batch, "operation": operation}
        summary_layout.addLayout(header)
        metric_row = QHBoxLayout()
        self.metrics: dict[str, tuple[QLabel, QLabel]] = {}
        for key in ("cycles", "channel_ng", "processing"):
            metric = QHBoxLayout()
            metric.setSpacing(Style.small)
            caption = QLabel()
            caption.setProperty("role", "secondary")
            value = QLabel("0")
            value.setStyleSheet("font-size: 18px; font-weight: 600;")
            metric.addWidget(caption)
            metric.addWidget(value)
            metric.addStretch()
            metric_row.addLayout(metric, 1)
            self.metrics[key] = (value, caption)
        summary_layout.addLayout(metric_row)
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
        self.parameters.setup_changed.connect(self._render)
        for name in PAGES[1:]:
            if name == "parameters":
                self.pages.addWidget(self.parameters)
                continue
            if name == "settings":
                self.pages.addWidget(self.parameters.settings)
                continue
            placeholder = QWidget()
            container = QVBoxLayout(placeholder)
            label = QLabel()
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            container.addWidget(label)
            self.placeholders[name] = label
            self.pages.addWidget(placeholder)
        event = self.event_frame = QFrame()
        event.setObjectName("event")
        event_layout = QVBoxLayout(event)
        self.event_label = QLabel()
        self.event_label.setWordWrap(True)
        event_layout.addWidget(self.event_label)
        # Fixed bounded presentation space prevents warning-induced grid jumps.
        event.setFixedHeight(76)
        root.addWidget(event)
        self.parameters.preview_changed.connect(self._render)
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
            and self.parameters.preview is not None
        ):
            self.parameters.leave_preview(lambda: self.navigate(index))
            return
        if (
            self.pages.currentWidget() is self.parameters.settings
            and index != 4
            and self.parameters.roi_workspace is not None
        ):
            self.parameters.leave_workspace(lambda: self.navigate(index))
            return
        if index == 4:
            self.parameters.open_roi_editor()
        self.pages.setCurrentIndex(index)
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
        self._render()

    @Slot()
    def _switch_language(self) -> None:
        self.set_language("en" if self.language == "zh" else "zh")

    def set_language(self, language: Language) -> None:
        self.language = language
        self.parameters.set_language(language)
        self.language_button.setText("English" if language == "zh" else "中文")
        for key, button in zip(PAGES, self.navigation, strict=True):
            button.setText(text(key, language))
        self.buttons["batch"].setToolTip(text("batch", language))
        self.buttons["batch"].setAccessibleName(text("batch", language))
        for key, (_, caption) in self.metrics.items():
            caption.setText(
                text("production_ng" if key == "channel_ng" else key, language)
            )
        self.metrics["channel_ng"][0].setToolTip(text("channel_count_help", language))
        self.metrics["channel_ng"][1].setToolTip(text("channel_count_help", language))
        for key, label in self.placeholders.items():
            label.setText(
                f"{text(key, language)}\n\n{text('placeholder', language)}\n{text(key + '_note', language)}"
            )
        self._render()

    @Slot()
    def _operate(self) -> None:
        if self._closing or not self.buttons["operation"].isEnabled():
            return
        action = primary_action(self.snapshot.state)
        current = self.bridge.application.snapshot()
        if action != primary_action(current.state):
            # A completed replay may reach READY before the next UI poll. Never
            # reinterpret a still-visible Stop/Reset click as a new Start.
            self._snapshot_changed(current)
            return
        if action == "start":
            self.bridge.start()
        elif action == "stop":
            self.bridge.stop()
        elif action == "reset":
            self.bridge.reset()

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
        self._render()

    @Slot()
    def _batch_dialog(self) -> None:
        if (
            self._closing
            or self.bridge.application.snapshot().state is not ApplicationState.READY
        ):
            return
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
        if command == "folder_source":
            self._source_issue = str(error)
        elif command == "start" and isinstance(error, BaseException):
            cause: BaseException | None = error
            for _ in range(8):  # Existing lifecycle wrapping preserves source causes.
                if isinstance(cause, FolderSourceError):
                    self._source_issue = str(cause)
                    break
                if cause is None:
                    break
                cause = cause.__cause__
        if command == "close" and not self.bridge.closing:
            self._closing = False
        self._command_issue = (
            True  # One bounded presentation fact, no exception retained.
        )
        self._render()

    @Slot(str)
    def _command_finished(self, command: str) -> None:
        if command == "start":
            self._source_issue = ""
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
        action = primary_action(snapshot.state)
        enabled = {
            "start": availability.start
            and not self.parameters.busy
            and not (
                self.parameters.session
                and (
                    self.parameters.session.machine_editor.dirty
                    or (
                        self.parameters.session.camera_sources
                        and self.parameters.session.camera_sources.dirty
                    )
                )
            ),
            "stop": availability.stop,
            "reset": availability.reset,
        }.get(action, False)
        operation = self.buttons["operation"]
        operation.setEnabled(bool(enabled) and not self._closing)
        operation.setText(text(action, language))
        operation.setIcon(production_icon(action))
        operation.setAccessibleName(operation.text())
        operation.setToolTip(
            text("configure_first", language)
            if snapshot.state is ApplicationState.UNCONFIGURED
            else text("software_stop", language)
            if action == "stop"
            else operation.text()
        )
        self.buttons["batch"].setEnabled(availability.batch and not self._closing)
        state = "closing" if self._closing else snapshot.state.name
        self.state_label.setText(text(state, language))
        self.state_label.setToolTip(
            text("simulation", language) if self.simulation else ""
        )
        self.state_icon.setPixmap(
            production_icon(snapshot.state.name).pixmap(Style.icon, Style.icon)
        )
        color = (
            Style.fault if snapshot.state is ApplicationState.FAULT else Style.secondary
        )
        self.state_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._render_identity()
        counts = snapshot.counters
        for key, count in (
            ("cycles", counts.cycles_completed),
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
        if self._command_issue or self._source_issue:
            event_key = "command_error"
        if snapshot.state is ApplicationState.FAULT:
            event_key = "fault_note"
        if self._closing:
            event_key = "close_wait" if self._command_issue else "closing"
        preview = (
            self.pages.currentWidget() is self.parameters
            and self.parameters.preview is not None
        )
        # Presentation scope only: preserve snapshots/counters and all fault paths.
        self.event_frame.setVisible(
            not preview
            or event_key
            in {
                "technical_note",
                "issue_note",
                "command_error",
                "fault_note",
                "close_wait",
                "closing",
            }
        )
        production_page = self.pages.currentIndex() == 0
        self.event_frame.setFixedHeight(
            Style.touch + 2 * Style.small if production_page else 76
        )
        message = text(event_key, language)
        if production_page and event_key in {
            "ready_note",
            "running_note",
            "ng_note",
            "STARTING",
            "STOPPING",
            "CLOSED",
        }:
            message = ""
        if production_page and snapshot.state is ApplicationState.UNCONFIGURED:
            message = text("configure_first", language)
        if self._source_issue and event_key == "command_error":
            message = text("source_error", language) + ": " + self._source_issue
        if not production_page and (
            counts.channel_unknown or counts.technical_error_cycles
        ):
            message += f"\n{text('unknown_count', language)}: {counts.channel_unknown:,} · {text('error_cycles', language)}: {counts.technical_error_cycles:,}"
        self.event_label.setText(message)
        self.event_label.setToolTip(message)
        warning = (
            event_key in {"technical_note", "issue_note", "command_error", "close_wait"}
            if production_page
            else bool(
                counts.technical_error_cycles
                or self._command_issue
                or snapshot.last_issue
            )
        )
        self.event_label.setStyleSheet(
            f"color: {Style.fault if snapshot.state is ApplicationState.FAULT else Style.warning if warning else Style.secondary};"
        )
        self._render_cards()

    def _render_identity(self) -> None:
        snapshot, language = self.snapshot, self.language
        active, candidate = snapshot.active, snapshot.candidate
        if active and (
            self._active_choice is None or snapshot.state is ApplicationState.RUNNING
        ):
            choice = next(
                (
                    c
                    for c in self.choices
                    if ConfigurationIdentity.from_prepared(c.prepared) == active
                ),
                None,
            )
            if choice is not None:
                self._active_choice = choice
        active_name = (
            self._active_choice.label
            if active
            and self._active_choice
            and ConfigurationIdentity.from_prepared(self._active_choice.prepared)
            == active
            else active.recipe_id
            if active
            else "--"
        )
        label = f"{text('production_recipe', language)}: {active_name}"
        candidate_choice = next(
            (
                c
                for c in self.choices
                if ConfigurationIdentity.from_prepared(c.prepared) == candidate
            ),
            None,
        )
        pending = candidate is not None and (
            candidate != active
            or (
                candidate_choice is not None
                and self._active_choice is not None
                and (
                    candidate_choice.label != self._active_choice.label
                    or candidate_choice.prepared.configuration
                    != self._active_choice.prepared.configuration
                )
            )
        )
        if candidate and pending:
            candidate_name = (
                candidate_choice.label if candidate_choice else candidate.recipe_id
            )
            label += f" · {text('pending_recipe', language)}: {candidate_name}"
        active_label = f"{text('production_recipe', language)}: {active_name}"
        self.identity_label.setText(
            self.identity_label.fontMetrics().elidedText(
                active_label, Qt.TextElideMode.ElideRight, self.identity_label.width()
            )
        )
        self.pending_label.setVisible(pending)
        pending_text = (
            f"{text('pending_badge', language)}: {candidate_choice.label if candidate_choice else candidate.recipe_id}"
            if candidate
            else ""
        )
        self.pending_label.setText(
            self.pending_label.fontMetrics().elidedText(
                pending_text, Qt.TextElideMode.ElideRight, self.pending_label.width()
            )
        )
        self.pending_label.setToolTip(label)
        self.pending_label.setAccessibleName(label)
        self.identity_label.setToolTip(label)
        self.identity_label.setAccessibleName(label)
        batch = f"{text('batch_id', language)}: {snapshot.batch_id or '--'}"
        self.batch_label.setText(
            self.batch_label.fontMetrics().elidedText(
                batch, Qt.TextElideMode.ElideRight, self.batch_label.width()
            )
        )
        self.batch_label.setToolTip(batch)
        self.batch_label.setAccessibleName(batch)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() is QEvent.Type.Resize and watched in (
            self.identity_label,
            self.batch_label,
        ):
            self._render_identity()
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
