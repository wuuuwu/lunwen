from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from PySide6.QtCore import (
    QAbstractListModel,
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QIcon

from paper_reviewer.application.models import RunSummary
from paper_reviewer.domain.review import ReviewFinding


@dataclass(frozen=True)
class ProviderDisplay:
    """Non-secret information used by provider selectors and run summaries.

    The GUI deliberately keeps this object independent from the credential
    store.  A service may return a ``ProviderConnection`` or a pydantic model;
    ``provider_display`` below normalizes either shape without exposing a key.
    """

    provider_ref: str
    display_name: str
    protocol: str
    base_url: str = ""
    default_model: str = ""
    has_key: bool = False
    archived: bool = False


_BUILTIN_PROVIDER_DISPLAY: dict[str, ProviderDisplay] = {
    "openai": ProviderDisplay(
        "openai", "OpenAI", "chat_completions", "https://api.openai.com/v1", "gpt-5-mini"
    ),
    "openai_responses": ProviderDisplay(
        "openai_responses",
        "OpenAI",
        "responses",
        "https://api.openai.com/v1",
        "gpt-5-mini",
    ),
    "deepseek": ProviderDisplay(
        "deepseek",
        "DeepSeek",
        "chat_completions",
        "https://api.deepseek.com",
        "deepseek-chat",
    ),
}


def _provider_field(source: object | None, *names: str, default: object = "") -> object:
    if source is None:
        return default
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def provider_protocol_text(protocol: object) -> str:
    value = getattr(protocol, "value", protocol)
    return {
        "chat_completions": "Chat Completions",
        "responses": "Responses API",
    }.get(str(value), str(value) or "未知接口")


def _base_provider_name(name: str, protocol: str) -> str:
    """Avoid repeating the protocol when a service label already includes it."""

    suffix = f" · {provider_protocol_text(protocol)}"
    return name.removesuffix(suffix).strip() or name


def provider_display(
    provider_ref: str,
    source: object | None = None,
    *,
    has_key: bool | None = None,
) -> ProviderDisplay:
    """Normalize a connection/snapshot or an old provider reference.

    ``source`` is intentionally optional: old task rows only contain a
    provider reference, while newer rows may carry snapshot fields.
    """

    reference = str(provider_ref or "")
    fallback = _BUILTIN_PROVIDER_DISPLAY.get(
        reference,
        ProviderDisplay(
            reference,
            "自定义 Provider" if reference.startswith("custom:") else reference,
            "",
        ),
    )
    name = str(
        _provider_field(
            source,
            "display_name",
            "provider_display_name",
            default=fallback.display_name,
        )
        or fallback.display_name
    )
    protocol = str(
        _provider_field(source, "protocol", "provider_protocol", default=fallback.protocol)
        or fallback.protocol
    )
    name = _base_provider_name(name, protocol)
    base_url = str(
        _provider_field(source, "base_url", "provider_base_url", default=fallback.base_url)
        or fallback.base_url
    )
    default_model = str(
        _provider_field(source, "default_model", default=fallback.default_model)
        or fallback.default_model
    )
    archived = bool(_provider_field(source, "archived", "is_archived", default=False))
    return ProviderDisplay(
        provider_ref=reference,
        display_name=name,
        protocol=protocol,
        base_url=base_url,
        default_model=default_model,
        has_key=fallback.has_key if has_key is None else has_key,
        archived=archived,
    )


def provider_label(provider_ref: str, model: str, source: object | None = None) -> str:
    """Return the safe task-facing label; never shows custom IDs or Base URLs."""

    display = provider_display(provider_ref, source)
    protocol = provider_protocol_text(display.protocol)
    provider_name = display.display_name
    if protocol and protocol != provider_name:
        provider_name = f"{provider_name} · {protocol}"
    return f"{provider_name} · {model}"


def provider_connections(service: object) -> list[ProviderDisplay]:
    """Read the service provider catalog with a legacy built-in fallback."""

    method = getattr(service, "list_provider_connections", None)
    if not callable(method):
        method = getattr(service, "list_providers", None)
    values: object = []
    try:
        object.__setattr__(service, "_provider_catalog_error", "")
    except Exception:
        pass
    if callable(method):
        try:
            values = method(include_archived=False)
        except TypeError:
            values = method()
        except Exception as error:
            try:
                object.__setattr__(service, "_provider_catalog_error", str(error))
            except Exception:
                pass
            values = []
    if values is None:
        values = []
    result: list[ProviderDisplay] = []
    if isinstance(values, Iterable) and not isinstance(values, (str, bytes, dict)):
        for value in values:
            reference = str(
                _provider_field(value, "provider_ref", "ref", "provider_id", default="")
            )
            if (
                reference
                and reference not in _BUILTIN_PROVIDER_DISPLAY
                and not reference.startswith("custom:")
            ):
                # A profile object exposes the bare UUID while a connection
                # exposes ``custom:<uuid>``.  Normalize both forms here.
                if len(reference) == 32:
                    reference = f"custom:{reference}"
                else:
                    continue
            if not reference:
                continue
            key_value = _provider_field(value, "has_key", default=None)
            if key_value is None:
                key_method = getattr(service, "provider_has_key", None)
                try:
                    key_value = key_method(reference) if callable(key_method) else None
                except Exception:
                    key_value = False
            if reference and not reference.startswith("custom:"):
                result.append(provider_display(reference, value, has_key=bool(key_value)))
            elif reference:
                result.append(provider_display(reference, value, has_key=bool(key_value)))
    by_ref = {item.provider_ref: item for item in result}
    builtin_items = [
        by_ref.get(reference, fallback)
        for reference, fallback in _BUILTIN_PROVIDER_DISPLAY.items()
    ]
    custom_items = [item for item in result if item.provider_ref.startswith("custom:")]
    return builtin_items + custom_items


def provider_has_key(service: object, provider_ref: str) -> bool:
    method = getattr(service, "provider_has_key", None)
    if callable(method):
        try:
            if bool(method(provider_ref)):
                return True
        except Exception:
            pass
    credentials = getattr(service, "credentials", None)
    method = getattr(credentials, "has", None)
    if callable(method):
        try:
            return bool(method(provider_ref))
        except Exception:
            return False
    return False


@dataclass(frozen=True)
class NavigationItem:
    item_id: str
    text: str
    icon: QIcon
    tooltip: str


class NavigationModel(QAbstractListModel):
    IdRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, items: list[NavigationItem]) -> None:
        super().__init__()
        self.items = items

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.items)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.text
        if role == Qt.ItemDataRole.DecorationRole:
            return item.icon
        if role in {Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole}:
            return item.tooltip
        if role == self.IdRole:
            return item.item_id
        return None

    def item_id(self, index: QModelIndex) -> str:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return ""
        return self.items[index.row()].item_id


