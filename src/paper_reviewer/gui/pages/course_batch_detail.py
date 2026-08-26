from __future__ import annotations

from itertools import pairwise
from typing import ClassVar

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.domain.batch import (
    BatchEvent,
    BatchItem,
    BatchRecord,
    BatchStatus,
)
from paper_reviewer.gui.batch_models import BatchItemsTableModel
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.theme import set_fluent_property
from paper_reviewer.gui.widgets import MessageBar, PageHeader


class CourseBatchDetailPage(QWidget):
    """Progress and item-level actions for a persisted course-paper batch."""

    stop_requested = Signal(str)
    resume_requested = Signal(str)
    retry_failed_requested = Signal(str)
    open_output_requested = Signal(str)
    run_open_requested = Signal(str)
    metadata_edit_requested = Signal(str, str)

    STATUS_TEXT: ClassVar[dict[str, str]] = {
        "created": "已创建",
        "running": "正在批量评测",
        "paused": "已停止，可继续",
        "completed": "全部完成",
        "completed_with_errors": "已完成，部分论文失败",
    }

    def __init__(self, icons: FluentIconService) -> None:
        super().__init__()
        self.icons = icons
        self._batch: BatchRecord | None = None
        self._busy_action = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(
            PageHeader(
                "批次详情",
                "查看每篇论文的信息提取、评测阶段、分数和报告状态。",
            )
        )

        self.message = MessageBar(icons)
        self.message.setObjectName("courseBatchDetailMessageBar")
        layout.addWidget(self.message)

        summary = QFrame()
        summary.setObjectName("courseBatchSummaryCard")
        summary.setProperty("fluentRole", "card")
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(16, 16, 16, 16)
        summary_layout.setSpacing(8)
        self.batch_title = QLabel("尚未选择批次")
        self.batch_title.setObjectName("courseBatchTitle")
        self.batch_title.setProperty("fluentType", "sectionTitle")
        self.batch_title.setAccessibleName("当前批次")
        self.batch_status = QLabel("状态：—")
        self.batch_status.setObjectName("courseBatchStatus")
        self.batch_status.setAccessibleName("批次状态")
        self.batch_progress = QLabel("进度：0 / 0")
        self.batch_progress.setObjectName("courseBatchProgress")
        self.batch_progress.setAccessibleName("批次处理进度")
        self.batch_output = QLabel("输出目录：—")
        self.batch_output.setObjectName("courseBatchOutputPath")
        self.batch_output.setProperty("fluentType", "secondary")
        self.batch_output.setWordWrap(True)
        self.batch_output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.batch_output.setAccessibleName("批次报告输出目录")
        summary_layout.addWidget(self.batch_title)
        summary_layout.addWidget(self.batch_status)
        summary_layout.addWidget(self.batch_progress)
        summary_layout.addWidget(self.batch_output)
        layout.addWidget(summary)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.stop_button = QPushButton("停止批次")
        self.stop_button.setObjectName("stopCourseBatchButton")
        self.stop_button.setIcon(icons.icon("stop"))
        self.stop_button.setAccessibleName("停止课程论文批次")
        self.stop_button.setToolTip("安全停止当前论文并保留检查点")
        set_fluent_property(self.stop_button, "fluentAppearance", "danger")
        self.stop_button.clicked.connect(self._request_stop)

        self.resume_button = QPushButton("继续批次")
        self.resume_button.setObjectName("resumeCourseBatchButton")
        self.resume_button.setIcon(icons.icon("play"))
        self.resume_button.setAccessibleName("继续课程论文批次")
        self.resume_button.setToolTip("从检查点继续尚未完成的论文")
        set_fluent_property(self.resume_button, "fluentAppearance", "primary")
        self.resume_button.clicked.connect(self._request_resume)

        self.retry_button = QPushButton("重试失败项")
        self.retry_button.setObjectName("retryFailedBatchItemsButton")
        self.retry_button.setIcon(icons.icon("refresh"))
        self.retry_button.setAccessibleName("重试批次中的失败论文")
        set_fluent_property(self.retry_button, "fluentAppearance", "secondary")
        self.retry_button.clicked.connect(self._request_retry)

        self.open_output_button = QPushButton("打开输出目录")
        self.open_output_button.setObjectName("openBatchOutputButton")
        self.open_output_button.setIcon(icons.icon("folder"))
        self.open_output_button.setAccessibleName("打开批次报告输出目录")
        set_fluent_property(
            self.open_output_button, "fluentAppearance", "secondary"
        )
        self.open_output_button.clicked.connect(self._request_open_output)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(self.resume_button)
        toolbar.addWidget(self.retry_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.open_output_button)
        layout.addLayout(toolbar)

        self.model = BatchItemsTableModel(icons)
        self.table = QTableView()
        self.table.setObjectName("courseBatchItemsTable")
        self.table.setAccessibleName("课程论文批次项目")
        self.table.setAccessibleDescription(
            "逐篇显示自动提取的信息、当前阶段、状态、总分和报告"
        )
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setIconSize(QSize(16, 16))
        self.table.verticalHeader().hide()
        self.table.doubleClicked.connect(self._open_index)
        self.table.activated.connect(self._open_index)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(72)
        layout.addWidget(self.table, 1)

        item_actions = QHBoxLayout()
        item_actions.setSpacing(8)
        self.open_run_button = QPushButton("打开单篇详情")
        self.open_run_button.setObjectName("openBatchRunButton")
        self.open_run_button.setAccessibleName("打开选中论文的任务详情")
        set_fluent_property(self.open_run_button, "fluentAppearance", "secondary")
        self.open_run_button.clicked.connect(self._request_open_selected)
        self.edit_metadata_button = QPushButton("修改提取信息")
        self.edit_metadata_button.setObjectName("editBatchMetadataButton")
        self.edit_metadata_button.setAccessibleName("修改选中论文的学生和题目信息")
        self.edit_metadata_button.setAccessibleDescription(
            "修改后将由应用服务本地重建报告和汇总表，不重新调用模型。"
        )
        set_fluent_property(
            self.edit_metadata_button, "fluentAppearance", "secondary"
        )
        self.edit_metadata_button.clicked.connect(self._request_edit_metadata)
        item_actions.addStretch(1)
        item_actions.addWidget(self.open_run_button)
        item_actions.addWidget(self.edit_metadata_button)
        layout.addLayout(item_actions)

        self._update_actions()
        self._set_tab_order()

    @property
    def batch_id(self) -> str:
        return self._batch.batch_id if self._batch is not None else ""

    @property
    def batch(self) -> BatchRecord | None:
        return self._batch

    def set_batch(self, batch: BatchRecord) -> None:
        selected_item_id = self._selected_item_id()
        self._batch = batch
        self.model.set_items(batch.items)
        self.batch_title.setText(f"批次 {batch.batch_id}")
        status = _enum_value(batch.status)
        status_text = self.STATUS_TEXT.get(status, status)
        self.batch_status.setText(f"状态：{status_text}")
        processed = sum(
            item.status.value
            in {"completed", "failed", "cancelled", "source_changed"}
            for item in batch.items
        )
        completed = sum(item.status.value == "completed" for item in batch.items)
        failed = sum(
            item.status.value in {"failed", "source_changed"} for item in batch.items
        )
        self.batch_progress.setText(
            f"进度：已处理 {processed} / {len(batch.items)}；"
            f"成功 {completed}；失败 {failed}"
        )
        output = batch.request.output_dir
        self.batch_output.setText(f"输出目录：{output}")
        self.batch_output.setToolTip(str(output))
        if batch.error:
            self.message.show_message(batch.error, severity="danger")
        elif status == "completed_with_errors":
            self.message.show_message(
                "批次已处理完成，但部分论文失败；可查看错误后重试失败项。",
                severity="warning",
            )
        elif status == "completed":
            self.message.show_message("批次中的论文已全部完成。", severity="success")
        elif status == "paused":
            self.message.show_message(
                "批次已安全停止。已完成结果和当前检查点均已保留。",
                severity="info",
            )
        else:
            self.message.clear()
        if selected_item_id:
            row = self.model.row_for_item(selected_item_id)
            if row >= 0:
                self.table.selectRow(row)
        self._update_actions()

    def clear(self) -> None:
        self._batch = None
        self.model.set_items([])
        self.batch_title.setText("尚未选择批次")
        self.batch_status.setText("状态：—")
        self.batch_progress.setText("进度：0 / 0")
        self.batch_output.setText("输出目录：—")
        self.message.clear()
        self._update_actions()

    def apply_event(self, event: BatchEvent) -> None:
        if self._batch is None or event.batch_id != self._batch.batch_id:
            return
        stage = str(event.payload.get("stage", "") or "")
        if event.item_id and stage:
            self.model.set_item_stage(event.item_id, stage)
        if event.message:
            self.message.show_message(event.message, severity="info")

    def set_busy(self, busy: bool, *, action: str = "") -> None:
        self._busy_action = (action or "all") if busy else ""
        targets = {
            "stop": self.stop_button,
            "resume": self.resume_button,
            "retry": self.retry_button,
            "metadata": self.edit_metadata_button,
        }
        for name, button in targets.items():
            set_fluent_property(button, "fluentBusy", busy and name == action)
        self._update_actions()

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        self.message.show_message(message, severity="danger")

    def _request_stop(self) -> None:
        if self._batch is not None and self.stop_button.isEnabled():
            self.stop_requested.emit(self._batch.batch_id)

    def _request_resume(self) -> None:
        if self._batch is not None and self.resume_button.isEnabled():
            self.resume_requested.emit(self._batch.batch_id)

    def _request_retry(self) -> None:
        if self._batch is not None and self.retry_button.isEnabled():
            self.retry_failed_requested.emit(self._batch.batch_id)

    def _request_open_output(self) -> None:
        if self._batch is not None and self.open_output_button.isEnabled():
            self.open_output_requested.emit(str(self._batch.request.output_dir))

    def _request_open_selected(self) -> None:
        item = self._selected_item()
        if item is not None and item.run_id and self.open_run_button.isEnabled():
            self.run_open_requested.emit(item.run_id)

    def _request_edit_metadata(self) -> None:
        item = self._selected_item()
        if (
            self._batch is not None
            and item is not None
            and self.edit_metadata_button.isEnabled()
        ):
            self.metadata_edit_requested.emit(self._batch.batch_id, item.item_id)

    def _open_index(self, index: object) -> None:
        from PySide6.QtCore import QModelIndex

        if not isinstance(index, QModelIndex) or not index.isValid():
            return
        self.table.selectRow(index.row())
        self._request_open_selected()

    def _selection_changed(self) -> None:
        self._update_actions()

    def _selected_item(self) -> BatchItem | None:
        selection = self.table.selectionModel().selectedRows()
        return self.model.item_at(selection[0].row()) if selection else None

    def _selected_item_id(self) -> str:
        item = self._selected_item()
        return item.item_id if item is not None else ""

    def _update_actions(self) -> None:
        batch = self._batch
        busy = bool(self._busy_action)
        status = _enum_value(batch.status) if batch is not None else ""
        self.stop_button.setEnabled(bool(batch and status == "running" and not busy))
        self.resume_button.setEnabled(
            bool(batch and status in {"created", "paused"} and not busy)
        )
        has_failed = bool(
            batch
            and any(item.status.value == "failed" for item in batch.items)
        )
        self.retry_button.setEnabled(
            bool(
                batch
                and status in {"paused", "completed_with_errors"}
                and has_failed
                and not busy
            )
        )
        self.open_output_button.setEnabled(bool(batch and not busy))
        item = self._selected_item()
        self.open_run_button.setEnabled(bool(item and item.run_id and not busy))
        self.edit_metadata_button.setEnabled(
            bool(
                item
                and item.metadata is not None
                and item.status.value != "running"
                and not busy
            )
        )

    def _set_tab_order(self) -> None:
        controls = [
            self.stop_button,
            self.resume_button,
            self.retry_button,
            self.open_output_button,
            self.table,
            self.open_run_button,
            self.edit_metadata_button,
        ]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)


def _enum_value(value: BatchStatus | str) -> str:
    return str(getattr(value, "value", value))
