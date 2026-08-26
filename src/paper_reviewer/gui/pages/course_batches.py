from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.domain.batch import BatchItemStatus, BatchRecord, BatchStatus
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader


class _IconProvider(Protocol):
    def icon(
        self,
        name: str,
        *,
        size: int = 20,
        color_role: str = "text_secondary",
    ) -> QIcon:
        """Return a theme-aware Fluent icon."""


class CourseBatchesTableModel(QAbstractTableModel):
    """Read-only summary model for persisted course-paper batches.

    The model deliberately exposes the complete :class:`BatchRecord` through
    ``BatchRole`` while keeping the visible columns concise and Chinese.  The
    page can therefore open a record by ID without leaking implementation
    details such as item IDs into the table.
    """

    BatchRole = Qt.ItemDataRole.UserRole + 1
    BatchIdRole = Qt.ItemDataRole.UserRole + 2
    StatusRole = Qt.ItemDataRole.UserRole + 3
    SourceDirectoryRole = Qt.ItemDataRole.UserRole + 4
    OutputDirectoryRole = Qt.ItemDataRole.UserRole + 5

    HEADERS: ClassVar[tuple[str, ...]] = (
        "创建时间",
        "输入文件夹",
        "论文数",
        "已完成数",
        "状态",
        "输出目录",
    )
    STATUS_TEXT: ClassVar[dict[str, str]] = {
        "created": "已创建",
        "running": "正在批量评测",
        "paused": "已暂停",
        "completed": "已完成",
        "completed_with_errors": "已完成，部分失败",
    }
    STATUS_DESCRIPTION: ClassVar[dict[str, str]] = {
        "created": "批次已创建，等待开始评测",
        "running": "批次正在按顺序评测论文",
        "paused": "批次已停止，可以继续或重试失败项",
        "completed": "批次中的所有论文均已完成",
        "completed_with_errors": "批次已结束，但有论文评测失败",
    }
    STATUS_ICON_NAMES: ClassVar[dict[str, str]] = {
        "created": "info",
        "running": "play",
        "paused": "stop",
        "completed": "check",
        "completed_with_errors": "warning",
    }

    def __init__(self, icons: _IconProvider | None = None) -> None:
        super().__init__()
        self.items: list[BatchRecord] = []
        self._icons = icons

    def set_items(self, items: Iterable[BatchRecord]) -> None:
        self.beginResetModel()
        self.items = list(items)
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
        status = _enum_value(item.status)
        values = self._display_values(item, status)

        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == self.BatchRole:
            return item
        if role == self.BatchIdRole:
            return item.batch_id
        if role == self.StatusRole:
            return status
        if role == self.SourceDirectoryRole:
            return str(item.request.source_dir)
        if role == self.OutputDirectoryRole:
            return str(item.request.output_dir)
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 4:
            return self._status_icon(status)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item, index.column(), status)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.HEADERS[index.column()]}：{values[index.column()]}"
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return self._accessible_description(item, status)
        return None

    def batch_at(self, row: int) -> BatchRecord | None:
        return self.items[row] if 0 <= row < len(self.items) else None

    def batch_id(self, row: int) -> str:
        item = self.batch_at(row)
        return item.batch_id if item is not None else ""

    def _display_values(self, item: BatchRecord, status: str) -> tuple[str, ...]:
        completed = sum(
            _enum_value(batch_item.status) == BatchItemStatus.COMPLETED.value
            for batch_item in item.items
        )
        return (
            _as_local_time(item.created_at).strftime("%Y-%m-%d %H:%M"),
            str(item.request.source_dir),
            str(len(item.items)),
            str(completed),
            self.STATUS_TEXT.get(status, status or "未知状态"),
            str(item.request.output_dir),
        )

    def _tooltip(self, item: BatchRecord, column: int, status: str) -> str:
        if column == 1:
            return str(item.request.source_dir)
        if column == 4:
            details = self.STATUS_DESCRIPTION.get(status, status or "未知批次状态")
            if item.error:
                details += f"\n错误：{item.error}"
            return details
        if column == 5:
            return str(item.request.output_dir)
        return ""

    def _accessible_description(self, item: BatchRecord, status: str) -> str:
        completed = sum(
            _enum_value(batch_item.status) == BatchItemStatus.COMPLETED.value
            for batch_item in item.items
        )
        description = self.STATUS_DESCRIPTION.get(status, status or "未知批次状态")
        return (
            f"批次 {item.batch_id}，共 {len(item.items)} 篇论文，"
            f"已完成 {completed} 篇。{description}。"
        )

    def _status_icon(self, status: str) -> QIcon:
        if self._icons is None:
            return QIcon()
        return self._icons.icon(self.STATUS_ICON_NAMES.get(status, "info"), size=16)


