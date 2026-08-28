from __future__ import annotations

import os
import unicodedata
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from yaml import YAMLError

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.batch_output import batch_output_conflict_message
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.config import load_review_profile
from paper_reviewer.domain.batch import BatchReviewRequest
from paper_reviewer.gui.batch_models import BatchSourcePreviewModel
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import (
    ProviderDisplay,
    provider_connections,
    provider_display,
    provider_has_key,
    provider_protocol_text,
)
from paper_reviewer.gui.pages.new_review_validation import model_choices
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader, PathPicker, RubricPreview


class CourseBatchNewPage(QWidget):
    """Folder-only entry point for course-paper batch assessment."""

    start_requested = Signal(object)
    settings_requested = Signal()

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        icons: FluentIconService,
    ) -> None:
        super().__init__()
        self.setObjectName("courseBatchNewPage")
        self.service = service
        self.preferences = preferences
        self.icons = icons
        self._busy = False
        self._setting_default_output = False
        self._output_was_edited = False
        self._default_output_source: Path | None = None
        self._submitted_output_paths: set[str] = set()
        self._visible_output_error = ""
        self._provider_catalog: list[ProviderDisplay] = []
        self._provider_catalog_error = ""
        self._source_paths: list[Path] = []
        self.validation: RubricValidationResult | None = None
        self.default_profile_path = _course_config_path(
            "course_paper_reviewers_v1.yaml",
            "configs/review_profiles/course_paper_reviewers_v1.yaml",
        )
        self.profile_path = self.default_profile_path

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("courseBatchNewScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("pageCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 32)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader(
                "新建课程论文批次",
                "选择一个文件夹，按课程评价标准顺序评测其中的 PDF 论文。",
            )
        )

        self.message = MessageBar(icons)
        self.message.setObjectName("courseBatchMessageBar")
        self.message.action_requested.connect(self.settings_requested)
        layout.addWidget(self.message)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.source_picker = PathPicker(
            suffix="",
            placeholder="拖放或选择包含课程论文 PDF 的文件夹",
            button_text="选择文件夹…",
        )
        self.source_picker.setObjectName("batchSourceDirectoryPicker")
        self.source_picker.edit.setObjectName("batchSourceDirectory")
        self.source_picker.edit.setAccessibleName("课程论文文件夹")
        self.source_picker.button.setAccessibleName("选择课程论文文件夹")
        self.source_picker.browse_requested.connect(self._browse_source)
        self.source_picker.path_changed.connect(self._source_changed)
        form.addRow("论文文件夹", self.source_picker)

        self.output_picker = PathPicker(
            suffix="",
            placeholder="选择批次报告输出目录",
            button_text="选择输出目录…",
        )
        self.output_picker.setObjectName("batchOutputDirectoryPicker")
        self.output_picker.edit.setObjectName("batchOutputDirectory")
        self.output_picker.edit.setAccessibleName("批次报告输出目录")
        self.output_picker.button.setAccessibleName("选择批次报告输出目录")
        self.output_picker.browse_requested.connect(self._browse_output)
        self.output_picker.path_changed.connect(self._output_changed)
        form.addRow("输出目录", self.output_picker)

        self.rubric_picker = PathPicker(
            suffix=".yaml",
            placeholder="选择课程论文 Rubric YAML",
        )
        self.rubric_picker.setObjectName("courseRubricPicker")
        self.rubric_picker.edit.setObjectName("courseRubricPath")
        self.rubric_picker.edit.setAccessibleName("课程论文 Rubric")
        self.rubric_picker.browse_requested.connect(self._browse_rubric)
        self.rubric_picker.path_changed.connect(self._rubric_changed)
        form.addRow("课程 Rubric", self.rubric_picker)

        self.provider = QComboBox()
        self.provider.setObjectName("courseBatchProviderSelector")
        self.provider.setAccessibleName("批次模型 Provider")
        self.provider.setToolTip("整个批次使用相同的 Provider 和接口协议")
        self.provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("Provider", self.provider)

        self.provider_info = QLabel()
        self.provider_info.setObjectName("courseBatchProviderInfo")
        self.provider_info.setWordWrap(True)
        self.provider_info.setProperty("fluentType", "secondary")
        self.provider_info.setAccessibleName("批次 Provider 接口信息")
        form.addRow("", self.provider_info)

        self.model = QComboBox()
        self.model.setObjectName("courseBatchModelSelector")
        self.model.setAccessibleName("批次评测模型")
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.currentTextChanged.connect(self._update_start_state)
        form.addRow("模型", self.model)

        self.external_search = QCheckBox("联网检索并核验参考文献")
        self.external_search.setObjectName("courseBatchExternalSearch")
        self.external_search.setAccessibleName("批次外部学术检索")
        self.external_search.setChecked(preferences.external_search)
        form.addRow("外部检索", self.external_search)

        self.cloud_processing_authorized = QCheckBox(
            "我确认拥有处理这些论文的授权，同意使用云端模型评测"
        )
        self.cloud_processing_authorized.setObjectName("batchCloudProcessingAuthorized")
        self.cloud_processing_authorized.setAccessibleName("批次云端处理授权确认")
        self.cloud_processing_authorized.stateChanged.connect(self._update_start_state)
        form.addRow("云端处理", self.cloud_processing_authorized)

        self.non_classified_confirmation = QCheckBox(
            "我确认批次中的论文均不包含涉密材料"
        )
        self.non_classified_confirmation.setObjectName("batchNonClassifiedConfirmation")
        self.non_classified_confirmation.setAccessibleName("批次非涉密材料确认")
        self.non_classified_confirmation.stateChanged.connect(self._update_start_state)
        form.addRow("材料属性", self.non_classified_confirmation)

        self.pii_output_confirmation = QCheckBox(
            "我知悉导出文件名和汇总表将包含姓名、学号、专业和题目"
        )
        self.pii_output_confirmation.setObjectName("batchPiiOutputConfirmation")
        self.pii_output_confirmation.setAccessibleName("个人信息输出风险确认")
        self.pii_output_confirmation.setAccessibleDescription(
            "必须确认。批次输出目录应按个人信息材料妥善管理。"
        )
        self.pii_output_confirmation.stateChanged.connect(self._update_start_state)
        form.addRow("个人信息", self.pii_output_confirmation)
        layout.addLayout(form)

        self.scan_summary = QLabel("尚未扫描论文文件夹")
        self.scan_summary.setObjectName("batchScanSummary")
        self.scan_summary.setProperty("fluentType", "sectionTitle")
        self.scan_summary.setWordWrap(True)
        self.scan_summary.setAccessibleName("批次扫描结果")
        layout.addWidget(self.scan_summary)

        self.preview_model = BatchSourcePreviewModel()
        self.preview_table = QTableView()
        self.preview_table.setObjectName("batchSourcePreviewTable")
        self.preview_table.setAccessibleName("批次论文扫描预览")
        self.preview_table.setAccessibleDescription(
            "显示所选文件夹顶层、将加入批次的 PDF 文件"
        )
        self.preview_table.setModel(self.preview_model)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.preview_table.setIconSize(QSize(16, 16))
        self.preview_table.verticalHeader().hide()
        self.preview_table.setMinimumHeight(180)
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.preview_table)

        self.request_estimate = QLabel(
            "选择论文文件夹后，将显示最低模型请求次数估算。"
        )
        self.request_estimate.setObjectName("batchRequestEstimate")
        self.request_estimate.setProperty("fluentType", "secondary")
        self.request_estimate.setWordWrap(True)
        self.request_estimate.setAccessibleName("批次模型请求次数估算")
        layout.addWidget(self.request_estimate)

        self.rubric_preview = RubricPreview()
        self.rubric_preview.setObjectName("courseRubricPreview")
        layout.addWidget(self.rubric_preview)

        self.start_button = QPushButton("开始批量评测")
        self.start_button.setObjectName("startCourseBatchButton")
        self.start_button.setIcon(icons.icon("play", color_role="text_on_brand"))
        self.start_button.setAccessibleName("开始课程论文批量评测")
        set_fluent_property(self.start_button, "fluentAppearance", "primary")
        self.start_button.clicked.connect(self._start)
        layout.addWidget(self.start_button)
        layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll)

        self._load_providers(preferred=preferences.default_provider)
        configured_rubric = Path(preferences.default_rubric) if preferences.default_rubric else None
        default_rubric = (
            configured_rubric
            if configured_rubric is not None and configured_rubric.is_file()
            else _course_config_path(
                "course_paper_v1.yaml", "configs/rubrics/course_paper_v1.yaml"
            )
        )
        self.rubric_picker.set_path(default_rubric)
        if self.validation is None:
            self._rubric_changed(str(default_rubric))
        self._update_start_state()
        self._set_tab_order()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        set_fluent_property(self.start_button, "fluentBusy", busy)
        self.start_button.setText("正在创建批次…" if busy else "开始批量评测")
        self._update_start_state()

    def show_batch_error(self, message: str) -> None:
        self.set_busy(False)
        self.message.show_message(message, severity="danger")

    def refresh_providers(self) -> None:
        current = str(self.provider.currentData() or self.preferences.default_provider)
        self._load_providers(preferred=current)

    def refresh_credentials(self) -> None:
        self.refresh_providers()
        self._update_start_state()

    def apply_preferences(self) -> None:
        self._load_providers(preferred=self.preferences.default_provider)
        self.external_search.setChecked(self.preferences.external_search)
        configured_rubric = (
            Path(self.preferences.default_rubric)
            if self.preferences.default_rubric
            else None
        )
        if configured_rubric is not None and configured_rubric.is_file():
            self.rubric_picker.set_path(configured_rubric)
        self._update_start_state()

    def _browse_source(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择课程论文文件夹")
        if selected:
            self.source_picker.set_path(selected)

    def _browse_output(self) -> None:
        initial = str(self.output_picker.path() or self.source_picker.path() or "")
        selected = QFileDialog.getExistingDirectory(self, "选择批次报告输出目录", initial)
        if selected:
            self._output_was_edited = True
            self.output_picker.set_path(selected)

    def _browse_rubric(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择课程论文 Rubric",
            "",
            "YAML 文件 (*.yaml *.yml)",
        )
        if selected:
            self.rubric_picker.set_path(selected)

    def _source_changed(self, _text: str = "") -> None:
        source = self.source_picker.path()
        self._source_paths = _scan_top_level_pdfs(source)
        self.preview_model.set_paths(self._source_paths)
        error = _source_error(source, len(self._source_paths))
        self.source_picker.set_invalid(error)
        if error:
            self.scan_summary.setText(error)
            self.scan_summary.setAccessibleDescription(error)
        else:
            count = len(self._source_paths)
            self.scan_summary.setText(f"已发现 {count} 篇顶层 PDF，将按文件名顺序评测。")
            self.scan_summary.setAccessibleDescription(
                f"扫描成功，共 {count} 篇课程论文，不包含子目录。"
            )
        self._update_request_estimate()
        if source and source.is_dir() and not self._output_was_edited:
            output = self.output_picker.path()
            if (
                source != self._default_output_source
                or output is None
                or _output_path_key(output) in self._submitted_output_paths
                or output.exists()
                or output.is_symlink()
            ):
                self._select_next_default_output(source)
        self._update_start_state()

    def _output_changed(self, _text: str = "") -> None:
        if not self._setting_default_output:
            self._output_was_edited = bool(self.output_picker.edit.text().strip())
            if not self._output_was_edited:
                source = self.source_picker.path()
                if source is not None and source.is_dir():
                    self._select_next_default_output(source)
                    return
        self._update_start_state()

    def _select_next_default_output(self, source: Path) -> None:
        default_output = _next_default_output(
            source,
            reserved_paths=self._submitted_output_paths,
        )
        self._setting_default_output = True
        self._output_was_edited = False
        try:
            self.output_picker.set_path(default_output)
        finally:
            self._setting_default_output = False
        self._default_output_source = source

    def _rubric_changed(self, text: str) -> None:
        path = Path(text.strip()) if text.strip() else None
        if path is None or not path.is_file() or path.suffix.casefold() not in {".yaml", ".yml"}:
            message = "请选择存在的课程 Rubric YAML 文件"
            self.validation = None
            self.rubric_picker.set_invalid(message)
            self.rubric_preview.set_result(
                RubricValidationResult(valid=False, errors=[message])
            )
            self.message.show_message(message, severity="danger")
            self._update_start_state()
            return
        self.profile_path = self._profile_for_rubric(path)
        self._update_request_estimate()
        result = self.service.validate_rubric(path, profile_path=self.profile_path)
        rubric = result.rubric
        if result.valid and getattr(rubric, "evaluation_mode", None) != "course_assessment":
            result = RubricValidationResult(
                valid=False,
                rubric=rubric,
                errors=["课程版只支持 evaluation_mode=course_assessment 的 Rubric"],
                warnings=result.warnings,
                weight_total=result.weight_total,
                profile_compatible=result.profile_compatible,
            )
        self.validation = result
        self.rubric_preview.set_result(result)
        if result.valid:
            self.rubric_picker.set_invalid(None)
            if result.warnings:
                self.message.show_message(" ".join(result.warnings), severity="warning")
            else:
                self.message.clear()
        else:
            message = "；".join(result.errors) or "课程 Rubric 校验失败"
            self.rubric_picker.set_invalid(message)
            self.message.show_message(message, severity="danger")
        self._update_start_state()

    def _profile_for_rubric(self, path: Path) -> Path:
        resolver = getattr(self.service, "resolve_profile_for_rubric", None)
        if callable(resolver):
            return Path(resolver(path, fallback_profile_path=self.default_profile_path))
        return self.default_profile_path

    def _update_request_estimate(self) -> None:
        count = len(self._source_paths)
        if not count:
            self.request_estimate.setText(
                "选择论文文件夹后，将显示最低模型请求次数估算。"
            )
            return
        try:
            reviewer_count = len(load_review_profile(self.profile_path).reviewers)
        except (OSError, ValueError, YAMLError):
            reviewer_count = 3
        requests_per_paper = reviewer_count + 2
        self.request_estimate.setText(
            f"最低约 {count * requests_per_paper} 次模型请求：每篇至少 1 次信息提取、"
            f"{reviewer_count} 次专项评阅和 1 次汇总；工具轮次可能增加实际请求。"
        )

    def _load_providers(self, *, preferred: str = "") -> None:
        current = preferred or str(self.provider.currentData() or "")
        self._provider_catalog = provider_connections(self.service)
        self.provider.blockSignals(True)
        self.provider.clear()
        for item in self._provider_catalog:
            protocol = provider_protocol_text(item.protocol)
            label = f"{item.display_name} · {protocol}" if protocol else item.display_name
            self.provider.addItem(label, item.provider_ref)
            index = self.provider.count() - 1
            self.provider.setItemData(index, item.base_url, Qt.ItemDataRole.ToolTipRole)
            self.provider.setItemData(
                index,
                f"{label}；整个批次固定使用此 Provider。",
                Qt.ItemDataRole.AccessibleDescriptionRole,
            )
        selected = self.provider.findData(current)
        if selected < 0:
            selected = self.provider.findData(self.preferences.default_provider)
        self.provider.setCurrentIndex(max(0, selected))
        self.provider.blockSignals(False)
        self._provider_catalog_error = str(
            getattr(self.service, "_provider_catalog_error", "") or ""
        )
        self._provider_changed()

    def _provider_changed(self, _index: int = -1) -> None:
        provider_ref = str(self.provider.currentData() or "")
        connection = next(
            (
                item
                for item in self._provider_catalog
                if item.provider_ref == provider_ref
            ),
            provider_display(provider_ref),
        )
        protocol = provider_protocol_text(connection.protocol) or "未知"
        has_key = provider_has_key(self.service, provider_ref)
        self.provider_info.setText(
            f"接口：{protocol} · {'已配置 API Key' if has_key else '尚未配置 API Key'}"
        )
        self.provider_info.setToolTip(
            f"{connection.display_name}\n协议：{protocol}\n"
            f"Base URL：{connection.base_url or '未提供'}"
        )
        self._load_models(connection)
        self._update_start_state()

    def _load_models(self, connection: ProviderDisplay | None = None) -> None:
        provider_ref = str(self.provider.currentData() or "openai")
        connection = connection or next(
            (
                item
                for item in self._provider_catalog
                if item.provider_ref == provider_ref
            ),
            provider_display(provider_ref),
        )
        choices, current = model_choices(
            connection,
            recent_models=list(self.preferences.recent_models.get(provider_ref, [])),
            default_provider=self.preferences.default_provider,
            default_model=self.preferences.default_model,
            provider_ref=provider_ref,
        )
        self.model.blockSignals(True)
        self.model.clear()
        for name in choices:
            self.model.addItem(name)
        self.model.setCurrentText(current)
        self.model.blockSignals(False)

    def _update_start_state(self, _value: object = None) -> None:
        source_error = _source_error(self.source_picker.path(), len(self._source_paths))
        output_error = _output_error(
            self.output_picker.path(),
            reserved_paths=self._submitted_output_paths,
        )
        self.output_picker.set_invalid(output_error)
        cloud_ok = self.cloud_processing_authorized.isChecked()
        non_classified_ok = self.non_classified_confirmation.isChecked()
        pii_ok = self.pii_output_confirmation.isChecked()
        set_fluent_property(self.cloud_processing_authorized, "fluentInvalid", not cloud_ok)
        set_fluent_property(
            self.non_classified_confirmation, "fluentInvalid", not non_classified_ok
        )
        set_fluent_property(self.pii_output_confirmation, "fluentInvalid", not pii_ok)
        self.cloud_processing_authorized.setAccessibleDescription(
            "已确认整个批次可使用云端模型处理。"
            if cloud_ok
            else "必须确认拥有处理批次中全部论文的授权。"
        )
        self.non_classified_confirmation.setAccessibleDescription(
            "已确认批次中的论文均不含涉密材料。"
            if non_classified_ok
            else "必须确认批次中的论文均不含涉密材料。"
        )
        self.pii_output_confirmation.setAccessibleDescription(
            "已知悉文件名和汇总表会包含学生个人信息。"
            if pii_ok
            else "必须确认个人信息输出风险，并妥善管理输出目录。"
        )
        provider_ref = str(self.provider.currentData() or "")
        has_key = provider_has_key(self.service, provider_ref)
        ready = bool(
            source_error is None
            and output_error is None
            and self.validation
            and self.validation.valid
            and self.profile_path.is_file()
            and provider_ref
            and self.model.currentText().strip()
            and has_key
            and cloud_ok
            and non_classified_ok
            and pii_ok
            and not self._busy
        )
        self.start_button.setEnabled(ready)
        missing = self._missing_requirements(
            source_error=source_error,
            output_error=output_error,
            has_key=has_key,
        )
        description = (
            "开始批量评测。"
            if not missing
            else "当前不能开始：" + "；".join(missing)
        )
        self.start_button.setAccessibleDescription(description)
        self.start_button.setToolTip(description)
        if provider_ref and not has_key and source_error is None:
            display = provider_display(provider_ref, self._selected_provider()).display_name
            self.message.show_message(
                f"尚未配置 {display} API Key。",
                severity="warning",
                action_text="前往设置",
            )
        if self._provider_catalog_error:
            self.message.show_message(
                "自定义 Provider 配置读取失败，已暂时隐藏自定义条目："
                f"{self._provider_catalog_error}",
                severity="danger",
            )
        visible_output_error = output_error if self._output_was_edited else None
        self._sync_visible_output_error(visible_output_error)

    def _sync_visible_output_error(self, error: str | None) -> None:
        if error:
            self._visible_output_error = error
            self.message.show_message(error, severity="danger")
            return
        previous = self._visible_output_error
        self._visible_output_error = ""
        if previous and self.message.message_label.text() == previous:
            self.message.clear()

    def _missing_requirements(
        self,
        *,
        source_error: str | None,
        output_error: str | None,
        has_key: bool,
    ) -> list[str]:
        missing: list[str] = []
        if source_error:
            missing.append(source_error)
        if output_error:
            missing.append(output_error)
        if not self.validation or not self.validation.valid:
            missing.append("课程 Rubric 未通过校验")
        if not self.model.currentText().strip():
            missing.append("模型名称为空")
        if not has_key:
            missing.append("API Key 未配置")
        if not self.cloud_processing_authorized.isChecked():
            missing.append("未确认云端处理授权")
        if not self.non_classified_confirmation.isChecked():
            missing.append("未确认材料非涉密")
        if not self.pii_output_confirmation.isChecked():
            missing.append("未确认个人信息输出风险")
        return missing

    def _selected_provider(self) -> ProviderDisplay | None:
        provider_ref = str(self.provider.currentData() or "")
        return next(
            (
                item
                for item in self._provider_catalog
                if item.provider_ref == provider_ref
            ),
            None,
        )

    def _start(self) -> None:
        self._source_changed()
        if not self.start_button.isEnabled():
            self.message.show_message(
                self.start_button.accessibleDescription(), severity="danger"
            )
            return
        rubric = self.rubric_picker.path()
        source = self.source_picker.path()
        output = self.output_picker.path()
        if rubric is None or source is None or output is None:
            return
        result = self.service.validate_rubric(rubric, profile_path=self.profile_path)
        if (
            not result.valid
            or getattr(result.rubric, "evaluation_mode", None) != "course_assessment"
        ):
            self._rubric_changed(str(rubric))
            return
        request = BatchReviewRequest(
            source_dir=source,
            output_dir=output,
            provider=str(self.provider.currentData() or ""),
            model=self.model.currentText().strip(),
            rubric=rubric,
            profile=self.profile_path,
            cloud_processing_authorized=True,
            contains_classified_material=False,
            pii_output_authorized=True,
            external_search=self.external_search.isChecked(),
        )
        self._submitted_output_paths.add(_output_path_key(output))
        self.start_requested.emit(request)
        if self._output_was_edited:
            self._update_start_state()
        else:
            self._select_next_default_output(source)

    def _set_tab_order(self) -> None:
        controls = [
            self.source_picker.edit,
            self.source_picker.button,
            self.output_picker.edit,
            self.output_picker.button,
            self.rubric_picker.edit,
            self.rubric_picker.button,
            self.provider,
            self.model,
            self.external_search,
            self.cloud_processing_authorized,
            self.non_classified_confirmation,
            self.pii_output_confirmation,
            self.preview_table,
            self.start_button,
        ]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)


