from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
)
from PySide6.QtGui import QIcon

from paper_reviewer.application.metadata_recheck import (
    metadata_recheck_fields,
    metadata_requires_local_recheck,
)
from paper_reviewer.domain.batch import BatchItem, BatchItemStatus

_FIELD_LABELS: dict[str, str] = {
    "student_name": "姓名",
    "student_id": "学号",
    "major": "专业",
    "paper_title": "题目",
}


class _IconProvider(Protocol):
    def icon(
        self,
        name: str,
        *,
        size: int = 20,
        color_role: str = "text_secondary",
    ) -> QIcon:
        """Return a theme-aware Fluent icon."""


@dataclass(frozen=True)
class BatchSourcePreview:
    """A cheap, non-hashed source row used before a batch is created."""

    path: Path
    size_bytes: int


class BatchSourcePreviewModel(QAbstractTableModel):
    """Read-only preview of the top-level PDFs selected for a batch."""

    PathRole = Qt.ItemDataRole.UserRole + 1
    HEADERS: ClassVar[tuple[str, ...]] = ("论文文件", "大小", "扫描结果")

    def __init__(self) -> None:
        super().__init__()
        self.items: list[BatchSourcePreview] = []

    def set_paths(self, paths: list[Path]) -> None:
        previews: list[BatchSourcePreview] = []
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            previews.append(BatchSourcePreview(path=path, size_bytes=size))
        self.beginResetModel()
        self.items = previews
        self.endResetModel()

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.items)

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation is Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
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
        values = (item.path.name, _format_size(item.size_bytes), "可加入批次")
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == self.PathRole:
            return str(item.path)
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(item.path)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.HEADERS[index.column()]}：{values[index.column()]}"
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return f"批次中的第 {index.row() + 1} 篇论文。"
        return None


