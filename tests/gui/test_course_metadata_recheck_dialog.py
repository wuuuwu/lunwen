from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from paper_reviewer.application.metadata_recheck import submission_metadata_sha256
from paper_reviewer.application.models import (
    BatchMetadataRecheckItem,
    BatchMetadataRecheckPreview,
    MetadataFieldSuggestion,
)
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.gui.dialogs.course_metadata_recheck import (
    CourseMetadataRecheckDialog,
)


def _metadata() -> SubmissionMetadata:
    return SubmissionMetadata(
        student_name="张三 得分：",
        student_id="202600010001",
        major="未识别专业",
        paper_title="示例学院",
        field_evidence={
            field: SubmissionFieldEvidence(
                source=SubmissionMetadataSource.PDF_METADATA,
                confidence=0.7,
            )
            for field in SUBMISSION_METADATA_FIELDS
        },
        warnings=["题目识别置信度较低，请人工核对"],
    )


def _preview(metadata: SubmissionMetadata) -> BatchMetadataRecheckPreview:
    return BatchMetadataRecheckPreview(
        batch_id="batch-1",
        items=[
            BatchMetadataRecheckItem(
                item_id="item-1",
                source_filename="论文.pdf",
                base_metadata_sha256=submission_metadata_sha256(metadata),
                suggestions=[
                    MetadataFieldSuggestion(
                        field="paper_title",
                        current_value=metadata.paper_title,
                        suggested_value="正文识别题目",
                        evidence=SubmissionFieldEvidence(
                            source=SubmissionMetadataSource.VISIBLE_HEADING,
                            confidence=0.96,
                            page=2,
                            block_id="title-block",
                            evidence="正文识别题目",
                        ),
                        reason="检测到可验证的正文题目。",
                    )
                ],
            )
        ],
    )


def test_recheck_dialog_allows_editing_suggestion_before_confirming(
    qapp: QApplication,
    qtbot: object,
) -> None:
    metadata = _metadata()
    dialog = CourseMetadataRecheckDialog(
        _preview(metadata),
        {"item-1": metadata},
    )
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    suggested_index = dialog.model.index(0, 4)
    assert dialog.model.flags(suggested_index) & Qt.ItemFlag.ItemIsEditable
    assert dialog.model.data(suggested_index, Qt.ItemDataRole.EditRole) == "正文识别题目"
    accepted_index = dialog.model.index(0, 0)
    assert dialog.model.flags(accepted_index) & Qt.ItemFlag.ItemIsUserCheckable
    assert not dialog.model.flags(accepted_index) & Qt.ItemFlag.ItemIsEditable
    assert dialog.model.setData(
        suggested_index,
        "人工核对后的题目",
        Qt.ItemDataRole.EditRole,
    )
    assert not dialog.apply_button.isEnabled()

    dialog.review_confirmation.setChecked(True)
    assert dialog.model.setData(
        suggested_index,
        "再次人工核对后的题目",
        Qt.ItemDataRole.EditRole,
    )
    assert not dialog.review_confirmation.isChecked()
    assert not dialog.apply_button.isEnabled()
    dialog.review_confirmation.setChecked(True)
    dialog.accept()

    decisions = dialog.result_decisions
    assert decisions is not None
    assert len(decisions) == 1
    assert decisions[0].accepted_fields == ["paper_title"]
    assert decisions[0].values["paper_title"] == "再次人工核对后的题目"
    assert decisions[0].human_reviewed is True


def test_recheck_dialog_exposes_local_evidence_and_accessible_controls(
    qapp: QApplication,
    qtbot: object,
) -> None:
    metadata = _metadata()
    dialog = CourseMetadataRecheckDialog(
        _preview(metadata),
        {"item-1": metadata},
    )
    qtbot.addWidget(dialog)  # type: ignore[attr-defined]

    evidence = dialog.model.data(dialog.model.index(0, 5), Qt.ItemDataRole.DisplayRole)
    assert "来源：正文可见标题" in evidence
    assert "置信度：96%" in evidence
    assert "第 2 页" in evidence
    assert dialog.table.accessibleName()
    assert dialog.review_confirmation.accessibleName()
    assert dialog.apply_button.accessibleName()