def _course_config_path(name: str, project_relative: str) -> Path:
    try:
        return bundled_config(name)
    except ValueError:
        return Path(__file__).resolve().parents[4] / project_relative


def _scan_top_level_pdfs(source: Path | None) -> list[Path]:
    if source is None or not source.is_dir():
        return []
    try:
        paths = [
            path
            for path in source.iterdir()
            if not path.is_symlink()
            and path.is_file()
            and path.suffix.casefold() == ".pdf"
        ]
    except OSError:
        return []
    return sorted(
        paths,
        key=lambda path: (
            unicodedata.normalize("NFKC", path.name).casefold(),
            path.name,
        ),
    )


def _source_error(source: Path | None, count: int) -> str | None:
    if source is None:
        return "请选择论文文件夹"
    if not source.is_dir():
        return "论文文件夹不存在或不可用"
    if count == 0:
        return "所选文件夹顶层没有 PDF 文件"
    if count > 100:
        return f"检测到 {count} 篇 PDF；单批最多支持 100 篇"
    return None


def _output_error(
    output: Path | None,
    *,
    reserved_paths: set[str] | None = None,
) -> str | None:
    if output is None:
        return "请选择批次报告输出目录"
    if reserved_paths and _output_path_key(output) in reserved_paths:
        return (
            "该输出目录刚刚已提交给一个新批次。"
            "请选择新的输出目录，或前往批次记录继续原批次。"
        )
    if output.exists():
        if not output.is_dir():
            return "输出路径已存在且不是文件夹"
        if not os.access(output, os.W_OK):
            return "输出目录不可写"
        conflict = batch_output_conflict_message(output)
        if conflict:
            return (
                f"{conflict}"
                "如需使用原有结果，请前往批次记录继续原批次。"
            )
        return None
    parent = output.parent
    if not parent.is_dir():
        return "输出目录的上级文件夹不存在"
    return None if os.access(parent, os.W_OK) else "输出目录的上级文件夹不可写"


def _next_default_output(source: Path, *, reserved_paths: set[str]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"课程论文评测报告_{timestamp}"
    sequence = 1
    while True:
        suffix = "" if sequence == 1 else f"_{sequence}"
        candidate = source / f"{base_name}{suffix}"
        if (
            _output_path_key(candidate) not in reserved_paths
            and not candidate.exists()
            and not candidate.is_symlink()
        ):
            return candidate
        sequence += 1


def _output_path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved))
