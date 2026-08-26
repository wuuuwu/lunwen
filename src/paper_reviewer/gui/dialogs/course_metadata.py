from __future__ import annotations

from collections.abc import Mapping
from itertools import pairwise

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
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


class CourseMetadataDialog(QDialog):
    """Edit the identity metadata extracted from a course-paper PDF.

    The dialog intentionally owns no persistence.  Callers should use
    :meth:`metadata` after ``exec()`` returns ``Accepted`` and pass the value
    to the application service.  A changed field receives explicit human
    provenance; evidence for unchanged fields is copied without alteration.
    """

    def __init__(
        self,
        metadata: SubmissionMetadata,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._original = metadata.model_copy(deep=True)
        self._result_metadata: SubmissionMetadata | None = None
        self._edits: dict[str, QLineEdit] = {}

        self.setObjectName("courseMetadataDialog")
        self.setModal(True)
        self.setWindowTitle("修改论文信息")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(14)

        description = QLabel(
            "请核对自动提取的姓名、学号、专业和题目。保存后只会在本地更新报告，"
            "不会重新调用模型。"
        )
        description.setObjectName("courseMetadataDescription")
        description.setProperty("fluentType", "secondary")
        description.setWordWrap(True)
        description.setAccessibleName("课程论文信息修改说明")
        root.addWidget(description)

        self.status_frame = QFrame()
        self.status_frame.setObjectName("courseMetadataStatus")
        self.status_frame.setProperty("fluentRole", "card")
        self.status_frame.setProperty("fluentSeverity", "warning")
        status_layout = QVBoxLayout(self.status_frame)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(4)
        self.needs_review_label = QLabel()
        self.needs_review_label.setObjectName("courseMetadataNeedsReview")
        self.needs_review_label.setAccessibleName("元数据人工核对状态")
        self.warnings_label = QLabel()
        self.warnings_label.setObjectName("courseMetadataWarnings")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setAccessibleName("元数据自动提取提示")
        status_layout.addWidget(self.needs_review_label)
        status_layout.addWidget(self.warnings_label)
        root.addWidget(self.status_frame)

        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for field in SUBMISSION_METADATA_FIELDS:
            edit = QLineEdit(str(getattr(self._original, field)))
            edit.setObjectName(f"courseMetadata{_camel_case(field)}Edit")
            edit.setAccessibleName(f"论文{_FIELD_LABELS[field]}")
            edit.setClearButtonEnabled(True)
            edit.textChanged.connect(self._clear_field_error)
            self._edits[field] = edit
            # Keep the conventional snake_case handles used by the existing
            # pages while retaining the field map for schema-driven code.
            setattr(self, field, edit)
            form.addRow(_FIELD_LABELS[field], edit)
        root.addLayout(form)

        self.error_label = QLabel()
        self.error_label.setObjectName("courseMetadataError")
        self.error_label.setProperty("fluentSeverity", "danger")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setAccessibleName("论文信息校验错误")
        self.error_label.hide()
        root.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.buttons.setObjectName("courseMetadataDialogButtons")
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setObjectName("saveCourseMetadataButton")
        self.save_button.setAccessibleName("保存论文信息")
        self.save_button.setToolTip("保存修改并更新本地报告")
        set_fluent_property(self.save_button, "fluentAppearance", "primary")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("cancelCourseMetadataButton")
        self.cancel_button.setAccessibleName("取消修改论文信息")
        self.cancel_button.setToolTip("放弃修改并关闭对话框")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._set_status()
        self._set_tab_order()
        self._edits["student_name"].setFocus(Qt.FocusReason.OtherFocusReason)

    def metadata(self) -> SubmissionMetadata:
        """Return the accepted result, or the original snapshot before saving."""

        return (self._result_metadata or self._original).model_copy(deep=True)

    @property
    def result_metadata(self) -> SubmissionMetadata | None:
        """Return the edited metadata after acceptance, if any."""

        return self._result_metadata.model_copy(deep=True) if self._result_metadata else None

    def accept(self) -> None:
        updated = self._build_metadata()
        if updated is None:
            return
        self._result_metadata = updated
        super().accept()

    def _build_metadata(self) -> SubmissionMetadata | None:
        values = {field: self._edits[field].text().strip() for field in SUBMISSION_METADATA_FIELDS}
        errors: list[str] = []
        for field, value in values.items():
            edit = self._edits[field]
            invalid = not value
            set_fluent_property(edit, "fluentInvalid", invalid)
            edit.setAccessibleDescription(
                f"{_FIELD_LABELS[field]}不能为空" if invalid else ""
            )
            if invalid:
                errors.append(f"{_FIELD_LABELS[field]}不能为空")
        if errors:
            self.error_label.setText("；".join(errors))
            self.error_label.setAccessibleDescription(self.error_label.text())
            self.error_label.show()
            return None

        evidence = {
            field: self._original.field_evidence[field].model_copy(deep=True)
            for field in SUBMISSION_METADATA_FIELDS
        }
        changed_fields: list[str] = []
        for field, value in values.items():
            if value == getattr(self._original, field):
                continue
            changed_fields.append(field)
            evidence[field] = SubmissionFieldEvidence(
                source=SubmissionMetadataSource.HUMAN_CORRECTION,
                confidence=1.0,
            )

        warnings = list(self._original.warnings)
        if changed_fields and "信息已人工修改" not in warnings:
            warnings.append("信息已人工修改")
        self.error_label.clear()
        self.error_label.hide()
        return SubmissionMetadata(
            student_name=values["student_name"],
            student_id=values["student_id"],
            major=values["major"],
            paper_title=values["paper_title"],
            field_evidence=evidence,
            warnings=warnings,
        )

    def _clear_field_error(self, _value: str) -> None:
        sender = self.sender()
        if isinstance(sender, QLineEdit) and sender.property("fluentInvalid"):
            set_fluent_property(sender, "fluentInvalid", False)
            sender.setAccessibleDescription("")
        if self.error_label.isVisible():
            self.error_label.hide()

    def _set_status(self) -> None:
        self.needs_review_label.setText(
            "状态：需要人工核对" if self._original.needs_review else "状态：自动提取完成"
        )
        warnings = self._original.warnings
        self.warnings_label.setText(
            "提示：\n" + "\n".join(f"• {warning}" for warning in warnings)
            if warnings
            else "提示：未发现自动提取警告。"
        )
        self.warnings_label.setAccessibleDescription(
            "；".join(warnings) if warnings else "未发现自动提取警告"
        )

    def _set_tab_order(self) -> None:
        fields = [self._edits[field] for field in SUBMISSION_METADATA_FIELDS]
        controls = [*fields, self.save_button, self.cancel_button]
        for current, following in pairwise(controls):
            QWidget.setTabOrder(current, following)


def _camel_case(field: str) -> str:
    return "".join(part.capitalize() for part in field.split("_"))
