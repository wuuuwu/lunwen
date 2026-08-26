from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.gui.dialogs.course_metadata import CourseMetadataDialog


def _metadata() -> SubmissionMetadata:
    return SubmissionMetadata(
        student_name="张三",
        student_id="20260001",
        major="公共管理",
        paper_title="课程治理案例分析",
        field_evidence={
            field: SubmissionFieldEvidence(
                source=(
                    SubmissionMetadataSource.FILE_NAME
                    if field == "student_name"
                    else SubmissionMetadataSource.COVER_LABEL
                ),
                confidence=0.5 if field == "student_name" else 0.95,
                page=1,
                block_id=f"block-{field}",
                evidence=f"原始{field}",
            )
            for field in SUBMISSION_METADATA_FIELDS
        },
        warnings=["姓名识别置信度较低，请人工核对"],
    )


def test_dialog_displays_warnings_and_returns_human_provenance(
    qapp: QApplication, qtbot: object
) -> None:
    original = _metadata()
    dialog = CourseMetadataDialog(original)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    assert dialog.objectName() == "courseMetadataDialog"
    assert dialog.needs_review_label.text() == "状态：需要人工核对"
    assert "姓名识别置信度较低" in dialog.warnings_label.text()
    assert dialog.student_name.accessibleName()

    dialog._edits["student_name"].setText("李四")
    dialog.accept()

    result = dialog.result_metadata
    assert result is not None
    assert result.student_name == "李四"
    assert result.field_evidence["student_name"].source is SubmissionMetadataSource.HUMAN_CORRECTION
    assert result.field_evidence["student_name"].confidence == 1
    assert result.field_evidence["student_name"].page is None
    assert result.field_evidence["student_name"].evidence is None
    unchanged = result.field_evidence["major"]
    assert unchanged.source is SubmissionMetadataSource.COVER_LABEL
    assert unchanged.page == 1
    assert "信息已人工修改" in result.warnings


def test_dialog_rejects_blank_values_and_keeps_escape_cancel(
    qapp: QApplication, qtbot: object
) -> None:
    dialog = CourseMetadataDialog(_metadata())
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._edits["paper_title"].clear()

    dialog.accept()

    assert dialog.result() == 0
    assert "题目不能为空" in dialog.error_label.text()
    assert dialog._edits["paper_title"].property("fluentInvalid") is True

    dialog.reject()
    assert dialog.result() == 0


def test_dialog_has_accessible_controls_and_tab_order(
    qapp: QApplication, qtbot: object
) -> None:
    dialog = CourseMetadataDialog(_metadata())
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    for field in SUBMISSION_METADATA_FIELDS:
        edit = dialog._edits[field]
        assert edit.accessibleName()
        assert edit.objectName().startswith("courseMetadata")
    assert dialog.save_button.accessibleName()
    assert dialog.cancel_button.accessibleName()
    assert dialog.focusProxy() is None
    assert dialog._edits["student_name"].focusPolicy() & Qt.FocusPolicy.TabFocus
