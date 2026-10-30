"""Localized production shell; application snapshots are authoritative."""

from collections.abc import Callable
from math import ceil

from PySide6.QtCore import QEvent, QEventLoop, QObject, QSize, Qt, Signal, Slot
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
    QSpacerItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from capsule_vision.application import (
    ApplicationState,
    PreparedConfiguration,
    ProductionApplicationSnapshot,
)
from capsule_vision.application.basler_connection import ConnectionState
from capsule_vision.application.editing import RecipeEditorSession
from capsule_vision.application.folder_replay import FolderSourceError
from capsule_vision.application.source_control import SourceControlState
from capsule_vision.communication.reporting import ReportOutcome
from capsule_vision.display import DisplayFrame, DisplaySnapshot
from capsule_vision.inspection import CameraId
from capsule_vision.inspection.production import (
    FindingSource,
    InspectionLayout,
    TechnicalStatus,
)
from capsule_vision.localization.catalog import text
from capsule_vision.localization.languages import Language, is_language
from capsule_vision.web.runtime import WebRuntime, WebState

from .camera_card import CameraCard
from .communication_page import CommunicationPage
from .editing import EditingCoordinator
from .history_page import HistoryPage
from .production_bridge import QtProductionBridge
from .production_presentation import (
    Indicator,
    controls,
    grid_columns,
    production_actions,
)
from .recipe_page import ModelLabelReader, RecipePage
from .shell import PAGES, NavigationBar, StatusBar
from .style import (
    Style,
    Theme,
    apply_theme,
    button_width,
    camera_icon,
    indicator_icon,
    navigation_icon,
    production_icon,
    set_button_caption,
    shell_icon,
    theme_colors,
)
from .vision_page import VisionPage


