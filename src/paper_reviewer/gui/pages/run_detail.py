from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.app_state import (
    COURSE_ORGANIZATION_NAME,
    COURSE_SETTINGS_NAME,
)
from paper_reviewer.application.models import ReportView, RunDetail, RunEvent
from paper_reviewer.domain.review import ReviewFinding
from paper_reviewer.domain.run import RunStatus
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import FindingsTableModel, provider_label
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader
from paper_reviewer.reporting.presentation import ReportPresentation

from .run_detail_presenter import (
    _course_conclusion,
    _course_grade,
    _display_value,
    _first,
    _format_hard_detail,
    _format_hard_report,
    _format_lines,
    _format_panel,
    _format_panel_review_detail,
    _items,
    _make_decision,
    _pending_items,
    _score_text,
    _status_value,
)


class RunDetailPage(QWidget):
    """Run progress and policy-aware report view.

    The policy-aware service models are intentionally read by semantic field
    names.  That keeps this screen compatible with old ``ReportView`` snapshots
    while the v2 domain models are being introduced by the application layer.
    """

    back_requested = Signal()
    cancel_requested = Signal(str)
    resume_requested = Signal(str)
    hard_rule_resolution_requested = Signal(str, object)
    panel_review_resolution_requested = Signal(str, object)
    resume_after_human_review_requested = Signal(str)
    report_export_requested = Signal(str, str, str, bool)

    STAGES: ClassVar[list[tuple[str, str]]] = [
        ("ingest", "解析论文"),
        ("evidence", "收集外部证据"),
        ("scoring", "专业化评分"),
        ("audit", "确定性审计"),
        ("panel", "独立专家面板"),
        ("meta", "Meta 评语"),
        ("report", "报告验证与生成"),
    ]
    COURSE_STAGES: ClassVar[list[tuple[str, str]]] = [
        ("ingest", "解析论文"),
        ("metadata", "提取学生与论文信息"),
        ("evidence", "收集外部证据"),
        ("reviews", "课程专项 Reviewer 评阅"),
        ("audit", "确定性审计"),
        ("meta", "Meta Review 汇总"),
        ("report", "报告验证与生成"),
    ]
    ACTIVE: ClassVar[set[RunStatus]] = {
        RunStatus.CREATED,
        RunStatus.INGESTING,
        RunStatus.INGESTED,
        RunStatus.BUILDING_EVIDENCE,
        RunStatus.EVIDENCE_READY,
        RunStatus.REVIEWING,
        RunStatus.AUDITING,
        RunStatus.META_REVIEWING,
        RunStatus.VALIDATING,
    }
    _ACTIVE_VALUES: ClassVar[set[str]] = {
        "created",
        "ingesting",
        "ingested",
        "building_evidence",
        "evidence_ready",
        "scoring",
        "reviewing",
        "auditing",
        "awaiting_hard_rule_confirmation",
        "panel_reviewing",
        "supplemental_reviewing",
        "awaiting_panel_review",
        "synthesizing",
        "meta_reviewing",
        "validating",
    }

    def __init__(self, icons: FluentIconService, service: Any | None = None) -> None:
        super().__init__()
        self.icons = icons
        self.service = service
        self.run_id = ""
        self.run_dir: Path | None = None
        self.completed_stages: set[str] = set()
        self._course_progress_mode = False
        self._active_course_stage: str | None = None
        self._last_progress_status: Any = "created"
        self._pending_hard_rules: list[Any] = []
        self._panel_review_required = False
        self._panel_review_detail = ""
        self._submitted_hard_rules: set[str] = set()
        self._operation_threads: list[Any] = []
        self._review_busy = False
        self._cancel_pending = False
        self._export_busy = False
        self._export_format = ""
        self._report_available = False
        self._report_generation = 0
        self._report_input_path = ""
        self._exported_report_path: Path | None = None
        self._export_trigger_button: QPushButton | None = None
        self._presentation: ReportPresentation | None = None

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 24, 24, 24)
        self.root.setSpacing(12)
        top = QHBoxLayout()
        back = QPushButton("返回任务记录")
        back.setObjectName("backToRunsButton")
        set_fluent_property(back, "fluentAppearance", "subtle")
        back.clicked.connect(self.back_requested)
        top.addWidget(back)
        top.addStretch(1)
        self.root.addLayout(top)
        self.header = PageHeader("任务详情", "查看阶段进度、错误信息和最终评测报告。")
        self.root.addWidget(self.header)
        self.message = MessageBar(icons)
        self.message.action_requested.connect(self._open_exported_report)
        self.root.addWidget(self.message)
        self.stack = QStackedWidget()
        self.progress_page = self._build_progress_page()
        self.report_page = self._build_report_page()
        self.stack.addWidget(self.progress_page)
        self.stack.addWidget(self.report_page)
        self.root.addWidget(self.stack, 1)

    @staticmethod
    def _scrollable_page(
        content: QWidget,
        object_name: str,
        accessible_name: str,
    ) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setAccessibleName(accessible_name)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(content)
        return scroll

    @staticmethod
    def _show_text_lines(widget: QPlainTextEdit, visible_lines: int) -> None:
        line_height = widget.fontMetrics().lineSpacing()
        widget.setMinimumHeight(line_height * (visible_lines + 2))
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    @staticmethod
    def _show_table_rows(table: QTableView, visible_rows: int) -> None:
        header_height = table.horizontalHeader().sizeHint().height()
        row_height = table.verticalHeader().defaultSectionSize()
        table.setMinimumHeight(header_height + row_height * visible_rows + row_height)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    @staticmethod
    def _show_list_rows(view: QListView | QListWidget, visible_rows: int) -> None:
        row_height = view.fontMetrics().lineSpacing() * 2
        view.setMinimumHeight(row_height * visible_rows)
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    @staticmethod
    def _scroll_to_top(scroll: QScrollArea) -> None:
        scroll.verticalScrollBar().setValue(0)

    def prepare_run(self, run_id: str = "", *, run_dir: Path | None = None) -> None:
        self._reset_report_context(False)
        self.run_id = run_id
        self.run_dir = run_dir
        self.completed_stages.clear()
        self._set_course_progress_mode(False)
        self._active_course_stage = None
        self._last_progress_status = "created"
        self._pending_hard_rules = []
        self._panel_review_required = False
        self._panel_review_detail = ""
        self._submitted_hard_rules.clear()
        self.stack.setCurrentWidget(self.progress_page)
        self.run_metadata.setText(
            f"任务 {run_id} · 正在恢复评测" if run_id else "正在创建评测任务…"
        )
        self.events.clear()
        self._update_stages([], "created")
        self.set_cancel_pending(run_id, False)
        self.cancel_button.setVisible(bool(run_id))
        self.resume_button.hide()
        self._show_hard_rule_review([])
        self.message.clear()
        self._scroll_to_top(self.progress_scroll)

    def _build_progress_page(self) -> QWidget:
        content = QWidget()
        content.setObjectName("pageCanvas")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.run_metadata = QLabel()
        self.run_metadata.setProperty("fluentType", "secondary")
        self.run_metadata.setWordWrap(True)
        self.run_metadata.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.stage_progress = QProgressBar()
        stages = self._stage_definitions()
        self.stage_progress.setRange(0, len(stages))
        self.stage_progress.setTextVisible(False)
        self.stage_model = QStandardItemModel()
        self.stage_list = QListView()
        self.stage_list.setModel(self.stage_model)
        self.stage_list.setAccessibleName("评测阶段")
        self._show_list_rows(self.stage_list, len(stages))

        self.hard_rule_review_frame = QFrame()
        self.hard_rule_review_frame.setProperty("fluentRole", "card")
        review_layout = QVBoxLayout(self.hard_rule_review_frame)
        review_layout.setContentsMargins(16, 16, 16, 16)
        review_layout.setSpacing(12)
        title = QLabel("评测完成后的人工复核")
        title.setProperty("fluentType", "sectionTitle")
        hint = QLabel(
            "AI 评测和报告已经完成。请逐项核对否决项或人工面板待办，"
            "填写复核人和理由后提交；保存过程不会再次调用模型。"
        )
        hint.setProperty("fluentType", "secondary")
        hint.setWordWrap(True)
        self.hard_rule_list = QListWidget()
        self.hard_rule_list.setObjectName("pendingHardRules")
        self.hard_rule_list.setAccessibleName("评测完成后的人工复核待办")
        self._show_list_rows(self.hard_rule_list, 3)
        self.hard_rule_list.currentRowChanged.connect(self._hard_rule_selected)
        self.hard_rule_detail = QPlainTextEdit()
        self.hard_rule_detail.setObjectName("hardRuleEvidenceDetail")
        self.hard_rule_detail.setReadOnly(True)
        self.hard_rule_detail.setAccessibleName("人工复核规则、专家意见和证据详情")
        self._show_text_lines(self.hard_rule_detail, 9)
        self.hard_rule_reviewer_input = QLineEdit()
        self.reviewer_input = self.hard_rule_reviewer_input
        self.hard_rule_reviewer_input.setObjectName("hardRuleReviewer")
        self.hard_rule_reviewer_input.setPlaceholderText("复核人（必填）")
        self.hard_rule_reviewer_input.setAccessibleName("人工复核人，必填")
        self.hard_rule_reviewer_input.textChanged.connect(self._update_review_actions)
        self.hard_rule_reason_input = QPlainTextEdit()
        self.reason_input = self.hard_rule_reason_input
        self.hard_rule_reason_input.setObjectName("hardRuleReviewReason")
        self.hard_rule_reason_input.setPlaceholderText("复核理由（必填，可记录线下检测报告结论）")
        self.hard_rule_reason_input.setAccessibleName("人工复核理由，必填")
        self._show_text_lines(self.hard_rule_reason_input, 5)
        self.hard_rule_reason_input.textChanged.connect(self._update_review_actions)
        self.hard_rule_error = QLabel()
        self.hard_rule_error.setProperty("fluentSeverity", "danger")
        self.hard_rule_error.setWordWrap(True)
        self.confirm_rule_button = QPushButton("确认成立")
        self.confirm_hard_rule_button = self.confirm_rule_button
        self.confirm_rule_button.setObjectName("confirmHardRuleButton")
        self.confirm_rule_button.setIcon(self.icons.icon("warning", color_role="danger_foreground"))
        set_fluent_property(self.confirm_rule_button, "fluentAppearance", "danger")
        self.confirm_rule_button.setAccessibleName("确认否决项成立")
        self.confirm_rule_button.clicked.connect(lambda: self._submit_hard_rule(True))
        self.dismiss_rule_button = QPushButton("确认不成立")
        self.dismiss_hard_rule_button = self.dismiss_rule_button
        self.dismiss_rule_button.setObjectName("dismissHardRuleButton")
        self.dismiss_rule_button.setIcon(self.icons.icon("check", color_role="success_foreground"))
        set_fluent_property(self.dismiss_rule_button, "fluentAppearance", "secondary")
        self.dismiss_rule_button.setAccessibleName("确认否决项不成立")
        self.dismiss_rule_button.clicked.connect(lambda: self._submit_hard_rule(False))
        buttons = QHBoxLayout()
        buttons.addWidget(self.confirm_rule_button)
        buttons.addWidget(self.dismiss_rule_button)
        buttons.addStretch(1)
        for review_widget in (
            title,
            hint,
            self.hard_rule_list,
            self.hard_rule_detail,
            self.hard_rule_reviewer_input,
            self.hard_rule_reason_input,
            self.hard_rule_error,
        ):
            review_layout.addWidget(review_widget)
        review_layout.addLayout(buttons)

        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setAccessibleName("任务事件")
        self.events.setPlaceholderText("任务开始后将在这里显示阶段事件。")
        self._show_text_lines(self.events, 10)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("取消评测")
        self.cancel_button.setIcon(self.icons.icon("stop"))
        set_fluent_property(self.cancel_button, "fluentAppearance", "danger")
        self.cancel_button.clicked.connect(self._cancel)
        self.resume_button = QPushButton("恢复评测")
        self.resume_button.setIcon(self.icons.icon("play"))
        self.resume_button.clicked.connect(self._resume)
        self.folder_button = QPushButton("打开任务目录")
        self.folder_button.setIcon(self.icons.icon("folder"))
        self.folder_button.clicked.connect(self._open_folder)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.resume_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        progress_widgets: tuple[QWidget, ...] = (
            self.run_metadata,
            self.stage_progress,
            self.stage_list,
            self.events,
        )
        for progress_widget in progress_widgets:
            layout.addWidget(progress_widget)
        layout.addLayout(actions)
        self._show_hard_rule_review([])
        self.progress_content = content
        self.progress_scroll = self._scrollable_page(
            content,
            "runProgressScroll",
            "任务进度可滚动内容",
        )
        return self.progress_scroll

    def _build_report_page(self) -> QWidget:
        content = QWidget()
        content.setObjectName("pageCanvas")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        self.report_metadata = QLabel()
        self.report_metadata.setProperty("fluentType", "secondary")
        self.report_metadata.setWordWrap(True)
        self.report_metadata.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.overall_summary = QLabel()
        self.overall_summary.setWordWrap(True)
        self.overall_summary.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.score_frame = QFrame()
        self.score_frame.setProperty("fluentRole", "card")
        score_layout = QHBoxLayout(self.score_frame)
        score_layout.setContentsMargins(16, 16, 16, 16)
        score_layout.setSpacing(12)
        self.total_score = QLabel()
        self.total_score.setProperty("fluentType", "pageTitle")
        self.dimension_scores = QLabel()
        self.dimension_scores.setWordWrap(True)
        score_layout.addWidget(self.total_score)
        score_layout.addWidget(self.dimension_scores, 1)
        self.unscored_message = MessageBar(self.icons)

        self.diagnostic_frame = QFrame()
        self.diagnostic_frame.setProperty("fluentRole", "card")
        diagnostic_layout = QVBoxLayout(self.diagnostic_frame)
        diagnostic_layout.setContentsMargins(16, 16, 16, 16)
        diagnostic_layout.setSpacing(12)
        self.diagnostic_title = QLabel("九项诊断评分（0–4）")
        self.diagnostic_title.setProperty("fluentType", "sectionTitle")
        self.diagnostic_summary = QLabel()
        self.diagnostic_summary.setProperty("fluentType", "secondary")
        self.diagnostic_summary.setWordWrap(True)
        self.diagnostic_scores_model = QStandardItemModel()
        self.diagnostic_scores_model.setHorizontalHeaderLabels(["指标", "分组", "等级", "加权贡献"])
        self.diagnostic_scores = QTableView()
        self.criterion_table = self.diagnostic_scores
        self.diagnostic_scores.setObjectName("diagnosticScores")
        self.diagnostic_scores.setModel(self.diagnostic_scores_model)
        self.diagnostic_scores.setAccessibleName("诊断评分明细")
        self.diagnostic_scores.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.diagnostic_scores.verticalHeader().hide()
        self.diagnostic_scores.horizontalHeader().setStretchLastSection(True)
        self._show_table_rows(self.diagnostic_scores, 9)
        self.experimental_score = QLabel()
        self.experimental_score.setWordWrap(True)
        for widget in (
            self.diagnostic_title,
            self.diagnostic_summary,
            self.diagnostic_scores,
            self.experimental_score,
        ):
            diagnostic_layout.addWidget(widget)

        self.hard_rule_report_frame = QFrame()
        self.hard_rule_report_frame.setProperty("fluentRole", "card")
        hard_layout = QVBoxLayout(self.hard_rule_report_frame)
        hard_layout.setContentsMargins(16, 16, 16, 16)
        hard_layout.setSpacing(12)
        hard_title = QLabel("否决项状态与人工处理记录")
        hard_title.setProperty("fluentType", "sectionTitle")
        self.hard_rule_report = QPlainTextEdit()
        self.hard_rules_report = self.hard_rule_report
        self.hard_rule_report.setObjectName("hardRuleReport")
        self.hard_rule_report.setReadOnly(True)
        self.hard_rule_report.setAccessibleName("否决项状态与人工处理记录")
        self._show_text_lines(self.hard_rule_report, 8)
        hard_layout.addWidget(hard_title)
        hard_layout.addWidget(self.hard_rule_report)

        self.panel_report_frame = QFrame()
        self.panel_report_frame.setProperty("fluentRole", "card")
        panel_layout = QVBoxLayout(self.panel_report_frame)
        panel_layout.setContentsMargins(16, 16, 16, 16)
        panel_layout.setSpacing(12)
        panel_title = QLabel("独立专家面板")
        panel_title.setProperty("fluentType", "sectionTitle")
        self.panel_report = QPlainTextEdit()
        self.expert_panel_report = self.panel_report
        self.panel_report.setObjectName("panelReport")
        self.panel_report.setReadOnly(True)
        self.panel_report.setAccessibleName("初评和复评专家意见")
        self._show_text_lines(self.panel_report, 12)
        panel_layout.addWidget(panel_title)
        panel_layout.addWidget(self.panel_report)

        self.decision_frame = QFrame()
        self.decision_frame.setProperty("fluentRole", "card")
        decision_layout = QVBoxLayout(self.decision_frame)
        decision_layout.setContentsMargins(16, 16, 16, 16)
        decision_layout.setSpacing(12)
        decision_title = QLabel("确定性决策路径与 AI 辅助抽检风险结论")
        decision_title.setProperty("fluentType", "sectionTitle")
        self.decision_path = QPlainTextEdit()
        self.decision_path.setObjectName("decisionPath")
        self.decision_path.setReadOnly(True)
        self.decision_path.setAccessibleName("确定性决策路径和风险结论")
        self._show_text_lines(self.decision_path, 8)
        decision_layout.addWidget(decision_title)
        decision_layout.addWidget(self.decision_path)
        self.disclaimers = QLabel()
        self.disclaimers.setObjectName("reportDisclaimers")
        self.disclaimers.setWordWrap(True)
        self.disclaimers.setProperty("fluentType", "secondary")
        self.disclaimers.setAccessibleName("报告使用免责声明")

        self.findings_frame = QFrame()
        self.findings_frame.setProperty("fluentRole", "card")
        findings_layout = QVBoxLayout(self.findings_frame)
        findings_layout.setContentsMargins(16, 16, 16, 16)
        findings_layout.setSpacing(12)
        findings_title = QLabel("问题与证据详情")
        findings_title.setProperty("fluentType", "sectionTitle")
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("findingsSplitter")
        split.setChildrenCollapsible(False)
        self.findings_model = FindingsTableModel()
        self.findings = QTableView()
        self.findings.setModel(self.findings_model)
        self.findings.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.findings.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.findings.verticalHeader().hide()
        self.findings.horizontalHeader().setStretchLastSection(True)
        self._show_table_rows(self.findings, 10)
        self.findings.selectionModel().currentRowChanged.connect(self._finding_selected)
        self.finding_detail = QPlainTextEdit()
        self.finding_detail.setReadOnly(True)
        self.finding_detail.setAccessibleName("Finding 详情")
        self._show_text_lines(self.finding_detail, 14)
        pane_minimum_width = self.fontMetrics().averageCharWidth() * 28
        self.findings.setMinimumWidth(pane_minimum_width)
        self.finding_detail.setMinimumWidth(pane_minimum_width)
        split.addWidget(self.findings)
        split.addWidget(self.finding_detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([620, 380])
        split.setMinimumHeight(
            max(self.findings.minimumHeight(), self.finding_detail.minimumHeight())
        )
        findings_layout.addWidget(findings_title)
        findings_layout.addWidget(split)

        self.notes_frame = QFrame()
        self.notes_frame.setProperty("fluentRole", "card")
        notes_layout = QVBoxLayout(self.notes_frame)
        notes_layout.setContentsMargins(16, 16, 16, 16)
        notes_layout.setSpacing(12)
        self.notes_title = QLabel("分歧、人工复核与审计说明")
        self.notes_title.setProperty("fluentType", "sectionTitle")
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setAccessibleName("分歧、人工核查和审计说明")
        self._show_text_lines(self.notes, 8)
        notes_layout.addWidget(self.notes_title)
        notes_layout.addWidget(self.notes)
        self.export_markdown_button = QPushButton("导出 Markdown")
        self.export_markdown_button.setObjectName("exportMarkdownButton")
        self.export_markdown_button.setAccessibleName("导出 Markdown 报告")
        self.export_markdown_button.setToolTip("将当前报告精确导出为 Markdown")
        self.export_markdown_button.setIcon(self._icon_or_null("arrow_download"))
        self.export_markdown_button.clicked.connect(lambda: self._request_report_export("markdown"))
        self.export_pdf_button = QPushButton("导出 PDF")
        self.export_pdf_button.setObjectName("exportPdfButton")
        self.export_pdf_button.setAccessibleName("导出 PDF 报告")
        self.export_pdf_button.setToolTip("将当前报告排版导出为正式 A4 PDF")
        self.export_pdf_button.setIcon(self._icon_or_null("arrow_download"))
        self.export_pdf_button.clicked.connect(lambda: self._request_report_export("pdf"))
        self.open_report_folder_button = QPushButton("打开报告目录")
        self.open_report_folder_button.setObjectName("openReportFolderButton")
        self.open_report_folder_button.setAccessibleName("打开报告目录")
        self.open_report_folder_button.setIcon(self._icon_or_null("folder"))
        self.open_report_folder_button.clicked.connect(self._open_folder)
        actions = QHBoxLayout()
        actions.addWidget(self.export_markdown_button)
        actions.addWidget(self.export_pdf_button)
        actions.addWidget(self.open_report_folder_button)
        actions.addStretch(1)
        report_widgets: tuple[QWidget, ...] = (
            self.report_metadata,
            self.hard_rule_review_frame,
            self.overall_summary,
            self.score_frame,
            self.unscored_message,
            self.diagnostic_frame,
            self.hard_rule_report_frame,
            self.panel_report_frame,
            self.decision_frame,
            self.disclaimers,
            self.findings_frame,
            self.notes_frame,
        )
        for report_widget in report_widgets:
            layout.addWidget(report_widget)
        layout.addLayout(actions)
        self._clear_policy_report()
        self._set_export_buttons_enabled(False)
        self.findings_splitter = split
        self.report_content = content
        self.report_scroll = self._scrollable_page(
            content,
            "runReportScroll",
            "评测报告可滚动内容",
        )
        return self.report_scroll

    def show_detail(self, detail: RunDetail, *, run_dir: Path) -> None:
        self._reset_report_context(False)
        self.run_id = detail.run.run_id
        self.run_dir = run_dir
        run = detail.run
        provider_source = (
            getattr(detail, "provider_snapshot", None)
            or getattr(run, "provider_snapshot", None)
            or detail
        )
        course_progress = self._is_course_detail(detail)
        self._set_course_progress_mode(course_progress)
        self.completed_stages = set(run.completed_stages)
        self._active_course_stage = None
        if course_progress:
            self._replay_course_stage_events(detail.events)
            if self.completed_stages.intersection(
                {"evidence", "reviews", "audit", "meta", "report"}
            ):
                self.completed_stages.add("metadata")
            status_stage = self._course_stage_for_status(run.status)
            if status_stage is not None:
                self._active_course_stage = status_stage
        self._last_progress_status = run.status
        self.set_cancel_pending(run.run_id, False)
        self.stack.setCurrentWidget(self.progress_page)
        self.run_metadata.setText(
            f"{Path(run.input_path).name} · "
            f"{provider_label(run.provider, run.model, provider_source)} · "
            f"{run.rubric_id} · 状态：{_display_value(run.status)}"
        )
        self._update_stages(sorted(self.completed_stages), run.status)
        self.events.setPlainText("\n".join(event.message for event in detail.events))
        self.cancel_button.setVisible(self._is_active_status(run.status))
        self.resume_button.setVisible(
            _status_value(run.status)
            in {
                "retryable_failure",
                "cancelled",
                "awaiting_hard_rule_confirmation",
                "awaiting_panel_review",
            }
        )
        pending = _first(detail, "pending_hard_rules", "hard_rule_assessments", "hard_rules")
        self._show_hard_rule_review(_pending_items(pending))
        status = _status_value(run.status)
        if status == "awaiting_hard_rule_confirmation":
            self.message.show_message(
                "这是旧版流程留下的中间门禁。点击“恢复评测”可先完成专家面板、"
                "Meta Review 和报告，人工复核将在报告页处理。",
                severity="warning",
            )
        elif status == "awaiting_panel_review":
            self.message.show_message(
                "这是旧版流程留下的人工面板门禁。点击“恢复评测”可先生成完整报告，"
                "随后在报告页处理人工复核。",
                severity="warning",
            )
        elif status in {"retryable_failure", "fatal_failure"}:
            recovery = (
                "可以从最近检查点恢复。"
                if status == "retryable_failure"
                else "该错误不可自动恢复，请查看日志和技术详情。"
            )
            self.message.show_message(
                f"任务失败：{run.error or '未知错误'}。{recovery}", severity="danger"
            )
        elif status == "cancelled":
            self.message.show_message("任务已取消，已完成的检查点仍然保留。", severity="warning")
        else:
            self.message.clear()
        self._scroll_to_top(self.progress_scroll)

    def append_event(self, event: RunEvent) -> None:
        if self.run_id and event.run_id != self.run_id:
            self.prepare_run(event.run_id, run_dir=self.run_dir)
        if not self.run_id:
            self.run_id = event.run_id
        self.events.appendPlainText(event.message)
        if event.event_type.startswith("submission_metadata_"):
            self._set_course_progress_mode(True)
        if event.stage and event.event_type.endswith("_completed"):
            self.completed_stages.add(event.stage)
            if self._active_course_stage == event.stage:
                self._active_course_stage = None
        elif (
            self._course_progress_mode
            and event.stage
            and event.event_type.endswith("_started")
        ):
            if event.stage != "metadata" and "metadata" not in self.completed_stages:
                # Once a later course stage starts, the local metadata artifact
                # necessarily exists even if the app stopped between writing it
                # and appending its completion trace event.
                self.completed_stages.add("metadata")
            self._active_course_stage = event.stage
        if event.status is not None:
            self._last_progress_status = event.status
            self.run_metadata.setText(f"任务 {event.run_id} · 状态：{_display_value(event.status)}")
            self.cancel_button.setVisible(self._is_active_status(event.status))
            self.resume_button.setVisible(
                _status_value(event.status) in {"retryable_failure", "cancelled"}
            )
            if _status_value(event.status) == "awaiting_hard_rule_confirmation":
                pending = _first(
                    event.payload,
                    "pending_hard_rules",
                    "hard_rule_assessments",
                    "hard_rules",
                )
                if pending is not None:
                    self._show_hard_rule_review(_pending_items(pending))
        elif self.run_id:
            self.cancel_button.show()
        self._update_stages(
            sorted(self.completed_stages),
            self._last_progress_status,
        )

    def show_report(self, report: ReportView, *, run_dir: Path) -> None:
        self._reset_report_context(True)
        presentation = ReportPresentation(report.rubric, report.presentation_profile)
        self._presentation = presentation if presentation.localized else None
        self.findings_model.set_presentation(self._presentation)
        self._set_review_busy(False)
        self.run_id = report.run.run_id
        self.run_dir = run_dir
        self._report_input_path = report.run.input_path
        provider_source = (
            getattr(report, "provider_snapshot", None)
            or getattr(report.run, "provider_snapshot", None)
            or report
        )
        self._set_export_buttons_enabled(True)
        self.stack.setCurrentWidget(self.report_page)
        course_report = getattr(report.rubric, "evaluation_mode", None) == "course_assessment"
        self._set_course_progress_mode(course_report)
        self._configure_report_mode(course_report)
        title = (
            report.document.title
            if report.document and report.document.title
            else Path(report.run.input_path).name
        )
        if course_report:
            metadata = report.submission_metadata
            paper_title = metadata.paper_title if metadata is not None else title
            student_name = metadata.student_name if metadata is not None else "未提取"
            student_id = metadata.student_id if metadata is not None else "未提取"
            major = metadata.major if metadata is not None else "未提取"
            self.report_metadata.setText(
                f"题目：{paper_title}\n"
                f"姓名：{student_name} · 学号：{student_id} · 专业：{major}\n"
                f"评价标准：{report.rubric.title} ({report.rubric.version}) · "
                f"{provider_label(report.run.provider, report.run.model, provider_source)}"
            )
        else:
            self.report_metadata.setText(
                f"{title} · {report.rubric.title} ({report.rubric.version}) · "
                f"{provider_label(report.run.provider, report.run.model, provider_source)}"
            )
        self.overall_summary.setText(
            self._presentation.narrative(report.review.overall_summary)
            if self._presentation is not None
            else report.review.overall_summary
        )
        if course_report and report.rubric.scoring_enabled:
            self.score_frame.show()
            self.unscored_message.hide()
            self._render_course_score_card(report)
        elif report.evaluation is not None:
            # v2 uses an experimental diagnostic score and a separate policy
            # risk decision. Do not present the legacy course-style verdict card.
            self.score_frame.hide()
            self.unscored_message.hide()
        elif report.rubric.scoring_enabled:
            self.score_frame.show()
            self.unscored_message.hide()
            verdict = report.review.verdict or "未设置结论"
            score = report.review.total_score
            self.total_score.setText(f"{score:g} 分\n{verdict}" if score is not None else verdict)
            titles = {item.dimension_id: item.title for item in report.rubric.dimensions}
            self.dimension_scores.setText(
                "\n".join(
                    f"{titles.get(key, key)}：{value:g}"
                    for key, value in report.dimension_scores.items()
                )
            )
        else:
            self.score_frame.hide()
            self.unscored_message.show_message(
                "当前 Rubric 仅提供评语，不生成分数。", severity="info"
            )
        if course_report:
            self._render_course_report(report)
            pending_rules: list[Any] = []
            panel_review_required = False
            self._show_hard_rule_review([])
        else:
            self._render_policy_report(report)
            review_summary = _first(report, "human_review_summary")
            panel_review_required = bool(
                _first(review_summary, "panel_review_required", default=False)
            )
            pending_rules = _pending_items(
                _first(report, "pending_hard_rules", default=[])
            )
            evaluation_source = _first(report, "evaluation", default=report)
            self._show_hard_rule_review(
                pending_rules,
                panel_review_required=panel_review_required,
                panel_review_detail=_format_panel_review_detail(
                    evaluation_source, self._presentation
                ),
            )
        self.findings_model.set_items(report.review.findings)
        self.findings.resizeColumnsToContents()
        if report.review.findings:
            self.findings.selectRow(0)
            self._set_finding_detail(report.review.findings[0])
        else:
            self.finding_detail.setPlainText("没有 Finding。")
        notes: list[str] = []
        if report.review.disagreements:
            notes.append(
                "Reviewer 分歧\n"
                + "\n".join(
                    f"• {self._localized_narrative(x)}"
                    for x in report.review.disagreements
                )
            )
        if report.review.human_checks:
            notes.append(
                ("需要教师关注\n" if course_report else "人工核查\n")
                + "\n".join(
                    f"• {self._localized_narrative(x)}" for x in report.review.human_checks
                )
            )
        if report.audit.errors or report.audit.warnings:
            notes.append(
                "审计说明\n"
                + "\n".join(
                    [
                        *(f"错误：{self._localized_narrative(x)}" for x in report.audit.errors),
                        *(f"警告：{self._localized_narrative(x)}" for x in report.audit.warnings),
                    ]
                )
            )
        empty_notes = (
            "没有额外的分歧或审计说明。"
            if course_report
            else "没有额外的分歧、人工核查或审计说明。"
        )
        self.notes.setPlainText("\n\n".join(notes) or empty_notes)
        self._scroll_to_top(self.report_scroll)
        if pending_rules or panel_review_required:
            pending_count = len(pending_rules) + int(panel_review_required)
            self.message.show_message(
                f"AI 评测和报告已完成，仍有 {pending_count} 项人工复核。"
                "当前风险结论待定，但可以导出带待定标记的报告。",
                severity="warning",
            )
        else:
            self.message.clear()

    def _configure_report_mode(self, course_report: bool) -> None:
        """在不改变控件身份的前提下切换报告语义。"""

        if course_report:
            self.diagnostic_title.setText("六项课程评价维度")
            self.diagnostic_scores_model.setHorizontalHeaderLabels(
                ["课程评价维度", "得分（0–100）", "权重", "加权贡献"]
            )
            self.diagnostic_scores.setAccessibleName("六项课程评价维度得分")
            self.notes_title.setText("分歧与审计说明")
            self.notes.setAccessibleName("课程评测分歧与审计说明")
        else:
            self.diagnostic_title.setText("九项诊断评分（0–4）")
            self.diagnostic_scores_model.setHorizontalHeaderLabels(
                ["指标", "分组", "等级", "加权贡献"]
            )
            self.diagnostic_scores.setAccessibleName("诊断评分明细")
            self.notes_title.setText("分歧、人工复核与审计说明")
            self.notes.setAccessibleName("分歧、人工核查和审计说明")
        for frame in (
            self.hard_rule_report_frame,
            self.panel_report_frame,
            self.decision_frame,
        ):
            frame.setVisible(not course_report)

    def _render_course_score_card(self, report: ReportView) -> None:
        total = report.review.total_score
        passing_score = (
            report.rubric.aggregation.passing_score
            if report.rubric.aggregation is not None
            else None
        )
        grade = _course_grade(total)
        conclusion = _course_conclusion(total, passing_score, report.review.verdict)
        total_text = f"{_score_text(total)} 分" if total is not None else "暂无"
        self.total_score.setText(f"课程总分\n{total_text}")
        passing_text = (
            f"{_score_text(passing_score)} 分" if passing_score is not None else "未设置"
        )
        self.dimension_scores.setText(
            f"五级等级：{grade}\n"
            f"课程要求结论：{conclusion}\n"
            f"及格参考线：{passing_text}"
        )

    def _render_course_report(self, report: ReportView) -> None:
        self.diagnostic_scores_model.removeRows(0, self.diagnostic_scores_model.rowCount())
        dimensions_by_id = {
            dimension.dimension_id: dimension for dimension in report.rubric.dimensions
        }
        for dimension in report.rubric.dimensions:
            score = report.dimension_scores.get(dimension.dimension_id)
            contribution = score * dimension.weight / 100 if score is not None else None
            row = (
                dimension.title,
                _score_text(score),
                f"{_score_text(dimension.weight)}%",
                _score_text(contribution),
            )
            items = [QStandardItem(value) for value in row]
            for item in items:
                item.setEditable(False)
            self.diagnostic_scores_model.appendRow(items)
        for dimension_id, score in report.dimension_scores.items():
            if dimension_id in dimensions_by_id:
                continue
            title = (
                self._presentation.dimension(dimension_id)
                if self._presentation is not None
                else "未命名指标"
            )
            items = [
                QStandardItem(title),
                QStandardItem(_score_text(score)),
                QStandardItem("未提供"),
                QStandardItem("未提供"),
            ]
            for item in items:
                item.setEditable(False)
            self.diagnostic_scores_model.appendRow(items)
        self.diagnostic_scores.resizeColumnsToContents()
        dimension_count = len(report.rubric.dimensions)
        self.diagnostic_summary.setText(
            f"本报告按当前课程评价标准的 {dimension_count} 个评价维度加权汇总。"
        )
        total = report.review.total_score
        passing_score = (
            report.rubric.aggregation.passing_score
            if report.rubric.aggregation is not None
            else None
        )
        self.experimental_score.setText(
            f"课程总分：{_score_text(total)} 分 · "
            f"五级等级：{_course_grade(total)} · "
            f"课程要求结论："
            f"{_course_conclusion(total, passing_score, report.review.verdict)}"
        )
        self.disclaimers.setText(
            "使用说明：\n"
            "• 本报告为 AI 辅助课程论文评测结果，最终成绩由任课教师确定。\n"
            "• 五级等级和及格参考线来自当前课程评价标准，使用前应结合课程大纲确认。\n"
            "• 系统不考察专业培养目标，也不据此认定学术不端。\n"
            "• 模型置信度是未经校准的自评，不作为统计概率。"
        )

    def _icon_or_null(self, name: str) -> QIcon:
        """Return a theme icon while allowing optional packaged icons to lag."""

        try:
            return self.icons.icon(name)
        except (OSError, ValueError, FileNotFoundError):
            return QIcon()

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        """Make a paper-derived name safe for Windows and portable exports."""

        import re

        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
        if not cleaned:
            cleaned = "论文"
        if cleaned.casefold() in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }:
            cleaned = f"_{cleaned}"
        return cleaned

    def _reset_report_context(self, available: bool) -> None:
        """Invalidate pending export UI whenever the displayed run changes."""

        self._report_generation += 1
        self._report_available = available
        self._report_input_path = ""
        self._exported_report_path = None
        self._export_trigger_button = None
        self._presentation = None
        if hasattr(self, "findings_model"):
            self.findings_model.set_presentation(None)
        self._set_export_busy(False)
        if hasattr(self, "message"):
            self.message.clear()
        if hasattr(self, "export_markdown_button"):
            self._set_export_buttons_enabled(available)

    def reset_report_export_state(self) -> None:
        """Clear transient report export state before navigation or run switching."""

        self._reset_report_context(False)

    def report_export_generation(self) -> int:
        return self._report_generation

    def _set_export_buttons_enabled(self, enabled: bool) -> None:
        for button in (self.export_markdown_button, self.export_pdf_button):
            button.setEnabled(enabled and not self._export_busy)
        self.open_report_folder_button.setEnabled(
            enabled and bool(self.run_dir) and not self._export_busy
        )

    def _set_export_busy(self, busy: bool, export_format: str = "") -> None:
        self._export_busy = busy
        if busy:
            self._export_format = export_format
        else:
            self._export_format = ""
        for button, value in (
            (self.export_markdown_button, busy and export_format == "markdown"),
            (self.export_pdf_button, busy and export_format == "pdf"),
        ):
            set_fluent_property(button, "fluentBusy", value)
            button.setAccessibleDescription("正在导出报告" if value else "")
        if hasattr(self, "export_markdown_button"):
            self._set_export_buttons_enabled(self._report_available)

    def _last_export_directory(self) -> Path:
        settings = QSettings(COURSE_ORGANIZATION_NAME, COURSE_SETTINGS_NAME)
        remembered = settings.value("reportExport/lastDirectory", "")
        if remembered:
            candidate = Path(str(remembered))
            if candidate.is_dir():
                return candidate
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        if documents:
            return Path(documents)
        return self.run_dir or Path.cwd()

    def _request_report_export(self, export_format: str) -> None:
        if self._export_busy or not self._report_available or not self.run_id:
            return
        suffix = ".md" if export_format == "markdown" else ".pdf"
        paper_path = PureWindowsPath(self._report_input_path)
        paper_stem = paper_path.stem or Path(self._report_input_path).stem or "论文"
        basename = self._sanitize_filename(paper_stem) + "_AI辅助评测报告"
        default_path = self._last_export_directory() / f"{basename}{suffix}"
        format_label = "Markdown" if export_format == "markdown" else "PDF"
        file_filter = "Markdown 文件 (*.md)" if export_format == "markdown" else "PDF 文件 (*.pdf)"
        dialog = QFileDialog(
            self,
            f"导出 {format_label} 报告",
            str(default_path),
            file_filter,
        )
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix(suffix.removeprefix("."))
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.selectFile(default_path.name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_files = dialog.selectedFiles()
        if not selected_files:
            return
        destination = Path(selected_files[0])
        if not destination.suffix:
            destination = destination.with_suffix(suffix)
        overwrite = destination.exists()
        if overwrite:
            answer = QMessageBox.question(
                self,
                "覆盖现有报告",
                f"文件“{destination.name}”已存在，确定要覆盖吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._export_trigger_button = (
            self.export_markdown_button if export_format == "markdown" else self.export_pdf_button
        )
        self._set_export_busy(True, export_format)
        self.report_export_requested.emit(
            self.run_id,
            export_format,
            str(destination),
            overwrite,
        )

    def report_export_succeeded(
        self,
        run_id: str,
        result: object,
        generation: int | None = None,
    ) -> bool:
        if (
            run_id != self.run_id
            or not self._report_available
            or (generation is not None and generation != self._report_generation)
        ):
            return False
        output = getattr(result, "output_path", getattr(result, "path", result))
        output_path = Path(str(output))
        self._exported_report_path = output_path
        QSettings(COURSE_ORGANIZATION_NAME, COURSE_SETTINGS_NAME).setValue(
            "reportExport/lastDirectory", str(output_path.parent)
        )
        self._set_export_busy(False)
        self.message.show_message(
            f"报告已导出：{output_path.name}",
            severity="success",
            action_text="打开文件",
        )
        self.message.action_button.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def report_export_failed(
        self,
        run_id: str,
        message: str,
        generation: int | None = None,
    ) -> bool:
        if (
            run_id != self.run_id
            or not self._report_available
            or (generation is not None and generation != self._report_generation)
        ):
            return False
        self._set_export_busy(False)
        self.message.show_message(f"报告导出失败：{message}", severity="danger")
        if self._export_trigger_button is not None:
            self._export_trigger_button.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def _open_exported_report(self) -> None:
        if self._exported_report_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._exported_report_path)))

    def _render_policy_report(self, report: ReportView) -> None:
        source = (
            _first(report, "evaluation_report", "evaluation", "policy_report", "scoring_report")
            or report
        )
        diagnostic = _first(
            source, "diagnostic_scores", "diagnostic_score", "criterion_assessments", "criteria"
        )
        self._render_diagnostic_scores(report, diagnostic, source)
        assessments = _first(source, "hard_rule_assessments", "hard_rules", "hard_rule_results")
        decisions = _first(source, "human_rule_decisions", "hard_rule_decisions", "human_decisions")
        self.hard_rule_report.setPlainText(
            _format_hard_report(assessments, decisions, self._presentation)
        )
        panel = _first(source, "panel_decision", "panel", "expert_panel")
        self.panel_report.setPlainText(_format_panel(panel, source, self._presentation))
        path = _first(
            source,
            "decision_path",
            "deterministic_decision_path",
            "risk_decision_path",
        )
        if path is None and panel is not None:
            path = _first(panel, "decision_path")
        lines = _format_lines(path, self._presentation)
        risk = _first(
            source,
            "risk_conclusion",
            "risk_verdict",
            "final_conclusion",
            "conclusion",
        )
        if risk is None and panel is not None:
            risk = _first(panel, "outcome", "verdict", "decision")
        if risk is not None:
            risk_text = (
                self._presentation.panel_outcome(risk)
                if self._presentation is not None
                else _display_value(risk)
            )
            lines.append(f"AI 辅助抽检风险结论：{risk_text}")
        self.decision_path.setPlainText("\n".join(lines) or "暂无结构化决策路径数据。")
        self.disclaimers.setText(
            "使用说明：\n"
            "• 本结果不是浙江省教育厅正式抽检结论。\n"
            "• 百分制和五级锚点为本项目自定义诊断规则。\n"
            "• 学术不端检测报告未由系统自动读取。\n"
            "• 模型置信度是未经校准的自评，不作为统计概率。"
        )

    def _render_diagnostic_scores(self, report: ReportView, diagnostic: Any, source: Any) -> None:
        self.diagnostic_scores_model.removeRows(0, self.diagnostic_scores_model.rowCount())
        score_source = diagnostic
        if score_source is not None:
            nested = _first(
                score_source,
                "assessments",
                "criterion_assessments",
                "criteria",
                "items",
            )
            if nested is not None:
                diagnostic = nested
        values = diagnostic if diagnostic is not None else report.dimension_scores
        if isinstance(values, Mapping):
            rows = [
                (
                    self._presentation.dimension(key)
                    if self._presentation is not None
                    else str(key),
                    "",
                    _display_value(value),
                    "",
                )
                for key, value in values.items()
            ]
        else:
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                values = [values] if values is not None else []
            rows = []
            dimensions = {item.dimension_id: item for item in report.rubric.dimensions}
            for item in values:
                criterion_id = _first(
                    item,
                    "criterion_id",
                    "dimension_id",
                    "id",
                    default="",
                )
                dimension = dimensions.get(str(criterion_id))
                title_value = _first(item, "title", "criterion_title", "dimension_title")
                title = (
                    self._presentation.dimension(criterion_id)
                    if self._presentation is not None
                    else _display_value(
                        title_value
                        or (dimension.title if dimension else None)
                        or criterion_id
                        or "未命名指标"
                    )
                )
                group_value = _first(item, "group", "group_title", "category", "group_id")
                if group_value is None and dimension is not None:
                    group_value = _first(dimension, "group", "group_id", "category")
                group = (
                    self._presentation.group(group_value)
                    if self._presentation is not None and group_value
                    else _display_value(group_value or "")
                )
                rating = _first(
                    item, "rating", "level", "score", "grade", "value", default="未提供"
                )
                contribution = _first(
                    item, "weighted_contribution", "contribution", "weighted_score", default=""
                )
                rows.append(
                    (
                        title,
                        group,
                        _display_value(rating),
                        _display_value(contribution) if contribution != "" else "",
                    )
                )
        for row in rows:
            items = [QStandardItem(value) for value in row]
            for item in items:
                item.setEditable(False)
            self.diagnostic_scores_model.appendRow(items)
        self.diagnostic_scores.resizeColumnsToContents()
        groups = _first(source, "group_scores", "diagnostic_group_scores")
        if groups is None and score_source is not None:
            groups = _first(score_source, "group_scores", "diagnostic_group_scores")
        summary_parts: list[str] = []
        for key, value in _items(groups):
            group_label = (
                self._presentation.group(key)
                if self._presentation is not None
                else _display_value(key)
            )
            summary_parts.append(f"{group_label}：{_display_value(value)}")
        summary = "；".join(summary_parts)
        self.diagnostic_summary.setText(f"分组得分：{summary}" if summary else "")
        total = _first(
            source, "experimental_total_score", "diagnostic_total_score", "diagnostic_score_total"
        )
        if total is None and score_source is not None:
            total = _first(
                score_source,
                "experimental_total_score",
                "diagnostic_total_score",
                "diagnostic_score_total",
                "total_score",
            )
        total = (
            total
            if total is not None
            else _first(report, "experimental_total_score", "diagnostic_total_score")
        )
        total = (
            total
            if total is not None
            else (report.review.total_score if report.rubric.scoring_enabled else None)
        )
        self.experimental_score.setText(
            f"实验性诊断总分：{_display_value(total)}（不设置及格线，不直接决定抽检风险结论）"
            if total is not None
            else "实验性诊断总分：暂无"
        )

    def _clear_policy_report(self) -> None:
        self.diagnostic_scores_model.removeRows(0, self.diagnostic_scores_model.rowCount())
        self.diagnostic_summary.clear()
        self.experimental_score.setText("实验性诊断总分：暂无")
        self.hard_rule_report.setPlainText("暂无结构化否决项数据。")
        self.panel_report.setPlainText("暂无独立专家面板数据。")
        self.decision_path.setPlainText("暂无结构化决策路径数据。")
        self.disclaimers.clear()

    def _stage_definitions(self) -> list[tuple[str, str]]:
        return self.COURSE_STAGES if self._course_progress_mode else self.STAGES

    def _set_course_progress_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._course_progress_mode == enabled:
            return
        self._course_progress_mode = enabled
        stages = self._stage_definitions()
        if hasattr(self, "stage_progress"):
            self.stage_progress.setRange(0, len(stages))
        if hasattr(self, "stage_list"):
            self._show_list_rows(self.stage_list, len(stages))

    @staticmethod
    def _is_course_detail(detail: RunDetail) -> bool:
        rubric_id = detail.run.rubric_id.partition("@")[0]
        return rubric_id == "course-paper-general-assessment" or any(
            event.event_type.startswith("submission_metadata_")
            for event in detail.events
        )

    def _replay_course_stage_events(self, events: list[RunEvent]) -> None:
        """Recover the metadata checkpoint and live stage from persisted trace."""

        active: str | None = None
        for event in events:
            stage = event.stage
            if not stage:
                continue
            if event.event_type.endswith("_completed"):
                self.completed_stages.add(stage)
                if active == stage:
                    active = None
            elif event.event_type.endswith("_started"):
                if stage != "metadata" and "metadata" not in self.completed_stages:
                    self.completed_stages.add("metadata")
                active = stage
        self._active_course_stage = active

    @staticmethod
    def _course_stage_for_status(status: Any) -> str | None:
        return {
            "ingesting": "ingest",
            "building_evidence": "evidence",
            "reviewing": "reviews",
            "auditing": "audit",
            "meta_reviewing": "meta",
            "synthesizing": "meta",
            "validating": "report",
        }.get(_status_value(status))

    def _update_stages(self, completed: list[str], status: Any) -> None:
        self.stage_model.clear()
        if self._course_progress_mode:
            current = self._active_course_stage or self._course_stage_for_status(status)
        else:
            current = {
                "ingesting": "ingest",
                "building_evidence": "evidence",
                "scoring": "scoring",
                "reviewing": "scoring",
                "auditing": "audit",
                "awaiting_hard_rule_confirmation": "panel",
                "panel_reviewing": "panel",
                "supplemental_reviewing": "panel",
                "awaiting_panel_review": "panel",
                "meta_reviewing": "meta",
                "synthesizing": "meta",
                "validating": "report",
            }.get(_status_value(status))
        stages = self._stage_definitions()
        for stage, label in stages:
            if stage in completed:
                prefix, state, icon = (
                    "已完成",
                    "completed",
                    self.icons.icon("check", color_role="success_foreground"),
                )
            elif stage == current:
                prefix, state, icon = (
                    "进行中",
                    "active",
                    self.icons.icon("refresh", color_role="brand_foreground"),
                )
            else:
                prefix, state, icon = (
                    "等待",
                    "pending",
                    self.icons.icon("info", color_role="text_tertiary"),
                )
            item = QStandardItem(icon, f"{prefix} · {label}")
            item.setEditable(False)
            item.setData(state)
            item.setAccessibleText(f"{label}，{prefix}")
            self.stage_model.appendRow(item)
        self.stage_progress.setValue(
            len(set(completed).intersection({stage for stage, _ in stages}))
        )

    def _show_hard_rule_review(
        self,
        rules: list[Any],
        *,
        panel_review_required: bool = False,
        panel_review_detail: str = "",
    ) -> None:
        self._pending_hard_rules = rules
        self._panel_review_required = panel_review_required
        self._panel_review_detail = panel_review_detail
        self.hard_rule_list.blockSignals(True)
        self.hard_rule_list.clear()
        for rule in rules:
            rule_id = str(_first(rule, "rule_id", "id", default="否决项"))
            raw_status = _first(rule, "status", "state", default="待复核")
            status = (
                self._presentation.hard_rule_status(raw_status)
                if self._presentation is not None
                else _display_value(raw_status)
            )
            description = (
                self._presentation.rule(rule_id)
                if self._presentation is not None
                else str(_first(rule, "description", "title", "rule", default=rule_id))
            )
            if rule_id in self._submitted_hard_rules:
                status = "已提交"
            item = QListWidgetItem(f"{status} · {description}")
            item.setData(Qt.ItemDataRole.UserRole, rule)
            item.setToolTip(
                description
                if self._presentation is not None
                else f"{rule_id}：{description}"
            )
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{description}，状态：{status}",
            )
            good = status in {"已驳回", "确认不成立", "dismissed", "已提交"}
            item.setIcon(
                self.icons.icon(
                    "check" if good else "warning",
                    color_role="success_foreground" if good else "warning_foreground",
                )
            )
            self.hard_rule_list.addItem(item)
        if panel_review_required:
            item = QListWidgetItem("待复核 · 专家面板无法判断")
            item.setData(Qt.ItemDataRole.UserRole, {"kind": "panel_review"})
            item.setToolTip("专家面板无法判断，需要人工给出最终风险结论")
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                "专家面板无法判断，状态：待人工复核",
            )
            item.setIcon(self.icons.icon("warning", color_role="warning_foreground"))
            self.hard_rule_list.addItem(item)
        self.hard_rule_list.blockSignals(False)
        has_reviews = bool(rules) or panel_review_required
        self.hard_rule_review_frame.setVisible(has_reviews)
        if has_reviews:
            self.hard_rule_list.setCurrentRow(0)
        else:
            self.hard_rule_detail.clear()
            self._update_review_actions()

    def _hard_rule_selected(self, row: int) -> None:
        if 0 <= row < len(self._pending_hard_rules):
            self.hard_rule_detail.setPlainText(
                _format_hard_detail(
                    self._pending_hard_rules[row], self._presentation
                )
            )
            self.hard_rule_error.clear()
            self.confirm_rule_button.setText("确认成立")
            self.dismiss_rule_button.setText("确认不成立")
            self.confirm_rule_button.setAccessibleName("确认否决项成立")
            self.dismiss_rule_button.setAccessibleName("确认否决项不成立")
        elif self._panel_review_required and row == len(self._pending_hard_rules):
            self.hard_rule_detail.setPlainText(
                self._panel_review_detail
                or "至少一名 AI 专家无法完成判断，请人工面板结合完整报告给出结论。"
            )
            self.hard_rule_error.clear()
            self.confirm_rule_button.setText("触发风险")
            self.dismiss_rule_button.setText("未触发风险")
            self.confirm_rule_button.setAccessibleName("人工面板确认触发风险")
            self.dismiss_rule_button.setAccessibleName("人工面板确认未触发风险")
        else:
            self.hard_rule_detail.clear()
        self._update_review_actions()

    def _update_review_actions(self) -> None:
        enabled = (
            not self._review_busy
            and self.hard_rule_list.currentRow() >= 0
            and bool(self.hard_rule_reviewer_input.text().strip())
            and bool(self.hard_rule_reason_input.toPlainText().strip())
        )
        self.confirm_rule_button.setEnabled(enabled)
        self.dismiss_rule_button.setEnabled(enabled)

    def _submit_hard_rule(self, confirmed: bool) -> None:
        if self._review_busy:
            return
        row = self.hard_rule_list.currentRow()
        reviewer = self.hard_rule_reviewer_input.text().strip()
        reason = self.hard_rule_reason_input.toPlainText().strip()
        item_count = len(self._pending_hard_rules) + int(self._panel_review_required)
        if row < 0 or row >= item_count:
            self.hard_rule_error.setText("请选择需要处理的人工复核待办。")
            return
        if not reviewer:
            self.hard_rule_error.setText("请填写复核人。")
            self.hard_rule_reviewer_input.setFocus()
            return
        if not reason:
            self.hard_rule_error.setText("请填写复核理由。")
            self.hard_rule_reason_input.setFocus()
            return
        if row == len(self._pending_hard_rules) and self._panel_review_required:
            panel_decision_payload: dict[str, object] = {
                "outcome": "risk_triggered" if confirmed else "risk_not_triggered",
                "reviewer": reviewer,
                "rationale": reason,
                "decided_at": datetime.now(UTC),
            }
            self._set_review_busy(True)
            self.hard_rule_error.setText("正在保存人工面板结论并更新报告…")
            self.panel_review_resolution_requested.emit(
                self.run_id, panel_decision_payload
            )
            if self.service is not None:
                self._invoke_service(
                    "resolve_panel_review",
                    (self.run_id, panel_decision_payload),
                    lambda _value: self._mark_panel_submitted(confirmed),
                )
            return

        rule = self._pending_hard_rules[row]
        rule_id = str(_first(rule, "rule_id", "id", default=f"rule-{row}"))
        decision: dict[str, object] = {
            "rule_id": rule_id,
            "confirmed": confirmed,
            "decision": "confirmed" if confirmed else "dismissed",
            "reviewer": reviewer,
            "reviewer_id": reviewer,
            "reason": reason,
            "rationale": reason,
            "decided_at": datetime.now(UTC),
            "reviewed_at": datetime.now(UTC).isoformat(),
        }
        self._submitted_hard_rules.add(rule_id)
        self._set_review_busy(True)
        self.hard_rule_error.setText("正在保存人工决定并更新报告…")
        self.hard_rule_resolution_requested.emit(self.run_id, decision)
        if self.service is None:
            return
        self._invoke_service(
            "resolve_hard_rule",
            (self.run_id, _make_decision(decision)),
            lambda _value: self._after_resolved(rule_id, confirmed),
        )

    def _after_resolved(self, rule_id: str, confirmed: bool) -> None:
        self._mark_rule_submitted(rule_id, confirmed)

    def _mark_rule_submitted(self, rule_id: str, confirmed: bool) -> None:
        self._set_review_busy(False)
        rule_label = (
            self._presentation.rule(rule_id)
            if self._presentation is not None
            else rule_id
        )
        self.hard_rule_error.setText(
            f"已保存：{rule_label} · {'确认成立' if confirmed else '确认不成立'}。报告已更新。"
        )
        item = self.hard_rule_list.currentItem()
        if item is not None:
            item.setText(f"已提交 · {item.toolTip().split('：', 1)[-1]}")
            item.setIcon(self.icons.icon("check", color_role="success_foreground"))
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{item.toolTip()}，状态：已提交",
            )

    def _mark_panel_submitted(self, triggered: bool) -> None:
        self._set_review_busy(False)
        conclusion = "触发风险" if triggered else "未触发风险"
        self.hard_rule_error.setText(f"已保存人工面板结论：{conclusion}。报告已更新。")
        item = self.hard_rule_list.currentItem()
        if item is not None:
            item.setText(f"已提交 · 人工面板结论：{conclusion}")
            item.setIcon(self.icons.icon("check", color_role="success_foreground"))

    def _set_review_busy(self, busy: bool) -> None:
        self._review_busy = busy
        for button in (self.confirm_rule_button, self.dismiss_rule_button):
            set_fluent_property(button, "fluentBusy", busy)
            button.setEnabled(False)
            button.setAccessibleDescription("正在保存人工复核决定" if busy else "")
        self.hard_rule_reviewer_input.setEnabled(not busy)
        self.hard_rule_reason_input.setEnabled(not busy)
        if not busy:
            self._update_review_actions()

    def _invoke_service(self, name: str, args: tuple[Any, ...], on_success: Any) -> None:
        method = getattr(self.service, name, None) if self.service is not None else None
        if not callable(method):
            self._service_failed(f"服务接口暂不可用：{name}")
            return
        try:
            result = method(*args)
        except TypeError:
            try:
                result = (
                    method(run_id=args[0], decision=args[1])
                    if len(args) > 1
                    else method(run_id=args[0])
                )
            except Exception as error:
                self._service_failed(str(error))
                return
        except Exception as error:
            self._service_failed(str(error))
            return
        if inspect.isawaitable(result):
            from paper_reviewer.gui.worker import AsyncTaskThread

            worker = AsyncTaskThread(lambda _emit: result)
            self._operation_threads.append(worker)
            worker.completed.connect(on_success)
            worker.failed.connect(lambda message, _trace: self._service_failed(message))
            worker.finished.connect(lambda: self._forget_operation(worker))
            worker.start()
        else:
            on_success(result)

    def _service_failed(self, message: str) -> None:
        self._set_review_busy(False)
        self.hard_rule_error.setText(f"保存人工决定失败：{message}")
        self.message.show_message(f"人工复核未保存：{message}", severity="danger")

    def show_human_review_error(self, run_id: str, message: str) -> None:
        if run_id == self.run_id:
            self._service_failed(message)

    def _forget_operation(self, worker: Any) -> None:
        if worker in self._operation_threads:
            self._operation_threads.remove(worker)
        worker.deleteLater()

    def _finding_selected(self, current: object, _previous: object) -> None:
        from PySide6.QtCore import QModelIndex

        if isinstance(current, QModelIndex):
            finding = self.findings_model.finding(current.row())
            if finding is not None:
                self._set_finding_detail(finding)

    def _set_finding_detail(self, finding: ReviewFinding) -> None:
        paper = (
            "\n".join(
                f"• 第 {ref.page or '?'} 页，块 {ref.block_id}: {ref.quote or ''}"
                for ref in finding.paper_evidence
            )
            or "无"
        )
        external = (
            "\n".join(
                f"• {ref.title or ref.doi or ref.url or ref.evidence_id}"
                f"（证据等级 {ref.level.value}）"
                for ref in finding.external_evidence
            )
            or "无"
        )
        self.finding_detail.setPlainText(
            f"问题\n{self._localized_narrative(finding.claim)}\n\n"
            f"解释\n{self._localized_narrative(finding.rationale)}\n\n"
            f"修改建议\n{self._localized_narrative(finding.recommendation)}\n\n"
            f"论文证据\n{paper}\n\n外部证据\n{external}"
        )

    def _localized_narrative(self, value: Any) -> str:
        return (
            self._presentation.narrative(value)
            if self._presentation is not None
            else _display_value(value)
        )

    def _cancel(self) -> None:
        if self.run_id and not self._cancel_pending:
            self.cancel_requested.emit(self.run_id)

    def set_cancel_pending(self, run_id: str, pending: bool) -> None:
        if run_id and self.run_id and run_id != self.run_id:
            return
        self._cancel_pending = pending
        self.cancel_button.setEnabled(not pending)
        self.cancel_button.setText("正在取消…" if pending else "取消评测")
        self.cancel_button.setAccessibleName("正在取消当前评测" if pending else "取消当前评测")
        set_fluent_property(self.cancel_button, "fluentBusy", pending)
        if pending:
            self.message.show_message(
                "正在安全停止评测并保存已完成的检查点…",
                severity="info",
            )

    def show_cancel_error(self, run_id: str, message: str) -> None:
        if run_id and self.run_id != run_id:
            return
        self.set_cancel_pending(run_id, False)
        self.message.show_message(f"取消评测失败：{message}", severity="danger")

    def _resume(self) -> None:
        if self.run_id:
            self.resume_requested.emit(self.run_id)

    def _open_folder(self) -> None:
        if self.run_dir is not None and self.run_dir.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_dir)))

    def _is_active_status(self, status: Any) -> bool:
        return _status_value(status) in self._ACTIVE_VALUES
