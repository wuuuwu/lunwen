from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, ClassVar, cast

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.application.metadata_recheck import submission_metadata_sha256
from paper_reviewer.application.models import (
    BatchMetadataRecheckItem,
    BatchMetadataRecheckPreview,
    MetadataFieldName,
    MetadataFieldSuggestion,
    MetadataRecheckDecision,
)
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.gui.theme import set_fluent_property

_FIELD_LABELS: Mapping[str, str] = {
    "student_name": "姓名",
    "student_id": "学号",
    "major": "专业",
    "paper_title": "题目",
}
_SOURCE_LABELS: Mapping[SubmissionMetadataSource, str] = {
    SubmissionMetadataSource.COVER_LABEL: "封面明确标签",
    SubmissionMetadataSource.VISIBLE_HEADING: "正文可见标题",
    SubmissionMetadataSource.MODEL_EVIDENCE: "既有模型证据",
    SubmissionMetadataSource.PDF_METADATA: "旧版 PDF 隐藏元数据",
    SubmissionMetadataSource.FILE_NAME: "结构化文件名",
    SubmissionMetadataSource.HUMAN_CORRECTION: "人工修正",
    SubmissionMetadataSource.PLACEHOLDER: "未识别占位值",
}


@dataclass
class _SuggestionRow:
    item: BatchMetadataRecheckItem
    suggestion: MetadataFieldSuggestion
    selected: bool
    edited_value: str


class MetadataRecheckDiffModel(QAbstractTableModel):
    """Checkable, read-only projection of metadata differences."""

    HEADERS: ClassVar[tuple[str, ...]] = (
        "采用",
        "论文",
        "字段",
        "当前值",
        "建议值",
        "依据与理由",
    )

    def __init__(self, preview: BatchMetadataRecheckPreview) -> None:
        super().__init__()
        self.rows = [
            _SuggestionRow(
                item,
                suggestion,
                suggestion.selected_by_default,
                suggestion.suggested_value,
            )
            for item in preview.items
            for suggestion in item.suggestions
        ]

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.rows)

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
            orientation is Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        suggestion = row.suggestion
        evidence = suggestion.evidence
        location = f"第 {evidence.page} 页" if evidence.page else "未定位页码"
        source = _SOURCE_LABELS.get(evidence.source, evidence.source.value)
        evidence_text = (evidence.evidence or "无可展示原文片段").strip()
        reason = suggestion.reason.strip() or "未提供额外说明"
        values = (
            "采用" if row.selected else "保留当前值",
            row.item.source_filename,
            _FIELD_LABELS.get(suggestion.field, suggestion.field),
            suggestion.current_value,
            row.edited_value,
            f"来源：{source}；置信度：{evidence.confidence:.0%}；{location}；"
            f"{reason}；证据：{evidence_text}",
        )
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.EditRole and index.column() == 4:
            # Inline editors request EditRole.  Return the current value so an
            # edit round-trip does not revert to the original suggestion.
            return row.edited_value
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if row.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{self.HEADERS[index.column()]}：{values[index.column()]}"
        if role == Qt.ItemDataRole.AccessibleDescriptionRole:
            return (
                f"{row.item.source_filename} 的"
                f"{_FIELD_LABELS.get(suggestion.field, suggestion.field)}差异。"
            )
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        if index.isValid() and index.column() == 4:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return False
        row = self.rows[index.row()]
        if index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole:
            selected = value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked
            if row.selected == selected:
                return False
            row.selected = selected
            self.dataChanged.emit(index, index, [role, Qt.ItemDataRole.DisplayRole])
            return True
        if index.column() == 4 and role == Qt.ItemDataRole.EditRole:
            edited = str(value).strip()
            if row.edited_value == edited:
                return False
            row.edited_value = edited
            self.dataChanged.emit(index, index, [role, Qt.ItemDataRole.DisplayRole])
            return True
        return False

    def accepted_fields(self, item_id: str) -> list[MetadataFieldName]:
        return [
            row.suggestion.field
            for row in self.rows
            if row.item.item_id == item_id and row.selected
        ]

    def selected_count(self) -> int:
        return sum(row.selected for row in self.rows)

    def selected_values(self, item_id: str) -> dict[MetadataFieldName, str]:
        return {
            row.suggestion.field: row.edited_value.strip()
            for row in self.rows
            if row.item.item_id == item_id and row.selected
        }


