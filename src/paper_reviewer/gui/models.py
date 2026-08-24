from __future__ import annotations

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
            return (
                item.paper_name,
                item.rubric_id,
                f"{item.provider} / {item.model}",
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
        "hard_rule": {"awaiting_hard_rule_confirmation"},
        "panel": {
            "panel_reviewing",
            "supplemental_reviewing",
            "awaiting_panel_review",
        },
        "reported": {"reported"},
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
