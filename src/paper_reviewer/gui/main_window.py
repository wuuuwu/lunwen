from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import ClassVar, cast

from PySide6.QtCore import QEvent, QModelIndex, QSettings, QSize, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
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
    COURSE_APP_DISPLAY_NAME,
    COURSE_ORGANIZATION_NAME,
    COURSE_SETTINGS_NAME,
    AppPaths,
    GuiPreferences,
    PreferencesStore,
    is_active_run_status,
    is_human_review_status,
    is_terminal_run_status,
    status_value,
)
from paper_reviewer.application.metadata_recheck import submission_metadata_sha256
from paper_reviewer.application.models import (
    BatchMetadataRecheckPreview,
    BatchMetadataRecheckResult,
    MetadataRecheckDecision,
    ReportExportFormat,
    ReportView,
    ReviewRequest,
    RunDetail,
    RunEvent,
)
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.domain.batch import BatchEvent, BatchRecord, BatchReviewRequest, BatchStatus
from paper_reviewer.domain.review import HumanPanelDecision, HumanRuleDecision
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.gui.dialogs.course_metadata import CourseMetadataDialog
from paper_reviewer.gui.dialogs.course_metadata_recheck import CourseMetadataRecheckDialog
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import (
    NavigationItem,
    NavigationModel,
    provider_label,
)
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.course_batch_detail import CourseBatchDetailPage
from paper_reviewer.gui.pages.course_batch_new import CourseBatchNewPage
from paper_reviewer.gui.pages.course_batches import CourseBatchesPage
from paper_reviewer.gui.pages.rubrics import RubricsPage
from paper_reviewer.gui.pages.run_detail import RunDetailPage
from paper_reviewer.gui.pages.runs import RunsPage
from paper_reviewer.gui.pages.settings import SettingsPage
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.theme import FluentThemeManager, ThemeMode
from paper_reviewer.gui.worker import AsyncOperation, AsyncTaskThread, EventEmitter


