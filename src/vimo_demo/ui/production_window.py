"""Layout-managed bilingual production shell; application snapshots are authoritative."""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal, Slot
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
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.inspection import CameraId
from vimo_demo.inspection.production import (
    FindingSource,
    InspectionLayout,
    TechnicalStatus,
)

from .camera_card import CameraCard
from .communication_page import CommunicationPage
from .display_frames import DisplayFrame, DisplaySnapshot
from .parameters_page import ParametersPage
from .production_bridge import QtProductionBridge, RecipeChoice
from .production_presentation import (
    Indicator,
    controls,
    grid_columns,
    primary_action,
    replay_actions,
)
from .production_style import (
    Language,
    Style,
    indicator_icon,
    navigation_icon,
    production_icon,
    stylesheet,
    text,
)
from .recipe_editor import RecipeEditorSession

PAGES = ("production", "parameters", "diagnostics", "history", "settings")


class ProductionWindow(QMainWindow):
    language_changed = Signal(str)

    def __init__(
        self,
        bridge: QtProductionBridge,
        choices: tuple[RecipeChoice, ...],
        *,
        language: Language = "zh",
        simulation: bool = False,
        recipe_editor: RecipeEditorSession | None = None,
        layout: InspectionLayout | None = None,
        configuration_issue: str = "",
        configuration_messages: tuple[str, ...] = (),
        configuration_notices: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__()
        if (
            len(bridge.frames.cameras) != 6
            or any(
                c.prepared.configuration.layout.camera_ids != bridge.frames.cameras
                for c in choices
            )
            or (layout is not None and layout.camera_ids != bridge.frames.cameras)
        ):
            raise ValueError(
                "Production screen requires six consistent camera identities"
            )
        self.bridge = bridge
        bridge.setParent(self)
        self.choices = choices
        self.language: Language = language
        self.simulation = simulation
        self.configuration_issue = configuration_issue
        self.configuration_messages = configuration_messages
        self._configuration_notices = configuration_notices
        self._event_message = ""
        self.snapshot = bridge.application.snapshot()
        self._frames: dict[CameraId, DisplayFrame] = {}
        self._columns = 0
        self._command_issue: tuple[str, str | None] | None = None
        self._command_detail = ""
        self._active_choice: RecipeChoice | None = None
        self._source_issue: tuple[str | None, str] | None = None
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
        summary_layout = self.summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(
            Style.space, Style.small, Style.space, Style.small
        )
        summary_layout.setSpacing(Style.small)
        header = self.summary_header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setHorizontalSpacing(Style.space)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        self.identity_group = QWidget()
        identity_row = QHBoxLayout(self.identity_group)
        identity_row.setContentsMargins(0, 0, 0, 0)
        identity_row.setSpacing(Style.small)
        header.addWidget(self.identity_group, 0, 0)
        self.state_icon = QLabel()
        self.state_icon.setFixedSize(Style.icon, Style.icon)
        identity_row.addWidget(self.state_icon)
        self.state_label = QLabel()
        self.state_label.setStyleSheet("font-weight: 600;")
        identity_row.addWidget(self.state_label)
        self.recipe_icon = QLabel()
        self.recipe_icon.setFixedSize(Style.icon, Style.icon)
        self.recipe_icon.setPixmap(navigation_icon(1).pixmap(Style.icon, Style.icon))
        identity_row.addWidget(self.recipe_icon)
        self.identity_label = QLabel()
        self.identity_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.identity_label.setMinimumWidth(2 * Style.touch)
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        identity_row.addWidget(self.identity_label, 1)
        self.pending_label = QLabel()
        self.pending_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.pending_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pending_label.setStyleSheet(f"color: {Style.secondary};")
        identity_row.addWidget(self.pending_label, 2)
        self.batch_group = QWidget()
        batch_row = QHBoxLayout(self.batch_group)
        batch_row.setContentsMargins(0, 0, 0, 0)
        batch_row.setSpacing(Style.small)
        header.addWidget(self.batch_group, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.batch_label = QLabel()
        self.batch_label.setTextFormat(Qt.TextFormat.PlainText)
        batch_row.addWidget(self.batch_label)
        batch = QPushButton()
        batch.setObjectName("productionBatch")
        batch.setStyleSheet(
            "QPushButton#productionBatch:disabled { border-color: transparent; }"
        )
        batch.setFixedSize(Style.touch, Style.touch)
        batch.setIcon(production_icon("batch"))
        batch.setIconSize(QSize(Style.icon, Style.icon))
        batch.clicked.connect(self._batch_dialog)
        batch_row.addWidget(batch)
        operation = QPushButton()
        operation.setObjectName("start")
        operation.setFixedSize(4 * Style.touch, Style.touch)
        operation.setIconSize(QSize(Style.icon, Style.icon))
        operation.clicked.connect(self._operate)
        self.buttons = {"batch": batch, "operation": operation}
        self.operation_group: QWidget = operation
        if bridge.local_replay is not None:
            self.operation_group = QWidget()
            operations = QHBoxLayout(self.operation_group)
            operations.setContentsMargins(0, 0, 0, 0)
            operations.setSpacing(Style.small)
            operation.setFixedWidth(3 * Style.touch)
            operations.addWidget(operation)
            secondary = QPushButton()
            secondary.setFixedSize(2 * Style.touch, Style.touch)
            secondary.setIconSize(QSize(Style.icon, Style.icon))
            secondary.clicked.connect(self._secondary_operation)
            operations.addWidget(secondary)
            self.buttons["secondary"] = secondary
        header.addWidget(self.operation_group, 0, 2, Qt.AlignmentFlag.AlignRight)
        summary_layout.addLayout(header)
        metric_row = QHBoxLayout()
        self.metrics: dict[str, tuple[QLabel, QLabel]] = {}
        for key in ("cycles", "channel_ok", "channel_ng", "processing"):
            metric = QHBoxLayout()
            metric.setSpacing(Style.small)
            caption = QLabel()
            caption.setProperty("role", "secondary")
            value = QLabel("0")
            value.setStyleSheet("font-size: 18px; font-weight: 600;")
            metric.addWidget(caption)
            metric.addWidget(value)
            metric.addStretch()
            metric_row.addLayout(metric, 3 if key in ("cycles", "processing") else 2)
            self.metrics[key] = (value, caption)
        summary_layout.addLayout(metric_row)
        page.addWidget(summary)
        workspace = QWidget()
        self.grid = QGridLayout(workspace)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Style.space)
        layout = choices[0].prepared.configuration.layout if choices else layout
        self.cards: tuple[CameraCard, ...] = (
            tuple(CameraCard(camera) for camera in layout.cameras) if layout else ()
        )
        self.configuration_placeholder = QLabel()
        self.configuration_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.configuration_placeholder.setWordWrap(True)
        if not self.cards:
            self.grid.addWidget(self.configuration_placeholder, 0, 0)
        else:
            self.configuration_placeholder.hide()
        page.addWidget(workspace, 1)
        self.placeholders: dict[str, QLabel] = {}
        self.parameters = ParametersPage(
            bridge,
            recipe_editor,
            simulation=simulation,
            configuration_messages=configuration_messages,
            configuration_details=configuration_issue,
        )
        self.parameters.applied.connect(self._recipe_applied)
        self.parameters.sources_verified.connect(self._sources_verified)
        self.parameters.setup_changed.connect(self._render)
        self.communication = CommunicationPage(reporting=bridge.reporting)
        if self.communication.reporting_panel is not None:
            self.communication.reporting_panel.update_state(self.snapshot.state)
            bridge.reporting_changed.connect(self.communication.reporting_panel.refresh)
        for name in PAGES[1:]:
            if name == "parameters":
                self.pages.addWidget(self.parameters)
                continue
            if name == "settings":
                self.pages.addWidget(self.parameters.settings)
                continue
            if name == "diagnostics":
                self.pages.addWidget(self.communication)
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
        self.event_layout = event_layout = QHBoxLayout(event)
        event_layout.setContentsMargins(Style.space, 3, Style.space, 3)
        event_layout.setSpacing(Style.small)
        self.event_icon = QLabel()
        self.event_icon.setFixedSize(Style.icon, Style.icon)
        event_layout.addWidget(self.event_icon)
        self.event_label = QLabel()
        self.event_label.setTextFormat(Qt.TextFormat.PlainText)
        self.event_label.setWordWrap(False)
        self.event_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        event_layout.addWidget(self.event_label, 1)
        # One reserved shell strip; navigation/messages never resize page workspaces.
        event.setFixedHeight(Style.body + 3 * Style.small)
        root.addWidget(event)
        self.parameters.preview_changed.connect(self._render)
        bridge.snapshot_changed.connect(self._snapshot_changed)
        bridge.replay_changed.connect(self._replay_changed)
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
        self.communication.set_language(language)
        self.language_button.setText("English" if language == "zh" else "中文")
        for key, button in zip(PAGES, self.navigation, strict=True):
            button.setText(text(key, language))
            button.setToolTip(text(key, language))
            button.setAccessibleName(text(key, language))
        self.buttons["batch"].setToolTip(text("batch", language))
        self.buttons["batch"].setAccessibleName(text("batch", language))
        for key, (_, caption) in self.metrics.items():
            caption.setText(
                text(
                    {"channel_ok": "production_ok", "channel_ng": "production_ng"}.get(
                        key, key
                    ),
                    language,
                )
            )
        for key in ("channel_ok", "channel_ng"):
            for label in self.metrics[key]:
                label.setToolTip(text("channel_count_help", language))
        for key, label in self.placeholders.items():
            label.setText(
                f"{text(key, language)}\n\n{text('placeholder', language)}\n{text(key + '_note', language)}"
            )
        self._render()
        self.language_changed.emit(language)

    @Slot()
    def _operate(self) -> None:
        if self._closing or not self.buttons["operation"].isEnabled():
            return
        action = self._actions()[0]
        current = self.bridge.application.snapshot()
        current_action = (
            self._current_actions()[0]
            if self.bridge.local_replay is not None
            else primary_action(current.state)
        )
        if action != current_action or (
            self.bridge.local_replay is not None
            and current.generation != self.snapshot.generation
        ):
            # A completed replay may reach READY before the next UI poll. Never
            # reinterpret a still-visible Stop/Reset click as a new Start.
            if self.bridge.local_replay is not None:
                self.bridge.refresh()
            self._snapshot_changed(current)
            return
        if action == "start":
            self.bridge.start()
        elif action == "stop":
            self.bridge.stop()
        elif action == "reset":
            self.bridge.reset()
        elif action == "replay_pause":
            self.bridge.pause_replay()
        elif action == "replay_continue":
            self.bridge.continue_replay()

    def _actions(self) -> tuple[str, str | None]:
        replay = self.bridge.replay_snapshot
        return (
            replay_actions(self.snapshot.state, replay.state if replay else None)
            if self.bridge.local_replay is not None
            else (primary_action(self.snapshot.state), None)
        )

    def _current_actions(self) -> tuple[str, str | None]:
        current = self.bridge.application.snapshot()
        replay = (
            self.bridge.local_replay.replay_snapshot()
            if self.bridge.local_replay is not None
            else None
        )
        return (
            replay_actions(current.state, replay.state if replay else None)
            if self.bridge.local_replay is not None
            else (primary_action(current.state), None)
        )

    @Slot()
    def _secondary_operation(self) -> None:
        secondary = self.buttons.get("secondary")
        if self._closing or secondary is None or not secondary.isEnabled():
            return
        action = self._actions()[1]
        if (
            action != self._current_actions()[1]
            or self.snapshot.generation != self.bridge.application.snapshot().generation
        ):
            self.bridge.refresh()
            return
        if action == "replay_step":
            self.bridge.step_replay()
        elif action == "replay_end":
            self.bridge.stop()

    @Slot(object)
    def _replay_changed(self, _snapshot: object) -> None:
        self._render()

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
        # Candidate preparation does not verify acquisition or repair app.json.
        self._retain_configuration_feedback(
            ("startup_source_unavailable", "startup_language", "startup_preferences")
        )
        self._render()

    def _retain_configuration_feedback(self, keys: tuple[str, ...]) -> None:
        self.configuration_messages = tuple(
            key for key in self.configuration_messages if key in keys
        )
        if self._configuration_notices:
            self.configuration_issue = "\n".join(
                detail
                for key, detail in self._configuration_notices
                if key in self.configuration_messages
            )
        elif not self.configuration_messages:
            self.configuration_issue = ""
        self.parameters.configuration_messages = self.configuration_messages
        self.parameters.configuration_details = self.configuration_issue
        self.parameters.refresh_state()

    @Slot()
    def _sources_verified(self) -> None:
        self._retain_configuration_feedback(
            tuple(
                k
                for k in self.configuration_messages
                if k != "startup_source_unavailable"
            )
        )
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
            recovered = (
                self.snapshot.state is ApplicationState.FAULT
                and snapshot.state
                in (ApplicationState.READY, ApplicationState.UNCONFIGURED)
                and snapshot.last_issue is None
                and not snapshot.runtime_owned
            )
            started = (
                snapshot.state is ApplicationState.RUNNING
                and self.snapshot.state is not ApplicationState.RUNNING
            )
            if recovered or started:
                # Authoritative recovery/readiness supersedes old UI command summaries.
                if not self._command_issue or self._command_issue[0] != "preferences":
                    self._command_issue = None
                    self._command_detail = ""
                self._source_issue = None
            if started:
                self._retain_configuration_feedback(
                    tuple(
                        k
                        for k in self.configuration_messages
                        if k != "startup_source_unavailable"
                    )
                )
            self.snapshot = snapshot
            if self.communication.reporting_panel is not None:
                self.communication.reporting_panel.update_state(snapshot.state)
            self._render()

    @Slot(object)
    def _frames_changed(self, frames: object) -> None:
        if isinstance(frames, DisplaySnapshot):
            self._frames = {frame.frame_id.camera_id: frame for frame in frames.frames}
            self._render_cards()

    @Slot(str, object)
    def _command_failed(self, command: str, error: object) -> None:
        source_error = error if command == "folder_source" else None
        if command in ("start", "replay_step") and isinstance(error, BaseException):
            cause: BaseException | None = error
            for _ in range(8):  # Existing lifecycle wrapping preserves source causes.
                if isinstance(cause, FolderSourceError):
                    source_error = cause
                    break
                if cause is None:
                    break
                cause = cause.__cause__
        if source_error is not None:
            description = str(source_error)
            camera = next(
                (
                    str(c)
                    for c in self.bridge.frames.cameras
                    if description.startswith((f"{c}:", f"{c} / "))
                ),
                None,
            )
            # Keep factual camera/type only, not paths, raw errors or exception objects.
            self._source_issue = (camera, type(source_error).__name__)
        if command == "close" and not self.bridge.closing:
            self._closing = False
            if not self.bridge.closed:
                self.communication.resume()
        # One bounded command/type summary; never retain the exception or raw SDK text.
        self._command_issue = (
            command,
            type(error).__name__ if isinstance(error, BaseException) else None,
        )
        # Preferences are configuration diagnostics, never raw native/SDK errors.
        self._command_detail = str(error) if command == "preferences" else ""
        self._render()

    @Slot(str)
    def _command_finished(self, command: str) -> None:
        if command == "start":
            self._source_issue = None
        previous = self._command_issue[0] if self._command_issue else None
        if previous == command or (
            previous != "preferences" and command != "preferences"
        ):
            self._command_issue = None
            self._command_detail = ""
        if command == "preferences":
            self._retain_configuration_feedback(
                tuple(
                    k
                    for k in self.configuration_messages
                    if k not in {"startup_language", "startup_preferences"}
                )
            )
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
        self.configuration_placeholder.setText(text("configure_first", language))
        availability = controls(snapshot)
        action, secondary_action = self._actions()
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
            "replay_pause": snapshot.state is ApplicationState.RUNNING,
            "replay_continue": snapshot.state is ApplicationState.RUNNING,
        }.get(action, False)
        operation = self.buttons["operation"]
        operation.setEnabled(bool(enabled) and not self._closing)
        operation.setText(
            text(
                "replay_start"
                if action == "start" and self.bridge.local_replay
                else action,
                language,
            )
        )
        operation.setIcon(production_icon(action))
        operation.setAccessibleName(operation.text())
        operation.setToolTip(
            text("configure_first", language)
            if snapshot.state is ApplicationState.UNCONFIGURED
            else text("software_stop", language)
            if action == "stop"
            else operation.text()
        )
        secondary = self.buttons.get("secondary")
        if secondary is not None:
            secondary.setVisible(secondary_action is not None)
            secondary.setText(text(secondary_action or "replay_step", language))
            secondary.setIcon(production_icon(secondary_action or "replay_step"))
            secondary.setAccessibleName(secondary.text())
            secondary.setToolTip(
                text(
                    "software_stop"
                    if secondary_action == "replay_end"
                    else "replay_help",
                    language,
                )
            )
            secondary.setEnabled(
                not self._closing
                and (
                    enabled
                    if action == "start"
                    else secondary_action in ("replay_step", "replay_end")
                )
            )
        self.buttons["batch"].setEnabled(availability.batch and not self._closing)
        state = "closing" if self._closing else snapshot.state.name
        self.state_label.setText(text(state, language))
        if (
            snapshot.state is ApplicationState.RUNNING
            and self.bridge.replay_snapshot is not None
            and self.bridge.replay_snapshot.state is ReplayState.PAUSED
        ):
            self.state_label.setText(text("replay_paused", language))
        self.state_label.setToolTip(
            text("replay_help", language)
            if self.bridge.local_replay is not None
            else text("simulation", language)
            if self.simulation
            else ""
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
            ("channel_ok", counts.business_ok),
            ("channel_ng", counts.business_ng),
        ):
            self.metrics[key][0].setText(f"{count:,}")
            self.metrics[key][0].setAccessibleName(
                f"{self.metrics[key][1].text()}: {count:,}"
            )
        result = snapshot.latest_production_result
        duration = (
            "--"
            if result is None or result.inspection_started_at is None
            else f"{(result.result_ready_at - result.inspection_started_at) * 1000:.0f} ms"
        )
        self.metrics["processing"][0].setText(duration)
        self._render_status()
        self._render_cards()

    def _render_status(self) -> None:
        snapshot, language = self.snapshot, self.language
        result = snapshot.latest_production_result
        event_key = snapshot.state.name
        if result and result.assessment.technical_status is TechnicalStatus.ERROR:
            event_key = "technical_note"
        if snapshot.last_issue:
            event_key = "issue_note"
        command = self._command_issue[0] if self._command_issue else None
        relevant_command = (
            self.pages.currentIndex() in (1, 4)
            if command == "recipe"
            else self.pages.currentIndex() == 0
            if command == "batch"
            else command is not None
        )
        if relevant_command or self._source_issue:
            event_key = "command_error"
        if snapshot.state is ApplicationState.FAULT:
            event_key = "fault_note"
        if self._closing:
            event_key = "close_wait" if self._command_issue else "closing"
        warning = event_key in {
            "technical_note",
            "issue_note",
            "command_error",
            "close_wait",
        }
        fault = snapshot.state is ApplicationState.FAULT
        message = (
            self._status_message(event_key, text(event_key, language))
            if warning or fault or self._closing
            else text("configure_first", language)
            if self.pages.currentIndex() == 0
            and snapshot.state is ApplicationState.UNCONFIGURED
            else ""
        )
        if self._source_issue and event_key == "command_error":
            camera, exception_type = self._source_issue
            message = " · ".join(
                part
                for part in (camera, text("source_error", language), exception_type)
                if part
            )
        if self.configuration_issue and not (warning or fault or self._closing):
            message = (
                " · ".join(text(key, language) for key in self.configuration_messages)
                if self.configuration_messages
                else (
                    f"{text('configure_first', language)} · {self.configuration_issue}"
                )
            )
        self._event_message = message
        detail = (
            self.configuration_issue
            if self.configuration_messages and not (warning or fault or self._closing)
            else ""
        )
        if event_key == "command_error" and command == "preferences":
            detail = "\n".join(
                part
                for part in (self._command_detail, self.configuration_issue)
                if part
            )
        self.event_label.setToolTip(
            "\n".join(part for part in (message, detail) if part)
        )
        self.event_label.setAccessibleName(message)
        self.event_label.setStyleSheet(
            f"color: {Style.fault if fault else Style.warning if warning else Style.secondary};"
        )
        self.event_icon.setVisible(bool(message))
        self.event_icon.setToolTip(message)
        self.event_icon.setAccessibleName(message)
        if message:
            icon = (
                production_icon("FAULT")
                if fault
                else indicator_icon(Indicator.ERROR)
                if warning
                else production_icon("STOPPING")
            )
            self.event_icon.setPixmap(icon.pixmap(Style.icon, Style.icon))
        self._render_status_text()

    def _status_message(self, event_key: str, fallback: str) -> str:
        """Describe existing technical evidence; never guess a cause or show raw SDK text."""
        language = self.language
        issue = self.snapshot.last_issue
        if event_key in {"fault_note", "issue_note"} and issue is not None:
            key = {
                "startup": "startup_error",
                "shutdown": "shutdown_error",
                "runtime": "runtime_error",
            }.get(issue.stage, "operation_error")
            return f"{text(key, language)} · {issue.exception_type}"
        if event_key == "fault_note":
            return text("runtime_error", language)
        if event_key == "command_error":
            summary = text("operation_error", language)
            if self._command_issue is not None:
                command, exception_type = self._command_issue
                if command == "preferences":
                    return text("preferences_error", language) + (
                        f" · {exception_type}" if exception_type else ""
                    )
                if command in {"start", "stop", "reset", "recipe", "batch"}:
                    summary = f"{text(command, language)} · {summary}"
                if exception_type:
                    summary += f" · {exception_type}"
            return summary
        if event_key != "technical_note":
            return fallback
        result = self.snapshot.latest_production_result
        if result is None:
            return text("inspection_issue", language)
        for camera in result.cameras:
            if camera.assessment.technical_status is not TechnicalStatus.ERROR:
                continue
            reason = camera.collection_issue or next(
                (
                    reason
                    for channel in camera.channels
                    for check in channel.checks
                    if check.required
                    and check.assessment.technical_status is TechnicalStatus.ERROR
                    for reason in check.reasons
                ),
                None,
            )
            if reason is None:
                return f"{camera.camera_id} · {text('inspection_issue', language)}"
            key = {
                "execution_error": "execution_error",
                "capsule.execution_error": "execution_error",
                "invalid_result": "invalid_result",
                "result_unavailable": "result_unavailable",
                "cycle_timed_out": "cycle_timeout",
                "cycle_shutdown": "cycle_shutdown",
                "cycle_failed": "inspection_issue",
            }.get(reason.code)
            summary = (
                text(key, language)
                if key is not None
                else f"{text('inspection_issue', language)} ({reason.code})"
            )
            source = (
                "ViMo"
                if reason.source is FindingSource.VIMO
                else text("traditional_short", language)
                if reason.source is FindingSource.TRADITIONAL
                else text("collection_short", language)
            )
            detail = f" · {reason.exception_type}" if reason.exception_type else ""
            return f"{camera.camera_id} · {source} {summary}{detail}"
        return text("inspection_issue", language)

    def _render_status_text(self) -> None:
        self.event_label.setText(
            self.event_label.fontMetrics().elidedText(
                " ".join(self._event_message.split()),
                Qt.TextElideMode.ElideRight,
                self.event_label.width(),
            )
        )

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
        candidate_choice = next(
            (
                c
                for c in self.choices
                if ConfigurationIdentity.from_prepared(c.prepared) == candidate
            ),
            None,
        )
        candidate_name = (
            candidate_choice.label
            if candidate_choice
            else candidate.recipe_id
            if candidate
            else "--"
        )
        display_name = active_name if active else candidate_name
        recipe_label = f"{text('production_recipe', language)}: {display_name}"
        label = recipe_label
        pending = (
            active is not None
            and candidate is not None
            and (
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
        )
        pending_text = f"{text('pending_recipe', language)}: {candidate_name}"
        if pending:
            label += f" · {pending_text}"
        self.pending_label.setVisible(pending)
        self.pending_label.setMinimumWidth(
            self.pending_label.fontMetrics().horizontalAdvance(
                f"{text('pending_recipe', language)}: …"
            )
        )
        batch = f"{text('batch_id', language)} {snapshot.batch_id or '--'}"
        # Reserve equal side columns so the whole Batch/edit group is centered.
        # Only shorten its text when the actual row cannot fit the full identity.
        margins = self.summary_layout.contentsMargins()
        row_width = (
            self.summary.contentsRect().width() - margins.left() - margins.right()
        )
        side_width = max(
            self.identity_group.minimumSizeHint().width(),
            self.operation_group.sizeHint().width()
            if self.bridge.local_replay is not None
            else self.buttons["operation"].width(),
        )
        batch_width = max(
            1,
            row_width
            - 2 * (side_width + self.summary_header.horizontalSpacing())
            - self.buttons["batch"].width()
            - Style.small,
        )
        self.batch_label.setFixedWidth(
            min(batch_width, self.batch_label.fontMetrics().horizontalAdvance(batch))
        )
        self.identity_label.setText(
            self.identity_label.fontMetrics().elidedText(
                display_name, Qt.TextElideMode.ElideRight, self.identity_label.width()
            )
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
        self.recipe_icon.setToolTip(label)
        self.recipe_icon.setAccessibleName(text("production_recipe", language))
        self.batch_label.setText(
            self.batch_label.fontMetrics().elidedText(
                batch, Qt.TextElideMode.ElideRight, self.batch_label.width()
            )
        )
        self.batch_label.setToolTip(batch)
        self.batch_label.setAccessibleName(batch)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() is QEvent.Type.Resize and watched in (
            self.summary,
            self.identity_label,
            self.pending_label,
            self.batch_label,
        ):
            self._render_identity()
        if event.type() is QEvent.Type.Resize and watched is self.event_label:
            self._render_status_text()
        if event.type() is QEvent.Type.Quit and not self.bridge.closed:
            self.close()
            return True
        return super().eventFilter(watched, event)

    @Slot()
    def _shutdown_finished(self) -> None:
        # An idle Communication page can call back inside the first closeEvent.
        # Reclosing there hides a native window before deferred cleanup finishes.
        if self._closing and self.bridge.closed and self.communication.closed:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.bridge.closed and self.communication.closed:
            application = QApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
            self.bridge.detach()
            self.communication.dispose()
            event.accept()
        else:
            event.ignore()
            if not self._closing:
                self.parameters.request_close(self._begin_close)

    def _begin_close(self) -> None:
        self._closing = True
        self._render()
        self.communication.request_close(self._shutdown_finished)
        self.bridge.close()
