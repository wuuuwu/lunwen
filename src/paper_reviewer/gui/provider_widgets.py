from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.domain.provider import ModelApiProtocol, normalize_base_url
from paper_reviewer.gui.theme import set_fluent_property

_INVALID_INDEX = QModelIndex()


def _value(item: object, name: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _is_archived(item: object) -> bool:
    value = _value(item, "is_archived", None)
    if value is not None:
        return bool(value)
    return _value(item, "archived_at") is not None


def _protocol_value(item: object) -> str:
    value = _value(item, "protocol", ModelApiProtocol.CHAT_COMPLETIONS)
    return getattr(value, "value", str(value))


def protocol_label(protocol: object) -> str:
    value = getattr(protocol, "value", str(protocol))
    return {
        ModelApiProtocol.CHAT_COMPLETIONS.value: "Chat Completions",
        ModelApiProtocol.RESPONSES.value: "Responses API",
    }.get(value, str(value))


@dataclass(frozen=True)
class ProviderFormValues:
    display_name: str
    protocol: ModelApiProtocol
    base_url: str
    default_model: str
    api_key: str


class ProviderTableModel(QAbstractTableModel):
    """A small, stable model for the settings page provider inventory."""

    HEADERS = ("名称", "接口协议", "Base URL", "默认模型", "状态")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[object] = []
        self._key_state: dict[str, bool] = {}

    def set_items(self, items: list[object], key_state: dict[str, bool] | None = None) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._key_state = dict(key_state or {})
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX
    ) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole or orientation is not Qt.Orientation.Horizontal:
            return None
        return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        ref = str(_value(item, "provider_ref", "custom:" + str(_value(item, "provider_id", ""))))
        archived = _is_archived(item)
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 2:
            return str(_value(item, "base_url", ""))
        if role == Qt.ItemDataRole.AccessibleDescriptionRole and index.column() == 4:
            return (
                "已归档"
                if archived
                else ("已配置 API Key" if self._key_state.get(ref, False) else "缺少 API Key")
            )
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if index.column() == 0:
            return str(_value(item, "display_name", "未命名 Provider"))
        if index.column() == 1:
            return protocol_label(_protocol_value(item))
        if index.column() == 2:
            return str(_value(item, "base_url", ""))
        if index.column() == 3:
            return str(_value(item, "default_model", ""))
        if archived:
            return "已归档"
        return "已配置 Key" if self._key_state.get(ref, False) else "缺少 Key"

    def item(self, row: int) -> object | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def provider_ref(self, row: int) -> str:
        item = self.item(row)
        if item is None:
            return ""
        return str(_value(item, "provider_ref", "custom:" + str(_value(item, "provider_id", ""))))