class CourseMetadataRecheckDialog(QDialog):
    """Preview metadata differences and construct explicit apply decisions."""

    def __init__(
        self,
        preview: BatchMetadataRecheckPreview,
        metadata_by_item: Mapping[str, SubmissionMetadata],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.preview = preview
        self._metadata_by_item = {
            key: value.model_copy(deep=True) for key, value in metadata_by_item.items()
        }
        self._result_decisions: list[MetadataRecheckDecision] | None = None
        self._model_revision = 0
        self._confirmed_revision: int | None = None

        self.setObjectName("courseMetadataRecheckDialog")
        self.setModal(True)
        self.setWindowTitle("批量重新检查信息 · 差异预览")
        self.setMinimumSize(900, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(14)

        description = QLabel(
            "以下内容只是重新检查建议，不会自动覆盖。请逐项选择要采用的差异，"
            "并对照论文原文完成确认。"
        )
        description.setObjectName("courseMetadataRecheckDescription")
        description.setProperty("fluentType", "secondary")
        description.setWordWrap(True)
        description.setAccessibleName("批量信息重新检查说明")
        root.addWidget(description)

        summary_card = QFrame()
        summary_card.setObjectName("courseMetadataRecheckSummaryCard")
        summary_card.setProperty("fluentRole", "card")
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(12, 10, 12, 10)
        summary_layout.setSpacing(4)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("courseMetadataRecheckSummary")
        self.summary_label.setAccessibleName("重新检查差异摘要")
        self.skipped_label = QLabel()
        self.skipped_label.setObjectName("courseMetadataRecheckSkipped")
        self.skipped_label.setWordWrap(True)
        self.skipped_label.setTextFormat(Qt.TextFormat.PlainText)
        self.skipped_label.setAccessibleName("未参与重新检查的论文")
        self.unresolved_label = QLabel()
        self.unresolved_label.setObjectName("courseMetadataRecheckUnresolved")
        self.unresolved_label.setWordWrap(True)
        self.unresolved_label.setTextFormat(Qt.TextFormat.PlainText)
        self.unresolved_label.setAccessibleName("重新检查后仍无法确定的字段")
        summary_layout.addWidget(self.summary_label)
        summary_layout.addWidget(self.skipped_label)
        summary_layout.addWidget(self.unresolved_label)
        root.addWidget(summary_card)

        self.model = MetadataRecheckDiffModel(preview)
        self.model.dataChanged.connect(self._model_changed)
        self.table = QTableView()
        self.table.setObjectName("courseMetadataRecheckDiffTable")
        self.table.setAccessibleName("论文信息重新检查差异")
        self.table.setAccessibleDescription(
            "空格键切换是否采用选中差异；建议值列可编辑，所有内容均为纯文本。"
        )
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        self.review_confirmation = QCheckBox(
            "我已查看全部差异并对照论文原文核对，确认按勾选项更新信息。"
        )
        self.review_confirmation.setObjectName("confirmMetadataRecheckApplyCheckBox")
        self.review_confirmation.setAccessibleName("确认已核对批量信息差异")
        self.review_confirmation.setAccessibleDescription(
            "必须明确确认后，才能应用差异并把这些论文标记为已人工核对。"
        )
        self.review_confirmation.stateChanged.connect(self._confirmation_changed)
        root.addWidget(self.review_confirmation)

        self.error_label = QLabel()
        self.error_label.setObjectName("courseMetadataRecheckError")
        self.error_label.setProperty("fluentSeverity", "danger")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setAccessibleName("批量信息核对错误")
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.buttons.setObjectName("courseMetadataRecheckButtons")
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("应用核对结果")
        self.apply_button.setObjectName("applyMetadataRecheckButton")
        self.apply_button.setAccessibleName("应用批量信息核对结果")
        self.apply_button.setToolTip("在本地更新选中差异并重建报告和汇总表")
        set_fluent_property(self.apply_button, "fluentAppearance", "primary")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("cancelMetadataRecheckButton")
        self.cancel_button.setAccessibleName("取消应用信息差异")
        self.buttons.clicked.connect(self._button_clicked)
        root.addWidget(self.buttons)

        self._update_summary()
        self._confirmation_changed()
        self._set_tab_order()

    @property
    def result_decisions(self) -> list[MetadataRecheckDecision] | None:
        if self._result_decisions is None:
            return None
        return [decision.model_copy(deep=True) for decision in self._result_decisions]

    def accept(self) -> None:
        if not self.review_confirmation.isChecked():
            self._show_error("请先确认已查看全部差异并对照论文原文核对。")
            return
        if self._confirmed_revision != self._model_revision:
            self._show_error("差异内容已变化，请重新勾选确认后再应用。")
            return
        decisions: list[MetadataRecheckDecision] = []
        for item in self.preview.items:
            metadata = self._metadata_by_item.get(item.item_id)
            if metadata is None:
                self._show_error(f"{item.source_filename} 缺少当前信息，无法应用差异。")
                return
            if submission_metadata_sha256(metadata) != item.base_metadata_sha256:
                self._show_error(
                    f"{item.source_filename} 的当前信息快照已变化，请重新预检后再应用。"
                )
                return
            accepted_fields = self.model.accepted_fields(item.item_id)
            selected_values = self.model.selected_values(item.item_id)
            values: dict[MetadataFieldName, str] = {
                cast(MetadataFieldName, field): str(getattr(metadata, field)).strip()
                for field in SUBMISSION_METADATA_FIELDS
            }
            for field in accepted_fields:
                suggested = selected_values[field]
                if not suggested:
                    self._show_error(
                        f"{item.source_filename} 的{_FIELD_LABELS.get(field, field)}建议值为空。"
                    )
                    return
                values[field] = suggested
            decisions.append(
                MetadataRecheckDecision(
                    item_id=item.item_id,
                    base_metadata_sha256=item.base_metadata_sha256,
                    values=values,
                    accepted_fields=accepted_fields,
                    human_reviewed=True,
                )
            )
        if not decisions:
            self._show_error("没有可应用的论文信息。")
            return
        self._result_decisions = decisions
        super().accept()

    def _button_clicked(self, button: object) -> None:
        if button is self.apply_button:
            self.accept()
        elif button is self.cancel_button:
            self.reject()

    def _update_summary(self) -> None:
        self._update_selection_summary()
        if self.preview.skipped:
            text = "未参与：\n" + "\n".join(
                f"• {item_id}：{reason}"
                for item_id, reason in sorted(self.preview.skipped.items())
            )
            self.skipped_label.setText(text)
            self.skipped_label.show()
        else:
            self.skipped_label.hide()
        unresolved = [
            f"{item.source_filename}："
            + "、".join(_FIELD_LABELS.get(field, field) for field in item.unresolved_fields)
            for item in self.preview.items
            if item.unresolved_fields
        ]
        if unresolved:
            self.unresolved_label.setText(
                "仍需人工判断：\n" + "\n".join(f"• {value}" for value in unresolved)
            )
            self.unresolved_label.show()
        else:
            self.unresolved_label.hide()

    def _update_selection_summary(self, *_args: object) -> None:
        selected = self.model.selected_count()
        self.summary_label.setText(
            f"已检查 {len(self.preview.items)} 篇；发现 {self.model.rowCount()} 项差异；"
            f"当前选择采用 {selected} 项。"
        )

    def _model_changed(self, *_args: object) -> None:
        """Invalidate the acknowledgement whenever a decision input changes."""

        self._model_revision += 1
        self._update_selection_summary()
        if self.review_confirmation.isChecked():
            self.review_confirmation.setChecked(False)
        else:
            self._confirmed_revision = None

    def _confirmation_changed(self, _state: int = 0) -> None:
        confirmed = self.review_confirmation.isChecked()
        self._confirmed_revision = self._model_revision if confirmed else None
        set_fluent_property(self.review_confirmation, "fluentInvalid", False)
        self.apply_button.setEnabled(confirmed and bool(self.preview.items))
        self.apply_button.setAccessibleDescription(
            "" if confirmed else "请先勾选已核对全部差异"
        )
        if self.error_label.isVisible():
            self.error_label.hide()

    def _show_error(self, message: str) -> None:
        set_fluent_property(self.review_confirmation, "fluentInvalid", True)
        self.review_confirmation.setAccessibleDescription(message)
        self.error_label.setText(message)
        self.error_label.setAccessibleDescription(message)
        self.error_label.show()

    def _set_tab_order(self) -> None:
        controls = [
            self.table,
            self.review_confirmation,
            self.apply_button,
            self.cancel_button,
        ]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)
