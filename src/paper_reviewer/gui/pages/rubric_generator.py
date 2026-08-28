from __future__ import annotations

from itertools import pairwise
from typing import Literal, cast

from pydantic import ValidationError
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.rubric_generator import (
    compile_rubric_generation,
    default_rubric_draft,
)
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.domain.rubric_generation import (
    CourseAssessmentBrief,
    DimensionPreference,
    ReviewerRole,
    RubricGenerationRequest,
    RubricGenerationResult,
    SavedRubricPackage,
    ScoringSettings,
    SubjectAssessmentMode,
)
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import (
    ProviderDisplay,
    provider_connections,
    provider_has_key,
    provider_protocol_text,
)
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.new_review_validation import model_choices
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, RubricPreview
from paper_reviewer.gui.worker import AsyncTaskThread

_ROLE_OPTIONS = (
    ("课程要求", "course_requirements"),
    ("专业内容", "subject_matter"),
    ("论证与结构", "argumentation"),
    ("写作与引用", "writing_norms"),
)


class DimensionPreferenceRow(QWidget):
    changed = Signal()
    remove_requested = Signal(object)

    def __init__(self, title: str, weight: int, role: str) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.title = QLineEdit(title)
        self.title.setAccessibleName("评价维度名称")
        self.weight = QSpinBox()
        self.weight.setRange(1, 100)
        self.weight.setSuffix("%")
        self.weight.setValue(weight)
        self.weight.setAccessibleName(f"{title}权重")
        self.role = QComboBox()
        self.role.setAccessibleName(f"{title} Reviewer 分工")
        for label, value in _ROLE_OPTIONS:
            self.role.addItem(label, value)
        self.role.setCurrentIndex(max(0, self.role.findData(role)))
        self.remove_button = QPushButton("删除")
        self.remove_button.setAccessibleName(f"删除评价维度{title}")
        self.remove_button.setProperty("fluentAppearance", "subtle")
        row.addWidget(self.title, 2)
        row.addWidget(self.weight)
        row.addWidget(self.role, 1)
        row.addWidget(self.remove_button)
        self.title.textChanged.connect(self.changed)
        self.weight.valueChanged.connect(self.changed)
        self.role.currentIndexChanged.connect(self.changed)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))

    def value(self) -> DimensionPreference:
        return DimensionPreference(
            title=self.title.text(),
            weight=self.weight.value(),
            reviewer_role=cast(ReviewerRole, str(self.role.currentData())),
        )

    def set_invalid(self, invalid: bool, description: str = "") -> None:
        set_fluent_property(self.title, "fluentInvalid", invalid)
        set_fluent_property(self.weight, "fluentInvalid", invalid)
        self.title.setAccessibleDescription(description)
        self.weight.setAccessibleDescription(description)


