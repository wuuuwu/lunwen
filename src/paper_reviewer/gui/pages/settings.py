from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.app_state import AppPaths, GuiPreferences
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.widgets import MessageBar, PageHeader, PathPicker


class SettingsPage(QWidget):
    preferences_changed = Signal()
    theme_changed = Signal(str)
    credentials_changed = Signal(str)

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        paths: AppPaths,
        icons: FluentIconService,
    ) -> None:
        super().__init__()
        self.service = service
        self.preferences = preferences
        self.paths = paths
        self.profile_path = bundled_config("three_reviewer.yaml")
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
        layout.addWidget(PageHeader("设置", "管理模型凭据、默认评测参数、主题和本地数据目录。"))
        self.message = MessageBar(icons)
        layout.addWidget(self.message)

        key_title = QLabel("API Key")
        key_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(key_title)
        self.key_fields: dict[str, QLineEdit] = {}
        for provider, label in (("openai", "OpenAI"), ("deepseek", "DeepSeek")):
            row = QHBoxLayout()
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText(
                "已安全保存；输入新 Key 可替换"
                if service.credentials.has(provider)
                else "输入 API Key"
            )
            field.setAccessibleName(f"{label} API Key")
            save = QPushButton("保存")
            delete = QPushButton("删除")
            save.clicked.connect(lambda _checked=False, p=provider: self._save_key(p))
            delete.clicked.connect(lambda _checked=False, p=provider: self._delete_key(p))
            row.addWidget(QLabel(label))
            row.addWidget(field, 1)
            row.addWidget(save)
            row.addWidget(delete)
            layout.addLayout(row)
            self.key_fields[provider] = field

        general_title = QLabel("默认评测参数")
        general_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(general_title)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.provider = QComboBox()
        self.provider.addItem("OpenAI", "openai")
        self.provider.addItem("DeepSeek", "deepseek")
        self.provider.setCurrentIndex(max(0, self.provider.findData(preferences.default_provider)))
        self.model = QLineEdit(preferences.default_model)
        self.default_rubric = PathPicker(suffix=".yaml", placeholder="默认 Rubric YAML")
        if preferences.default_rubric:
            self.default_rubric.set_path(preferences.default_rubric)
        self.default_rubric.browse_requested.connect(self._browse_rubric)
        self.external_search = QCheckBox("默认启用外部学术检索")
        self.external_search.setChecked(preferences.external_search)
        form.addRow("默认 Provider", self.provider)
        form.addRow("默认模型", self.model)
        form.addRow("默认 Rubric", self.default_rubric)
        form.addRow("", self.external_search)
        layout.addLayout(form)

        appearance_title = QLabel("外观与可访问性")
        appearance_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(appearance_title)
        appearance = QFormLayout()
        self.theme = QComboBox()
        self.theme.addItem("跟随系统", "system")
        self.theme.addItem("浅色", "light")
        self.theme.addItem("深色", "dark")
        self.theme.addItem("高对比度", "high_contrast")
        self.theme.setCurrentIndex(max(0, self.theme.findData(preferences.theme)))
        self.motion = QComboBox()
        self.motion.addItem("跟随系统", "system")
        self.motion.addItem("减少动画", "reduced")
        self.motion.addItem("关闭动画", "none")
        self.motion.setCurrentIndex(max(0, self.motion.findData(preferences.motion)))
        appearance.addRow("主题", self.theme)
        appearance.addRow("动画", self.motion)
        layout.addLayout(appearance)

        save_preferences = QPushButton("保存设置")
        save_preferences.clicked.connect(self._save_preferences)
        layout.addWidget(save_preferences)

        locations_title = QLabel("本地数据")
        locations_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(locations_title)
        locations = QHBoxLayout()
        data_button = QPushButton("打开数据目录")
        logs_button = QPushButton("打开日志目录")
        data_button.setIcon(icons.icon("folder"))
        logs_button.setIcon(icons.icon("folder"))
        data_button.clicked.connect(lambda: self._open_directory(paths.root))
        logs_button.clicked.connect(lambda: self._open_directory(paths.logs_dir))
        locations.addWidget(data_button)
        locations.addWidget(logs_button)
        locations.addStretch(1)
        layout.addLayout(locations)
        layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll)

    def _save_key(self, provider: str) -> None:
        field = self.key_fields[provider]
        try:
            self.service.credentials.set(provider, field.text())
        except Exception as error:
            self.message.show_message(f"保存失败：{error}", severity="danger")
            return
        field.clear()
        field.setPlaceholderText("已安全保存；输入新 Key 可替换")
        self.credentials_changed.emit(provider)
        self.message.show_message(f"{provider} API Key 已保存到系统凭据库。", severity="success")

    def _delete_key(self, provider: str) -> None:
        try:
            self.service.credentials.delete(provider)
        except Exception as error:
            self.message.show_message(f"删除失败：{error}", severity="danger")
            return
        self.key_fields[provider].clear()
        self.key_fields[provider].setPlaceholderText("输入 API Key")
        self.credentials_changed.emit(provider)
        self.message.show_message(f"已删除 {provider} API Key。", severity="success")

    def apply_preferences(self) -> None:
        self.provider.setCurrentIndex(
            max(0, self.provider.findData(self.preferences.default_provider))
        )
        self.model.setText(self.preferences.default_model)
        self.default_rubric.set_path(self.preferences.default_rubric)
        self.external_search.setChecked(self.preferences.external_search)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.preferences.theme)))
        self.motion.setCurrentIndex(max(0, self.motion.findData(self.preferences.motion)))

    def show_preferences_saved(self) -> None:
        self.message.show_message("设置已保存。", severity="success")

    def show_preferences_error(self, message: str) -> None:
        self.message.show_message(f"设置保存失败：{message}", severity="danger")

    def _browse_rubric(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择默认 Rubric", "", "YAML (*.yaml *.yml)")
        if path:
            self.default_rubric.set_path(path)

    def _save_preferences(self) -> None:
        self.preferences.default_provider = str(self.provider.currentData())
        self.preferences.default_model = self.model.text().strip()
        rubric = self.default_rubric.path()
        if rubric is not None:
            if not rubric.is_file():
                message = "请选择存在的 Rubric YAML 文件"
                self.default_rubric.set_invalid(message)
                self.message.show_message(message, severity="danger")
                return
            validation = self.service.validate_rubric(
                rubric, profile_path=self.profile_path
            )
            if not validation.valid:
                message = "；".join(validation.errors)
                self.default_rubric.set_invalid(message)
                self.message.show_message(message, severity="danger")
                return
        self.default_rubric.set_invalid(None)
        self.preferences.default_rubric = (
            str(rubric.resolve()) if rubric and rubric.is_file() else ""
        )
        self.preferences.external_search = self.external_search.isChecked()
        self.preferences.theme = str(self.theme.currentData())
        self.preferences.motion = str(self.motion.currentData())
        self.preferences_changed.emit()
        self.theme_changed.emit(self.preferences.theme)

    def _open_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
