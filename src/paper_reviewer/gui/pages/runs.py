from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.models import RunSummary
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.models import RunsFilterProxyModel, RunsTableModel
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import PageHeader


class RunsPage(QWidget):
    refresh_requested = Signal()
    run_open_requested = Signal(str)

    def __init__(self, icons: FluentIconService) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(PageHeader("任务记录", "查看评测状态、打开报告或恢复失败任务。"))

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索论文名称")
        self.search.setAccessibleName("搜索论文名称")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", "")
        self.status_filter.addItem("进行中", "active")
        self.status_filter.addItem("待人工复核", "hard_rule")
        self.status_filter.addItem("专家评审", "panel")
        self.status_filter.addItem("已完成", "reported")
        self.status_filter.addItem("失败或取消", "problem")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setIcon(icons.icon("refresh"))
        set_fluent_property(self.refresh_button, "fluentAppearance", "subtle")
        self.refresh_button.clicked.connect(self.refresh_requested)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.status_filter)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)

        self.model = RunsTableModel(icons)
        self.proxy = RunsFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(16, 16))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._open_index)
        self.table.activated.connect(self._open_index)
        self.search.textChanged.connect(self.proxy.set_search_text)
        self.status_filter.currentIndexChanged.connect(self._apply_status_filter)
        layout.addWidget(self.table, 1)

    def set_items(self, items: list[RunSummary]) -> None:
        self.model.set_items(items)
        self.table.resizeColumnsToContents()

    def set_loading(self, loading: bool) -> None:
        self.refresh_button.setEnabled(not loading)
        set_fluent_property(self.refresh_button, "fluentBusy", loading)

    def _open_index(self, proxy_index: object) -> None:
        from PySide6.QtCore import QModelIndex

        if not isinstance(proxy_index, QModelIndex):
            return
        source = self.proxy.mapToSource(proxy_index)
        run_id = self.model.run_id(source.row())
        if run_id:
            self.run_open_requested.emit(run_id)

    def _apply_status_filter(self) -> None:
        mode = str(self.status_filter.currentData() or "")
        self.proxy.set_status_mode(mode)
