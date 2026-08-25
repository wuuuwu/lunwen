from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import (
    ProviderDisplay,
    provider_connections,
    provider_display,
    provider_has_key,
    provider_protocol_text,
)
from paper_reviewer.gui.pages.new_review_validation import (
    build_review_request,
    evaluate_start_state,
    is_valid_pdf,
    model_choices,
    paper_info_text,
    resolve_profile_for_rubric,
    validate_discipline_profile,
)
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader, PathPicker, RubricPreview


class NewReviewPage(QWidget):
    start_requested = Signal(object)
    settings_requested = Signal()

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        icons: FluentIconService,
    ) -> None:
        super().__init__()
        self.service = service
        self.preferences = preferences
        self._legacy_profile_path = bundled_config("three_reviewer.yaml")
        self._zhejiang_profile_path = self._optional_bundled_config(
            "zhejiang_undergraduate_specialists_v1.yaml",
            "configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml",
        )
        self._zhejiang_rubric_path = self._optional_bundled_config(
            "zhejiang_undergraduate_thesis_v2.yaml",
            "configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml",
        )
        self.profile_path = self._zhejiang_profile_path or self._legacy_profile_path
        self.validation: RubricValidationResult | None = None
        self._busy = False
        self._provider_catalog: list[ProviderDisplay] = []
        self._provider_catalog_error = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("pageCanvas")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 32)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeader("新建评测", "选择论文和 Rubric，确认模型配置后开始证据化评阅。")
        )

        self.message = MessageBar(icons)
        self.message.action_requested.connect(self.settings_requested)
        layout.addWidget(self.message)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.discipline_name = QLineEdit()
        self.discipline_name.setObjectName("disciplineName")
        self.discipline_name.setPlaceholderText("例如：计算机科学与技术")
        self.discipline_name.setAccessibleName("专业名称")
        self.discipline_name.setAccessibleDescription("必填。用于选择适用的专业评阅上下文。")
        self.discipline_name.textChanged.connect(self._inputs_changed)
        form.addRow("专业名称", self.discipline_name)
        self.discipline_name_error = self._error_label()
        form.addRow("", self.discipline_name_error)

        self.paper_picker = PathPicker(suffix=".pdf", placeholder="拖放或选择一个 PDF 文件")
        self.paper_picker.browse_requested.connect(self._browse_paper)
        self.paper_picker.path_changed.connect(self._inputs_changed)
        form.addRow("论文 PDF", self.paper_picker)

        self.paper_info = QLabel("尚未选择文件")
        self.paper_info.setProperty("fluentType", "secondary")
        form.addRow("", self.paper_info)

        self.rubric_picker = PathPicker(suffix=".yaml", placeholder="选择 Rubric YAML")
        self.rubric_picker.browse_requested.connect(self._browse_rubric)
        self.rubric_picker.path_changed.connect(self._rubric_changed)
        form.addRow("Rubric", self.rubric_picker)

        self.discipline_profile_picker = PathPicker(
            suffix=".yaml", placeholder="可选：选择专业培养目标 YAML"
        )
        self.discipline_profile_picker.browse_requested.connect(self._browse_discipline_profile)
        self.discipline_profile_picker.path_changed.connect(self._discipline_profile_changed)
        self.discipline_profile_picker.setAccessibleName("专业培养目标 YAML（可选）")
        # Stable aliases keep the field names discoverable to the application
        # shell and to automation without exposing any report-file state.
        self.discipline_profile = self.discipline_profile_picker
        form.addRow("专业培养目标", self.discipline_profile_picker)
        self.discipline_profile_error = self._error_label()
        form.addRow("", self.discipline_profile_error)

        self.provider = QComboBox()
        self.provider.setObjectName("providerSelector")
        self.provider.setAccessibleName("模型 Provider")
        self.provider.setToolTip("选择模型接口协议；自定义 Provider 的协议和端点不可在此处修改")
        self.provider.currentIndexChanged.connect(self._provider_changed)
        form.addRow("Provider", self.provider)

        self.provider_info = QLabel()
        self.provider_info.setObjectName("providerInfo")
        self.provider_info.setWordWrap(True)
        self.provider_info.setProperty("fluentType", "secondary")
        self.provider_info.setAccessibleName("Provider 接口信息")
        form.addRow("", self.provider_info)
        self._load_providers(preferred=preferences.default_provider)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._load_models()
        self.model.currentTextChanged.connect(self._inputs_changed)
        form.addRow("模型", self.model)

        self.external_search = QCheckBox(
            "自动联网检索并核验参考文献（DDGS / OpenAlex / Crossref / arXiv）"
        )
        self.external_search.setChecked(preferences.external_search)
        form.addRow("外部检索", self.external_search)

        self.cloud_processing_authorized = QCheckBox(
            "我确认拥有处理该论文的授权，同意使用云端模型评测"
        )
        self.cloud_processing_authorized.setObjectName("cloudProcessingAuthorized")
        self.cloud_processing_authorized.setAccessibleName("云端处理授权确认")
        self.cloud_processing_authorized.setAccessibleDescription(
            "开始云端评测前必须确认拥有处理论文的授权。"
        )
        self.cloud_authorized = self.cloud_processing_authorized
        self.cloud_processing_authorized.stateChanged.connect(self._inputs_changed)
        form.addRow("云端处理", self.cloud_processing_authorized)

        self.non_classified_confirmation = QCheckBox("我确认论文不包含涉密材料")
        self.non_classified_confirmation.setObjectName("nonClassifiedConfirmation")
        self.non_classified_confirmation.setAccessibleName("非涉密材料确认")
        self.non_classified_confirmation.setAccessibleDescription(
            "开始云端评测前必须声明论文不包含涉密材料。"
        )
        self.non_classified = self.non_classified_confirmation
        self.non_classified_confirmation.stateChanged.connect(self._inputs_changed)
        form.addRow("材料属性", self.non_classified_confirmation)

        # The detection-report workflow is intentionally only a placeholder in v2.
        # Keep this as a normal secondary action so it remains keyboard accessible,
        # while making it impossible to accidentally persist or upload a report.
        self.integrity_report_button = QPushButton("添加查重/学术不端检测报告")
        self.integrity_report_button.setObjectName("integrityReportButton")
        self.integrity_report_button.setAccessibleName(
            "添加查重/学术不端检测报告（后续版本功能）"
        )
        self.integrity_report_button.setAccessibleDescription(
            "后续版本功能。当前不会打开文件选择器，也不会读取或保存检测报告。"
        )
        self.integrity_report_button.setToolTip(
            "后续版本功能：当前不会打开文件选择器或读取检测报告"
        )
        set_fluent_property(self.integrity_report_button, "fluentAppearance", "secondary")
        self.integrity_report_button.clicked.connect(self._show_integrity_report_placeholder)
        form.addRow("检测报告", self.integrity_report_button)
        layout.addLayout(form)

        self.preview = RubricPreview()
        layout.addWidget(self.preview)

        self.start_button = QPushButton("开始评测")
        self.start_button.setIcon(icons.icon("play", color_role="text_on_brand"))
        self.start_button.setAccessibleDescription("开始新的论文评测任务")
        set_fluent_property(self.start_button, "fluentAppearance", "primary")
        self.start_button.clicked.connect(self._start)
        layout.addWidget(self.start_button)
        layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll)

        default_rubric = self._default_rubric_path(preferences.default_rubric)
        self.rubric_picker.set_path(default_rubric)
        self._rubric_changed(str(default_rubric))
        self._inputs_changed()
        if hasattr(self, "start_button"):
            self._update_start_state()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        set_fluent_property(self.start_button, "fluentBusy", busy)
        self.start_button.setText("正在启动…" if busy else "开始评测")
        self._update_start_state()

    def show_run_error(self, message: str) -> None:
        self.set_busy(False)
        self.message.show_message(message, severity="danger")

    def apply_preferences(self) -> None:
        self._load_providers(preferred=self.preferences.default_provider)
        self._load_models()
        self.external_search.setChecked(self.preferences.external_search)
        default_rubric = self._default_rubric_path(self.preferences.default_rubric)
        self.rubric_picker.set_path(default_rubric)
        self.profile_path = self._profile_for_rubric(default_rubric)
        self._inputs_changed()
        self._update_start_state()

    def refresh_credentials(self) -> None:
        self.refresh_providers()
        self._update_start_state()

    def refresh_providers(self) -> None:
        """Refresh the shared provider catalog while retaining a valid choice."""

        current = str(self.provider.currentData() or self.preferences.default_provider)
        self._load_providers(preferred=current)
        if hasattr(self, "model"):
            self._load_models()
        if hasattr(self, "start_button"):
            self._update_start_state()

    def _browse_paper(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择论文 PDF", "", "PDF 文件 (*.pdf)")
        if path:
            self.paper_picker.set_path(path)

    def _browse_rubric(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Rubric", "", "YAML 文件 (*.yaml *.yml)"
        )
        if path:
            self.rubric_picker.set_path(path)

    def _browse_discipline_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择专业培养目标 YAML",
            "",
            "YAML 文件 (*.yaml *.yml)",
        )
        if path:
            self.discipline_profile_picker.set_path(path)

    def _discipline_profile_changed(self, text: str) -> None:
        path = Path(text.strip()) if text.strip() else None
        message = validate_discipline_profile(path)
        if message:
            self.discipline_profile_picker.set_invalid(message)
            self._set_error(self.discipline_profile_error, message)
        else:
            self.discipline_profile_picker.set_invalid(None)
            self._set_error(self.discipline_profile_error, None)
        self._update_start_state()

    def _show_integrity_report_placeholder(self) -> None:
        self.message.show_message(
            "该功能已预留，当前版本请在线下查看检测报告，并在否决项人工复核时填写核查结论。",
            severity="info",
        )

    @staticmethod
    def _error_label() -> QLabel:
        label = QLabel()
        label.setProperty("fluentType", "secondary")
        label.setWordWrap(True)
        label.hide()
        return label

    @staticmethod
    def _set_error(label: QLabel, message: str | None) -> None:
        if message:
            label.setText(f"错误：{message}")
            label.show()
        else:
            label.clear()
            label.hide()

    def _rubric_changed(self, text: str) -> None:
        path = Path(text.strip()) if text.strip() else None
        if path is None or not path.is_file():
            self.validation = None
            message = "请选择存在的 Rubric YAML 文件"
            self.rubric_picker.set_invalid(message)
            self.preview.set_result(
                RubricValidationResult(valid=False, errors=[message])
            )
            self.message.show_message(message, severity="danger")
            self._update_start_state()
            return
        self.profile_path = self._profile_for_rubric(path)
        self.validation = self.service.validate_rubric(path, profile_path=self.profile_path)
        self.preview.set_result(self.validation)
        if self.validation.valid:
            self.rubric_picker.set_invalid(None)
            if self.validation.warnings:
                self.message.show_message(" ".join(self.validation.warnings), severity="warning")
            else:
                self.message.clear()
        else:
            message = "；".join(self.validation.errors)
            self.rubric_picker.set_invalid(message)
            self.message.show_message(message, severity="danger")
        self._update_start_state()

    def _load_providers(self, *, preferred: str = "") -> None:
        current = preferred or str(self.provider.currentData() or "")
        catalog = provider_connections(self.service)
        self._provider_catalog = catalog
        self.provider.blockSignals(True)
        self.provider.clear()
        for item in catalog:
            label = item.display_name
            protocol = provider_protocol_text(item.protocol)
            if protocol:
                label = f"{label} · {protocol}"
            self.provider.addItem(label, item.provider_ref)
            index = self.provider.count() - 1
            self.provider.setItemData(index, item.base_url, Qt.ItemDataRole.ToolTipRole)
            self.provider.setItemData(
                index,
                f"{label}；Base URL 仅用于本次 Provider 配置，不会写入报告。",
                Qt.ItemDataRole.AccessibleDescriptionRole,
            )
        selected = self.provider.findData(current)
        if selected < 0:
            selected = self.provider.findData(self.preferences.default_provider)
        self.provider.setCurrentIndex(max(0, selected))
        self.provider.blockSignals(False)
        self._provider_changed()
        catalog_error = str(getattr(self.service, "_provider_catalog_error", "") or "")
        self._provider_catalog_error = catalog_error
        if catalog_error and hasattr(self, "message"):
            self.message.show_message(
                f"自定义 Provider 配置读取失败，已暂时隐藏自定义条目：{catalog_error}",
                severity="danger",
            )

    def _provider_changed(self, _index: int = -1) -> None:
        provider_ref = str(self.provider.currentData() or "")
        connection = next(
            (item for item in self._provider_catalog if item.provider_ref == provider_ref),
            provider_display(provider_ref),
        )
        protocol = provider_protocol_text(connection.protocol)
        status = (
            "已配置 API Key"
            if provider_has_key(self.service, provider_ref)
            else "尚未配置 API Key"
        )
        self.provider_info.setText(f"接口：{protocol or '未知'} · {status}")
        self.provider_info.setToolTip(
            f"{connection.display_name}\n协议：{protocol or '未知'}\n"
            f"Base URL：{connection.base_url or '未提供'}"
        )
        if hasattr(self, "model"):
            self._load_models()
        if hasattr(self, "start_button"):
            self._update_start_state()

    def _load_models(self) -> None:
        provider = str(self.provider.currentData() or "openai")
        recent = list(self.preferences.recent_models.get(provider, []))
        connection = next(
            (item for item in self._provider_catalog if item.provider_ref == provider),
            provider_display(provider),
        )
        choices, current = model_choices(
            connection,
            recent_models=recent,
            default_provider=self.preferences.default_provider,
            default_model=self.preferences.default_model,
            provider_ref=provider,
        )
        self.model.blockSignals(True)
        self.model.clear()
        for name in choices:
            self.model.addItem(name)
        self.model.setCurrentText(current)
        self.model.blockSignals(False)

    def _inputs_changed(self, _value: object = "") -> None:
        discipline_name = self.discipline_name.text().strip()
        if discipline_name:
            set_fluent_property(self.discipline_name, "fluentInvalid", False)
            self.discipline_name.setAccessibleDescription(
                "已填写。用于选择适用的专业评阅上下文。"
            )
            self._set_error(self.discipline_name_error, None)
        else:
            set_fluent_property(self.discipline_name, "fluentInvalid", True)
            message = "专业名称为必填项"
            self.discipline_name.setAccessibleDescription(message)
            self._set_error(self.discipline_name_error, message)
        paper = self.paper_picker.path()
        self.paper_info.setText(paper_info_text(paper))
        if is_valid_pdf(paper):
            self.paper_picker.set_invalid(None)
        elif paper:
            self.paper_picker.set_invalid("请选择存在的 PDF 文件")
        self._update_start_state()

    def _update_start_state(self) -> None:
        paper = self.paper_picker.path()
        provider = str(self.provider.currentData() or "")
        cloud_ok = self.cloud_processing_authorized.isChecked()
        non_classified_ok = self.non_classified_confirmation.isChecked()
        set_fluent_property(self.cloud_processing_authorized, "fluentInvalid", not cloud_ok)
        set_fluent_property(
            self.non_classified_confirmation, "fluentInvalid", not non_classified_ok
        )
        self.cloud_processing_authorized.setAccessibleDescription(
            "已确认云端处理授权。"
            if cloud_ok
            else "必须确认拥有处理论文的授权后才能开始云端评测。"
        )
        self.non_classified_confirmation.setAccessibleDescription(
            "已确认论文不包含涉密材料。"
            if non_classified_ok
            else "必须确认论文不包含涉密材料后才能开始云端评测。"
        )
        start_state = evaluate_start_state(
            discipline_name=self.discipline_name.text(),
            paper=paper,
            rubric_valid=bool(self.validation and self.validation.valid),
            discipline_profile_valid=self._discipline_profile_is_valid(),
            model_name=self.model.currentText(),
            provider_ref=provider,
            provider_key_available=provider_has_key(self.service, provider),
            cloud_authorized=cloud_ok,
            non_classified=non_classified_ok,
            busy=self._busy,
        )
        self.start_button.setEnabled(start_state.valid)
        if start_state.configuration_ready and not provider_has_key(self.service, provider):
            display = provider_display(provider, self._selected_provider()).display_name
            self.message.show_message(
                f"尚未配置 {display} API Key。", severity="warning", action_text="前往设置"
            )
        if self._provider_catalog_error:
            self.message.show_message(
                "自定义 Provider 配置读取失败，已暂时隐藏自定义条目："
                f"{self._provider_catalog_error}",
                severity="danger",
            )

    def _discipline_profile_is_valid(self) -> bool:
        path = self.discipline_profile_picker.path()
        return validate_discipline_profile(path) is None

    @staticmethod
    def _optional_bundled_config(name: str, project_relative: str) -> Path | None:
        """Resolve a new built-in config while remaining compatible with old bundles."""
        try:
            candidate = bundled_config(name)
        except ValueError:
            candidate = Path(__file__).resolve().parents[3] / project_relative
        return candidate if candidate.is_file() else None

    def _default_rubric_path(self, preference: str) -> Path:
        if preference:
            preferred = Path(preference)
            if preferred.is_file():
                return preferred
        return self._zhejiang_rubric_path or bundled_config("unscored_draft.yaml")

    def _profile_for_rubric(self, path: Path) -> Path:
        return resolve_profile_for_rubric(
            path,
            zhejiang_profile_path=self._zhejiang_profile_path,
            legacy_profile_path=self._legacy_profile_path,
        )

    def _start(self) -> None:
        paper = self.paper_picker.path()
        rubric = self.rubric_picker.path()
        if (
            paper is None
            or not is_valid_pdf(paper)
            or rubric is None
        ):
            self._inputs_changed()
            return
        if not self.discipline_name.text().strip():
            self._inputs_changed()
            self.message.show_message("请填写专业名称。", severity="danger")
            self.discipline_name.setFocus()
            return
        if not self._discipline_profile_is_valid():
            self._discipline_profile_changed(self.discipline_profile_picker.edit.text())
            self.message.show_message("请修正专业培养目标 YAML。", severity="danger")
            self.discipline_profile_picker.edit.setFocus()
            return
        if not self.cloud_processing_authorized.isChecked():
            self.message.show_message("请确认拥有云端处理授权后再开始评测。", severity="danger")
            self.cloud_processing_authorized.setFocus()
            return
        if not self.non_classified_confirmation.isChecked():
            self.message.show_message("请确认论文不包含涉密材料后再开始评测。", severity="danger")
            self.non_classified_confirmation.setFocus()
            return
        provider = str(self.provider.currentData())
        model_name = self.model.currentText().strip()
        validation = self.service.validate_rubric(
            rubric, profile_path=self.profile_path
        )
        self.validation = validation
        self.preview.set_result(validation)
        if not validation.valid:
            message = "；".join(validation.errors)
            self.rubric_picker.set_invalid(message)
            self.message.show_message(message, severity="danger")
            self._update_start_state()
            return
        if not provider_has_key(self.service, provider):
            display = provider_display(provider, self._selected_provider()).display_name
            self.message.show_message(
                f"尚未配置 {display} API Key。",
                severity="warning",
                action_text="前往设置",
            )
            self._update_start_state()
            return
        self.preferences.default_provider = provider
        self.preferences.default_model = model_name
        self.preferences.external_search = self.external_search.isChecked()
        recent = self.preferences.recent_models.setdefault(provider, [])
        if model_name in recent:
            recent.remove(model_name)
        recent.insert(0, model_name)
        del recent[8:]
        request = build_review_request(
            paper=paper,
            provider=provider,
            model=model_name,
            rubric=rubric,
            profile=self.profile_path,
            external_search=self.external_search.isChecked(),
            discipline_name=self.discipline_name.text(),
            discipline_profile=self.discipline_profile_picker.path(),
            cloud_processing_authorized=self.cloud_processing_authorized.isChecked(),
            contains_classified_material=not self.non_classified_confirmation.isChecked(),
        )
        self.set_busy(True)
        self.start_requested.emit(request)

    def _selected_provider(self) -> ProviderDisplay:
        provider = str(self.provider.currentData() or "")
        return next(
            (item for item in self._provider_catalog if item.provider_ref == provider),
            provider_display(provider),
        )

    def selected_provider_display(self, provider_ref: str | None = None) -> ProviderDisplay:
        """Return the selected non-secret connection for the shell status bar."""

        if provider_ref is None or provider_ref == str(self.provider.currentData() or ""):
            return self._selected_provider()
        return next(
            (item for item in self._provider_catalog if item.provider_ref == provider_ref),
            provider_display(provider_ref),
        )
