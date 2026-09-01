from __future__ import annotations

import pytest
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
    assert "结构化文件名" in dialog._evidence_labels["student_name"].text()
    assert "置信度：50%" in dialog._evidence_labels["student_name"].text()
    assert "待核对" in dialog._evidence_labels["student_name"].text()

    dialog._edits["student_name"].setText("李四")
    assert not dialog.save_button.isEnabled()
    dialog.review_confirmation.setChecked(True)
    dialog.accept()

    result = dialog.result_metadata
    assert result is not None
    assert result.student_name == "李四"
    assert result.field_evidence["student_name"].source is SubmissionMetadataSource.HUMAN_CORRECTION
    assert result.field_evidence["student_name"].confidence == 1
    assert result.field_evidence["student_name"].page is None
    assert result.field_evidence["student_name"].evidence is None
    unchanged = result.field_evidence["paper_title"]
    assert unchanged.source is SubmissionMetadataSource.COVER_LABEL
    assert unchanged.page == 1
    assert "信息已人工修改" in result.warnings


def test_dialog_rejects_blank_values_and_keeps_escape_cancel(
    qapp: QApplication, qtbot: object
) -> None:
    dialog = CourseMetadataDialog(_metadata())
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]
    dialog._edits["paper_title"].clear()
    dialog.review_confirmation.setChecked(True)

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
    assert dialog.review_confirmation.accessibleName()
    assert dialog.focusProxy() is None
    assert dialog._edits["student_name"].focusPolicy() & Qt.FocusPolicy.TabFocus


def test_dialog_can_confirm_unchanged_values_without_replacing_evidence(
    qapp: QApplication,
    qtbot: object,
) -> None:
    original = _metadata()
    dialog = CourseMetadataDialog(original)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog.review_confirmation.setChecked(True)
    dialog.accept()

    result = dialog.result_metadata
    assert result is not None
    assert result.human_reviewed is True
    assert result.field_evidence == original.field_evidence


def test_dialog_clears_confirmation_when_a_field_changes(
    qapp: QApplication,
    qtbot: object,
) -> None:
    dialog = CourseMetadataDialog(_metadata())
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    dialog.review_confirmation.setChecked(True)
    assert dialog.save_button.isEnabled()
    dialog.paper_title.setText("人工核对后的题目")

    assert not dialog.review_confirmation.isChecked()
    assert not dialog.save_button.isEnabled()


@pytest.mark.parametrize(
    ("field", "value", "source", "reason"),
    [
        (
            "paper_title",
            "示例学院",
            SubmissionMetadataSource.PDF_METADATA,
            "PDF 隐藏标题",
        ),
        (
            "student_name",
            "张三 得分：",
            SubmissionMetadataSource.COVER_LABEL,
            "后续字段标签",
        ),
    ],
)
def test_dialog_marks_high_confidence_legacy_anomalies_for_recheck(
    qapp: QApplication,
    qtbot: object,
    field: str,
    value: str,
    source: SubmissionMetadataSource,
    reason: str,
) -> None:
    original = _metadata()
    clean_evidence = {
        metadata_field: detail.model_copy(update={"confidence": 0.95})
        for metadata_field, detail in original.field_evidence.items()
    }
    clean_evidence[field] = clean_evidence[field].model_copy(
        update={"source": source}
    )
    original = original.model_copy(
        update={field: value, "field_evidence": clean_evidence}
    )

    dialog = CourseMetadataDialog(original)
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    assert dialog.needs_review_label.text() == "状态：建议重新检查"
    evidence = dialog._evidence_labels[field].text()
    assert "建议重新检查" in evidence
    assert reason in evidence