class _IconProvider(Protocol):
    def icon(self, name: str, *, size: int = 20, color_role: str = "text_secondary") -> QIcon:
        """Return a theme-aware Fluent icon."""


class RunsTableModel(QAbstractTableModel):
    StatusRole = Qt.ItemDataRole.UserRole + 1
    HEADERS: ClassVar[list[str]] = [
        "论文", "Rubric", "Provider / 模型", "创建时间", "状态", "更新时间"
    ]
    STATUS_TEXT: ClassVar[dict[str, str]] = {
        "created": "已创建",
        "ingesting": "正在解析",
        "ingested": "解析完成",
        "building_evidence": "收集证据",
        "evidence_ready": "证据就绪",
        "reviewing": "正在评测",
        "scoring": "正在评分",
        "auditing": "正在审计",
        "awaiting_hard_rule_confirmation": "待人工复核",
        "panel_reviewing": "专家初评",
        "supplemental_reviewing": "专家复评",
        "awaiting_panel_review": "待面板复核",
        "synthesizing": "正在汇总",
        "meta_reviewing": "汇总评测",
        "validating": "生成报告",
        "reported_pending_human_review": "评测完成 · 待人工复核",
        "reported": "已完成",
        "retryable_failure": "失败，可恢复",
        "fatal_failure": "失败",
        "cancelled": "已取消",
    }
    # Status is conveyed by text as well as a Fluent icon.  The icon names
    # intentionally come from the existing single icon family; callers do not
    # need to know about colors or theme variants.
    STATUS_ICON_NAMES: ClassVar[dict[str, str]] = {
        "created": "info",
        "ingesting": "search",
        "ingested": "check",
        "building_evidence": "search",
        "evidence_ready": "check",
        "reviewing": "play",
        "scoring": "rubric",
        "auditing": "check",
        "awaiting_hard_rule_confirmation": "warning",
        "panel_reviewing": "play",
        "supplemental_reviewing": "play",
        "awaiting_panel_review": "warning",
        "synthesizing": "rubric",
        "meta_reviewing": "rubric",
        "validating": "check",
        "reported_pending_human_review": "warning",
        "reported": "check",
        "retryable_failure": "warning",
        "fatal_failure": "error",
        "cancelled": "stop",
    }
    STATUS_DESCRIPTION: ClassVar[dict[str, str]] = {
        "created": "任务已创建，等待开始",
        "ingesting": "正在解析论文文件",
        "ingested": "论文解析已完成",
        "building_evidence": "正在收集外部证据",
        "evidence_ready": "外部证据已准备完成",
        "reviewing": "正在进行评阅",
        "scoring": "正在生成九项诊断评分",
        "auditing": "正在执行确定性审计",
        "awaiting_hard_rule_confirmation": "等待人工确认否决项",
        "panel_reviewing": "正在进行三人专家初评",
        "supplemental_reviewing": "正在进行条件性专家复评",
        "awaiting_panel_review": "等待人工面板复核",
        "synthesizing": "正在汇总评语和风险结论",
        "meta_reviewing": "正在汇总评测结果",
        "validating": "正在验证并生成报告",
        "reported_pending_human_review": "AI 评测和报告已完成，等待人工复核",
        "reported": "评测报告已生成",
        "retryable_failure": "任务失败，可以恢复",
        "fatal_failure": "任务失败，无法自动恢复",
        "cancelled": "任务已取消",
    }

    def __init__(self, icons: _IconProvider | None = None) -> None:
        super().__init__()
        self.items: list[RunSummary] = []
        self._icons = icons

    def set_items(self, items: list[RunSummary]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.items)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation is Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            run_source = getattr(item, "provider_snapshot", None) or item
            return (
                item.paper_name,
                item.rubric_id,
                provider_label(item.provider, item.model, run_source),
                _as_local_time(item.created_at).strftime("%Y-%m-%d %H:%M"),
                self.STATUS_TEXT.get(item.status.value, item.status.value),
                _as_local_time(item.updated_at).strftime("%Y-%m-%d %H:%M"),
            )[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 4:
                return self.STATUS_DESCRIPTION.get(item.status.value, item.status.value)
            return item.error or item.run_id
        if role == Qt.ItemDataRole.UserRole:
            return item.run_id
        if role == self.StatusRole:
            return item.status.value
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 4:
            return self._status_icon(item.status.value)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            value = self.data(index, Qt.ItemDataRole.DisplayRole)
            return f"{self.HEADERS[index.column()]}：{value}"
        if role == Qt.ItemDataRole.AccessibleDescriptionRole and index.column() == 4:
            status = self.STATUS_TEXT.get(item.status.value, item.status.value)
            description = self.STATUS_DESCRIPTION.get(item.status.value, status)
            return f"状态：{status}。{description}。"
        return None

    def _status_icon(self, status: str) -> QIcon:
        if self._icons is None:
            return QIcon()
        icon_name = self.STATUS_ICON_NAMES.get(status, "info")
        return self._icons.icon(icon_name, size=16)

    def run_id(self, row: int) -> str:
        return self.items[row].run_id if 0 <= row < len(self.items) else ""


class RunsFilterProxyModel(QSortFilterProxyModel):
    STATUS_GROUPS: ClassVar[dict[str, set[str]]] = {
        "active": {
            "created",
            "ingesting",
            "ingested",
            "building_evidence",
            "evidence_ready",
            "reviewing",
            "scoring",
            "auditing",
            "awaiting_hard_rule_confirmation",
            "panel_reviewing",
            "supplemental_reviewing",
            "awaiting_panel_review",
            "synthesizing",
            "meta_reviewing",
            "validating",
        },
        "hard_rule": {
            "awaiting_hard_rule_confirmation",
            "awaiting_panel_review",
            "reported_pending_human_review",
        },
        "panel": {
            "panel_reviewing",
            "supplemental_reviewing",
            "awaiting_panel_review",
        },
        "reported": {"reported", "reported_pending_human_review"},
        "problem": {"retryable_failure", "fatal_failure", "cancelled"},
    }

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._search_text = ""
        self._status_mode = ""

    def set_search_text(self, text: str) -> None:
        self.beginFilterChange()
        self._search_text = text.casefold().strip()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_status_mode(self, mode: str) -> None:
        self.beginFilterChange()
        self._status_mode = mode
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(
        self,
        source_row: int,
        source_parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        model = self.sourceModel()
        if model is None:
            return False
        paper = str(model.data(model.index(source_row, 0, source_parent))).casefold()
        status = str(
            model.data(
                model.index(source_row, 4, source_parent),
                RunsTableModel.StatusRole,
            )
            or ""
        )
        allowed = self.STATUS_GROUPS.get(self._status_mode)
        matches_status = allowed is None or status in allowed
        return matches_status and (not self._search_text or self._search_text in paper)


def _as_local_time(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone()


class FindingsTableModel(QAbstractTableModel):
    HEADERS: ClassVar[list[str]] = ["严重程度", "维度", "问题摘要", "置信度", "人工核查"]
    SEVERITY_TEXT: ClassVar[dict[str, str]] = {
        "critical": "严重",
        "major": "主要",
        "minor": "次要",
        "suggestion": "建议",
    }

    def __init__(self) -> None:
        super().__init__()
        self.items: list[ReviewFinding] = []

    def set_items(self, items: list[ReviewFinding]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.items)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation is Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                self.SEVERITY_TEXT.get(item.severity.value, item.severity.value),
                item.dimension_id,
                item.claim,
                f"{item.confidence:.0%}",
                "需要" if item.needs_human_check else "否",
            )[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return item.finding_id
        return None

    def finding(self, row: int) -> ReviewFinding | None:
        return self.items[row] if 0 <= row < len(self.items) else None