class ProviderTableView(QTableView):
    provider_activated = Signal(str)

    def __init__(self, model: ProviderTableModel) -> None:
        super().__init__()
        self.provider_model = model
        self.setObjectName("customProvidersTable")
        self.setModel(model)
        self.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setMinimumHeight(150)
        self.setAccessibleName("自定义 Provider 列表")
        self.doubleClicked.connect(self._activated)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(
            1, self.horizontalHeader().ResizeMode.ResizeToContents
        )
        self.horizontalHeader().setSectionResizeMode(2, self.horizontalHeader().ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(3, self.horizontalHeader().ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(
            4, self.horizontalHeader().ResizeMode.ResizeToContents
        )

    def _activated(self, index: QModelIndex) -> None:
        if index.isValid():
            self.provider_activated.emit(self.provider_model.provider_ref(index.row()))


class ProviderEditorDialog(QDialog):
    """Create/edit dialog; persistence is deliberately owned by SettingsPage."""

    save_requested = Signal()
    test_requested = Signal(object)

    def __init__(
        self,
        *,
        existing: object | None = None,
        has_key: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._existing = existing
        self._replace_mode = existing is None
        self.setObjectName("providerEditorDialog")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setWindowTitle("添加自定义 Provider" if existing is None else "编辑自定义 Provider")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(14)
        self.description = QLabel(
            "使用 OpenAI-compatible 接口。远程地址必须为 HTTPS；HTTP 仅允许本机回环地址。"
        )
        self.description.setWordWrap(True)
        self.description.setProperty("fluentType", "secondary")
        root.addWidget(self.description)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.display_name = QLineEdit()
        self.display_name.setObjectName("providerDisplayName")
        self.display_name.setAccessibleName("Provider 显示名称")
        self.display_name.setPlaceholderText("例如：校内模型")
        self.protocol = QComboBox()
        self.protocol.setObjectName("providerProtocol")
        self.protocol.setAccessibleName("Provider 接口协议")
        self.protocol.addItem("Chat Completions", ModelApiProtocol.CHAT_COMPLETIONS)
        self.protocol.addItem("Responses API", ModelApiProtocol.RESPONSES)
        self.base_url = QLineEdit()
        self.base_url.setObjectName("providerBaseUrl")
        self.base_url.setAccessibleName("Provider Base URL")
        self.base_url.setPlaceholderText("https://example.com/v1")
        self.api_key = QLineEdit()
        self.api_key.setObjectName("providerApiKey")
        self.api_key.setAccessibleName("Provider API Key")
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(
            "输入新 Key；留空则保留现有 Key" if existing else "输入 API Key"
        )
        self.default_model = QLineEdit()
        self.default_model.setObjectName("providerDefaultModel")
        self.default_model.setAccessibleName("Provider 默认模型")
        self.default_model.setPlaceholderText("例如：reviewer-v1")
        form.addRow("显示名称", self.display_name)
        form.addRow("接口协议", self.protocol)
        form.addRow("Base URL", self.base_url)
        form.addRow("API Key", self.api_key)
        form.addRow("默认模型", self.default_model)
        root.addLayout(form)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setAccessibleName("Provider 兼容性测试结果")
        self.error_label.setProperty("fluentType", "danger")
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.replace_endpoint = QPushButton("更换端点或协议…")
        self.replace_endpoint.setObjectName("replaceProviderEndpointButton")
        self.replace_endpoint.setAccessibleName("更换 Provider 端点或协议")
        self.replace_endpoint.setToolTip("创建新的 Provider 配置，并归档当前配置")
        self.replace_endpoint.clicked.connect(self._enable_replacement)
        if existing is None:
            self.replace_endpoint.hide()
        else:
            root.addWidget(self.replace_endpoint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("saveProviderButton")
        self.save_button.setAccessibleName("保存 Provider")
        set_fluent_property(self.save_button, "fluentAppearance", "primary")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setAccessibleName("取消 Provider 编辑")
        self.test_button = QPushButton("测试兼容性")
        self.test_button.setObjectName("testProviderButton")
        self.test_button.setAccessibleName("测试 Provider 兼容性")
        self.test_button.setToolTip("将发送一次可能计费的最小工具调用请求")
        self.buttons.addButton(self.test_button, QDialogButtonBox.ButtonRole.ActionRole)
        self.buttons.accepted.connect(self._save_clicked)
        self.buttons.rejected.connect(self.reject)
        self.test_button.clicked.connect(self._test_clicked)
        root.addWidget(self.buttons)

        if existing is not None:
            self.display_name.setText(str(_value(existing, "display_name", "")))
            protocol = _protocol_value(existing)
            protocol_index = self.protocol.findData(protocol)
            if protocol_index < 0:
                protocol_index = self.protocol.findData(ModelApiProtocol(protocol))
            self.protocol.setCurrentIndex(max(0, protocol_index))
            self.base_url.setText(str(_value(existing, "base_url", "")))
            self.default_model.setText(str(_value(existing, "default_model", "")))
            self.base_url.setReadOnly(True)
            self.protocol.setEnabled(False)
            self.description.setText(
                "可修改显示名称和默认模型。端点与协议不可原地修改；如需替换，请使用“更换端点或协议”。"
            )
            if has_key:
                self.api_key.setPlaceholderText("已安全保存；输入新 Key 可轮换")
            else:
                self.api_key.setPlaceholderText("尚未配置 Key；输入 API Key")

        self.setTabOrder(self.display_name, self.protocol)
        self.setTabOrder(self.protocol, self.base_url)
        self.setTabOrder(self.base_url, self.api_key)
        self.setTabOrder(self.api_key, self.default_model)
        self.setTabOrder(self.default_model, self.test_button)

    @property
    def provider_ref(self) -> str:
        if not self._existing:
            return ""
        return str(
            _value(
                self._existing,
                "provider_ref",
                "custom:" + str(_value(self._existing, "provider_id", "")),
            )
        )

    @property
    def replacement_mode(self) -> bool:
        return self._replace_mode

    def values(self) -> ProviderFormValues:
        protocol_value = self.protocol.currentData()
        protocol = (
            protocol_value
            if isinstance(protocol_value, ModelApiProtocol)
            else ModelApiProtocol(str(protocol_value))
        )
        return ProviderFormValues(
            display_name=self.display_name.text().strip(),
            protocol=protocol,
            base_url=self.base_url.text().strip(),
            default_model=self.default_model.text().strip(),
            api_key=self.api_key.text(),
        )

    def validate_fields(self, *, require_api_key: bool = False) -> ProviderFormValues | None:
        values = self.values()
        errors: list[str] = []
        for field, message in (
            (self.display_name, "显示名称不能为空"),
            (self.default_model, "默认模型不能为空"),
        ):
            invalid = not field.text().strip()
            set_fluent_property(field, "fluentInvalid", invalid)
            field.setAccessibleDescription(message if invalid else "")
            if invalid:
                errors.append(message)
        needs_url = self._existing is None or self._replace_mode
        if needs_url:
            try:
                normalize_base_url(values.base_url)
                invalid_url = False
            except ValueError as error:
                invalid_url = True
                errors.append(str(error))
            set_fluent_property(self.base_url, "fluentInvalid", invalid_url)
            self.base_url.setAccessibleDescription(errors[-1] if invalid_url else "")
        if require_api_key and not values.api_key.strip():
            set_fluent_property(self.api_key, "fluentInvalid", True)
            self.api_key.setAccessibleDescription("API Key 不能为空")
            errors.append("API Key 不能为空")
        else:
            set_fluent_property(self.api_key, "fluentInvalid", False)
            self.api_key.setAccessibleDescription("")
        if errors:
            self.error_label.setText("；".join(errors))
            self.error_label.show()
            return None
        self.error_label.hide()
        return values

    def set_busy(self, busy: bool) -> None:
        for widget in (
            self.display_name,
            self.protocol,
            self.base_url,
            self.api_key,
            self.default_model,
            self.test_button,
            self.save_button,
            self.cancel_button,
            self.replace_endpoint,
        ):
            widget.setEnabled(not busy)
        if self._existing is not None and not self._replace_mode:
            self.protocol.setEnabled(False)
            self.base_url.setEnabled(False)
        set_fluent_property(self.save_button, "fluentBusy", busy)
        set_fluent_property(self.test_button, "fluentBusy", busy)
        if busy:
            self.save_button.setText("处理中…")
            self.test_button.setText("测试中…")
        else:
            self.save_button.setText("保存")
            self.test_button.setText("测试兼容性")

    def show_test_result(self, success: bool, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setAccessibleDescription(message)
        self.error_label.setProperty("fluentType", "secondary" if success else "danger")
        self.error_label.show()
        self.error_label.style().unpolish(self.error_label)
        self.error_label.style().polish(self.error_label)

    def _enable_replacement(self) -> None:
        self._replace_mode = True
        self.protocol.setEnabled(True)
        self.base_url.setReadOnly(False)
        self.base_url.clear()
        self.base_url.setFocus(Qt.FocusReason.OtherFocusReason)
        self.replace_endpoint.hide()
        self.description.setText(
            "正在创建新配置。保存成功后，当前 Provider 会被归档，历史任务仍可按旧端点恢复。"
        )
        self.api_key.setPlaceholderText("输入新配置的 API Key")

    def _save_clicked(self) -> None:
        if self.validate_fields(
            require_api_key=self._existing is None or self._replace_mode
        ):
            self.save_requested.emit()

    def _test_clicked(self) -> None:
        values = self.validate_fields(
            require_api_key=not bool(self.provider_ref) or self._replace_mode
        )
        if values is not None:
            self.test_requested.emit(values)


# Public semantic alias for callers that refer to the feature as a custom
# provider dialog.  Keep the implementation name descriptive for the edit and
# replacement modes as well.
CustomProviderDialog = ProviderEditorDialog