class MainWindow(QMainWindow):
    NAVIGATION: ClassVar[list[tuple[str, str, str, str]]] = [
        ("new_review", "新建批次", "add_document", "创建课程论文批量评测"),
        ("batches", "批次记录", "history", "查看、继续和重试课程论文批次"),
        ("runs", "单篇记录", "folder", "查看每篇论文的任务和报告"),
        ("rubrics", "课程 Rubric", "rubric", "校验和预览课程评价标准"),
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
        self._batch_worker: AsyncTaskThread | None = None
        # Batch execution identity must never be inferred from the record that
        # happens to be visible in ``batch_detail_page``.  Users may inspect a
        # second batch while the first one is still running, and asynchronous
        # load callbacks may arrive in either order.
        self._running_batch_id = ""
        self._running_batch_run_ids: set[str] = set()
        self._run_to_batch: dict[str, str] = {}
        self._running_batch_record: BatchRecord | None = None
        self._batch_worker_generation = 0
        self._batch_view_generation = 0
        self._batch_list_generation = 0
        self._batch_locked_run_id = ""
        # Keep the historical set of batch IDs for integrations and older
        # tests, while the mapping below is the actual ownership record.  A
        # fresh token is issued for every preview/apply operation so a late
        # callback can release only its own Busy state.
        self._metadata_recheck_inflight: set[str] = set()
        self._metadata_recheck_operations: dict[str, tuple[int, str]] = {}
        self._metadata_recheck_token = 0
        self._closing = False
        self._active_run_id = ""
        self._active_run_status = ""
        self._restored_active_run_id = ""
        self._active_run_events: list[RunEvent] = []
        self._run_return_page = "runs"
        self._detail_request_generation = 0
        self._report_export_inflight: set[tuple[str, int]] = set()
        self._manual_sidebar_visible = preferences.sidebar_expanded
        self._settings = QSettings(COURSE_ORGANIZATION_NAME, COURSE_SETTINGS_NAME)

        self.setWindowTitle(f"{COURSE_APP_DISPLAY_NAME} · 课程论文批量评测")
        self.setMinimumSize(900, 600)
        self.resize(1180, 760)
        self._build_menu()
        self._build_shell()
        self._restore_window_state()
        self.theme.theme_changed.connect(self._theme_changed)
        self.navigate(preferences.current_navigation)
        self.refresh_runs()
        self.refresh_batches()
        if preferences.active_batch_id:
            # Reopen the last non-terminal batch after an abnormal shutdown.
            # Loading it does not start model work; stale ``running`` records
            # are projected as paused and require an explicit Continue action.
            self.open_batch(preferences.active_batch_id)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        new_action = QAction("新建批次(&N)", self)
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
        self.new_review_page = CourseBatchNewPage(
            self.service, self.preferences, self.icons
        )
        self.batches_page = CourseBatchesPage(self.icons)
        self.runs_page = RunsPage(self.icons)
        course_rubric = bundled_config("course_paper_v1.yaml")
        course_profile = bundled_config("course_paper_reviewers_v1.yaml")
        self.rubrics_page = RubricsPage(
            self.service,
            self.preferences,
            self.icons,
            operation_registry=self._operation_registry,
            profile_path=course_profile,
            default_rubric_path=course_rubric,
        )
        self.settings_page = SettingsPage(
            self.service,
            self.preferences,
            self.paths,
            self.icons,
            operation_registry=self._operation_registry,
            profile_path=course_profile,
            default_rubric_path=course_rubric,
        )
        self.run_detail_page = RunDetailPage(self.icons)
        self.batch_detail_page = CourseBatchDetailPage(self.icons)
        self.page_by_id = {
            "new_review": self.new_review_page,
            "batches": self.batches_page,
            "runs": self.runs_page,
            "rubrics": self.rubrics_page,
            "settings": self.settings_page,
        }
        for page in self.page_by_id.values():
            self.pages.addWidget(page)
        self.pages.addWidget(self.run_detail_page)
        self.pages.addWidget(self.batch_detail_page)
        shell.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self.new_review_page.start_requested.connect(self.start_batch)
        self.new_review_page.settings_requested.connect(lambda: self.navigate("settings"))
        self.runs_page.refresh_requested.connect(self.refresh_runs)
        self.runs_page.run_open_requested.connect(self._open_run_from_runs)
        self.batches_page.refresh_requested.connect(self.refresh_batches)
        self.batches_page.batch_open_requested.connect(self.open_batch)
        self.batch_detail_page.stop_requested.connect(self.stop_batch)
        self.batch_detail_page.resume_requested.connect(self.resume_batch)
        self.batch_detail_page.retry_failed_requested.connect(self.retry_failed_batch_items)
        self.batch_detail_page.open_output_requested.connect(
            lambda value: self._open_directory(Path(value))
        )
        self.batch_detail_page.run_open_requested.connect(self._open_batch_run)
        self.batch_detail_page.metadata_edit_requested.connect(self.edit_batch_metadata)
        self.batch_detail_page.metadata_recheck_requested.connect(
            self.recheck_batch_metadata
        )
        self.rubrics_page.preferences_changed.connect(self._rubric_preferences_changed)
        self.settings_page.preferences_changed.connect(self._settings_preferences_changed)
        self.settings_page.theme_changed.connect(self._theme_from_settings)
        self.settings_page.credentials_changed.connect(self._credentials_changed)
        self.run_detail_page.back_requested.connect(self._back_from_run_detail)
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
        # Leaving the batch detail invalidates its transient metadata dialogs
        # and Busy state.  The worker itself is allowed to unwind; its result
        # is ignored by the operation-token check below.
        invalidate = getattr(self, "_invalidate_metadata_recheck_operations", None)
        if callable(invalidate):
            invalidate()
        self.run_detail_page.reset_report_export_state()
        self._detail_request_generation += 1
        self._batch_view_generation += 1
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
        elif page_id == "batches":
            self.refresh_batches()
        elif page_id == "settings":
            self.settings_page.apply_preferences()
        self._save_preferences()

    def start_batch(self, request: object) -> None:
        if not isinstance(request, BatchReviewRequest):
            return
        if self._has_active_evaluation_worker():
            return

        async def operation(emit: EventEmitter) -> BatchRecord:
            created = await self.service.create_batch(request)
            emit(
                BatchEvent(
                    batch_id=created.batch_id,
                    event_type="batch_created",
                    status=created.status,
                    message="课程论文批次已创建。",
                    payload={"record": created.model_dump(mode="json")},
                )
            )
            return await self.service.run_batch(created.batch_id, event_sink=emit)

        # The id of a previous batch must not filter the first event emitted by
        # this newly-created batch.  The worker binds its immutable id from the
        # ``batch_created`` event before accepting any later events.
        invalidate = getattr(self, "_invalidate_metadata_recheck_operations", None)
        if callable(invalidate):
            invalidate()
        self._running_batch_id = ""
        self._running_batch_run_ids.clear()
        self._running_batch_record = None
        self.preferences.active_batch_id = None
        self._save_preferences()
        self._batch_view_generation += 1
        self.new_review_page.set_busy(True)
        self.batch_detail_page.clear()
        self.pages.setCurrentWidget(self.batch_detail_page)
        self._start_batch_worker(operation, action="start", batch_id="")
        self._set_global_status(fallback="正在创建课程论文批次")

    def resume_batch(self, batch_id: str) -> None:
        if self._has_active_evaluation_worker():
            return

        async def operation(emit: EventEmitter) -> BatchRecord:
            return await self.service.resume_batch(batch_id, event_sink=emit)

        self._set_active_batch_preference(batch_id)
        if self._is_batch_detail_visible(batch_id):
            self.batch_detail_page.set_busy(True, action="resume")
        self._start_batch_worker(operation, action="resume", batch_id=batch_id)
        self._set_global_status(fallback="正在继续课程论文批次")

    def retry_failed_batch_items(self, batch_id: str) -> None:
        if self._has_active_evaluation_worker():
            return

        async def operation(emit: EventEmitter) -> BatchRecord:
            return await self.service.retry_failed_items(batch_id, event_sink=emit)

        self._set_active_batch_preference(batch_id)
        if self._is_batch_detail_visible(batch_id):
            self.batch_detail_page.set_busy(True, action="retry")
        self._start_batch_worker(operation, action="retry", batch_id=batch_id)
        self._set_global_status(fallback="正在重试批次失败项")

    def stop_batch(self, batch_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "停止批次",
            "确定停止当前批次吗？当前论文会安全取消，已完成检查点和后续队列会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._is_batch_detail_visible(batch_id):
            self.batch_detail_page.set_busy(True, action="stop")
        if self._batch_worker_owns(batch_id):
            assert self._batch_worker is not None
            self._batch_worker.cancel_task()
            return
        # A stale ``running`` record may belong to an interrupted previous app
        # session, or another batch may currently own the live worker.  Never
        # cancel that other worker; persist only the requested batch as paused.
        self._persist_paused_batch(batch_id, self._batch_view_generation)

    def _persist_paused_batch(self, batch_id: str, view_generation: int) -> None:
        async def operation(_emit: EventEmitter) -> BatchRecord:
            return await self.service.pause_batch(batch_id)

        self._run_async(
            operation,
            lambda value: self._batch_mutation_completed(
                value,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                success_message="批次已暂停，可继续",
            ),
            lambda message, trace: self._batch_mutation_failed(
                message,
                trace,
                expected_batch_id=batch_id,
                view_generation=view_generation,
            ),
        )

    def _start_batch_worker(
        self,
        operation: AsyncOperation,
        *,
        action: str,
        batch_id: str,
    ) -> None:
        self._batch_worker_generation += 1
        generation = self._batch_worker_generation
        worker = AsyncTaskThread(operation)
        self._batch_worker = worker
        self._running_batch_id = batch_id
        self._running_batch_run_ids.clear()
        self._running_batch_record = None
        self._track_worker(worker)
        worker.setProperty("batchAction", action)
        worker.setProperty("batchId", batch_id)
        worker.event_received.connect(
            lambda value, owner=worker, token=generation: self._batch_event(
                value, owner, token
            )
        )
        worker.completed.connect(
            lambda value, owner=worker, token=generation: self._batch_worker_completed(
                value, owner, token
            )
        )
        worker.failed.connect(
            lambda message, trace, owner=worker, token=generation: self._batch_worker_failed(
                message, trace, owner, token
            )
        )
        worker.task_cancelled.connect(
            lambda owner=worker, token=generation: self._batch_worker_cancelled(owner, token)
        )
        worker.start()

    def _batch_event(
        self,
        value: object,
        worker: AsyncTaskThread,
        generation: int,
    ) -> None:
        if not isinstance(value, BatchEvent):
            return
        if generation != self._batch_worker_generation or self._batch_worker is not worker:
            return
        owner_id = self._worker_batch_id(worker)
        if owner_id and value.batch_id != owner_id:
            return
        if not owner_id:
            worker.setProperty("batchId", value.batch_id)
            self._running_batch_id = value.batch_id
        elif not self._running_batch_id:
            self._running_batch_id = owner_id
        self._set_active_batch_preference(value.batch_id)

        record_value = value.payload.get("record")
        if isinstance(record_value, dict):
            try:
                record = BatchRecord.model_validate(record_value)
            except ValueError:
                pass
            else:
                self._remember_batch_record(record)
                if self._can_apply_live_batch_event(value.batch_id):
                    self._set_batch_detail_record(record, live_worker=True)
        run_id = value.payload.get("run_id")
        if isinstance(run_id, str) and run_id:
            self._running_batch_run_ids.add(run_id)
            self._run_to_batch[run_id] = value.batch_id
        if self._is_batch_detail_visible(value.batch_id):
            self.batch_detail_page.apply_event(value)
        self._set_global_status(fallback=value.message)

    def _batch_worker_completed(
        self,
        value: object,
        worker: AsyncTaskThread,
        generation: int,
    ) -> None:
        if generation != self._batch_worker_generation or self._batch_worker is not worker:
            return
        if not isinstance(value, BatchRecord):
            self._batch_worker_failed(
                "批次操作没有返回有效状态", "", worker, generation
            )
            return
        owner_id = self._worker_batch_id(worker)
        if owner_id and owner_id != value.batch_id:
            self._batch_worker_failed("批次操作返回了不匹配的批次状态", "", worker, generation)
            return
        self._remember_batch_record(value)
        self._remember_batch_completion(value)
        self.new_review_page.set_busy(False)
        if self._can_apply_live_batch_event(value.batch_id):
            self.batch_detail_page.set_busy(False)
            self._set_batch_detail_record(value, live_worker=True)
        self._set_global_status(
            fallback=(
                "批次已暂停，可继续"
                if value.status is BatchStatus.PAUSED
                else "课程论文批次已完成"
            )
        )
        self.refresh_batches()
        self.refresh_runs()

    def _batch_worker_failed(
        self,
        message: str,
        _trace: str,
        worker: AsyncTaskThread,
        generation: int,
    ) -> None:
        if generation != self._batch_worker_generation or self._batch_worker is not worker:
            return
        batch_id = self._worker_batch_id(worker)
        self.new_review_page.set_busy(False)
        if batch_id and self._is_batch_detail_visible(batch_id):
            self.batch_detail_page.set_busy(False)
            self.batch_detail_page.show_error(message)
        elif not batch_id:
            self.new_review_page.show_batch_error(message)
        self._set_global_status(fallback="批次操作失败，需要处理")
        self.refresh_batches()

    def _batch_worker_cancelled(
        self,
        worker: AsyncTaskThread,
        generation: int,
    ) -> None:
        if generation != self._batch_worker_generation or self._batch_worker is not worker:
            return
        batch_id = self._worker_batch_id(worker)
        self.new_review_page.set_busy(False)
        if batch_id and self._is_batch_detail_visible(batch_id):
            self.batch_detail_page.set_busy(False)
        if batch_id:
            self._persist_paused_batch(batch_id, self._batch_view_generation)
        else:
            self._set_global_status(fallback="批次已停止")

    def refresh_batches(self) -> None:
        self._batch_list_generation += 1
        list_generation = self._batch_list_generation
        view_generation = self._batch_view_generation
        self.batches_page.set_loading(True)

        async def operation(_emit: EventEmitter) -> object:
            return await self.service.list_batches()

        self._run_async(
            operation,
            lambda value: self._batches_loaded(value, list_generation, view_generation),
            lambda message, _trace: self._batches_failed(message, list_generation),
        )

    def _batches_loaded(
        self,
        value: object,
        list_generation: int,
        view_generation: int,
    ) -> None:
        if list_generation != self._batch_list_generation:
            return
        if isinstance(value, list):
            records = [item for item in value if isinstance(item, BatchRecord)]
            for record in records:
                self._remember_batch_record(record)
            self.batches_page.set_items(
                [self._batch_record_for_display(record) for record in records]
            )
            visible_batch_id = self.batch_detail_page.batch_id
            if (
                view_generation == self._batch_view_generation
                and self.pages.currentWidget() is self.batch_detail_page
                and visible_batch_id
            ):
                visible = next(
                    (record for record in records if record.batch_id == visible_batch_id),
                    None,
                )
                if visible is not None:
                    self._set_batch_detail_record(visible)
        self.batches_page.set_loading(False)

    def _batches_failed(self, message: str, list_generation: int) -> None:
        if list_generation != self._batch_list_generation:
            return
        self.batches_page.set_loading(False)
        self.batches_page.show_error(message)

    def open_batch(self, batch_id: str) -> None:
        self._invalidate_metadata_recheck_operations()
        self._batch_view_generation += 1
        view_generation = self._batch_view_generation

        async def operation(_emit: EventEmitter) -> BatchRecord:
            return await self.service.get_batch(batch_id)

        self._set_global_status(fallback="正在加载批次详情")
        self._run_async(
            operation,
            lambda value: self._batch_loaded(value, batch_id, view_generation),
            lambda message, trace: self._batch_load_failed(
                message, trace, batch_id, view_generation
            ),
        )

    def _open_batch_run(self, run_id: str) -> None:
        self._run_return_page = "batch_detail"
        batch_id = self.batch_detail_page.batch_id
        if batch_id:
            self._run_to_batch[run_id] = batch_id
        self._batch_view_generation += 1
        self.open_run(run_id)

    def _open_run_from_runs(self, run_id: str) -> None:
        self._run_return_page = "runs"
        self._batch_view_generation += 1
        self.open_run(run_id)

    def edit_batch_metadata(self, batch_id: str, item_id: str) -> None:
        if self._has_active_evaluation_worker():
            return
        batch = self.batch_detail_page.batch
        if batch is None or batch.batch_id != batch_id:
            return
        item = next((value for value in batch.items if value.item_id == item_id), None)
        if item is None or item.metadata is None:
            self.batch_detail_page.show_error("该论文尚无可修改的提取信息。")
            return
        expected_metadata_sha256 = submission_metadata_sha256(item.metadata)
        dialog = CourseMetadataDialog(item.metadata, parent=self)
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        metadata = dialog.result_metadata
        if metadata is None:
            return

        async def operation(_emit: EventEmitter) -> BatchRecord:
            return await self.service.update_submission_metadata(
                batch_id,
                item_id,
                metadata,
                expected_metadata_sha256=expected_metadata_sha256,
            )

        view_generation = self._batch_view_generation
        self.batch_detail_page.set_busy(True, action="metadata")
        self._run_async(
            operation,
            lambda value: self._batch_mutation_completed(
                value,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                success_message="提取信息已更新，报告和汇总表已在本地重建",
            ),
            lambda message, trace: self._batch_mutation_failed(
                message,
                trace,
                expected_batch_id=batch_id,
                view_generation=view_generation,
            ),
        )

    def recheck_batch_metadata(self, batch_id: str) -> None:
        if self._has_active_evaluation_worker():
            self.batch_detail_page.show_error(
                "批次评测运行期间不能重新检查信息；请等待完成或先停止批次。"
            )
            return
        is_active = getattr(self, "_metadata_recheck_is_active", None)
        if callable(is_active) and is_active(batch_id):
            return
        batch = self.batch_detail_page.batch
        if batch is None or batch.batch_id != batch_id:
            return
        answer = QMessageBox.question(
            self,
            "批量重新检查信息",
            "重新检查只会在本机重新读取原 PDF，不会联网、不会调用模型或产生费用。"
            "检查结果会先显示差异预览，不会自动覆盖。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        view_generation = self._batch_view_generation
        operation_key = MainWindow._begin_metadata_recheck(self, batch_id, "preview")
        if operation_key is None:
            return
        _set_metadata_recheck_busy_for_controller(
            self,
            True,
            batch_id=batch_id,
            operation_key=operation_key,
        )

        async def operation(_emit: EventEmitter) -> BatchMetadataRecheckPreview:
            return await self.service.preview_batch_metadata_recheck(batch_id)

        _run_metadata_async_for_controller(
            self,
            operation,
            lambda value: self._metadata_recheck_preview_loaded(
                value,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            ),
            lambda message, trace: self._metadata_recheck_failed(
                message,
                trace,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            ),
            lambda: self._metadata_recheck_cancelled(
                batch_id,
                view_generation,
                operation_key,
            ),
        )

    def _metadata_recheck_preview_loaded(
        self,
        value: object,
        *,
        expected_batch_id: str,
        view_generation: int,
        operation_key: tuple[str, int] | None = None,
    ) -> None:
        operation_key = self._coerce_metadata_operation_key(
            expected_batch_id, operation_key
        )
        if operation_key is None or not self._metadata_recheck_is_current(
            expected_batch_id, operation_key
        ):
            return
        if (
            not isinstance(value, BatchMetadataRecheckPreview)
            or value.batch_id != expected_batch_id
        ):
            self._metadata_recheck_failed(
                "重新检查没有返回有效的差异预览。",
                "",
                expected_batch_id=expected_batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            )
            return
        if (
            view_generation != self._batch_view_generation
            or not self._is_batch_detail_visible(expected_batch_id)
        ):
            self._finish_metadata_recheck(operation_key)
            return
        self._finish_metadata_recheck(operation_key)
        if not value.items:
            skipped = "；".join(value.skipped.values())
            self.batch_detail_page.message.show_message(
                "没有可预览的信息差异。" + (f" {skipped}" if skipped else ""),
                severity="info",
            )
            return
        batch = self.batch_detail_page.batch
        if batch is None or batch.batch_id != expected_batch_id:
            return
        metadata_by_item = {
            item.item_id: item.metadata
            for item in batch.items
            if item.metadata is not None
        }
        dialog = CourseMetadataRecheckDialog(value, metadata_by_item, parent=self)
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        decisions = dialog.result_decisions
        if not decisions:
            return
        self._apply_metadata_recheck(
            expected_batch_id,
            decisions,
            view_generation=view_generation,
        )

    def _apply_metadata_recheck(
        self,
        batch_id: str,
        decisions: list[MetadataRecheckDecision],
        *,
        view_generation: int,
    ) -> None:
        operation_key = self._begin_metadata_recheck(batch_id, "apply")
        if operation_key is None:
            return
        _set_metadata_recheck_busy_for_controller(
            self,
            True,
            batch_id=batch_id,
            operation_key=operation_key,
        )

        async def operation(
            _emit: EventEmitter,
        ) -> tuple[BatchMetadataRecheckResult, BatchRecord]:
            result = await self.service.apply_batch_metadata_recheck(batch_id, decisions)
            record = await self.service.get_batch(batch_id)
            return result, record

        _run_metadata_async_for_controller(
            self,
            operation,
            lambda value: self._metadata_recheck_applied(
                value,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            ),
            lambda message, trace: self._metadata_recheck_failed(
                message,
                trace,
                expected_batch_id=batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            ),
            lambda: self._metadata_recheck_cancelled(
                batch_id,
                view_generation,
                operation_key,
            ),
        )

    def _metadata_recheck_applied(
        self,
        value: object,
        *,
        expected_batch_id: str,
        view_generation: int,
        operation_key: tuple[str, int] | None = None,
    ) -> None:
        operation_key = self._coerce_metadata_operation_key(
            expected_batch_id, operation_key
        )
        if operation_key is None or not self._metadata_recheck_is_current(
            expected_batch_id, operation_key
        ):
            return
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or not isinstance(value[0], BatchMetadataRecheckResult)
            or not isinstance(value[1], BatchRecord)
            or value[0].batch_id != expected_batch_id
            or value[1].batch_id != expected_batch_id
        ):
            self._metadata_recheck_failed(
                "应用信息核对结果后没有返回有效批次状态。",
                "",
                expected_batch_id=expected_batch_id,
                view_generation=view_generation,
                operation_key=operation_key,
            )
            return
        result, record = value
        self._finish_metadata_recheck(operation_key)
        self._remember_batch_record(record)
        if (
            view_generation == self._batch_view_generation
            and self._is_batch_detail_visible(expected_batch_id)
        ):
            self.batch_detail_page.set_busy(False)
            self._set_batch_detail_record(record)
            if result.failed_items:
                filenames = {
                    item.item_id: item.source.filename for item in record.items
                }
                failure_details = "；".join(
                    f"{filenames.get(item_id, item_id)}：{reason}"
                    for item_id, reason in result.failed_items.items()
                )
                self.batch_detail_page.message.show_message(
                    f"已更新 {len(result.updated_item_ids)} 篇；"
                    f"另有 {len(result.failed_items)} 篇更新失败，可重试。"
                    f"失败项：{failure_details}",
                    severity="warning",
                )
            else:
                self.batch_detail_page.message.show_message(
                    f"已核对并更新 {len(result.updated_item_ids)} 篇论文信息；"
                    "报告和汇总表已在本地重建。",
                    severity="success",
                )
        self.refresh_batches()
        self.refresh_runs()

    def _metadata_recheck_failed(
        self,
        message: str,
        _trace: str,
        *,
        expected_batch_id: str,
        view_generation: int,
        operation_key: tuple[str, int] | None = None,
    ) -> None:
        operation_key = self._coerce_metadata_operation_key(
            expected_batch_id, operation_key
        )
        if operation_key is None or not self._metadata_recheck_is_current(
            expected_batch_id, operation_key
        ):
            return
        self._finish_metadata_recheck(operation_key)
        if (
            view_generation == self._batch_view_generation
            and self._is_batch_detail_visible(expected_batch_id)
        ):
            self.batch_detail_page.set_busy(False)
            self.batch_detail_page.show_error(message)
            self._set_global_status(fallback="批量信息重新检查失败，需要处理")

    def _metadata_recheck_cancelled(
        self,
        batch_id: str,
        view_generation: int,
        operation_key: tuple[str, int],
    ) -> None:
        """Release a cancelled metadata operation and refresh partial writes.

        Applying decisions is item-by-item.  A cancellation can therefore
        arrive after an earlier item has already rebuilt its report.  Refresh
        the persisted batch from disk (unless the window is closing) so the
        UI reflects those committed items instead of retaining a stale table.
        """

        if not self._metadata_recheck_is_current(batch_id, operation_key):
            return
        self._finish_metadata_recheck(operation_key)
        if self._closing:
            return
        self._refresh_batch_after_metadata_cancel(batch_id, view_generation)

    def _refresh_batch_after_metadata_cancel(
        self, batch_id: str, view_generation: int
    ) -> None:
        async def operation(_emit: EventEmitter) -> BatchRecord:
            return await self.service.get_batch(batch_id)

        self._run_async(
            operation,
            lambda value: self._metadata_cancel_refresh_completed(
                value, expected_batch_id=batch_id, view_generation=view_generation
            ),
            lambda _message, _trace: None,
        )

    def _metadata_cancel_refresh_completed(
        self,
        value: object,
        *,
        expected_batch_id: str,
        view_generation: int,
    ) -> None:
        if not isinstance(value, BatchRecord) or value.batch_id != expected_batch_id:
            return
        if view_generation != self._batch_view_generation:
            return
        self._remember_batch_record(value)
        if self._is_batch_detail_visible(expected_batch_id):
            self._set_batch_detail_record(value)
        self.refresh_batches()
        self.refresh_runs()

    def _metadata_recheck_is_active(self, batch_id: str) -> bool:
        return batch_id in getattr(self, "_metadata_recheck_inflight", set())

    def _begin_metadata_recheck(
        self, batch_id: str, phase: str
    ) -> tuple[str, int] | None:
        if batch_id in getattr(self, "_metadata_recheck_inflight", set()):
            return None
        self._metadata_recheck_token = int(
            getattr(self, "_metadata_recheck_token", 0)
        ) + 1
        operation_key = (batch_id, self._metadata_recheck_token)
        self._metadata_recheck_inflight.add(batch_id)
        operations = getattr(self, "_metadata_recheck_operations", None)
        if operations is None:
            operations = {}
            self._metadata_recheck_operations = operations
        operations[batch_id] = (operation_key[1], phase)
        return operation_key

    def _coerce_metadata_operation_key(
        self, batch_id: str, operation_key: tuple[str, int] | None
    ) -> tuple[str, int] | None:
        if operation_key is not None:
            return operation_key
        operations = getattr(self, "_metadata_recheck_operations", {})
        current = operations.get(batch_id)
        if current is None:
            return None
        return (batch_id, current[0])

    def _metadata_recheck_is_current(
        self, batch_id: str, operation_key: tuple[str, int]
    ) -> bool:
        if operation_key[0] != batch_id:
            return False
        operations = getattr(self, "_metadata_recheck_operations", {})
        current = operations.get(batch_id)
        return bool(
            batch_id in getattr(self, "_metadata_recheck_inflight", set())
            and current is not None
            and current[0] == operation_key[1]
        )

    def _finish_metadata_recheck(self, operation_key: tuple[str, int]) -> bool:
        batch_id, token = operation_key
        operations = getattr(self, "_metadata_recheck_operations", {})
        current = operations.get(batch_id)
        if current is None or current[0] != token:
            return False
        operations.pop(batch_id, None)
        self._metadata_recheck_inflight.discard(batch_id)
        _set_metadata_recheck_busy_for_controller(
            self,
            False,
            batch_id=batch_id,
            operation_key=operation_key,
        )
        return True

    def _invalidate_metadata_recheck_operations(self) -> None:
        operations = getattr(self, "_metadata_recheck_operations", {})
        keys = [(batch_id, value[0]) for batch_id, value in operations.items()]
        operations.clear()
        self._metadata_recheck_inflight.clear()
        for operation_key in keys:
            _set_metadata_recheck_busy_for_controller(
                self,
                False,
                batch_id=operation_key[0],
                operation_key=operation_key,
            )

    def _set_metadata_recheck_busy(
        self,
        busy: bool,
        *,
        batch_id: str,
        operation_key: tuple[str, int],
    ) -> None:
        visibility_check = getattr(self, "_is_batch_detail_visible", None)
        visible = (
            visibility_check(batch_id)
            if callable(visibility_check)
            else getattr(self.batch_detail_page, "batch_id", "") == batch_id
        )
        if not visible:
            return
        # Keep compatibility with lightweight page doubles used by consumers
        # of the pre-token controller API; the real page accepts ``token``.
        try:
            self.batch_detail_page.set_busy(
                busy,
                action="metadata_recheck" if busy else "",
                token=operation_key,
            )
        except TypeError:
            self.batch_detail_page.set_busy(
                busy,
                action="metadata_recheck" if busy else "",
            )

    def _run_metadata_async(
        self,
        operation: AsyncOperation,
        on_success: Callable[[object], None],
        on_failure: Callable[[str, str], None],
        on_cancelled: Callable[[], None],
    ) -> AsyncTaskThread | None:
        # ``_run_async`` binds this handler before starting its QThread.  The
        # attribute-based hook keeps the public helper signature compatible
        # with existing integrations that replace ``_run_async`` in tests.
        operation._task_cancelled_handler = on_cancelled  # type: ignore[attr-defined]
        return self._run_async(operation, on_success, on_failure)

    def _back_from_run_detail(self) -> None:
        self._batch_view_generation += 1
        if self._run_return_page == "batch_detail" and self.batch_detail_page.batch_id:
            self.pages.setCurrentWidget(self.batch_detail_page)
        else:
            self.navigate("runs")
        self._run_return_page = "runs"

    def _has_active_evaluation_worker(self) -> bool:
        return bool(
            (self._review_worker is not None and self._review_worker.isRunning())
            or (self._batch_worker is not None and self._batch_worker.isRunning())
        )

    def _batch_loaded(
        self,
        value: object,
        expected_batch_id: str,
        view_generation: int,
    ) -> None:
        if view_generation != self._batch_view_generation:
            return
        if not isinstance(value, BatchRecord) or value.batch_id != expected_batch_id:
            self._batch_load_failed(
                "批次详情没有返回有效状态", "", expected_batch_id, view_generation
            )
            return
        self._remember_batch_record(value)
        self._set_batch_detail_record(value)
        self.pages.setCurrentWidget(self.batch_detail_page)
        self._set_global_status(fallback="批次详情已加载")

    def _batch_load_failed(
        self,
        message: str,
        _trace: str,
        _expected_batch_id: str,
        view_generation: int,
    ) -> None:
        if view_generation != self._batch_view_generation:
            return
        self.batches_page.show_error(message)
        self._set_global_status(fallback="批次详情加载失败")

    def _batch_mutation_completed(
        self,
        value: object,
        *,
        expected_batch_id: str,
        view_generation: int,
        success_message: str,
    ) -> None:
        if not isinstance(value, BatchRecord) or value.batch_id != expected_batch_id:
            self._batch_mutation_failed(
                "批次操作没有返回有效状态",
                "",
                expected_batch_id=expected_batch_id,
                view_generation=view_generation,
            )
            return
        self._remember_batch_record(value)
        self._remember_batch_completion(value)
        if (
            view_generation == self._batch_view_generation
            and self._is_batch_detail_visible(expected_batch_id)
        ):
            self.batch_detail_page.set_busy(False)
            self._set_batch_detail_record(value)
            self._set_global_status(fallback=success_message)
        self.refresh_batches()
        self.refresh_runs()

    def _batch_mutation_failed(
        self,
        message: str,
        _trace: str,
        *,
        expected_batch_id: str,
        view_generation: int,
    ) -> None:
        if (
            view_generation == self._batch_view_generation
            and self._is_batch_detail_visible(expected_batch_id)
        ):
            self.batch_detail_page.set_busy(False)
            self.batch_detail_page.show_error(message)
            self._set_global_status(fallback="批次操作失败，需要处理")
        self.refresh_batches()

    def _remember_batch_record(self, record: BatchRecord) -> None:
        run_ids = {item.run_id for item in record.items if item.run_id}
        for run_id in run_ids:
            self._run_to_batch[run_id] = record.batch_id
        if record.batch_id == self._running_batch_id:
            self._running_batch_record = record
            self._running_batch_run_ids.update(run_ids)

    def _remember_batch_completion(self, record: BatchRecord) -> None:
        if record.status in {BatchStatus.COMPLETED, BatchStatus.COMPLETED_WITH_ERRORS}:
            if self.preferences.active_batch_id == record.batch_id:
                self._set_active_batch_preference(None)
            return
        self._set_active_batch_preference(record.batch_id)

    def _set_active_batch_preference(self, batch_id: str | None) -> None:
        if self.preferences.active_batch_id == batch_id:
            return
        self.preferences.active_batch_id = batch_id
        self._save_preferences()

    def _batch_worker_owns(self, batch_id: str) -> bool:
        return bool(
            batch_id
            and self._batch_worker is not None
            and self._batch_worker.isRunning()
            and self._running_batch_id == batch_id
        )

    @staticmethod
    def _worker_batch_id(worker: AsyncTaskThread) -> str:
        value = worker.property("batchId")
        return value if isinstance(value, str) else ""

    def _is_batch_detail_visible(self, batch_id: str) -> bool:
        return bool(
            batch_id
            and self.pages.currentWidget() is self.batch_detail_page
            and self.batch_detail_page.batch_id == batch_id
        )

    def _can_apply_live_batch_event(self, batch_id: str) -> bool:
        if self.pages.currentWidget() is not self.batch_detail_page:
            return False
        visible_batch_id = self.batch_detail_page.batch_id
        return not visible_batch_id or visible_batch_id == batch_id

    def _batch_record_for_display(self, record: BatchRecord) -> BatchRecord:
        if record.status is BatchStatus.RUNNING and not self._batch_worker_owns(
            record.batch_id
        ):
            return record.model_copy(update={"status": BatchStatus.PAUSED})
        return record

    def _set_batch_detail_record(
        self,
        record: BatchRecord,
        *,
        live_worker: bool = False,
    ) -> None:
        stale_running = record.status is BatchStatus.RUNNING and not (
            live_worker or self._batch_worker_owns(record.batch_id)
        )
        self.batch_detail_page.set_batch(
            record.model_copy(update={"status": BatchStatus.PAUSED})
            if stale_running
            else record
        )
        if stale_running:
            self.batch_detail_page.message.show_message(
                "检测到上次应用异常退出留下的运行状态；可从已有检查点继续该批次。",
                severity="warning",
            )

    def _is_run_managed_by_live_batch(self, run_id: str) -> bool:
        if (
            not run_id
            or self._batch_worker is None
            or not self._batch_worker.isRunning()
            or not self._running_batch_id
        ):
            return False
        return bool(
            run_id in self._running_batch_run_ids
            or self._run_to_batch.get(run_id) == self._running_batch_id
        )

    def _guard_batch_managed_run_action(self, run_id: str) -> bool:
        if not self._is_run_managed_by_live_batch(run_id):
            return False
        message = (
            "该单篇任务正由课程批次统一管理，不能单独取消或恢复。"
            "请返回批次详情，使用“停止批次”安全停止当前论文并保留检查点。"
        )
        self.run_detail_page.message.show_message(message, severity="warning")
        self._set_global_status(fallback="请通过批次详情停止正在运行的单篇任务")
        return True

    def _apply_batch_managed_run_policy(self, run_id: str) -> None:
        if not self._is_run_managed_by_live_batch(run_id):
            self._release_batch_managed_run_policy()
            return
        buttons = (
            self.run_detail_page.cancel_button,
            self.run_detail_page.resume_button,
        )
        if not any(not button.isHidden() for button in buttons):
            return
        self._batch_locked_run_id = run_id
        notice = (
            "该单篇任务正由课程批次统一管理。若要中止，请返回批次详情并选择“停止批次”。"
        )
        for button in buttons:
            if not button.isHidden():
                button.setEnabled(False)
                button.setToolTip(notice)
                button.setAccessibleDescription(notice)
        self.run_detail_page.message.show_message(notice, severity="warning")

    def _release_batch_managed_run_policy(self) -> None:
        if not self._batch_locked_run_id:
            return
        for button in (
            self.run_detail_page.cancel_button,
            self.run_detail_page.resume_button,
        ):
            if not button.isHidden() and not button.property("fluentBusy"):
                button.setEnabled(True)
            button.setToolTip("")
            button.setAccessibleDescription("")
        self._batch_locked_run_id = ""

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
        self.context_status.setText(provider_label(request.provider, request.model))

    def resume_review(self, run_id: str) -> None:
        batch_guard = getattr(self, "_guard_batch_managed_run_action", None)
        if callable(batch_guard) and batch_guard(run_id):
            return
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
        batch_guard = getattr(self, "_guard_batch_managed_run_action", None)
        if callable(batch_guard) and batch_guard(run_id):
            return
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
        self.new_review_page.show_batch_error(message)
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
            self._apply_batch_managed_run_policy(value.run.run_id)
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
    ) -> AsyncTaskThread:
        worker = AsyncTaskThread(operation)
        self._track_worker(worker)
        worker.completed.connect(on_success)
        worker.failed.connect(on_failure or self._show_worker_error)
        cancelled_handler = getattr(operation, "_task_cancelled_handler", None)
        if callable(cancelled_handler):
            worker.task_cancelled.connect(cancelled_handler)
        worker.start()
        return worker

    def _track_worker(self, worker: AsyncTaskThread) -> None:
        self._operation_registry.track(worker, self._worker_finished)

    def _worker_finished(self, worker: AsyncTaskThread) -> None:
        """Clear role-specific references after the common registry cleanup."""

        if self._review_worker is worker:
            self._review_worker = None
        if self._batch_worker is worker:
            finished_batch_id = self._running_batch_id
            finished_record = self._running_batch_record
            self._batch_worker = None
            self._running_batch_id = ""
            self._running_batch_run_ids.clear()
            self._running_batch_record = None
            if (
                finished_record is not None
                and finished_record.batch_id == finished_batch_id
                and self._is_batch_detail_visible(finished_batch_id)
            ):
                self._set_batch_detail_record(finished_record)
            self._release_batch_managed_run_policy()

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
            f"关于 {COURSE_APP_DISPLAY_NAME}",
            (
                "Course Paper Reviewer 0.1.0\n\n"
                "面向普通课程论文的批量 AI 辅助评测桌面端。\n"
                "本结果仅供教师评阅参考，不替代教师正式评分。"
            ),
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
        review_running = self._review_worker is not None and self._review_worker.isRunning()
        batch_running = self._batch_worker is not None and self._batch_worker.isRunning()
        metadata_inflight: set[str] = getattr(
            self, "_metadata_recheck_inflight", set()
        )
        metadata_operations: dict[str, tuple[int, str]] = getattr(
            self, "_metadata_recheck_operations", {}
        )
        metadata_running = bool(metadata_inflight)
        if review_running or batch_running or metadata_running:
            if batch_running:
                title = "批次仍在进行"
                message = (
                    "退出会安全停止当前批次。已完成论文、当前检查点和后续队列都会保留。"
                )
            elif review_running:
                title = "评测仍在进行"
                message = "退出会取消当前评测。已完成检查点会保留。"
            elif any(
                phase == "apply"
                for _token, phase in metadata_operations.values()
            ):
                title = "信息核对正在应用"
                message = (
                    "退出会停止正在应用的论文信息核对。已经提交的项目会保留，"
                    "剩余项目可稍后重新检查。"
                )
            else:
                title = "信息重新检查仍在进行"
                message = "退出会停止本机信息重新检查，已保存的评测结果不会受影响。"
            answer = QMessageBox.question(
                self,
                title,
                message + "是否停止并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._closing = True
        running_workers = self._operation_registry.cancel_running()
        deadline = monotonic() + 5.0
        for worker in running_workers:
            remaining_ms = max(0, int((deadline - monotonic()) * 1000))
            worker.wait(remaining_ms)
        if any(worker.isRunning() for worker in running_workers):
            # The close request is rejected and the window remains usable.
            # Do not leave cancellation callbacks in a permanent shutdown
            # mode, otherwise a later metadata cancellation would skip the
            # refresh that reflects already committed items.
            self._closing = False
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


def _set_metadata_recheck_busy_for_controller(
    controller: object,
    busy: bool,
    *,
    batch_id: str,
    operation_key: tuple[str, int],
) -> None:
    """Call the token-aware page helper with legacy-controller tolerance."""

    method = getattr(controller, "_set_metadata_recheck_busy", None)
    if callable(method):
        method(busy, batch_id=batch_id, operation_key=operation_key)
        return
    # A few embedders construct a minimal controller object around the static
    # MainWindow handlers.  Keep those integrations functional while the real
    # MainWindow always takes the branch above.
    MainWindow._set_metadata_recheck_busy(
        cast(MainWindow, controller),
        busy,
        batch_id=batch_id,
        operation_key=operation_key,
    )


def _run_metadata_async_for_controller(
    controller: object,
    operation: AsyncOperation,
    on_success: Callable[[object], None],
    on_failure: Callable[[str, str], None],
    on_cancelled: Callable[[], None],
) -> AsyncTaskThread | None:
    """Run metadata work through a real or legacy controller instance."""

    method = getattr(controller, "_run_metadata_async", None)
    if callable(method):
        return cast(
            AsyncTaskThread | None,
            method(operation, on_success, on_failure, on_cancelled),
        )
    return MainWindow._run_metadata_async(
        cast(MainWindow, controller),
        operation,
        on_success,
        on_failure,
        on_cancelled,
    )
