from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import ClassVar

from PySide6.QtCore import QEvent, QModelIndex, QSettings, QSize, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from paper_reviewer.application.app_state import (
    AppPaths,
    GuiPreferences,
    PreferencesStore,
    is_active_run_status,
    is_human_review_status,
    is_terminal_run_status,
    status_value,
)
from paper_reviewer.application.models import (
    ReportExportFormat,
    ReportView,
    ReviewRequest,
    RunDetail,
    RunEvent,
)
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.domain.review import HumanPanelDecision, HumanRuleDecision
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import (
    NavigationItem,
    NavigationModel,
    provider_label,
)
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.new_review import NewReviewPage
from paper_reviewer.gui.pages.rubrics import RubricsPage
from paper_reviewer.gui.pages.run_detail import RunDetailPage
from paper_reviewer.gui.pages.runs import RunsPage
from paper_reviewer.gui.pages.settings import SettingsPage
from paper_reviewer.gui.theme import FluentThemeManager, ThemeMode
from paper_reviewer.gui.worker import AsyncOperation, AsyncTaskThread, EventEmitter


class MainWindow(QMainWindow):
    NAVIGATION: ClassVar[list[tuple[str, str, str, str]]] = [
        ("new_review", "新建评测", "add_document", "创建新的论文评测任务"),
        ("runs", "任务记录", "history", "查看任务、报告和恢复状态"),
        ("rubrics", "Rubric 管理", "rubric", "校验和预览 Rubric"),
        ("settings", "设置", "settings", "管理凭据、默认参数和外观"),
    ]
    STATUS_TEXT: ClassVar[dict[str, str]] = {
        "created": "已创建评测",
        "ingesting": "正在解析论文",
        "ingested": "论文解析完成",
        "building_evidence": "正在收集外部学术证据",
        "evidence_ready": "外部证据已准备",
        "scoring": "正在进行诊断评分",
        "reviewing": "正在进行多 Reviewer 评测",
        "auditing": "正在执行确定性审计",
        "awaiting_hard_rule_confirmation": "等待人工复核",
        "panel_reviewing": "正在进行专家面板评议",
        "supplemental_reviewing": "正在进行补充专家评议",
        "awaiting_panel_review": "等待人工面板复核",
        "synthesizing": "正在汇总评测结果",
        "meta_reviewing": "正在生成 Meta 评语",
        "validating": "正在验证并生成报告",
        "reported_pending_human_review": "评测完成 · 待人工复核",
        "reported": "评测已完成",
        "retryable_failure": "评测失败，可恢复",
        "fatal_failure": "评测失败，需要处理",
        "cancelled": "评测已取消",
    }

    def __init__(
        self,
        *,
        service: ReviewApplicationService,
        paths: AppPaths,
        preferences: GuiPreferences,
        preferences_store: PreferencesStore,
        theme: FluentThemeManager,
    ) -> None:
        super().__init__()
        self.service = service
        self.paths = paths
        self.preferences = preferences
        self.preferences_store = preferences_store
        self.theme = theme
        self.icons = FluentIconService(theme)
        self._operation_registry = AsyncOperationRegistry()
        # Keep the historical attribute as a live alias.  Apart from being
        # useful to integrations, this preserves the shutdown behavior while
        # the registry centralizes tracking and cleanup for every operation.
        self._workers = self._operation_registry.workers
        self._review_worker: AsyncTaskThread | None = None
        self._active_run_id = ""
        self._active_run_status = ""
        self._restored_active_run_id = ""
        self._active_run_events: list[RunEvent] = []
        self._detail_request_generation = 0
        self._report_export_inflight: set[tuple[str, int]] = set()
        self._manual_sidebar_visible = preferences.sidebar_expanded
        self._settings = QSettings("PaperReviewer", "PaperReviewer")

        self.setWindowTitle("Paper Reviewer · 论文评测")
        self.setMinimumSize(900, 600)
        self.resize(1180, 760)
        self._build_menu()
        self._build_shell()
        self._restore_window_state()
        self.theme.theme_changed.connect(self._theme_changed)
        self.navigate(preferences.current_navigation)
        self.refresh_runs()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        new_action = QAction("新建评测(&N)", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(lambda: self.navigate("new_review"))
        runs_action = QAction("打开报告目录", self)
        runs_action.triggered.connect(lambda: self._open_directory(self.paths.runs_dir))
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(new_action)
        file_menu.addAction(runs_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        view_menu = self.menuBar().addMenu("视图(&V)")
        self.sidebar_action = QAction("显示侧栏", self)
        self.sidebar_action.setCheckable(True)
        self.sidebar_action.setChecked(self.preferences.sidebar_expanded)
        self.sidebar_action.setShortcut("Ctrl+B")
        self.sidebar_action.toggled.connect(self._set_sidebar_visible)
        view_menu.addAction(self.sidebar_action)
        theme_menu = view_menu.addMenu("主题")
        for label, mode in (
            ("跟随系统", ThemeMode.SYSTEM),
            ("浅色", ThemeMode.LIGHT),
            ("深色", ThemeMode.DARK),
            ("高对比度", ThemeMode.HIGH_CONTRAST),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _checked=False, value=mode: self._set_theme(value))
            theme_menu.addAction(action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        logs_action = QAction("打开日志目录", self)
        logs_action.triggered.connect(lambda: self._open_directory(self.paths.logs_dir))
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(logs_action)
        help_menu.addAction(about_action)

    def _build_shell(self) -> None:
        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setProperty("fluentRole", "sidebar")
        self.sidebar.setMinimumWidth(216)
        self.sidebar.setMaximumWidth(216)
        sidebar_layout = QHBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 8, 0, 8)
        self.navigation = QListView()
        self.navigation.setObjectName("primaryNavigation")
        self.navigation.setIconSize(QSize(20, 20))
        self.navigation.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.navigation.setAccessibleName("主导航")
        self.navigation_model = self._navigation_model()
        self.navigation.setModel(self.navigation_model)
        self.navigation.clicked.connect(self._navigation_clicked)
        self.navigation.activated.connect(self._navigation_clicked)
        sidebar_layout.addWidget(self.navigation)
        shell.addWidget(self.sidebar)

        self.pages = QStackedWidget()
        self.new_review_page = NewReviewPage(self.service, self.preferences, self.icons)
        self.runs_page = RunsPage(self.icons)
        self.rubrics_page = RubricsPage(self.service, self.preferences, self.icons)
        self.settings_page = SettingsPage(
            self.service,
            self.preferences,
            self.paths,
            self.icons,
            operation_registry=self._operation_registry,
        )
        self.run_detail_page = RunDetailPage(self.icons)
        self.page_by_id = {
            "new_review": self.new_review_page,
            "runs": self.runs_page,
            "rubrics": self.rubrics_page,
            "settings": self.settings_page,
        }
        for page in self.page_by_id.values():
            self.pages.addWidget(page)
        self.pages.addWidget(self.run_detail_page)
        shell.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self.new_review_page.start_requested.connect(self.start_review)
        self.new_review_page.settings_requested.connect(lambda: self.navigate("settings"))
        self.runs_page.refresh_requested.connect(self.refresh_runs)
        self.runs_page.run_open_requested.connect(self.open_run)
        self.rubrics_page.preferences_changed.connect(self._rubric_preferences_changed)
        self.settings_page.preferences_changed.connect(self._settings_preferences_changed)
        self.settings_page.theme_changed.connect(self._theme_from_settings)
        self.settings_page.credentials_changed.connect(self._credentials_changed)
        self.run_detail_page.back_requested.connect(lambda: self.navigate("runs"))
        self.run_detail_page.cancel_requested.connect(self.cancel_review)
        self.run_detail_page.resume_requested.connect(self.resume_review)
        self.run_detail_page.report_export_requested.connect(self._export_report_requested)
        self.run_detail_page.hard_rule_resolution_requested.connect(
            self.resolve_hard_rule_and_resume
        )
        self.run_detail_page.panel_review_resolution_requested.connect(
            self.resolve_panel_review_and_refresh
        )

        status = QStatusBar()
        self.global_status = QLabel("就绪")
        self.context_status = QLabel("未选择模型或 Rubric")
        self.global_status.setAccessibleName("全局状态：就绪")
        status.addWidget(self.global_status, 1)
        status.addPermanentWidget(self.context_status)
        self.setStatusBar(status)

    def _navigation_model(self) -> NavigationModel:
        return NavigationModel(
            [
                NavigationItem(
                    item_id=item_id,
                    text=text,
                    icon=self.icons.icon(icon),
                    tooltip=tooltip,
                )
                for item_id, text, icon, tooltip in self.NAVIGATION
            ]
        )

    def navigate(self, page_id: str) -> None:
        self.run_detail_page.reset_report_export_state()
        self._detail_request_generation += 1
        page = self.page_by_id.get(page_id)
        if page is None:
            page_id = "new_review"
            page = self.page_by_id[page_id]
        self.pages.setCurrentWidget(page)
        self.preferences.current_navigation = page_id
        for row in range(self.navigation_model.rowCount()):
            index = self.navigation_model.index(row, 0)
            if self.navigation_model.item_id(index) == page_id:
                self.navigation.setCurrentIndex(index)
                break
        if page_id == "runs":
            self.refresh_runs()
        elif page_id == "settings":
            self.settings_page.apply_preferences()
        self._save_preferences()

    def start_review(self, request: object) -> None:
        if not isinstance(request, ReviewRequest):
            return
        if self._review_worker is not None and self._review_worker.isRunning():
            return

        async def operation(emit: EventEmitter) -> RunRecord:
            return await self.service.start_review(request, event_sink=emit)

        self._active_run_id = ""
        self._active_run_status = "created"
        self._active_run_events.clear()
        self.preferences.active_run_id = None
        self._detail_request_generation += 1
        self._save_preferences()
        self.run_detail_page.prepare_run()
        self.pages.setCurrentWidget(self.run_detail_page)
        self._start_review_worker(operation)
        self.global_status.setText("正在评测")
        selected = self.new_review_page.selected_provider_display(request.provider)
        self.context_status.setText(provider_label(request.provider, request.model, selected))

    def resume_review(self, run_id: str) -> None:
        if self._review_worker is not None and self._review_worker.isRunning():
            return

        async def operation(emit: EventEmitter) -> RunRecord:
            return await self.service.resume_review(run_id, event_sink=emit)

        self._active_run_id = run_id
        self._active_run_status = ""
        self._restored_active_run_id = run_id
        self.preferences.active_run_id = run_id
        self._save_preferences()
        self._active_run_events.clear()
        self._detail_request_generation += 1
        self.run_detail_page.prepare_run(run_id, run_dir=self.paths.runs_dir / run_id)
        self.pages.setCurrentWidget(self.run_detail_page)
        self._start_review_worker(operation)
        self.global_status.setText("正在恢复评测")

    def resolve_hard_rule_and_resume(self, run_id: str, value: object) -> None:
        """Persist one post-report decision and refresh deterministic artifacts."""

        if self._review_worker is not None and self._review_worker.isRunning():
            return
        try:
            decision = HumanRuleDecision.model_validate(value)
        except ValueError as error:
            self.run_detail_page.message.show_message(str(error), severity="danger")
            return

        async def operation(_emit: EventEmitter) -> RunRecord:
            await self.service.resolve_hard_rule(run_id, decision)
            return (await self.service.get_run(run_id)).run

        self._active_run_id = run_id
        self._active_run_status = "reported_pending_human_review"
        self.preferences.active_run_id = run_id
        self._save_preferences()
        self._start_review_worker(operation)
        self.global_status.setText("正在保存人工复核决定并更新报告")

    def resolve_panel_review_and_refresh(self, run_id: str, value: object) -> None:
        if self._review_worker is not None and self._review_worker.isRunning():
            return
        try:
            decision = HumanPanelDecision.model_validate(value)
        except ValueError as error:
            self.run_detail_page.message.show_message(str(error), severity="danger")
            return

        async def operation(_emit: EventEmitter) -> RunRecord:
            await self.service.resolve_panel_review(run_id, decision)
            return (await self.service.get_run(run_id)).run

        self._active_run_id = run_id
        self._active_run_status = "reported_pending_human_review"
        self.preferences.active_run_id = run_id
        self._save_preferences()
        self._start_review_worker(operation)
        self.global_status.setText("正在保存人工面板结论并更新报告")

    def _start_review_worker(self, operation: AsyncOperation) -> None:
        worker = AsyncTaskThread(operation)
        self._review_worker = worker
        self._track_worker(worker)
        worker.event_received.connect(self._run_event)
        worker.completed.connect(self._review_completed)
        worker.failed.connect(self._review_failed)
        worker.task_cancelled.connect(self._review_cancelled)
        worker.start()

    def cancel_review(self, run_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "取消评测",
            "确定取消当前评测吗？已完成的阶段会保留，之后可以从检查点恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_detail_page.set_cancel_pending(run_id, True)
        self.global_status.setText("正在安全取消评测")
        if (
            self._review_worker is not None
            and self._review_worker.isRunning()
            and (not self._active_run_id or self._active_run_id == run_id)
        ):
            self._review_worker.cancel_task()
        else:
            self._persist_cancelled_run(run_id)

    def _persist_cancelled_run(self, run_id: str) -> None:
        """Persist cancellation after the live worker has safely stopped."""

        async def operation(_emit: EventEmitter) -> RunRecord:
            return await self.service.cancel_review(run_id)

        self._run_async(
            operation,
            self._cancel_review_completed,
            lambda message, trace: self._cancel_review_failed(run_id, message, trace),
        )

    def refresh_runs(self) -> None:
        self.runs_page.set_loading(True)

        async def operation(_emit: EventEmitter) -> object:
            return await self.service.list_runs()

        self._run_async(
            operation,
            self._runs_loaded,
            lambda message, _trace: self._runs_failed(message),
        )

    def open_run(self, run_id: str) -> None:
        self.run_detail_page.reset_report_export_state()
        self._detail_request_generation += 1
        request_generation = self._detail_request_generation

        async def operation(_emit: EventEmitter) -> object:
            detail = await self.service.get_run(run_id)
            if detail.run.status in {
                RunStatus.REPORTED,
                RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
            }:
                return await self.service.load_report(run_id)
            return detail

        self.global_status.setText("正在加载任务")
        self._run_async(
            operation,
            lambda value: self._run_loaded(value, request_generation, run_id),
            lambda message, trace: self._run_load_failed(message, trace, request_generation),
        )

    @staticmethod
    def _report_export_format(value: str) -> ReportExportFormat:
        return ReportExportFormat(value)

    def _export_report_requested(
        self,
        run_id: str,
        export_format: str,
        destination: str,
        overwrite: bool,
    ) -> None:
        if run_id != self.run_detail_page.run_id:
            return
        request_generation = self._detail_request_generation
        page_generation = self.run_detail_page.report_export_generation()
        request_key = (run_id, page_generation)
        if request_key in self._report_export_inflight:
            return
        try:
            export_value = self._report_export_format(export_format)
        except ValueError:
            self.run_detail_page.report_export_failed(
                run_id,
                f"不支持的报告导出格式：{export_format}",
                page_generation,
            )
            return
        self._report_export_inflight.add(request_key)

        async def operation(_emit: EventEmitter) -> object:
            method = getattr(self.service, "export_report", None)
            if not callable(method):
                raise RuntimeError("服务接口暂不可用：export_report")
            return await method(
                run_id,
                export_value,
                Path(destination),
                overwrite=overwrite,
            )

        self._run_async(
            operation,
            lambda value: self._report_export_completed(
                value, run_id, request_generation, page_generation, request_key
            ),
            lambda message, _trace: self._report_export_failed(
                message, run_id, request_generation, page_generation, request_key
            ),
        )

    def _report_export_completed(
        self,
        value: object,
        run_id: str,
        request_generation: int,
        page_generation: int,
        request_key: tuple[str, int],
    ) -> None:
        self._report_export_inflight.discard(request_key)
        if request_generation != self._detail_request_generation:
            return
        if self.run_detail_page.report_export_succeeded(run_id, value, page_generation):
            self._set_global_status(fallback="报告已导出")

    def _report_export_failed(
        self,
        message: str,
        run_id: str,
        request_generation: int,
        page_generation: int,
        request_key: tuple[str, int],
    ) -> None:
        self._report_export_inflight.discard(request_key)
        if request_generation != self._detail_request_generation:
            return
        self.run_detail_page.report_export_failed(run_id, message, page_generation)

    def _run_event(self, value: object) -> None:
        if not isinstance(value, RunEvent):
            return
        if self._active_run_id and value.run_id != self._active_run_id:
            return
        if not self._active_run_id:
            self._active_run_id = value.run_id
        event_status = self._event_status_value(value)
        if event_status:
            self._remember_active_run(value.run_id, event_status)
        self._active_run_events.append(value)
        run_dir = self.paths.runs_dir / value.run_id
        if not self.run_detail_page.run_id:
            self.run_detail_page.prepare_run(value.run_id, run_dir=run_dir)
            for event in self._active_run_events:
                self.run_detail_page.append_event(event)
        elif self.run_detail_page.run_id == value.run_id:
            self.run_detail_page.run_dir = run_dir
            self.run_detail_page.append_event(value)
        self._set_global_status(event_status, fallback=value.message)

    def _review_completed(self, value: object) -> None:
        if not isinstance(value, RunRecord):
            return
        self._active_run_id = value.run_id
        self._restored_active_run_id = value.run_id
        run_status = status_value(value.status)
        self._remember_active_run(value.run_id, run_status)
        self.new_review_page.set_busy(False)
        if is_human_review_status(value.status):
            self._set_global_status(run_status)
        elif is_terminal_run_status(value.status):
            self._set_global_status(run_status)
        else:
            # A completed worker is not itself proof that a report exists. In
            # particular, the service intentionally returns after the human
            # gate so the GUI can resume it later.
            self._set_global_status(run_status, fallback="评测已暂停，可恢复")
        self.open_run(value.run_id)
        self.refresh_runs()

    def _review_failed(self, message: str, _trace: str) -> None:
        if self._active_run_status == "reported_pending_human_review":
            self.run_detail_page.show_human_review_error(self._active_run_id, message)
            self._set_global_status(
                "reported_pending_human_review",
                fallback="人工复核保存失败，需要处理",
            )
            return
        self.new_review_page.show_run_error(message)
        self._set_global_status("retryable_failure", fallback="评测失败，需要处理")
        if self._active_run_id:
            self.open_run(self._active_run_id)
        self.refresh_runs()

    def _review_cancelled(self) -> None:
        self.new_review_page.set_busy(False)
        run_id = self._active_run_id or self.run_detail_page.run_id
        if run_id:
            # The orchestrator normally persists cancellation while unwinding.
            # Calling the idempotent service here also covers cancellation during
            # service startup, before execution entered the orchestrator.
            self._persist_cancelled_run(run_id)
            return
        self._set_global_status("cancelled")

    def _cancel_review_completed(self, value: object) -> None:
        if not isinstance(value, RunRecord):
            self._cancel_review_failed(
                self.run_detail_page.run_id,
                "取消操作没有返回有效任务状态",
                "",
            )
            return
        self._active_run_id = value.run_id
        self._remember_active_run(value.run_id, value.status)
        self.run_detail_page.set_cancel_pending(value.run_id, False)
        self._set_global_status(value.status, fallback="评测已取消")
        self.open_run(value.run_id)
        self.refresh_runs()

    def _cancel_review_failed(self, run_id: str, message: str, _trace: str) -> None:
        self.run_detail_page.set_cancel_pending(run_id, False)
        self.run_detail_page.show_cancel_error(run_id, message)
        self._set_global_status("", fallback="取消评测失败，需要处理")

    def _runs_loaded(self, value: object) -> None:
        if isinstance(value, list):
            self.runs_page.set_items(value)
            self._restore_active_run(value)
        self.runs_page.set_loading(False)

    def _runs_failed(self, message: str) -> None:
        self.runs_page.set_loading(False)
        self._show_blocking_error(message)

    def _run_loaded(
        self,
        value: object,
        request_generation: int,
        expected_run_id: str,
    ) -> None:
        if request_generation != self._detail_request_generation:
            return
        if isinstance(value, ReportView):
            if value.run.run_id != expected_run_id:
                return
            self.run_detail_page.show_report(value, run_dir=self.paths.runs_dir / value.run.run_id)
            source = (
                getattr(value, "provider_snapshot", None)
                or getattr(value.run, "provider_snapshot", None)
                or value
            )
            self.context_status.setText(
                f"{provider_label(value.run.provider, value.run.model, source)} · "
                f"{value.run.rubric_id}"
            )
            self._remember_active_run(value.run.run_id, value.run.status)
            self._set_global_status(value.run.status, fallback="报告已加载")
        elif isinstance(value, RunDetail):
            if value.run.run_id != expected_run_id:
                return
            self.run_detail_page.show_detail(value, run_dir=self.paths.runs_dir / value.run.run_id)
            source = (
                getattr(value, "provider_snapshot", None)
                or getattr(value.run, "provider_snapshot", None)
                or value
            )
            self.context_status.setText(
                f"{provider_label(value.run.provider, value.run.model, source)} · "
                f"{value.run.rubric_id}"
            )
            run_status = status_value(value.run.status)
            self._remember_active_run(value.run.run_id, run_status)
            self._set_global_status(run_status, fallback="任务详情已加载")
        self.pages.setCurrentWidget(self.run_detail_page)

    def _run_load_failed(
        self,
        message: str,
        _trace: str,
        request_generation: int,
    ) -> None:
        if request_generation == self._detail_request_generation:
            self._show_blocking_error(message)

    def _run_async(
        self,
        operation: AsyncOperation,
        on_success: Callable[[object], None],
        on_failure: Callable[[str, str], None] | None = None,
    ) -> None:
        worker = AsyncTaskThread(operation)
        self._track_worker(worker)
        worker.completed.connect(on_success)
        worker.failed.connect(on_failure or self._show_worker_error)
        worker.start()

    def _track_worker(self, worker: AsyncTaskThread) -> None:
        self._operation_registry.track(worker, self._worker_finished)

    def _worker_finished(self, worker: AsyncTaskThread) -> None:
        """Clear role-specific references after the common registry cleanup."""

        if self._review_worker is worker:
            self._review_worker = None

    def _navigation_clicked(self, index: QModelIndex) -> None:
        page_id = self.navigation_model.item_id(index)
        if page_id:
            self.navigate(page_id)

    def _set_sidebar_visible(self, visible: bool) -> None:
        self._manual_sidebar_visible = visible
        self.preferences.sidebar_expanded = visible
        self.sidebar.setVisible(visible)
        self._save_preferences()

    def _set_theme(self, mode: ThemeMode) -> None:
        self.preferences.theme = mode.value
        self.theme.set_mode(mode)
        self._save_preferences()

    def _theme_from_settings(self, mode: str) -> None:
        self.theme.set_mode(mode)

    def _theme_changed(self, _mode: str) -> None:
        current_id = self.preferences.current_navigation
        self.navigation_model = self._navigation_model()
        self.navigation.setModel(self.navigation_model)
        self.navigate(current_id)

    def _credentials_changed(self, _provider: str) -> None:
        self.new_review_page.refresh_credentials()

    def _settings_preferences_changed(self) -> None:
        if not self._save_preferences():
            return
        self.new_review_page.apply_preferences()
        self.rubrics_page.apply_preferences()
        self.settings_page.show_preferences_saved()

    def _rubric_preferences_changed(self) -> None:
        if self._save_preferences():
            self.new_review_page.apply_preferences()
        else:
            self.rubrics_page.show_preferences_error("无法写入本地设置文件")

    def _save_preferences(self) -> bool:
        try:
            self.preferences_store.save(self.preferences)
        except OSError as error:
            self.global_status.setText("设置保存失败，需要处理")
            self.settings_page.show_preferences_error(str(error))
            return False
        return True

    def _open_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Paper Reviewer",
            "Paper Reviewer 0.1.0\n\n基于 Rubric 和证据的本科论文评测桌面端。",
        )

    def _show_blocking_error(self, message: str) -> None:
        QMessageBox.critical(self, "操作失败", message)
        self._set_global_status("", fallback="操作失败，需要处理")

    def _show_worker_error(self, message: str, _trace: str) -> None:
        self._show_blocking_error(message)

    def _event_status_value(self, event: RunEvent) -> str:
        """Read a status from a live event, including forward-compatible payloads."""

        if event.status is not None:
            return status_value(event.status)
        payload_status = event.payload.get("status")
        return payload_status if isinstance(payload_status, str) else ""

    def _set_global_status(self, status: object = "", *, fallback: str = "") -> None:
        value = status_value(status) if status else ""
        message = self.STATUS_TEXT.get(value, fallback or value or "就绪")
        self.global_status.setText(message)
        self.global_status.setAccessibleName(f"全局状态：{message}")

    def _remember_active_run(self, run_id: str, status: object) -> None:
        if not run_id:
            return
        value = status_value(status)
        self._active_run_id = run_id
        self._active_run_status = value
        if is_terminal_run_status(value):
            self._clear_active_run()
            return
        if is_active_run_status(value) or is_human_review_status(value):
            if self.preferences.active_run_id != run_id:
                self.preferences.active_run_id = run_id
                self._save_preferences()

    def _clear_active_run(self) -> None:
        self._active_run_status = ""
        self.preferences.active_run_id = None
        self._save_preferences()

    def _restore_active_run(self, value: list[object]) -> None:
        run_id = self.preferences.active_run_id
        if not run_id:
            return
        for item in value:
            item_id = getattr(item, "run_id", None)
            item_status = getattr(item, "status", None)
            if item_id != run_id:
                continue
            if is_active_run_status(item_status) or is_human_review_status(item_status):
                if self._restored_active_run_id == run_id:
                    return
                self._restored_active_run_id = run_id
                # Defer opening until the current event-loop turn has finished
                # updating the table; this also prevents a refresh from
                # replacing a user-selected run.
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, lambda selected=run_id: self.open_run(selected))
            else:
                self._clear_active_run()
            return

    def _restore_window_state(self) -> None:
        geometry = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if state is not None:
            self.restoreState(state)
        self.sidebar.setVisible(self.preferences.sidebar_expanded)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        page = self.pages.currentWidget() if hasattr(self, "pages") else None
        if page is None or not hasattr(self, "sidebar"):
            return
        required_width = page.minimumSizeHint().width() + self.sidebar.minimumWidth() + 80
        automatically_hide = self.width() < required_width
        self.sidebar.setVisible(self._manual_sidebar_visible and not automatically_hide)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._review_worker is not None and self._review_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "评测仍在进行",
                "退出会取消当前评测。已完成检查点会保留。是否取消并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        running_workers = self._operation_registry.cancel_running()
        deadline = monotonic() + 5.0
        for worker in running_workers:
            remaining_ms = max(0, int((deadline - monotonic()) * 1000))
            worker.wait(remaining_ms)
        if any(worker.isRunning() for worker in running_workers):
            self.global_status.setText("后台任务正在安全停止，请稍后重试退出")
            QMessageBox.warning(
                self,
                "任务正在停止",
                "后台任务尚未安全结束，应用将继续保持打开。请稍候再退出。",
            )
            event.ignore()
            return
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/state", self.saveState())
        self._save_preferences()
        event.accept()
