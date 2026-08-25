from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from paper_reviewer.gui.operations import AsyncOperationRegistry
from paper_reviewer.gui.pages.settings_sections import (
    CredentialSettingsMixin,
    PreferencesSettingsMixin,
    ProviderSettingsMixin,
)
from paper_reviewer.gui.provider_widgets import (
    ProviderEditorDialog,
    ProviderTableModel,
    ProviderTableView,
)
from paper_reviewer.gui.resource_paths import bundled_config
from paper_reviewer.gui.widgets import MessageBar, PageHeader, PathPicker
from paper_reviewer.gui.worker import AsyncTaskThread


class SettingsPage(
    ProviderSettingsMixin,
    CredentialSettingsMixin,
    PreferencesSettingsMixin,
    QWidget,
):
    """Settings screen with a stable widget contract and split operations.

    Widget construction intentionally remains here: object names, tab order,
    signal wiring and Fluent properties form part of the GUI contract. The
    Provider, credential and preference operations live in internal sections.
    """

    preferences_changed = Signal()
    theme_changed = Signal(str)
    credentials_changed = Signal(str)

    def __init__(
        self,
        service: ReviewApplicationService,
        preferences: GuiPreferences,
        paths: AppPaths,
        icons: FluentIconService,
        operation_registry: AsyncOperationRegistry | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.preferences = preferences
        self.paths = paths
        self.operation_registry = operation_registry
        self.profile_path = bundled_config("three_reviewer.yaml")
        self._provider_dialog: ProviderEditorDialog | None = None
        self._provider_test_workers: list[AsyncTaskThread] = []
        self._provider_action_busy = False
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

        custom_title = QLabel("自定义 Provider")
        custom_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(custom_title)
        custom_description = QLabel(
            "管理多个 OpenAI-compatible 接口配置。API Key 仅保存到系统凭据库，"
            "端点和协议变更会创建新配置。"
        )
        custom_description.setWordWrap(True)
        custom_description.setProperty("fluentType", "secondary")
        layout.addWidget(custom_description)
        custom_toolbar = QHBoxLayout()
        custom_toolbar.setSpacing(8)
        self.provider_filter = QComboBox()
        self.provider_filter.setObjectName("customProviderFilter")
        self.provider_filter.setAccessibleName("自定义 Provider 筛选")
        self.provider_filter.addItem("仅显示活动", False)
        self.provider_filter.addItem("显示活动和归档", True)
        self.provider_filter.currentIndexChanged.connect(self._load_custom_providers)
        self.add_provider_button = QPushButton("添加 Provider")
        self.add_provider_button.setObjectName("addCustomProviderButton")
        self.add_provider_button.setAccessibleName("添加自定义 Provider")
        self.add_provider_button.clicked.connect(self._add_custom_provider)
        custom_toolbar.addWidget(self.provider_filter)
        custom_toolbar.addStretch(1)
        custom_toolbar.addWidget(self.add_provider_button)
        layout.addLayout(custom_toolbar)

        self.provider_table_model = ProviderTableModel(self)
        self.provider_table = ProviderTableView(self.provider_table_model)
        self.provider_table.provider_activated.connect(self._edit_custom_provider)
        layout.addWidget(self.provider_table)

        provider_actions = QHBoxLayout()
        provider_actions.setSpacing(8)
        self.edit_provider_button = QPushButton("编辑")
        self.replace_provider_button = QPushButton("更换端点/协议")
        self.rotate_provider_key_button = QPushButton("轮换 Key")
        self.delete_provider_key_button = QPushButton("删除 Key")
        self.archive_provider_button = QPushButton("归档")
        self.delete_provider_button = QPushButton("永久删除")
        self.restore_provider_button = QPushButton("恢复")
        for button, object_name in (
            (self.edit_provider_button, "editCustomProviderButton"),
            (self.replace_provider_button, "replaceCustomProviderButton"),
            (self.rotate_provider_key_button, "rotateCustomProviderKeyButton"),
            (self.delete_provider_key_button, "deleteCustomProviderKeyButton"),
            (self.archive_provider_button, "archiveCustomProviderButton"),
            (self.restore_provider_button, "restoreCustomProviderButton"),
            (self.delete_provider_button, "deleteCustomProviderButton"),
        ):
            button.setObjectName(object_name)
        for button, name, tooltip in (
            (self.edit_provider_button, "编辑自定义 Provider", "修改显示名称和默认模型"),
            (self.replace_provider_button, "更换 Provider 端点或协议", "创建新配置并归档旧配置"),
            (
                self.rotate_provider_key_button,
                "轮换 Provider API Key",
                "替换系统凭据库中的 API Key",
            ),
            (
                self.delete_provider_key_button,
                "删除 Provider API Key",
                "删除该 Provider 的系统凭据",
            ),
            (self.archive_provider_button, "归档自定义 Provider", "保留配置以便历史任务恢复"),
            (self.restore_provider_button, "恢复自定义 Provider", "将归档配置恢复为活动配置"),
            (
                self.delete_provider_button,
                "永久删除自定义 Provider",
                "仅允许删除未被历史任务引用的归档配置",
            ),
        ):
            button.setAccessibleName(name)
            button.setToolTip(tooltip)
            button.setEnabled(False)
            provider_actions.addWidget(button)
        self.edit_provider_button.clicked.connect(self._edit_selected_provider)
        self.replace_provider_button.clicked.connect(self._replace_selected_provider)
        self.rotate_provider_key_button.clicked.connect(self._rotate_selected_provider_key)
        self.delete_provider_key_button.clicked.connect(self._delete_selected_provider_key)
        self.archive_provider_button.clicked.connect(self._archive_selected_provider)
        self.restore_provider_button.clicked.connect(self._restore_selected_provider)
        self.delete_provider_button.clicked.connect(self._delete_selected_provider)
        self.provider_table.selectionModel().selectionChanged.connect(
            lambda *_args: self._update_provider_actions()
        )
        layout.addLayout(provider_actions)
        self.provider_store_error = QLabel()
        self.provider_store_error.setProperty("fluentType", "danger")
        self.provider_store_error.setWordWrap(True)
        self.provider_store_error.hide()
        layout.addWidget(self.provider_store_error)
        self._load_custom_providers()

        general_title = QLabel("默认评测参数")
        general_title.setProperty("fluentType", "sectionTitle")
        layout.addWidget(general_title)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.provider = QComboBox()
        self.model = QLineEdit(preferences.default_model)
        self._provider_default_models: dict[str, str] = {}
        self._refresh_default_provider_choices(preferences.default_provider)
        self.provider.activated.connect(self._default_provider_activated)
        self.default_rubric = PathPicker(suffix=".yaml", placeholder="默认 Rubric YAML")
        if preferences.default_rubric:
            self.default_rubric.set_path(preferences.default_rubric)
        self.default_rubric.browse_requested.connect(self._browse_rubric)
        self.external_search = QCheckBox("默认启用联网检索与参考文献自动核验")
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