class BatchItemsTableModel(QAbstractTableModel):
    """Read-only batch item model with transient, event-derived stages."""

    ItemIdRole = Qt.ItemDataRole.UserRole + 1
    RunIdRole = Qt.ItemDataRole.UserRole + 2
    StatusRole = Qt.ItemDataRole.UserRole + 3
    ReportPathRole = Qt.ItemDataRole.UserRole + 4
    MetadataReviewRole = Qt.ItemDataRole.UserRole + 5

    HEADERS: ClassVar[tuple[str, ...]] = (
        "原文件名",
        "姓名",
        "学号",
        "专业",
        "题目",
        "信息核对",
        "当前阶段",
        "状态",
        "总分",
        "报告",
    )
    STATUS_TEXT: ClassVar[dict[str, str]] = {
        "queued": "等待评测",
        "running": "正在评测",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已停止",
        "source_changed": "源文件已变更",
    }
    STATUS_DESCRIPTION: ClassVar[dict[str, str]] = {
        "queued": "论文尚未开始评测",
        "running": "论文正在按课程 Rubric 评测",
        "completed": "课程论文报告已生成",
        "failed": "本篇评测失败，可在批次停止后重试失败项",
        "cancelled": "本篇评测已安全停止，检查点会被保留",
        "source_changed": "源 PDF 在创建批次后发生变化，未继续处理",
    }
    STATUS_ICON_NAMES: ClassVar[dict[str, str]] = {
        "queued": "info",
        "running": "play",
        "completed": "check",
        "failed": "error",
        "cancelled": "stop",
        "source_changed": "warning",
    }
    DEFAULT_STAGE: ClassVar[dict[str, str]] = {
        "queued": "等待开始",
        "running": "正在评测",
        "completed": "报告已生成",
        "failed": "评测失败",
        "cancelled": "已停止",
        "source_changed": "源文件检查",
    }
    STAGE_TEXT: ClassVar[dict[str, str]] = {
        "ingest": "解析论文",
        "ingesting": "解析论文",
        "metadata": "提取学生与论文信息",
        "metadata_extraction": "提取学生与论文信息",
        "evidence": "收集外部证据",
        "reviews": "三名专项 Reviewer 评阅",
        "reviewing": "三名专项 Reviewer 评阅",
        "audit": "确定性审计",
        "auditing": "确定性审计",
        "meta": "Meta Review 汇总",
        "meta_reviewing": "Meta Review 汇总",
        "report": "验证并生成报告",
        "validating": "验证并生成报告",
        "export": "导出课程论文报告",
    }

    def __init__(self, icons: _IconProvider | None = None) -> None:
        super().__init__()
        self.items: list[BatchItem] = []
        self._icons = icons
        self._stages: dict[str, str] = {}

    def set_items(self, items: list[BatchItem]) -> None:
        running_ids = {
            item.item_id
            for item in items
            if _enum_value(item.status) == "running"
        }
        self.beginResetModel()
        self.items = list(items)
        self._stages = {
            item_id: stage
            for item_id, stage in self._stages.items()
            if item_id in running_ids
        }
        self.endResetModel()

    def set_item_stage(self, item_id: str, stage: str) -> None:
        normalized = stage.strip()
        if not normalized:
            self._stages.pop(item_id, None)
        else:
            self._stages[item_id] = self.STAGE_TEXT.get(normalized, normalized)
        row = self.row_for_item(item_id)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 6), self.index(row, 6))

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.items)

    def columnCount(
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation is Qt.Orientation.Horizontal
            and 0 <= section < len(self.HEADERS)
        ):
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
        status = _enum_value(item.status)
        values = self._display_values(item, status)
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == self.ItemIdRole:
            return item.item_id
        if role == self.RunIdRole:
            return item.run_id or ""
        if role == self.StatusRole:
            return status
        if role == self.ReportPathRole:
            return str(item.report_path or "")
        if role == self.MetadataReviewRole:
            return self._metadata_review_value(item)
        if role == Qt.ItemDataRole.DecorationRole:
            if index.column() == 5:
                return self._metadata_review_icon(item)
            if index.column() == 7:
                return self._status_icon(status)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item, index.column(), status)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.HEADERS[index.column()]}：{values[index.column()]}"
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return self._accessible_description(item, status)
        return None

    def item_at(self, row: int) -> BatchItem | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def row_for_item(self, item_id: str) -> int:
        return next(
            (row for row, item in enumerate(self.items) if item.item_id == item_id),
            -1,
        )

    def _display_values(self, item: BatchItem, status: str) -> tuple[str, ...]:
        metadata = item.metadata
        score = "—" if item.total_score is None else f"{item.total_score:.1f}"
        report = "已生成" if item.report_path else "—"
        return (
            item.source.filename,
            metadata.student_name if metadata else "未识别姓名",
            metadata.student_id if metadata else "未识别学号",
            metadata.major if metadata else "未识别专业",
            metadata.paper_title if metadata else "未识别题目",
            self._metadata_review_text(item),
            self._stages.get(item.item_id, self.DEFAULT_STAGE.get(status, status)),
            self.STATUS_TEXT.get(status, status or "未知状态"),
            score,
            report,
        )

    def _tooltip(self, item: BatchItem, column: int, status: str) -> str:
        if column == 0:
            return str(item.source.path)
        if column == 5:
            return self._metadata_review_tooltip(item)
        if column == 7:
            details = self.STATUS_DESCRIPTION.get(status, status)
            if item.error:
                details += f"\n错误：{item.error}"
            if item.warnings:
                details += "\n提示：" + "；".join(item.warnings)
            return details
        if column == 9 and item.report_path:
            return str(item.report_path)
        if item.metadata and 1 <= column <= 4:
            field = ("student_name", "student_id", "major", "paper_title")[column - 1]
            if field in item.metadata.pending_review_fields:
                return f"{_FIELD_LABELS[field]}的自动提取结果需要人工核对。"
            if metadata_requires_local_recheck(item.metadata):
                recheck_fields = metadata_recheck_fields(item.metadata)
                if field in recheck_fields:
                    return (
                        f"{_FIELD_LABELS[field]}可能包含旧版提取异常，"
                        "建议重新检查。"
                    )
        return ""

    def _accessible_description(self, item: BatchItem, status: str) -> str:
        parts = [self.STATUS_DESCRIPTION.get(status, status)]
        if item.metadata and item.metadata.needs_review:
            fields = "、".join(
                _FIELD_LABELS[field] for field in item.metadata.pending_review_fields
            )
            parts.append(f"自动提取的{fields or '学生信息或题目'}需要人工核对")
        elif item.metadata and metadata_requires_local_recheck(item.metadata):
            fields = "、".join(
                _FIELD_LABELS[field] for field in metadata_recheck_fields(item.metadata)
            )
            parts.append(f"历史提取结果建议重新检查：{fields}")
        if item.warnings:
            parts.append("提示：" + "；".join(item.warnings))
        if item.error:
            parts.append("存在可查看的脱敏错误信息")
        return "。".join(part.rstrip("。") for part in parts if part) + "。"

    def _status_icon(self, status: str) -> QIcon:
        if self._icons is None:
            return QIcon()
        return self._icons.icon(self.STATUS_ICON_NAMES.get(status, "info"), size=16)

    @staticmethod
    def _metadata_review_value(item: BatchItem) -> str:
        metadata = item.metadata
        if metadata is None:
            return "missing"
        if metadata.human_reviewed:
            return "reviewed"
        if metadata.needs_review:
            return "pending"
        return "recheck" if metadata_requires_local_recheck(metadata) else "automatic"

    def _metadata_review_text(self, item: BatchItem) -> str:
        value = self._metadata_review_value(item)
        if value == "reviewed":
            return "已人工核对"
        if value == "pending":
            metadata = item.metadata
            assert metadata is not None
            labels = {
                "student_name": "姓名",
                "student_id": "学号",
                "major": "专业",
                "paper_title": "题目",
            }
            fields = "、".join(
                labels.get(field, field) for field in metadata.pending_review_fields
            )
            return f"待核对（{fields}）" if fields else "待核对"
        if value == "recheck":
            metadata = item.metadata
            assert metadata is not None
            fields = "、".join(
                _FIELD_LABELS[field] for field in metadata_recheck_fields(metadata)
            )
            return f"建议重新检查（{fields}）" if fields else "建议重新检查"
        if value == "automatic":
            return "自动提取"
        return "尚未提取"

    def _metadata_review_tooltip(self, item: BatchItem) -> str:
        metadata = item.metadata
        if metadata is None:
            return "尚未提取姓名、学号、专业和题目。"
        if metadata.human_reviewed:
            return "姓名、学号、专业和题目已由用户明确确认。"
        if not metadata_requires_local_recheck(metadata):
            return "自动提取置信度达到当前阈值，尚未经人工核对。"
        parts = ["自动提取的信息尚未经过人工确认。"]
        if metadata.pending_review_fields:
            labels = {
                "student_name": "姓名",
                "student_id": "学号",
                "major": "专业",
                "paper_title": "题目",
            }
            fields = "、".join(
                labels.get(field, field) for field in metadata.pending_review_fields
            )
            parts.append(f"低置信度或占位字段：{fields}。")
        legacy_fields = [
            field
            for field in metadata_recheck_fields(metadata)
            if field not in metadata.pending_review_fields
        ]
        if legacy_fields:
            fields = "、".join(_FIELD_LABELS[field] for field in legacy_fields)
            parts.append(f"历史提取异常字段：{fields}，建议重新检查。")
        if metadata.warnings:
            parts.append("提示：" + "；".join(metadata.warnings))
        return "\n".join(parts)

    def _metadata_review_icon(self, item: BatchItem) -> QIcon:
        if self._icons is None:
            return QIcon()
        value = self._metadata_review_value(item)
        icon_name = {
            "reviewed": "check",
            "pending": "warning",
            "recheck": "warning",
            "automatic": "info",
        }.get(value, "info")
        return self._icons.icon(icon_name, size=16)


def _enum_value(value: BatchItemStatus | str) -> str:
    return str(getattr(value, "value", value))


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.2f} MB"
