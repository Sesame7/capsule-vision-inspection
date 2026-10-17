"""Layout-managed bilingual production shell; application snapshots are authoritative."""

from collections.abc import Callable
from math import ceil

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont, QFontMetricsF, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
from vimo_demo.application.basler_connection import ConnectionState
from vimo_demo.application.folder_replay import FolderSourceError
from vimo_demo.application.local_replay import ReplayState
from vimo_demo.communication.reporting import ReportOutcome
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
    production_actions,
)
from .production_style import (
    Language,
    Style,
    Theme,
    apply_theme,
    camera_icon,
    indicator_icon,
    navigation_icon,
    production_icon,
    shell_icon,
    text,
    theme_colors,
)
from .recipe_editor import RecipeEditorSession
from .shell import PAGES, NavigationBar, StatusBar


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
        self.theme: Theme = "dark"
        self._web_observation: Callable[[], tuple[bool, str]] | None = None
        self._web_interval_ms = 500
        self._service_presentation: tuple[object, ...] | None = None
        self._operation_presentation: tuple[object, ...] | None = None
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
        self._acquisition_progress: tuple[int, str] | None = None
        self._closing = False
        self._presenting_identity = False
        self.setMinimumSize(*Style.minimum)
        self.setWindowTitle("ViMo · Production")
        font = QFont()
        font.setFamilies(["Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        self.setFont(font)
        apply_theme(self, self.theme)
        shell = QWidget()
        shell.setObjectName("shell")
        self.setCentralWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(Style.space, Style.space, Style.space, Style.space)
        root.setSpacing(Style.space)
        self.navbar = NavigationBar()
        self.navigation = self.navbar.buttons
        self.language_selector = self.navbar.language
        self.theme_button = self.navbar.theme
        self.navbar.page_selected.connect(self.navigate)
        self.navbar.language_selected.connect(self._select_language)
        self.navbar.theme_requested.connect(self._switch_theme)
        root.addWidget(self.navbar)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        production = QWidget()
        production.setObjectName("production")
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
        header.setColumnStretch(1, 1)
        self.identity_group = QWidget()
        identity_row = QVBoxLayout(self.identity_group)
        identity_row.setContentsMargins(0, 0, 0, 0)
        identity_row.setSpacing(2)
        self.identity_group.setMinimumHeight(60)
        header.addWidget(self.identity_group, 0, 1)
        self.state_label = QLabel()
        self.state_label.setObjectName("productionState")
        self.state_label.setStyleSheet("font-weight: 600;")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity_row.addWidget(self.state_label)
        self.recipe_identity = QWidget()
        recipe_row = QHBoxLayout(self.recipe_identity)
        recipe_row.setContentsMargins(0, 0, 0, 0)
        recipe_row.setSpacing(Style.small)
        identity_row.addWidget(self.recipe_identity, 0, Qt.AlignmentFlag.AlignHCenter)
        self.recipe_icon = QLabel()
        self.recipe_icon.setFixedSize(Style.icon, Style.icon)
        self.recipe_icon.setPixmap(navigation_icon(1).pixmap(Style.icon, Style.icon))
        recipe_row.addWidget(self.recipe_icon)
        self.identity_label = QLabel()
        self.identity_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.identity_label.setMinimumWidth(2 * Style.touch)
        self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
        recipe_row.addWidget(self.identity_label, 1)
        self.pending_label = QLabel()
        self.pending_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.pending_label.setTextFormat(Qt.TextFormat.PlainText)
        self.pending_label.setProperty("role", "secondary")
        self.pending_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        identity_row.addWidget(self.pending_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.batch_group = QFrame()
        self.batch_group.setObjectName("batchBadge")
        batch_row = QHBoxLayout(self.batch_group)
        batch_row.setContentsMargins(10, 4, 6, 4)
        batch_row.setSpacing(Style.small)
        header.addWidget(self.batch_group, 0, 0, Qt.AlignmentFlag.AlignLeft)
        self.batch_icon = QLabel()
        self.batch_icon.setFixedSize(Style.icon, Style.icon)
        batch_row.addWidget(self.batch_icon)
        self.batch_label = QLabel()
        self.batch_label.setTextFormat(Qt.TextFormat.PlainText)
        self.batch_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.batch_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        batch_row.addWidget(self.batch_label, 1)
        batch = QPushButton()
        batch.setObjectName("productionBatch")
        batch.setFixedSize(Style.touch, Style.touch)
        batch.setIconSize(QSize(Style.icon, Style.icon))
        batch.clicked.connect(self._batch_dialog)
        batch_row.addWidget(batch)
        self.buttons = {"batch": batch}
        self.operation_group = QWidget()
        operations = QHBoxLayout(self.operation_group)
        operations.setContentsMargins(0, 0, 0, 0)
        operations.setSpacing(Style.small)
        self.context_control = QStackedWidget()
        self.context_control.setFixedSize(Style.touch, 48)
        self.context_control.addWidget(QWidget())
        operations.addWidget(self.context_control)
        self._operation_buttons = QButtonGroup(self)
        self._operation_buttons.idClicked.connect(self._operate)
        for index, key in enumerate(("operation", "pause", "stop", "secondary")):
            button = QPushButton()
            button.setProperty("iconAlignment", "leading")
            button.setIconSize(QSize(Style.icon, Style.icon))
            button.setFixedHeight(48)
            self._operation_buttons.addButton(button, index)
            self.buttons[key] = button
            if key == "secondary":
                button.setFixedWidth(Style.touch)
                self.context_control.addWidget(button)
            else:
                button.setObjectName(
                    "start" if key == "operation" else "productionAction"
                )
                operations.addWidget(button)
        header.addWidget(self.operation_group, 0, 2, Qt.AlignmentFlag.AlignRight)
        summary_layout.addLayout(header)
        metrics = QFrame()
        metrics.setObjectName("productionMetrics")
        metric_row = self.metric_row = QHBoxLayout(metrics)
        metric_row.setContentsMargins(12, 8, 12, 8)
        metric_row.setSpacing(12)
        self.metrics: dict[str, tuple[QLabel, QLabel]] = {}
        self.metric_icons: dict[str, QLabel] = {}
        for column, key in enumerate(
            ("cycles", "channel_ok", "channel_ng", "processing")
        ):
            if column:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFixedHeight(36)
                metric_row.addWidget(separator)
            group = QWidget()
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            icon = self.metric_icons[key] = QLabel()
            icon.setFixedSize(28, 28)
            row.addWidget(icon)
            labels = QVBoxLayout()
            labels.setSpacing(2)
            caption = QLabel()
            caption.setProperty("role", "secondary")
            caption.setStyleSheet("font-size: 13px;")
            value = QLabel("0")
            value.setStyleSheet("font-size: 24px; font-weight: 600;")
            labels.addWidget(caption)
            labels.addWidget(value)
            row.addLayout(labels, 1)
            metric_row.addWidget(group, 1)
            self.metrics[key] = (value, caption)
        summary_layout.addWidget(metrics)
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
        self.event_frame = StatusBar()
        self.event_icon = self.event_frame.event_icon
        self.event_label = self.event_frame.event_label
        root.addWidget(self.event_frame)
        self.parameters.preview_changed.connect(self._render)
        bridge.snapshot_changed.connect(self._snapshot_changed)
        bridge.replay_changed.connect(self._replay_changed)
        bridge.source_progress_changed.connect(self._source_progress_changed)
        bridge.frames_changed.connect(self._frames_changed)
        bridge.presentation_ready.connect(self._render_cards)
        bridge.command_failed.connect(self._command_failed)
        bridge.command_finished.connect(self._command_finished)
        bridge.shutdown_finished.connect(self._shutdown_finished)
        bridge.reporting_changed.connect(self._render_services)
        bridge.timer.timeout.connect(self._render_services)
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
        # Keep the actual destination selected while a dirty-draft decision is pending.
        self.navigation[self.pages.currentIndex()].setChecked(True)
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
        self.navigation[index].setFocus(Qt.FocusReason.OtherFocusReason)
        self._refresh_theme_icons()
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
        self._render()

    @Slot(str)
    def _select_language(self, language: str) -> None:
        if language in ("zh", "en"):
            self.set_language("zh" if language == "zh" else "en")

    @Slot()
    def _switch_theme(self) -> None:
        self.set_theme("light" if self.theme == "dark" else "dark")

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        apply_theme(self, theme)
        self._refresh_theme_icons()
        self.communication.set_language(self.language)
        self._render()

    def _refresh_theme_icons(self) -> None:
        self.navbar.present(self.language, self.theme, self.pages.currentIndex())
        self.parameters.refresh_theme()
        workspace = self.parameters.roi_workspace
        if workspace is not None:
            workspace.refresh_theme()
        preview = self.parameters.preview
        if preview is not None:
            preview.refresh_theme()
        colors = theme_colors(self)
        for key, icon in self.metric_icons.items():
            symbol = (
                "OK"
                if key == "channel_ok"
                else "NG"
                if key == "channel_ng"
                else "clock"
                if key == "processing"
                else "cycles"
            )
            icon.setPixmap(
                (
                    indicator_icon(
                        Indicator.OK if key == "channel_ok" else Indicator.NG,
                        colors.ok if key == "channel_ok" else colors.ng,
                    )
                    if key in ("channel_ok", "channel_ng")
                    else production_icon(symbol, colors.secondary)
                ).pixmap(28, 28)
            )
        self.communication.settings_button.setIcon(
            navigation_icon(1, theme_colors(self).secondary)
        )

    def set_language(self, language: Language) -> None:
        self.language = language
        self.parameters.set_language(language)
        self.communication.set_language(language)
        self._refresh_theme_icons()
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

    @Slot(int)
    def _operate(self, index: int) -> None:
        key = ("operation", "pause", "stop", "secondary")[index]
        if self._closing or not self.buttons[key].isEnabled():
            return
        action = self._actions()[key]
        current = self.bridge.application.snapshot()
        if (
            action != self._current_actions()[key]
            or current.generation != self.snapshot.generation
        ):
            # Never reinterpret a stale displayed action as a new Start/session.
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
        elif action == "replay_step":
            self.bridge.step_replay()

    def _actions(self) -> dict[str, str | None]:
        replay = self.bridge.replay_snapshot
        return production_actions(
            self.snapshot,
            (replay.state if replay else ReplayState.STOPPED)
            if self.bridge.controls_enabled
            else None,
        )

    def _current_actions(self) -> dict[str, str | None]:
        replay = self.bridge.control_snapshot()
        return production_actions(
            self.bridge.application.snapshot(),
            (replay.state if replay else ReplayState.STOPPED)
            if self.bridge.controls_enabled
            else None,
        )

    def _render_operations(self) -> None:
        actions = self._actions()
        replay = self.bridge.replay_snapshot
        middle_step = bool(
            self.bridge.controls_enabled
            and replay
            and replay.state in (ReplayState.PAUSED, ReplayState.STEPPING)
        )
        draft_locked = bool(
            self.parameters.busy
            or self.parameters.session
            and (
                self.parameters.session.machine_editor.dirty
                or self.parameters.session.camera_sources
                and self.parameters.session.camera_sources.dirty
            )
        )
        identity = (
            tuple(actions.items()),
            middle_step,
            self.language,
            self.theme,
            self._closing,
            draft_locked,
            self.font().key(),
        )
        if identity == self._operation_presentation:
            return
        self._operation_presentation = identity
        for key, action in actions.items():
            button = self.buttons[key]
            enabled = action is not None and not self._closing
            if action == "start":
                enabled = enabled and not draft_locked
            button.setEnabled(bool(enabled))
            label = text(
                "replay_continue"
                if key == "operation" and action == "replay_continue"
                else {
                    "operation": "replay_start",
                    "pause": "replay_step" if middle_step else "replay_pause",
                    "stop": "production_stop",
                    "secondary": "reset",
                }[key],
                self.language,
            )
            button.setText("" if key == "secondary" else label)
            button.setAccessibleName(label)
            button.setToolTip(
                text("configure_first", self.language)
                if key == "operation"
                and self.snapshot.state is ApplicationState.UNCONFIGURED
                else text("pause_unsupported", self.language)
                if key == "pause" and not self.bridge.controls_enabled
                else text("software_stop", self.language)
                if key == "stop"
                else label
            )
            kind = {
                "operation": "start",
                "pause": "replay_step" if middle_step else "replay_pause",
                "stop": "stop",
                "secondary": "reset",
            }[key]
            button.setIcon(
                production_icon(
                    kind,
                    "#ffffff" if key == "operation" else theme_colors(self).primary,
                )
            )
            button.ensurePolished()
        width = max(
            96,
            max(
                button.fontMetrics().horizontalAdvance(text(caption, language))
                for key, button in self.buttons.items()
                if key in ("operation", "pause", "stop")
                for caption in (
                    "replay_start",
                    "replay_continue",
                    "replay_pause",
                    "replay_step",
                    "production_stop",
                )
                for language in ("zh", "en")
            )
            + Style.icon
            + 28,
        )
        for key in ("operation", "pause", "stop"):
            self.buttons[key].setFixedWidth(width)
        self.operation_group.setFixedWidth(Style.touch + 3 * width + 3 * Style.small)
        self.context_control.setCurrentIndex(1 if actions["secondary"] else 0)

    @Slot(object)
    def _replay_changed(self, _snapshot: object) -> None:
        self._render()

    @Slot(int, str)
    def _source_progress_changed(self, generation: int, phase: str) -> None:
        if generation != self.bridge.application.snapshot().generation:
            return
        self._acquisition_progress = (generation, phase)
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
        if self.bridge.refreshing:
            return  # Snapshot and latest pixels are presented together after refresh.
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
        colors = theme_colors(self)
        self.navbar.present(self.language, self.theme, self.pages.currentIndex())
        snapshot, language = self.snapshot, self.language
        self.configuration_placeholder.setText(text("configure_first", language))
        availability = controls(snapshot)
        self._render_operations()
        self.buttons["batch"].setEnabled(availability.batch and not self._closing)
        state = "closing" if self._closing else snapshot.state.name
        if (
            not self._closing
            and snapshot.state is ApplicationState.RUNNING
            and self.bridge.replay_snapshot is not None
        ):
            state = {
                ReplayState.PAUSED: "replay_paused"
                if self.bridge.replay_enabled
                else "source_paused",
                ReplayState.PAUSING: "source_pausing",
                ReplayState.STEPPING: "source_stepping",
                ReplayState.RESUMING: "source_resuming",
            }.get(self.bridge.replay_snapshot.state, state)
        self.state_label.setText(
            text(
                f"state_{state}"
                if state
                in (
                    "READY",
                    "STARTING",
                    "RUNNING",
                    "replay_paused",
                    "source_paused",
                    "source_pausing",
                    "source_stepping",
                    "source_resuming",
                    "STOPPING",
                    "FAULT",
                    "closing",
                )
                else state,
                language,
            )
        )
        self.state_label.setToolTip(
            text(state, language) + "\n" + text("replay_help", language)
            if self.bridge.replay_enabled
            else text(state, language) + "\n" + text("basler_control_help", language)
            if self.bridge.controls_enabled
            else text(state, language) + "\n" + text("simulation", language)
            if self.simulation
            else text(state, language)
        )
        color = {
            "READY": colors.secondary,
            "STARTING": colors.accent,
            "RUNNING": colors.ok,
            "replay_paused": colors.warning,
            "source_paused": colors.warning,
            "source_pausing": colors.warning,
            "source_stepping": colors.accent,
            "source_resuming": colors.accent,
            "STOPPING": colors.warning,
            "FAULT": colors.fault,
        }.get(state, colors.secondary)
        self.state_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.state_label.setAccessibleName(self.state_label.toolTip())
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
        colors = theme_colors(self)
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
            if warning or fault
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
        acquisition_detail = ""
        if not message and snapshot.state in (
            ApplicationState.STARTING,
            ApplicationState.RUNNING,
        ):
            sources = (
                self.parameters.session.camera_sources
                if self.parameters.session
                else None
            )
            if (
                sources is not None
                and sources.saved.mode == "basler"
                and not (
                    self.parameters.session and self.parameters.session.cli_sources
                )
            ):
                progress = self._acquisition_progress
                if (
                    self.bridge.source_connection is not None
                    and self.bridge.source_connection.source_snapshot().state
                    is ConnectionState.CONNECTED
                ):
                    progress = snapshot.generation, "basler_armed"
                acquisition_detail = text(
                    progress[1]
                    if progress and progress[0] == snapshot.generation
                    else "basler_preparing",
                    language,
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
        workspace = self.parameters.roi_workspace
        vision_feedback = (
            workspace.feedback
            if workspace is not None and self.pages.currentIndex() == 4
            else None
        )
        if vision_feedback is not None and not (
            fault or self._closing or snapshot.last_issue or self._source_issue
        ):
            key, detail = vision_feedback
            message, warning = text(key, language), True
            if detail == message:
                detail = ""
            self._event_message = message
        self.event_label.setToolTip(
            "\n".join(part for part in (message, detail) if part)
        )
        self.event_label.setAccessibleName(message)
        self.state_label.setToolTip(
            "\n".join((self.state_label.accessibleName(), acquisition_detail))
            if acquisition_detail
            and self.bridge.replay_snapshot is not None
            and self.bridge.replay_snapshot.state is not ReplayState.AUTOMATIC
            else acquisition_detail or self.state_label.accessibleName()
        )
        self.event_label.setStyleSheet(
            f"color: {colors.fault if fault else colors.warning if warning else colors.secondary};"
        )
        self.event_icon.setVisible(bool(message))
        self.event_icon.setToolTip(message)
        self.event_icon.setAccessibleName(message)
        if message:
            icon = (
                production_icon("FAULT", colors.fault)
                if fault
                else indicator_icon(Indicator.ERROR, colors.warning)
                if warning
                else production_icon("STOPPING", colors.secondary)
            )
            self.event_icon.setPixmap(icon.pixmap(Style.icon, Style.icon))
        self._render_status_text()
        self._render_services()

    def observe_web(
        self, observation: Callable[[], tuple[bool, str]], *, interval_ms: int
    ) -> None:
        """Read existing owner observations on the bridge's existing refresh tick."""
        self._web_observation = observation
        self._web_interval_ms = interval_ms
        self._render_services()

    def _render_services(self) -> None:
        bar, colors = self.event_frame, theme_colors(self)
        bar.refresh_icons()
        selection = (
            self.bridge.reporting.reporting_selection()
            if self.bridge.reporting
            else None
        )
        report = self.bridge.reporting_snapshot
        plc = "未启用" if self.language == "zh" else "Disabled"
        if selection and selection.enabled:
            plc = "待启动" if self.language == "zh" else "Next Start"
        if report:
            outcome = report.last_outcome
            if outcome and outcome.outcome is ReportOutcome.ACKNOWLEDGED:
                plc = f"{'上次 ACK' if self.language == 'zh' else 'Last ACK'} {outcome.sequence}"
            elif outcome and outcome.outcome in (
                ReportOutcome.FAILED,
                ReportOutcome.REJECTED,
            ):
                plc = "报告失败" if self.language == "zh" else "Report failed"
            elif report.thread_alive:
                plc = "报告中" if self.language == "zh" else "Reporting"
        available, detail = (
            self._web_observation() if self._web_observation else (False, "")
        )
        web = (
            f"{self._web_interval_ms} ms"
            if available
            else ("不可用" if self.language == "zh" else "Unavailable")
            if detail
            else ("未启用" if self.language == "zh" else "Off")
        )
        state_text, state_tip = self.state_label.text(), self.state_label.toolTip()
        state_color = (
            colors.fault
            if self.snapshot.state is ApplicationState.FAULT
            else colors.ok
            if self.snapshot.state in (ApplicationState.READY, ApplicationState.RUNNING)
            else colors.secondary
        )
        state_icon = shell_icon("state", state_color)
        session = self.parameters.session
        sources = (
            session.camera_sources.saved if session and session.camera_sources else None
        )
        if (
            sources is not None
            and session is not None
            and not session.cli_sources
            and self.snapshot.state
            in (ApplicationState.READY, ApplicationState.RUNNING)
            and not (self._closing or self._event_message or self.snapshot.last_issue)
        ):
            if sources.mode == "folder":
                state_text, state_color = (
                    text("source_folder_status", self.language),
                    colors.secondary,
                )
                state_tip += "\n" + text("source_folder_status_tip", self.language)
                state_icon = shell_icon("folder", state_color)
            else:
                connection = self.bridge.source_connection
                snapshot = connection.source_snapshot() if connection else None
                count = (
                    sum(
                        any(
                            c.camera_id == row.camera_id
                            and c.basler_serial == row.serial
                            for c in sources.cameras
                        )
                        for row in snapshot.cameras
                    )
                    if snapshot
                    else 0
                )
                connected = (
                    snapshot is not None and snapshot.state is ConnectionState.CONNECTED
                )
                state_text = f"{count}/{len(sources.cameras)}"
                state_color = (
                    colors.ok
                    if connected and count == len(sources.cameras)
                    else colors.warning
                    if snapshot and snapshot.state is not ConnectionState.DISCONNECTED
                    else colors.secondary
                )
                state_icon = camera_icon(state_color)
                state_tip += "\n" + text("source_basler_status_tip", self.language)
                state_tip += "\n" + text(
                    "source_"
                    + (snapshot.state.name.lower() if snapshot else "disconnected"),
                    self.language,
                )
                if snapshot and snapshot.error:
                    state_tip += "\n" + snapshot.error
        identity = (
            self.theme,
            self.language,
            state_text,
            state_tip,
            state_color,
            plc,
            web,
            detail,
        )
        if identity == self._service_presentation:
            return
        self._service_presentation = identity
        bar.state.setText(state_text)
        bar.state.setToolTip(state_tip)
        bar.state_icon.setToolTip(state_tip)
        bar.state_icon.setAccessibleName(state_tip)
        bar.state_icon.setPixmap(state_icon.pixmap(20, 20))
        bar.state.setStyleSheet(f"color: {state_color};")
        bar.plc.setText(f"PLC · {plc}")
        bar.plc.setToolTip(f"PLC · {plc}\n{text('plc_ack_hint', self.language)}")
        bar.web.setText(f"Web · {web}")
        bar.web.setToolTip(f"Web · {web}" + (f"\n{detail}" if detail else ""))
        bar.web.setStyleSheet(
            f"color: {colors.secondary if available or not detail else colors.warning};"
        )

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
        # Child resize events must not interrupt a partially updated header layout.
        if self._presenting_identity:
            return
        self._presenting_identity = True
        try:
            self._update_identity()
        finally:
            self._presenting_identity = False

    def _update_identity(self) -> None:
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
        batch = f"{text('batch_id', language)} {snapshot.batch_id or '—'}"
        self.batch_label.ensurePolished()
        batch_padding = 16 + 2 * self.batch_group.frameWidth()
        # Equal outer columns anchor Batch/players; the central identities use
        # separate compact lines, so prepared Recipes do not steal action space.
        self.state_label.ensurePolished()
        state_width = max(
            self.state_label.fontMetrics().horizontalAdvance(text(key, lang))
            for key in (
                "UNCONFIGURED",
                "CLOSED",
                "state_READY",
                "state_STARTING",
                "state_RUNNING",
                "state_replay_paused",
                "state_source_paused",
                "state_source_pausing",
                "state_source_stepping",
                "state_source_resuming",
                "state_STOPPING",
                "state_FAULT",
                "state_closing",
            )
            for lang in ("zh", "en")
        )
        self.state_label.setMinimumWidth(state_width)
        side_width = self.operation_group.width()
        self.summary_header.setColumnMinimumWidth(0, side_width)
        self.summary_header.setColumnMinimumWidth(2, side_width)
        batch_fixed = (
            self.buttons["batch"].width()
            + self.batch_icon.width()
            + batch_padding
            + 2 * Style.small
        )
        batch_width = max(1, side_width - batch_fixed)
        self.batch_label.setFixedWidth(
            min(
                batch_width,
                max(
                    # Integer metrics can round below the elision width at high DPI.
                    ceil(
                        QFontMetricsF(self.batch_label.font()).horizontalAdvance(
                            f"{text('batch_id', lang)} {snapshot.batch_id or '—'}"
                        )
                    )
                    for lang in ("zh", "en")
                ),
            )
        )
        self.batch_group.setFixedWidth(self.batch_label.width() + batch_fixed)
        main_width = max(
            self.identity_label.minimumWidth(),
            self.identity_label.fontMetrics().horizontalAdvance(display_name),
        )
        self.identity_label.setMaximumWidth(main_width)
        self.recipe_identity.setMaximumWidth(
            self.recipe_icon.width() + Style.small + main_width
        )
        self.pending_label.setMaximumWidth(
            max(
                self.pending_label.minimumWidth(),
                self.pending_label.fontMetrics().horizontalAdvance(pending_text),
            )
        )
        # Let the center column shrink without retaining widths from a larger window.
        self.identity_label.setText(display_name)
        self.pending_label.setText(pending_text)
        self.summary_header.activate()
        for group in (self.batch_group, self.identity_group, self.recipe_identity):
            layout = group.layout()
            if layout is not None:
                layout.activate()
        # Respect the actual player/identity minimum instead of clipping controls
        # when a restored window or larger font requests too little width.
        self.setMinimumWidth(max(Style.minimum[0], self.minimumSizeHint().width()))
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
        self.recipe_icon.setPixmap(
            navigation_icon(1, theme_colors(self).secondary).pixmap(
                Style.icon, Style.icon
            )
        )
        self.buttons["batch"].setIcon(
            production_icon("batch", theme_colors(self).secondary)
        )
        self.batch_icon.setPixmap(
            production_icon("tag", theme_colors(self).secondary).pixmap(
                Style.icon, Style.icon
            )
        )
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