class CourseBatchesFilterProxyModel(QSortFilterProxyModel):
    """Case-insensitive search over all human-readable batch columns."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)


class CourseBatchesPage(QWidget):
    """Batch history page for the independent course-paper application."""

    refresh_requested = Signal()
    batch_open_requested = Signal(str)

    def __init__(self, icons: FluentIconService) -> None:
        super().__init__()
        self.setObjectName("courseBatchesPage")
        self._loading = False
        self._error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "批次记录",
                "查看课程论文批量评测的进度、结果和输出目录。",
            )
        )

        self.message = MessageBar(icons)
        self.message.setObjectName("courseBatchesMessageBar")
        self.message.setAccessibleName("批次记录提示")
        layout.addWidget(self.message)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("courseBatchesSearch")
        self.search.setPlaceholderText("搜索输入文件夹或输出目录")
        self.search.setAccessibleName("搜索批次记录")
        self.search.setToolTip("按输入文件夹或输出目录筛选批次记录")

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setObjectName("refreshCourseBatchesButton")
        self.refresh_button.setIcon(icons.icon("refresh"))
        self.refresh_button.setAccessibleName("刷新批次记录")
        self.refresh_button.setToolTip("重新加载已保存的课程论文批次")
        set_fluent_property(self.refresh_button, "fluentAppearance", "subtle")
        self.refresh_button.clicked.connect(self.refresh_requested)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.model = CourseBatchesTableModel(icons)
        self.proxy = CourseBatchesFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("courseBatchesTable")
        self.table.setAccessibleName("课程论文批次记录")
        self.table.setAccessibleDescription(
            "显示创建时间、输入文件夹、论文数量、批次状态和输出目录；双击或按 Enter 打开批次详情。"
        )
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setIconSize(QSize(16, 16))
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._open_index)
        self.table.activated.connect(self._open_index)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        layout.addWidget(self.table, 1)

        QWidget.setTabOrder(self.search, self.refresh_button)
        QWidget.setTabOrder(self.refresh_button, self.table)

    def set_items(self, items: list[BatchRecord]) -> None:
        self._error = ""
        self.model.set_items(items)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.message.clear()

    def set_loading(self, loading: bool) -> None:
        self._loading = loading
        self.refresh_button.setEnabled(not loading)
        set_fluent_property(self.refresh_button, "fluentBusy", loading)
        self.refresh_button.setText("正在刷新…" if loading else "刷新")

    def show_error(self, message: str) -> None:
        self._error = str(message).strip()
        self.set_loading(False)
        self.message.show_message(self._error or "批次记录加载失败。", severity="danger")

    def _open_index(self, index: object) -> None:
        if not isinstance(index, QModelIndex) or not index.isValid():
            return
        source = self.proxy.mapToSource(index)
        batch_id = self.model.batch_id(source.row())
        if batch_id:
            self.batch_open_requested.emit(batch_id)


# Short aliases make the page convenient to import from code that calls the
# destination simply "batches", while preserving the explicit course name.
BatchesPage = CourseBatchesPage
BatchRecordsTableModel = CourseBatchesTableModel


def _enum_value(value: BatchStatus | BatchItemStatus | str) -> str:
    return str(getattr(value, "value", value))


def _as_local_time(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone()