class MainWindow(QMainWindow):
    closed = Signal()
    language_changed = Signal(str)

    def __init__(
        self,
        bridge: QtProductionBridge,
        *,
        language: Language = "zh",
        label_reader: ModelLabelReader | None = None,
        recipe_editor: RecipeEditorSession | None = None,
        layout: InspectionLayout | None = None,
        configuration_issue: str = "",
        configuration_messages: tuple[str, ...] = (),
        configuration_notices: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__()
        snapshot = bridge.application.snapshot()
        prepared = (
            snapshot.active_prepared
            if snapshot.state is ApplicationState.RUNNING
            else snapshot.candidate_prepared
        )
        if prepared is not None:
            layout = prepared.configuration.layout
        if (
            len(bridge.frames.cameras) not in (5, 6)
            and not (
                not bridge.frames.cameras
                and layout is None
                and snapshot.state is ApplicationState.UNCONFIGURED
            )
        ) or (layout is not None and layout.camera_ids != bridge.frames.cameras):
            raise ValueError(
                "Production screen requires five or six consistent camera identities"
            )
        self.bridge = bridge
        bridge.setParent(self)
        self.language: Language = language
        self.theme: Theme = "dark"
        self._service_presentation: tuple[object, ...] | None = None
        self._operation_presentation: tuple[object, ...] | None = None
        self.configuration_issue = configuration_issue
        self.configuration_messages = configuration_messages
        self._configuration_notices = configuration_notices
        self._event_message = ""
        self.snapshot = snapshot
        self._frames: dict[CameraId, DisplayFrame] = {}
        self._columns = 0
        self._command_issue: tuple[str, str | None] | None = None
        self._command_detail = ""
        self._source_issue: tuple[str | None, str] | None = None
        self._closing = False
        self._presenting_identity = False
        self.setMinimumSize(*Style.minimum)
        self.setWindowTitle("Capsule Vision Inspection · Production")
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
        production.setObjectName("page_production")
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
        self._batch_balance = QSpacerItem(
            0, 0, QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        header.addItem(self._batch_balance, 0, 0)
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
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
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
        metrics = self.metrics_frame = QFrame()
        metrics.setObjectName("productionMetrics")
        # Width follows the page; the current grid must not force a wider window
        # before it can wrap in response to fonts, translation or resize.
        metrics.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        metric_row = self.metric_row = QGridLayout(metrics)
        metric_row.setContentsMargins(12, 8, 12, 8)
        metric_row.setSpacing(12)
        self._metric_columns = 0
        self._metric_groups: list[QWidget] = []
        self.metrics: dict[str, tuple[QLabel, QLabel]] = {}
        self.metric_icons: dict[str, QLabel] = {}
        for column, key in enumerate(
            ("cycles", "channel_ok", "channel_ng", "channel_empty", "processing")
        ):
            group = QWidget()
            self._metric_groups.append(group)
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
            metric_row.addWidget(group, 0, column)
            self.metrics[key] = (value, caption)
        summary_layout.addWidget(metrics)
        page.addWidget(summary)
        workspace = QWidget()
        self.grid = QGridLayout(workspace)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Style.space)
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
        self.editing = EditingCoordinator(bridge, recipe_editor, parent=self)
        self.vision_workspace: VisionPage | None = None
        self.vision_page = QWidget()
        self._vision_layout = QVBoxLayout(self.vision_page)
        self._vision_layout.setContentsMargins(0, 0, 0, 0)
        self.recipe_page = RecipePage(
            bridge,
            self.editing,
            label_reader=label_reader,
            configuration_messages=configuration_messages,
            configuration_details=configuration_issue,
        )
        self.editing.applied.connect(self._recipe_applied)
        self.recipe_page.setup_changed.connect(self._render)
        self.editing.changed.connect(self._refresh_vision)
        self.editing.changed.connect(self._render)
        bridge.snapshot_changed.connect(self._refresh_vision)
        bridge.source_connection_changed.connect(self._refresh_vision)
        bridge.model_preparation_changed.connect(self._refresh_vision)
        bridge.frames_changed.connect(self._refresh_vision)
        self.communication = CommunicationPage(reporting=bridge.reporting)
        self.history = HistoryPage(bridge.application.history)
        if self.communication.reporting_panel is not None:
            self.communication.reporting_panel.update_state(self.snapshot.state)
            bridge.reporting_changed.connect(self.communication.reporting_panel.refresh)
        for name in PAGES[1:]:
            if name == "page_recipe":
                self.pages.addWidget(self.recipe_page)
                continue
            if name == "page_vision":
                self.pages.addWidget(self.vision_page)
                continue
            if name == "page_communication":
                self.pages.addWidget(self.communication)
                continue
            if name == "page_history":
                self.pages.addWidget(self.history)
        self.event_frame = StatusBar()
        self.event_icon = self.event_frame.event_icon
        self.event_label = self.event_frame.event_label
        root.addWidget(self.event_frame)
        self.recipe_page.preview_changed.connect(self._render)
        bridge.snapshot_changed.connect(self._snapshot_changed)
        bridge.model_preparation_changed.connect(self._controls_changed)
        bridge.archive_changed.connect(self._controls_changed)
        bridge.control_changed.connect(self._controls_changed)
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
            self.grid.setRowStretch(
                i, 1 if i < (len(self.cards) + columns - 1) // columns else 0
            )
            self.grid.setColumnStretch(i, 1 if i < columns else 0)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._arrange()
        self._arrange_metrics()
        super().resizeEvent(event)

    def _arrange_metrics(self) -> None:
        if not hasattr(self, "_metric_groups"):
            return
        margins = self.metric_row.contentsMargins()
        available = self.metrics_frame.width() - margins.left() - margins.right()
        widths = [group.minimumSizeHint().width() for group in self._metric_groups]
        columns = 1
        for candidate in (5, 3, 2):
            required = sum(max(widths[i::candidate]) for i in range(candidate))
            required += (candidate - 1) * self.metric_row.horizontalSpacing()
            if required <= available:
                columns = candidate
                break
        if columns == self._metric_columns:
            return
        self._metric_columns = columns
        for group in self._metric_groups:
            self.metric_row.removeWidget(group)
        for index in range(5):
            self.metric_row.setColumnStretch(index, int(index < columns))
        for index, group in enumerate(self._metric_groups):
            self.metric_row.addWidget(group, index // columns, index % columns)

    def open_vision(self) -> None:
        session = self.editing.session
        if (
            session is None
            or session.machine_path is None
            or self.editing.closing
            or self.vision_workspace is not None
        ):
            return
        workspace = VisionPage(
            self.editing,
            session.machine_editor.camera_ids[0],
            self.bridge.frames,
            self._vision_closed,
            language=self.language,
            parent=self.vision_page,
            connection=self.bridge.source_connection,
        )
        self.vision_workspace = workspace
        workspace.sources_verified.connect(self._sources_verified)
        workspace.setup_saved.connect(self._setup_saved)
        workspace.feedback_changed.connect(self._render)
        self._vision_layout.addWidget(workspace, 1)

    def _vision_closed(self) -> None:
        if self.vision_workspace is not None:
            workspace, self.vision_workspace = self.vision_workspace, None
            self._vision_layout.removeWidget(workspace)
            workspace.deleteLater()

    def _refresh_vision(self) -> None:
        if self.vision_workspace is not None:
            self.vision_workspace.refresh()

    def leave_vision(self, after: Callable[[], None]) -> None:
        if self.editing.busy:
            self.editing.when_idle(lambda: self.leave_vision(after))
        elif self.vision_workspace is None:
            after()
        else:
            self.vision_workspace.request_back(after)

    def navigate(self, index: int) -> None:
        if self.editing.closing:
            return
        # Keep the actual destination selected while a dirty-draft decision is pending.
        self.navigation[self.pages.currentIndex()].setChecked(True)
        if (
            self.pages.currentWidget() is self.recipe_page
            and index != 1
            and self.recipe_page.preview is not None
        ):
            self.recipe_page.leave_preview(lambda: self.navigate(index))
            return
        if (
            self.pages.currentWidget() is self.vision_page
            and index != 4
            and self.vision_workspace is not None
        ):
            self.leave_vision(lambda: self.navigate(index))
            return
        if index == 4:
            self.open_vision()
        if index == 3:
            self.history.activate()
        self.pages.setCurrentIndex(index)
        self.navigation[index].setFocus(Qt.FocusReason.OtherFocusReason)
        self._refresh_theme_icons()
        for i, button in enumerate(self.navigation):
            button.setChecked(i == index)
        self._render()

    @Slot(str)
    def _select_language(self, language: str) -> None:
        if is_language(language):
            self.set_language(language)

    @Slot()
    def _switch_theme(self) -> None:
        self.set_theme("light" if self.theme == "dark" else "dark")

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        apply_theme(self, theme)
        self._refresh_theme_icons()
        self.communication.set_language(self.language)
        self.history.set_language(self.language)
        self._render()

    def _refresh_theme_icons(self) -> None:
        self.navbar.present(self.language, self.theme, self.pages.currentIndex())
        self.recipe_page.refresh_theme()
        workspace = self.vision_workspace
        if workspace is not None:
            workspace.refresh_theme()
        preview = self.recipe_page.preview
        if preview is not None:
            preview.refresh_theme()
        colors = theme_colors(self)
        for key, icon in self.metric_icons.items():
            state = {
                "channel_ok": Indicator.OK,
                "channel_ng": Indicator.NG,
                "channel_empty": Indicator.EMPTY,
            }.get(key)
            icon.setPixmap(
                (
                    indicator_icon(
                        state,
                        colors.ok
                        if state is Indicator.OK
                        else colors.ng
                        if state is Indicator.NG
                        else colors.secondary,
                    )
                    if state is not None
                    else production_icon(
                        "clock" if key == "processing" else "cycles", colors.secondary
                    )
                ).pixmap(28, 28)
            )

        self.communication.refresh_icons()

    def set_language(self, language: Language) -> None:
        self.language = language
        self.recipe_page.set_language(language)
        if self.vision_workspace is not None:
            self.vision_workspace.set_language(language)
        self.communication.set_language(language)
        self.history.set_language(language)
        self._refresh_theme_icons()
        self.buttons["batch"].setToolTip(text("batch", language))
        self.buttons["batch"].setAccessibleName(text("batch", language))
        for key, (_, caption) in self.metrics.items():
            full = text(
                {
                    "channel_ok": "production_ok",
                    "channel_ng": "production_ng",
                    "channel_empty": "production_empty",
                }.get(key, key),
                language,
            )
            caption.setText(
                text("short_cycles", language)
                if key == "cycles"
                else {
                    "channel_ok": "OK",
                    "channel_ng": "NG",
                    "channel_empty": "EMPTY",
                    "processing": "Δt",
                }[key]
            )
            caption.setToolTip(full)
            caption.setAccessibleName(full)
        for key in ("channel_ok", "channel_ng", "channel_empty"):
            self.metrics[key][0].setToolTip(text("channel_count_help", language))
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
            self.bridge.pause_source()
        elif action == "replay_continue":
            self.bridge.resume_source()
        elif action == "replay_step":
            self.bridge.step_source()

    def _actions(self) -> dict[str, str | None]:
        replay = self.bridge.control_observation
        return production_actions(
            self.snapshot,
            (replay.state if replay else SourceControlState.STOPPED)
            if self.bridge.controls_enabled
            else None,
        )

    def _current_actions(self) -> dict[str, str | None]:
        replay = self.bridge.control_snapshot()
        return production_actions(
            self.bridge.application.snapshot(),
            (replay.state if replay else SourceControlState.STOPPED)
            if self.bridge.controls_enabled
            else None,
        )

    def _render_operations(self) -> None:
        actions = self._actions()
        replay = self.bridge.control_observation
        middle_step = bool(
            self.bridge.controls_enabled
            and replay
            and replay.state in (SourceControlState.PAUSED, SourceControlState.STEPPING)
        )
        draft_locked = bool(
            self.editing.busy
            or self.bridge.application.preparation_snapshot().owns_resources
            or self.editing.session
            and (
                self.editing.session.machine_editor.dirty
                or self.editing.session.camera_sources
                and self.editing.session.camera_sources.dirty
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
        widths = {
            key: button_width(self.buttons[key], captions, padding=2)
            for key, captions in (
                ("operation", ("replay_start", "replay_continue")),
                ("pause", ("replay_pause", "replay_step")),
                ("stop", ("production_stop",)),
            )
        }
        for key, width in widths.items():
            button = self.buttons[key]
            set_button_caption(button, button.accessibleName(), width)
        self.operation_group.setFixedWidth(
            Style.touch + sum(widths.values()) + 3 * Style.small
        )
        self.context_control.setCurrentIndex(1 if actions["secondary"] else 0)

    @Slot(object)
    def _controls_changed(self, _snapshot: object) -> None:
        self._render()

    @Slot(object)
    def _recipe_applied(self, prepared: object) -> None:
        if not isinstance(prepared, PreparedConfiguration):
            return
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
        self.recipe_page.configuration_messages = self.configuration_messages
        self.recipe_page.configuration_details = self.configuration_issue
        self.recipe_page.refresh_state()

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

    @Slot(bool, bool)
    def _setup_saved(self, machine: bool, sources: bool) -> None:
        cleared: set[str] = {"startup_machine"} if machine else set()
        if sources:
            cleared.add("startup_sources")
        session = self.editing.session
        if (
            session is not None
            and session.machine_editor.persisted
            and session.camera_sources is not None
            and session.camera_sources.persisted
        ):
            cleared.add("camera_setup")
        self._retain_configuration_feedback(
            tuple(k for k in self.configuration_messages if k not in cleared)
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
        if command in ("start", "source_step") and isinstance(error, BaseException):
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
            self.editing.resume()
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
        prepared = (
            self.snapshot.active_prepared
            if self.snapshot.state is ApplicationState.RUNNING
            else self.snapshot.candidate_prepared
        )
        if prepared is not None:
            layouts = prepared.configuration.layout.cameras
            if tuple(card.camera_layout.camera_id for card in self.cards) != tuple(
                camera.camera_id for camera in layouts
            ):
                # The display store invalidates old cameras. Preserve its latest
                # pixels, already delivered in this bridge refresh batch.
                for card in self.cards:
                    self.grid.removeWidget(card)
                    card.hide()
                    card.deleteLater()
                self.cards = tuple(CameraCard(camera) for camera in layouts)
                self._columns = 0
                self._arrange()
            for card, camera in zip(self.cards, layouts, strict=True):
                card.camera_layout = camera
        for card in self.cards:
            card.present(
                self._frames.get(card.camera_layout.camera_id),
                self.snapshot,
                self.language,
            )

    def _render(self) -> None:
        colors = theme_colors(self)
        self.navbar.present(self.language, self.theme, self.pages.currentIndex())
        snapshot, language = self.snapshot, self.language
        session = self.editing.session
        sources = session.camera_sources if session else None
        folder_controls = self.bridge.controls_enabled and (
            sources is None
            or (session is not None and session.cli_sources)
            or sources.saved.mode == "folder"
        )
        self.configuration_placeholder.setText(text("configure_first", language))
        availability = controls(snapshot)
        self._render_operations()
        self.buttons["batch"].setEnabled(availability.batch and not self._closing)
        state = "closing" if self._closing else snapshot.state.name
        if (
            not self._closing
            and snapshot.state is ApplicationState.RUNNING
            and self.bridge.control_observation is not None
        ):
            state = {
                SourceControlState.PAUSED: "replay_paused"
                if folder_controls
                else "source_paused",
                SourceControlState.PAUSING: "source_pausing",
                SourceControlState.STEPPING: "source_stepping",
                SourceControlState.RESUMING: "source_resuming",
            }.get(self.bridge.control_observation.state, state)
        self.state_label.setText(
            text(
                f"state_{state}"
                if state
                in (
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
        self.state_label.setAccessibleName(text(state, language))
        self._render_identity()
        counts = snapshot.counters
        for key, count in (
            ("cycles", counts.cycles_completed),
            ("channel_ok", counts.business_ok),
            ("channel_ng", counts.business_ng),
            ("channel_empty", counts.business_empty),
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
        self.metrics["processing"][0].setAccessibleName(
            f"{self.metrics['processing'][1].text()}: {duration}"
        )
        self._arrange_metrics()
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
        self._event_message = message
        archive = self.bridge.archive_snapshot
        if (
            not message
            and not self._closing
            and archive is not None
            and archive.last_issue
        ):
            message, warning = text("archive_issue", language), True
            self._event_message = message
        detail = (
            self.configuration_issue
            if self.configuration_messages and not (warning or fault or self._closing)
            else ""
        )
        if (
            archive is not None
            and archive.last_issue
            and message == text("archive_issue", language)
        ):
            detail = archive.last_issue
        if event_key == "command_error" and command == "preferences":
            detail = "\n".join(
                part
                for part in (self._command_detail, self.configuration_issue)
                if part
            )
        workspace = self.vision_workspace
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
        self.state_label.setToolTip("")
        self.event_label.setStyleSheet(
            f"color: {colors.fault if fault else colors.warning if warning else colors.secondary};"
        )
        self.event_icon.setVisible(bool(message))
        self.event_icon.setToolTip("")
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

    def bind_web_runtime(self, runtime: WebRuntime) -> None:
        self.communication.bind_web_runtime(runtime)
        self._render_services()

    def _render_services(self) -> None:
        bar, colors = self.event_frame, theme_colors(self)
        bar.refresh_icons(self.language)
        for widget in (
            bar.state,
            bar.state_icon,
            bar.plc,
            bar.plc_icon,
            bar.web,
            bar.web_icon,
        ):
            if widget.toolTip():
                widget.setToolTip("")
        selection = (
            self.bridge.reporting.reporting_selection()
            if self.bridge.reporting
            else None
        )
        report = self.bridge.reporting_snapshot
        plc_key = (
            "service_next_start" if selection and selection.enabled else "service_off"
        )
        if report:
            outcome = report.last_outcome
            if (
                report.failure_issue
                or report.cleanup_issue
                or (
                    outcome
                    and outcome.outcome
                    in (ReportOutcome.FAILED, ReportOutcome.REJECTED)
                )
            ):
                plc_key = "technical_error"
            elif report.thread_alive:
                plc_key = "web_running"
        plc = text(plc_key, self.language)
        runtime = self.communication.web_panel.runtime
        observed = runtime.snapshot() if runtime else None
        self.communication.web_panel.refresh()
        web_key = "service_off"
        if observed is not None:
            web_key = {
                WebState.STOPPED: "service_off",
                WebState.STARTING: "web_starting",
                WebState.RUNNING: "web_running",
                WebState.STOPPING: "STOPPING",
                WebState.FAILED: "technical_error",
            }[observed.state]
        web = text(web_key, self.language)
        web_color = (
            colors.warning
            if observed is not None
            and observed.state is not WebState.RUNNING
            and observed.error
            else colors.secondary
        )
        state_text = self.state_label.text()
        state_color = (
            colors.fault
            if self.snapshot.state is ApplicationState.FAULT
            else colors.ok
            if self.snapshot.state in (ApplicationState.READY, ApplicationState.RUNNING)
            else colors.secondary
        )
        state_icon = shell_icon("state", state_color)
        session = self.editing.session
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
        identity = (
            self.theme,
            self.language,
            state_text,
            state_color,
            plc,
            web,
            web_color,
        )
        if identity == self._service_presentation:
            return
        self._service_presentation = identity
        bar.state.setText(state_text)
        bar.state_icon.setAccessibleName(bar.state.accessibleName())
        bar.state_icon.setPixmap(state_icon.pixmap(20, 20))
        bar.state.setStyleSheet(f"color: {state_color};")
        bar.plc.setText(f"PLC · {plc}")
        bar.web.setText(f"Web · {web}")
        bar.web.setStyleSheet(f"color: {web_color};")

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
        active, candidate = snapshot.active_prepared, snapshot.candidate_prepared
        active_name = active.configuration.display_name if active else "--"
        candidate_name = candidate.configuration.display_name if candidate else "--"
        display_name = active_name if active else candidate_name
        recipe_label = f"{text('page_recipe', language)}: {display_name}"
        label = recipe_label
        pending = snapshot.has_pending_configuration
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
        # Balance the outer columns when possible. On narrow windows the empty
        # space after Batch can serve the active name without moving controls.
        self.state_label.ensurePolished()
        state_width = max(
            self.state_label.fontMetrics().horizontalAdvance(text(key, lang))
            for key in (
                "UNCONFIGURED",
                "CLOSED",
                "READY",
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
            for lang in ("zh", "en", self.language)
        )
        self.state_label.setMinimumWidth(state_width)
        side_width = self.operation_group.width()
        self.summary_header.setColumnMinimumWidth(2, side_width)
        batch_fixed = (
            self.buttons["batch"].width()
            + self.batch_icon.width()
            + batch_padding
            + 2 * Style.small
        )
        main_width = max(
            self.identity_label.minimumWidth(),
            ceil(
                QFontMetricsF(self.identity_label.font()).horizontalAdvance(
                    display_name
                )
            )
            + 2,
        )
        self.identity_label.setMaximumWidth(main_width)
        self.recipe_identity.setMaximumWidth(
            self.recipe_icon.width() + Style.small + main_width
        )
        # Balance the outer columns when space permits, with a preferred spacer
        # that can shrink instead of retaining a wide window's minimum width.
        margins = self.summary_layout.contentsMargins()
        header_width = (
            self.summary.contentsRect().width()
            - margins.left()
            - margins.right()
            - 2 * self.summary_header.horizontalSpacing()
        )
        identity_width = max(
            state_width,
            self.recipe_identity.maximumWidth(),
            self.pending_label.fontMetrics().horizontalAdvance(pending_text)
            if pending
            else 0,
        )
        # Borrow unused Batch reservation before shortening the active/pending name.
        # Arbitrary Batch IDs retain their full identity in the existing tooltip.
        batch_width = max(
            1, min(side_width, header_width - side_width - identity_width) - batch_fixed
        )
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
                    for lang in ("zh", "en", self.language)
                ),
            )
        )
        self.batch_group.setFixedWidth(self.batch_label.width() + batch_fixed)
        self._batch_balance.changeSize(
            min(side_width, max(0, header_width - side_width - identity_width)),
            0,
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Minimum,
        )
        self.summary_header.invalidate()
        self.pending_label.setMaximumWidth(
            max(
                self.pending_label.minimumWidth(),
                self.pending_label.fontMetrics().horizontalAdvance(pending_text),
            )
        )
        # Let the center column shrink without retaining widths from a larger window.
        self.identity_label.setText(display_name)
        self.pending_label.setText(pending_text)
        self.recipe_identity.updateGeometry()
        self.identity_group.updateGeometry()
        self.summary_header.activate()
        for group in (self.batch_group, self.identity_group, self.recipe_identity):
            layout = group.layout()
            if layout is not None:
                layout.activate()
        # Respect the actual player/identity minimum instead of clipping controls
        # when a restored window or larger font requests too little width.
        # Refresh the stacked widget's cached layout-item hint after translated
        # child sizes change, before promoting that hint to a window constraint.
        self.pages.updateGeometry()
        shell = self.centralWidget()
        if shell is not None:
            shell.updateGeometry()
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
        self.recipe_icon.setAccessibleName(text("page_recipe", language))
        self.batch_label.setText(
            self.batch_label.fontMetrics().elidedText(
                batch, Qt.TextElideMode.ElideRight, self.batch_label.width()
            )
        )
        self.batch_label.setToolTip(batch)
        self.batch_label.setAccessibleName(batch)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.metrics_frame and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            self._arrange_metrics()
        if event.type() is QEvent.Type.Resize and watched in (
            self.summary,
            self.identity_label,
            self.pending_label,
            self.batch_label,
        ):
            self._render_identity()
        if event.type() is QEvent.Type.Resize and watched is self.event_label:
            self._render_status_text()
        if event.type() is QEvent.Type.Quit and not (
            self.bridge.closed and self.communication.closed
        ):
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
            self.history.dispose()
            self.recipe_page.dispose()
            self.editing.dispose()
            event.accept()
            self.closed.emit()
        else:
            event.ignore()
            if not self._closing:
                self.request_editing_close(self._begin_close)

    def finish_close(self) -> None:
        """Drain the normal Close path after the outer event loop has exited."""
        if self.bridge.closed and self.communication.closed:
            return
        loop = QEventLoop()
        self.closed.connect(loop.quit)
        try:
            self.close()
            if not (self.bridge.closed and self.communication.closed):
                loop.exec()
        finally:
            self.closed.disconnect(loop.quit)
        if not (self.bridge.closed and self.communication.closed):
            raise RuntimeError("Window resource owners did not finish closing")

    def request_editing_close(self, final: Callable[[], None]) -> None:
        self.editing.drain(
            lambda: self.recipe_page.settle(lambda: self._guard_editing_close(final))
        )

    def _guard_editing_close(self, final: Callable[[], None]) -> None:
        # Jobs are actually joined before Save/Discard dialogs admit new work.
        self.editing.resume(notify=False)
        session = self.editing.session
        if (
            self.vision_workspace is None
            and session is not None
            and (
                session.machine_editor.dirty
                or bool(session.camera_sources and session.camera_sources.dirty)
            )
        ):
            self.open_vision()
        self.leave_vision(
            lambda: self.recipe_page.guard_dirty(
                lambda: self.editing.drain(lambda: self.recipe_page.settle(final))
            )
        )

    def _begin_close(self) -> None:
        self._closing = True
        self.editing.closing = True
        self._render()
        self.communication.request_close(self._shutdown_finished)
        self.bridge.close()