class RubricGeneratorWidget(QWidget):
    package_saved = Signal(object, bool)

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        icons: FluentIconService,
        *,
        operation_registry: AsyncOperationRegistry | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("rubricGenerator")
        self.service = service
        self.preferences = preferences
        self.operation_registry = operation_registry
        self._workers: list[AsyncTaskThread] = []
        self._worker: AsyncTaskThread | None = None
        self._current_result: RubricGenerationResult | None = None
        self._saved_package: SavedRubricPackage | None = None
        self._parent_package_id: str | None = None
        self._pending_revision = False
        self._busy = False
        self._building_dimensions = False
        self._dimensions_pristine = True
        self.dimension_rows: list[DimensionPreferenceRow] = []
        self._provider_catalog: list[ProviderDisplay] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        self.message = MessageBar(icons)
        root.addWidget(self.message)

        self.step_label = QLabel()
        self.step_label.setObjectName("rubricGeneratorStepLabel")
        self.step_label.setProperty("fluentType", "sectionTitle")
        root.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.setObjectName("rubricGeneratorSteps")
        self.stack.addWidget(self._course_page())
        self.stack.addWidget(self._subject_page())
        self.stack.addWidget(self._dimensions_page())
        self.stack.addWidget(self._scoring_page())
        self.stack.addWidget(self._generate_page())
        root.addWidget(self.stack, 1)

        navigation = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.setObjectName("rubricGeneratorBack")
        self.back_button.clicked.connect(self._back)
        self.next_button = QPushButton("下一步")
        self.next_button.setObjectName("rubricGeneratorNext")
        self.next_button.setProperty("fluentAppearance", "primary")
        self.next_button.clicked.connect(self._next)
        self.cancel_button = QPushButton("取消生成")
        self.cancel_button.setObjectName("rubricGeneratorCancel")
        self.cancel_button.setProperty("fluentAppearance", "outline")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        navigation.addWidget(self.back_button)
        navigation.addStretch(1)
        navigation.addWidget(self.cancel_button)
        navigation.addWidget(self.next_button)
        root.addLayout(navigation)

        self.subject_mode.currentIndexChanged.connect(self._subject_mode_changed)
        self.passing_enabled.toggled.connect(self.passing_score.setEnabled)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self._subject_mode_changed()
        self.refresh_providers()
        self._show_step(0)
        self._connect_design_changes()
        self._set_tab_order()

    def _course_page(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        description = QLabel(
            "先告诉系统这门课希望学生完成什么。课程要求越具体，生成的评价标准越可靠。"
        )
        description.setWordWrap(True)
        description.setProperty("fluentType", "secondary")
        layout.addWidget(description)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.course_name = QLineEdit()
        self.course_name.setObjectName("rubricCourseName")
        self.course_name.setPlaceholderText("例如：数据库原理")
        self.course_name.setAccessibleName("课程名称")
        self.course_level = QComboBox()
        self.course_level.setObjectName("rubricCourseLevel")
        self.course_level.addItem("本科", "undergraduate")
        self.course_level.addItem("专科", "college")
        self.course_level.addItem("研究生", "graduate")
        self.assignment_requirements = QPlainTextEdit()
        self.assignment_requirements.setObjectName("rubricAssignmentRequirements")
        self.assignment_requirements.setPlaceholderText(
            "粘贴作业要求、必须完成的任务、篇幅或成果形式。"
        )
        self.assignment_requirements.setAccessibleName("课程论文作业要求")
        self.learning_outcomes = QPlainTextEdit()
        self.learning_outcomes.setObjectName("rubricLearningOutcomes")
        self.learning_outcomes.setPlaceholderText("每行一个学习目标")
        self.learning_outcomes.setAccessibleName("课程学习目标")
        form.addRow("课程名称", self.course_name)
        form.addRow("学生层次", self.course_level)
        form.addRow("作业要求", self.assignment_requirements)
        form.addRow("学习目标", self.learning_outcomes)
        layout.addLayout(form)
        layout.addStretch(1)
        return self._scroll(content)

    def _subject_page(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        description = QLabel(
            "选择专业内容评价的深度。启用后，标准只依据您提供的学习目标和知识点，不自行扩展课程边界。"
        )
        description.setWordWrap(True)
        description.setProperty("fluentType", "secondary")
        layout.addWidget(description)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.subject_mode = QComboBox()
        self.subject_mode.setObjectName("rubricSubjectMode")
        self.subject_mode.addItem("不评测专业内容", SubjectAssessmentMode.NONE)
        self.subject_mode.addItem("基础课程知识评测", SubjectAssessmentMode.BASIC)
        self.subject_mode.addItem("专业深度评测", SubjectAssessmentMode.SPECIALIST)
        self.subject_mode.setCurrentIndex(1)
        self.subject_name = QLineEdit()
        self.subject_name.setObjectName("rubricSubjectName")
        self.subject_name.setPlaceholderText("例如：计算机科学与技术 / 教育学 / 经济学")
        self.subject_name.setAccessibleName("课程所属专业或领域")
        self.core_topics = QPlainTextEdit()
        self.core_topics.setObjectName("rubricCoreTopics")
        self.core_topics.setPlaceholderText("每行一个核心概念、理论、方法或技术")
        self.core_topics.setAccessibleName("课程核心知识点")
        self.common_errors = QPlainTextEdit()
        self.common_errors.setObjectName("rubricCommonErrors")
        self.common_errors.setPlaceholderText("每行一个常见严重错误，可留空")
        self.common_errors.setAccessibleName("专业内容常见错误")
        self.external_evidence = QCheckBox("评价时要求使用外部资料核验专业事实或数据")
        self.external_evidence.setObjectName("rubricExternalEvidence")
        form.addRow("专业评价方式", self.subject_mode)
        form.addRow("课程领域", self.subject_name)
        form.addRow("核心知识点", self.core_topics)
        form.addRow("常见错误", self.common_errors)
        form.addRow("", self.external_evidence)
        layout.addLayout(form)
        warning = QLabel(
            "专业深度评测只能辅助教师筛查问题，不能替代任课教师或领域专家作出最终判断。"
        )
        warning.setWordWrap(True)
        warning.setProperty("fluentType", "secondary")
        layout.addWidget(warning)
        layout.addStretch(1)
        return self._scroll(content)

    def _dimensions_page(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        description = QLabel(
            "确认评价维度、权重和 Reviewer 分工。模型只能补充描述、检查点和等级标准，"
            "不能改变这里确认的内容。"
        )
        description.setWordWrap(True)
        description.setProperty("fluentType", "secondary")
        layout.addWidget(description)
        headings = QHBoxLayout()
        for text, stretch in (("评价维度", 2), ("权重", 0), ("Reviewer 分工", 1), ("", 0)):
            label = QLabel(text)
            label.setProperty("fluentType", "bodyStrong")
            headings.addWidget(label, stretch)
        layout.addLayout(headings)
        self.dimension_rows_layout = QVBoxLayout()
        self.dimension_rows_layout.setSpacing(8)
        layout.addLayout(self.dimension_rows_layout)
        actions = QHBoxLayout()
        self.add_dimension_button = QPushButton("添加维度")
        self.add_dimension_button.setObjectName("addRubricDimension")
        self.add_dimension_button.clicked.connect(self._add_blank_dimension)
        self.dimension_total = QLabel()
        self.dimension_total.setObjectName("rubricDimensionWeightTotal")
        actions.addWidget(self.add_dimension_button)
        actions.addStretch(1)
        actions.addWidget(self.dimension_total)
        layout.addLayout(actions)
        layout.addStretch(1)
        return self._scroll(content)

    def _scoring_page(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        description = QLabel(
            "总分固定折算为 100 分；各维度可以使用 0–100、0–10 或 0–5 等整数范围。"
        )
        description.setWordWrap(True)
        description.setProperty("fluentType", "secondary")
        layout.addWidget(description)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        score_range = QWidget()
        score_range_layout = QHBoxLayout(score_range)
        score_range_layout.setContentsMargins(0, 0, 0, 0)
        self.minimum_score = QSpinBox()
        self.minimum_score.setRange(-100, 999)
        self.minimum_score.setValue(0)
        self.minimum_score.setAccessibleName("各维度评分下限")
        self.maximum_score = QSpinBox()
        self.maximum_score.setRange(-99, 1000)
        self.maximum_score.setValue(100)
        self.maximum_score.setAccessibleName("各维度评分上限")
        score_range_layout.addWidget(self.minimum_score)
        score_range_layout.addWidget(QLabel("至"))
        score_range_layout.addWidget(self.maximum_score)
        score_range_layout.addStretch(1)
        self.anchor_count = QComboBox()
        self.anchor_count.addItem("五级：缺失 / 不足 / 基本 / 良好 / 优秀", 5)
        self.anchor_count.addItem("四级：未达到 / 基本 / 充分 / 突出", 4)
        passing = QWidget()
        passing_layout = QHBoxLayout(passing)
        passing_layout.setContentsMargins(0, 0, 0, 0)
        self.passing_enabled = QCheckBox("设置及格线")
        self.passing_enabled.setChecked(True)
        self.passing_score = QDoubleSpinBox()
        self.passing_score.setRange(0, 100)
        self.passing_score.setValue(60)
        self.passing_score.setSuffix(" 分")
        self.passing_score.setAccessibleName("课程总分及格线")
        passing_layout.addWidget(self.passing_enabled)
        passing_layout.addWidget(self.passing_score)
        passing_layout.addStretch(1)
        self.additional_instructions = QPlainTextEdit()
        self.additional_instructions.setObjectName("rubricAdditionalInstructions")
        self.additional_instructions.setPlaceholderText(
            "例如：优秀档必须比较至少两种课程理论；不要评价创新性。"
        )
        self.additional_instructions.setAccessibleName("评价标准附加要求")
        form.addRow("维度评分范围", score_range)
        form.addRow("评分等级", self.anchor_count)
        form.addRow("课程总分", QLabel("固定折算为 100 分"))
        form.addRow("及格判断", passing)
        form.addRow("附加要求", self.additional_instructions)
        layout.addLayout(form)
        layout.addStretch(1)
        return self._scroll(content)

    def _generate_page(self) -> QScrollArea:
        content = QWidget()
        layout = QVBoxLayout(content)
        provider_form = QFormLayout()
        provider_form.setVerticalSpacing(12)
        self.provider = QComboBox()
        self.provider.setObjectName("rubricGeneratorProvider")
        self.provider.setAccessibleName("Rubric 生成 Provider")
        self.model = QComboBox()
        self.model.setObjectName("rubricGeneratorModel")
        self.model.setEditable(True)
        self.model.setAccessibleName("Rubric 生成模型")
        self.provider_info = QLabel()
        self.provider_info.setWordWrap(True)
        self.provider_info.setProperty("fluentType", "secondary")
        provider_form.addRow("Provider", self.provider)
        provider_form.addRow("模型", self.model)
        provider_form.addRow("", self.provider_info)
        layout.addLayout(provider_form)
        model_notice = QLabel("点击生成后，课程要求会发送给所选云端模型；不会发送学生论文。")
        model_notice.setWordWrap(True)
        model_notice.setProperty("fluentType", "secondary")
        layout.addWidget(model_notice)
        generate_actions = QHBoxLayout()
        self.local_template_button = QPushButton("使用基础模板")
        self.local_template_button.setObjectName("useLocalRubricTemplate")
        self.local_template_button.setToolTip("不调用模型，使用当前维度生成可继续编辑的基础标准")
        self.local_template_button.clicked.connect(self._use_local_template)
        self.generate_button = QPushButton("生成评价标准初稿")
        self.generate_button.setObjectName("generateRubricDraft")
        self.generate_button.setProperty("fluentAppearance", "primary")
        self.generate_button.clicked.connect(self._generate)
        generate_actions.addWidget(self.local_template_button)
        generate_actions.addWidget(self.generate_button)
        generate_actions.addStretch(1)
        layout.addLayout(generate_actions)
        self.preview = RubricPreview()
        self.preview.setObjectName("generatedRubricPreview")
        layout.addWidget(self.preview, 1)
        revision_title = QLabel("对话调整")
        revision_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(revision_title)
        revision_help = QLabel(
            "可调整描述、检查点和评分等级措辞。维度、权重或评分范围请返回前面的步骤修改后重新生成。"
        )
        revision_help.setWordWrap(True)
        revision_help.setProperty("fluentType", "secondary")
        layout.addWidget(revision_help)
        self.revision_instruction = QPlainTextEdit()
        self.revision_instruction.setObjectName("rubricRevisionInstruction")
        self.revision_instruction.setPlaceholderText(
            "例如：把优秀等级写得更具体，强调方法选择的理由。"
        )
        self.revision_instruction.setAccessibleName("Rubric 调整要求")
        layout.addWidget(self.revision_instruction)
        result_actions = QHBoxLayout()
        self.revise_button = QPushButton("应用文字调整")
        self.revise_button.setObjectName("reviseRubricDraft")
        self.revise_button.clicked.connect(self._revise)
        self.save_button = QPushButton("保存方案")
        self.save_button.setObjectName("saveRubricPackage")
        self.save_button.clicked.connect(lambda: self._save(False))
        self.save_default_button = QPushButton("保存并设为默认")
        self.save_default_button.setObjectName("saveDefaultRubricPackage")
        self.save_default_button.setProperty("fluentAppearance", "primary")
        self.save_default_button.clicked.connect(lambda: self._save(True))
        result_actions.addWidget(self.revise_button)
        result_actions.addStretch(1)
        result_actions.addWidget(self.save_button)
        result_actions.addWidget(self.save_default_button)
        layout.addLayout(result_actions)
        self._set_result_actions_enabled(False)
        return self._scroll(content)

    @staticmethod
    def _scroll(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def refresh_providers(self) -> None:
        preferred = str(self.provider.currentData() or self.preferences.default_provider)
        try:
            self._provider_catalog = provider_connections(self.service)
            error = ""
        except Exception as exception:
            self._provider_catalog = []
            error = str(exception)
        self.provider.blockSignals(True)
        self.provider.clear()
        for item in self._provider_catalog:
            protocol = provider_protocol_text(item.protocol)
            label = f"{item.display_name} · {protocol}" if protocol else item.display_name
            self.provider.addItem(label, item.provider_ref)
        selected = self.provider.findData(preferred)
        if selected < 0:
            selected = self.provider.findData(self.preferences.default_provider)
        self.provider.setCurrentIndex(max(0, selected))
        self.provider.blockSignals(False)
        if error:
            self.message.show_message(f"Provider 配置读取失败：{error}", severity="danger")
        self._provider_changed()

    def _provider_changed(self, _index: int = -1) -> None:
        provider_ref = str(self.provider.currentData() or "")
        connection = next(
            (item for item in self._provider_catalog if item.provider_ref == provider_ref),
            None,
        )
        if connection is None:
            self.model.clear()
            self.provider_info.setText("没有可用的 Provider。")
            self.generate_button.setEnabled(False)
            return
        choices, current = model_choices(
            connection,
            recent_models=list(self.preferences.recent_models.get(provider_ref, [])),
            default_provider=self.preferences.default_provider,
            default_model=self.preferences.default_model,
            provider_ref=provider_ref,
        )
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(choices)
        self.model.setCurrentText(current)
        self.model.blockSignals(False)
        has_key = provider_has_key(self.service, provider_ref)
        protocol = provider_protocol_text(connection.protocol) or "未知协议"
        self.provider_info.setText(
            f"接口：{protocol} · {'已配置 API Key' if has_key else '尚未配置 API Key'}"
        )
        self.generate_button.setEnabled(has_key and not self._busy)

    def _subject_mode_changed(self, _index: int = -1) -> None:
        self._invalidate_result()
        mode = self._subject_mode()
        enabled = mode is not SubjectAssessmentMode.NONE
        for widget in (self.subject_name, self.core_topics, self.common_errors):
            widget.setEnabled(enabled)
        self.external_evidence.setEnabled(enabled)
        if not enabled:
            self.external_evidence.setChecked(False)
        if self._dimensions_pristine:
            self._populate_default_dimensions(mode)

    def _populate_default_dimensions(self, mode: SubjectAssessmentMode) -> None:
        if not hasattr(self, "dimension_rows_layout"):
            return
        if mode is SubjectAssessmentMode.NONE:
            rows = [
                ("课程任务完成度", 30, "course_requirements"),
                ("论证与证据", 25, "argumentation"),
                ("结构与逻辑", 20, "argumentation"),
                ("文字表达", 15, "writing_norms"),
                ("引用格式规范", 10, "writing_norms"),
            ]
        else:
            subject_title = (
                "专业知识与方法运用"
                if mode is SubjectAssessmentMode.SPECIALIST
                else "课程知识理解与运用"
            )
            subject_weight = 40 if mode is SubjectAssessmentMode.SPECIALIST else 30
            task_weight = 15 if mode is SubjectAssessmentMode.SPECIALIST else 20
            rows = [
                ("课程任务完成度", task_weight, "course_requirements"),
                (subject_title, subject_weight, "subject_matter"),
                ("论证与证据", 20, "argumentation"),
                ("结构与逻辑", 15, "argumentation"),
                (
                    "文字表达",
                    5 if mode is SubjectAssessmentMode.SPECIALIST else 10,
                    "writing_norms",
                ),
                ("引用格式规范", 5, "writing_norms"),
            ]
        self._building_dimensions = True
        try:
            for row in self.dimension_rows:
                row.setParent(None)
                row.deleteLater()
            self.dimension_rows.clear()
            for title, weight, role in rows:
                self._append_dimension(title, weight, role)
        finally:
            self._building_dimensions = False
        self._dimensions_pristine = True
        self._update_dimension_total()

    def _append_dimension(self, title: str, weight: int, role: str) -> None:
        row = DimensionPreferenceRow(title, weight, role)
        row.changed.connect(self._dimension_changed)
        row.remove_requested.connect(self._remove_dimension)
        self.dimension_rows.append(row)
        self.dimension_rows_layout.addWidget(row)

    def _add_blank_dimension(self) -> None:
        if len(self.dimension_rows) >= 10:
            self.message.show_message("最多支持 10 个评价维度。", severity="warning")
            return
        self._append_dimension("新评价维度", 5, "course_requirements")
        self._dimensions_pristine = False
        self._update_dimension_total()
        self.dimension_rows[-1].title.selectAll()
        self.dimension_rows[-1].title.setFocus()

    def _remove_dimension(self, row: object) -> None:
        if not isinstance(row, DimensionPreferenceRow) or row not in self.dimension_rows:
            return
        if len(self.dimension_rows) <= 2:
            self.message.show_message("评价方案至少需要两个维度。", severity="warning")
            return
        self.dimension_rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._dimensions_pristine = False
        self._update_dimension_total()

    def _dimension_changed(self) -> None:
        if not self._building_dimensions:
            self._dimensions_pristine = False
            self._invalidate_result()
        self._update_dimension_total()

    def _update_dimension_total(self) -> None:
        total = sum(row.weight.value() for row in self.dimension_rows)
        remaining = 100 - total
        if remaining == 0:
            text = "权重合计 100%"
            severity = "bodyStrong"
        elif remaining > 0:
            text = f"权重合计 {total}%，还差 {remaining}%"
            severity = "danger"
        else:
            text = f"权重合计 {total}%，超出 {-remaining}%"
            severity = "danger"
        self.dimension_total.setText(text)
        self.dimension_total.setAccessibleDescription(text)
        self.dimension_total.setProperty("fluentType", severity)
        self.dimension_total.style().unpolish(self.dimension_total)
        self.dimension_total.style().polish(self.dimension_total)

    def _show_step(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        titles = ("课程与作业", "专业内容", "评价维度与权重", "打分方式", "生成与确认")
        self.step_label.setText(f"第 {index + 1}/5 步 · {titles[index]}")
        self.back_button.setEnabled(index > 0 and not self._busy)
        self.next_button.setVisible(index < 4)
        self.next_button.setEnabled(index < 4 and not self._busy)

    def _next(self) -> None:
        current = self.stack.currentIndex()
        if not self._validate_step(current):
            return
        self.message.clear()
        self._show_step(min(4, current + 1))

    def _back(self) -> None:
        if self._busy:
            return
        self.message.clear()
        self._show_step(max(0, self.stack.currentIndex() - 1))

    def _validate_step(self, index: int) -> bool:
        errors: list[str] = []
        if index == 0:
            for widget, message in (
                (self.course_name, "请填写课程名称"),
                (self.assignment_requirements, "请填写课程论文作业要求"),
            ):
                invalid = (
                    not widget.toPlainText().strip()
                    if isinstance(widget, QPlainTextEdit)
                    else not widget.text().strip()
                )
                set_fluent_property(widget, "fluentInvalid", invalid)
                widget.setAccessibleDescription(message if invalid else "")
                if invalid:
                    errors.append(message)
        elif index == 1 and self._subject_mode() is not SubjectAssessmentMode.NONE:
            missing_subject = not self.subject_name.text().strip()
            missing_topics = not self._lines(self.learning_outcomes) and not self._lines(
                self.core_topics
            )
            if missing_subject:
                errors.append("请填写课程所属专业或领域")
            if missing_topics:
                errors.append("请至少填写一个学习目标或核心知识点")
            set_fluent_property(
                self.subject_name,
                "fluentInvalid",
                missing_subject,
            )
            set_fluent_property(self.core_topics, "fluentInvalid", missing_topics)
            self.subject_name.setAccessibleDescription(
                "请填写课程所属专业或领域" if missing_subject else ""
            )
            self.core_topics.setAccessibleDescription(
                "请至少填写一个学习目标或核心知识点" if missing_topics else ""
            )
        elif index == 2:
            if len(self.dimension_rows) < 2:
                errors.append("评价方案至少需要两个维度")
            total = sum(row.weight.value() for row in self.dimension_rows)
            if total != 100:
                errors.append(f"评价维度权重必须合计 100%，当前为 {total}%")
            seen: set[str] = set()
            for row in self.dimension_rows:
                title = row.title.text().strip().casefold()
                invalid = not title or title in seen
                row.set_invalid(invalid, "评价维度名称为空或重复" if invalid else "")
                if title:
                    seen.add(title)
            if any(row.title.property("fluentInvalid") for row in self.dimension_rows):
                errors.append("评价维度名称不能为空或重复")
            if self._subject_mode() is SubjectAssessmentMode.NONE and any(
                row.role.currentData() == "subject_matter" for row in self.dimension_rows
            ):
                errors.append("不评测专业内容时不能分配专业内容 Reviewer")
        elif index == 3:
            try:
                self._scoring_settings()
            except ValidationError as error:
                errors.extend(self._validation_messages(error))
            invalid_scoring = bool(errors)
            for scoring_widget in (
                self.minimum_score,
                self.maximum_score,
                self.anchor_count,
            ):
                set_fluent_property(scoring_widget, "fluentInvalid", invalid_scoring)
                scoring_widget.setAccessibleDescription(
                    "；".join(errors) if invalid_scoring else ""
                )
        if errors:
            message = "；".join(dict.fromkeys(errors))
            self.message.show_message(message, severity="danger")
            return False
        return True

    def _build_request(self) -> RubricGenerationRequest:
        for index in range(4):
            if not self._validate_step(index):
                raise ValueError("请先修正前面步骤中的必填项。")
        return RubricGenerationRequest(
            brief=CourseAssessmentBrief(
                course_name=self.course_name.text(),
                course_level=str(self.course_level.currentData()),
                assignment_requirements=self.assignment_requirements.toPlainText(),
                learning_outcomes=self._lines(self.learning_outcomes),
                subject_assessment_mode=self._subject_mode(),
                subject_name=self.subject_name.text(),
                core_topics=self._lines(self.core_topics),
                common_errors=self._lines(self.common_errors),
                external_evidence_required=self.external_evidence.isChecked(),
                dimension_preferences=[row.value() for row in self.dimension_rows],
            ),
            scoring=self._scoring_settings(),
            additional_instructions=self.additional_instructions.toPlainText(),
        )

    def _scoring_settings(self) -> ScoringSettings:
        anchor_count: Literal[4, 5] = 4 if int(self.anchor_count.currentData()) == 4 else 5
        return ScoringSettings(
            minimum_score=self.minimum_score.value(),
            maximum_score=self.maximum_score.value(),
            anchor_count=anchor_count,
            passing_score=self.passing_score.value() if self.passing_enabled.isChecked() else None,
        )

    def _subject_mode(self) -> SubjectAssessmentMode:
        value = self.subject_mode.currentData()
        return (
            value if isinstance(value, SubjectAssessmentMode) else SubjectAssessmentMode(str(value))
        )

    @staticmethod
    def _lines(widget: QPlainTextEdit) -> list[str]:
        return [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]

    def _generate(self) -> None:
        if self._busy:
            return
        try:
            request = self._build_request()
        except (ValidationError, ValueError) as error:
            self.message.show_message(self._error_text(error), severity="danger")
            return
        provider_ref, model = self._provider_values()
        if not provider_ref or not model:
            self.message.show_message("请选择 Provider 并填写模型名称。", severity="danger")
            return
        if not provider_has_key(self.service, provider_ref):
            self.message.show_message("所选 Provider 尚未配置 API Key。", severity="danger")
            return

        async def operation(_emit: object) -> RubricGenerationResult:
            return await self.service.generate_rubric(
                request,
                provider_ref=provider_ref,
                model=model,
            )

        self._pending_revision = False
        self._start_worker(operation, "正在生成评价标准…")

    def _use_local_template(self) -> None:
        if self._busy:
            return
        try:
            request = self._build_request()
            result = compile_rubric_generation(request, default_rubric_draft(request))
        except (ValidationError, ValueError) as error:
            self.message.show_message(self._error_text(error), severity="danger")
            return
        self._pending_revision = False
        self._generation_completed(result)
        self.message.show_message(
            "已生成本地基础模板，未调用云端模型；请预览后保存或使用对话调整。",
            severity="success",
        )

    def _revise(self) -> None:
        if self._busy or self._current_result is None:
            return
        instruction = self.revision_instruction.toPlainText().strip()
        if not instruction:
            self.message.show_message("请填写希望调整的内容。", severity="danger")
            self.revision_instruction.setFocus()
            return
        provider_ref, model = self._provider_values()
        if not provider_ref or not model or not provider_has_key(self.service, provider_ref):
            self.message.show_message(
                "对话调整需要选择已配置 API Key 的 Provider 和模型。",
                severity="danger",
            )
            return
        current = self._current_result
        if self._saved_package is not None:
            self._parent_package_id = self._saved_package.manifest.package_id

        async def operation(_emit: object) -> RubricGenerationResult:
            return await self.service.revise_rubric(
                current,
                instruction,
                provider_ref=provider_ref,
                model=model,
            )

        self._pending_revision = True
        self._start_worker(operation, "正在根据教师要求调整评价标准…")

    def _provider_values(self) -> tuple[str, str]:
        return str(self.provider.currentData() or ""), self.model.currentText().strip()

    def _start_worker(self, operation: object, message: str) -> None:
        if not callable(operation):
            return
        worker = AsyncTaskThread(operation)
        self._worker = worker
        worker.completed.connect(self._generation_completed)
        worker.failed.connect(self._generation_failed)
        worker.task_cancelled.connect(self._generation_cancelled)
        if self.operation_registry is not None:
            self.operation_registry.track(worker, self._worker_finished)
        else:
            self._workers.append(worker)
            worker.finished.connect(lambda: self._untrack_worker(worker))
        self._set_busy(True)
        self.message.show_message(message, severity="info")
        worker.start()

    def _worker_finished(self, worker: AsyncTaskThread) -> None:
        if self._worker is worker:
            self._worker = None

    def _untrack_worker(self, worker: AsyncTaskThread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        self._worker_finished(worker)
        worker.deleteLater()

    def _generation_completed(self, value: object) -> None:
        self._set_busy(False)
        if not isinstance(value, RubricGenerationResult):
            self.message.show_message("模型返回了无法识别的评价标准结果。", severity="danger")
            return
        self._current_result = value
        if not self._pending_revision:
            self._parent_package_id = None
        self._saved_package = None
        self.preview.set_result(
            RubricValidationResult(
                valid=True,
                rubric=value.rubric,
                warnings=value.warnings,
                weight_total=sum(item.weight for item in value.rubric.dimensions),
                profile_compatible=True,
            )
        )
        self._set_result_actions_enabled(True)
        self.revision_instruction.clear()
        self.message.show_message(
            "评价标准已生成并通过结构、权重和 Reviewer 覆盖校验，请预览后保存。",
            severity="success",
        )

    def _generation_failed(self, message: str, _traceback: str) -> None:
        self._set_busy(False)
        self.message.show_message(f"评价标准生成失败：{message}", severity="danger")

    def _generation_cancelled(self) -> None:
        self._set_busy(False)
        self.message.show_message("已取消本次评价标准生成。", severity="info")

    def _cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel_task()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.stack.setEnabled(not busy)
        self.back_button.setEnabled(not busy and self.stack.currentIndex() > 0)
        self.next_button.setEnabled(not busy and self.stack.currentIndex() < 4)
        self.cancel_button.setVisible(busy)
        set_fluent_property(self.generate_button, "fluentBusy", busy)
        set_fluent_property(self.revise_button, "fluentBusy", busy)
        if busy:
            self.generate_button.setText("生成中…")
            self.revise_button.setText("调整中…")
        else:
            self.generate_button.setText("生成评价标准初稿")
            self.revise_button.setText("应用文字调整")
            self._provider_changed()
            self._set_result_actions_enabled(self._current_result is not None)

    def _set_result_actions_enabled(self, enabled: bool) -> None:
        self.revise_button.setEnabled(enabled and not self._busy)
        self.save_button.setEnabled(enabled and not self._busy and self._saved_package is None)
        self.save_default_button.setEnabled(
            enabled and not self._busy and self._saved_package is None
        )
        self.revision_instruction.setEnabled(enabled and not self._busy)

    def _save(self, set_default: bool) -> None:
        if self._current_result is None or self._busy:
            return
        provider_ref, model = self._provider_values()
        try:
            saved = self.service.save_rubric_generation(
                self._current_result,
                provider_ref=provider_ref,
                model=model,
                parent_package_id=self._parent_package_id,
            )
        except (OSError, ValidationError, ValueError) as error:
            self.message.show_message(
                f"评价方案保存失败：{self._error_text(error)}",
                severity="danger",
            )
            return
        self._saved_package = saved
        self._parent_package_id = saved.manifest.package_id
        self._set_result_actions_enabled(True)
        self.package_saved.emit(saved, set_default)
        self.message.show_message(
            "评价方案已保存并设为默认。" if set_default else "评价方案已保存。",
            severity="success",
        )

    def _set_tab_order(self) -> None:
        controls = [
            self.course_name,
            self.course_level,
            self.assignment_requirements,
            self.learning_outcomes,
            self.subject_mode,
            self.subject_name,
            self.core_topics,
            self.common_errors,
            self.external_evidence,
            self.add_dimension_button,
            self.minimum_score,
            self.maximum_score,
            self.anchor_count,
            self.passing_enabled,
            self.passing_score,
            self.additional_instructions,
            self.provider,
            self.model,
            self.local_template_button,
            self.generate_button,
            self.preview,
            self.revision_instruction,
            self.revise_button,
            self.save_button,
            self.save_default_button,
        ]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)

    def _connect_design_changes(self) -> None:
        self.course_name.textChanged.connect(self._invalidate_result)
        self.course_level.currentIndexChanged.connect(self._invalidate_result)
        self.assignment_requirements.textChanged.connect(self._invalidate_result)
        self.learning_outcomes.textChanged.connect(self._invalidate_result)
        self.subject_name.textChanged.connect(self._invalidate_result)
        self.core_topics.textChanged.connect(self._invalidate_result)
        self.common_errors.textChanged.connect(self._invalidate_result)
        self.external_evidence.toggled.connect(self._invalidate_result)
        self.minimum_score.valueChanged.connect(self._invalidate_result)
        self.maximum_score.valueChanged.connect(self._invalidate_result)
        self.anchor_count.currentIndexChanged.connect(self._invalidate_result)
        self.passing_enabled.toggled.connect(self._invalidate_result)
        self.passing_score.valueChanged.connect(self._invalidate_result)
        self.additional_instructions.textChanged.connect(self._invalidate_result)

    def _invalidate_result(self, _value: object = None) -> None:
        if self._current_result is None:
            return
        self._current_result = None
        self._saved_package = None
        self._parent_package_id = None
        self._set_result_actions_enabled(False)
        self.preview.set_result(
            RubricValidationResult(
                valid=False,
                errors=["教师输入已变化，请重新生成评价标准。"],
            )
        )
        self.message.show_message(
            "教师输入已变化，请重新生成后再保存。",
            severity="warning",
        )

    @staticmethod
    def _validation_messages(error: ValidationError) -> list[str]:
        return [str(item["msg"]).removeprefix("Value error, ") for item in error.errors()]

    def _error_text(self, error: Exception) -> str:
        if isinstance(error, ValidationError):
            return "；".join(self._validation_messages(error))
        return str(error)
