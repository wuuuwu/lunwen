from __future__ import annotations

import pytest

from paper_reviewer.application.metadata_recheck import (
    MetadataRecheckValidationError,
    apply_metadata_recheck_decision,
    build_metadata_suggestions,
    metadata_requires_local_recheck,
    submission_metadata_sha256,
)
from paper_reviewer.application.models import MetadataRecheckDecision
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)


def _metadata(
    *,
    title: str = "示例学院",
    title_source: SubmissionMetadataSource = SubmissionMetadataSource.PDF_METADATA,
    title_confidence: float = 0.7,
    name: str = "张三 得分：",
    name_source: SubmissionMetadataSource = SubmissionMetadataSource.COVER_LABEL,
    name_confidence: float = 0.95,
    human_reviewed: bool = False,
) -> SubmissionMetadata:
    evidence = {
        field: SubmissionFieldEvidence(
            source=SubmissionMetadataSource.COVER_LABEL,
            confidence=0.95,
            page=1,
            block_id=f"block-{field}",
            evidence=str(field),
        )
        for field in SUBMISSION_METADATA_FIELDS
    }
    evidence["student_name"] = SubmissionFieldEvidence(
        source=name_source,
        confidence=name_confidence,
        page=1,
        block_id="name-block",
        evidence=name,
    )
    evidence["paper_title"] = SubmissionFieldEvidence(
        source=title_source,
        confidence=title_confidence,
        page=2,
        block_id="title-block",
        block_ids=["title-block", "subtitle-block"],
        evidence=title,
    )
    return SubmissionMetadata(
        student_name=name,
        student_id="202600010001",
        paper_title=title,
        field_evidence=evidence,
        warnings=["题目识别置信度较低，请人工核对"],
        human_reviewed=human_reviewed,
    )


def test_recheck_suggests_same_value_when_visible_evidence_replaces_hidden_title() -> None:
    current = _metadata(title="真实论文题目")
    candidate = _metadata(
        title="真实论文题目",
        title_source=SubmissionMetadataSource.VISIBLE_HEADING,
        title_confidence=0.96,
        name="张三",
    )

    suggestions, unresolved = build_metadata_suggestions(current, candidate)

    by_field = {suggestion.field: suggestion for suggestion in suggestions}
    assert by_field["paper_title"].current_value == "真实论文题目"
    assert by_field["paper_title"].suggested_value == "真实论文题目"
    assert by_field["paper_title"].evidence.source is SubmissionMetadataSource.VISIBLE_HEADING
    assert by_field["paper_title"].selected_by_default is True
    assert by_field["student_name"].suggested_value == "张三"
    assert unresolved == []


def test_recheck_does_not_select_existing_human_correction_by_default() -> None:
    current = _metadata(
        name="人工姓名",
        name_source=SubmissionMetadataSource.HUMAN_CORRECTION,
        name_confidence=1.0,
    )
    candidate = _metadata(name="封面姓名")

    suggestions, _unresolved = build_metadata_suggestions(current, candidate)

    name = next(item for item in suggestions if item.field == "student_name")
    assert name.selected_by_default is False
    assert "人工修正" in name.reason


def test_recheck_keeps_unresolved_field_editable_but_unselected() -> None:
    current = _metadata(
        title_source=SubmissionMetadataSource.VISIBLE_HEADING,
        title_confidence=0.96,
    )
    current = current.model_copy(
        update={
            "student_id": "未识别学号",
            "field_evidence": {
                **current.field_evidence,
                "student_id": SubmissionFieldEvidence(
                    source=SubmissionMetadataSource.PLACEHOLDER,
                    confidence=0,
                ),
            },
        }
    )
    candidate = current.model_copy(deep=True)

    suggestions, unresolved = build_metadata_suggestions(current, candidate)

    student_id = next(item for item in suggestions if item.field == "student_id")
    assert student_id.selected_by_default is False
    assert "编辑" in student_id.reason
    assert unresolved == ["student_id"]


def test_apply_recheck_accepts_edited_value_and_records_human_provenance() -> None:
    current = _metadata()
    candidate = _metadata(
        title="面向可信应用——人工智能伦理治理路径研究",
        title_source=SubmissionMetadataSource.VISIBLE_HEADING,
        title_confidence=0.96,
        name="张三",
    )
    decision = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256=submission_metadata_sha256(current),
        values={
            "student_name": "张三",
            "student_id": current.student_id,
            "paper_title": "人工确认后的完整题目",
        },
        accepted_fields=["student_name", "paper_title"],
        human_reviewed=True,
    )

    result = apply_metadata_recheck_decision(current, candidate, decision)

    assert result is not None
    assert result.student_name == "张三"
    assert result.paper_title == "人工确认后的完整题目"
    assert result.human_reviewed is True
    title_evidence = result.field_evidence["paper_title"]
    assert title_evidence.source is SubmissionMetadataSource.HUMAN_CORRECTION
    assert title_evidence.confidence == 1
    assert title_evidence.block_ids == ["title-block", "subtitle-block"]
    assert "题目识别置信度较低，请人工核对" not in result.warnings
    assert "信息已人工修改" in result.warnings


def test_confirm_without_changes_preserves_evidence_and_only_sets_reviewed() -> None:
    current = _metadata()
    decision = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256=submission_metadata_sha256(current),
        values={
            "student_name": current.student_name,
            "student_id": current.student_id,
            "paper_title": current.paper_title,
        },
        accepted_fields=[],
        human_reviewed=True,
    )

    result = apply_metadata_recheck_decision(current, current, decision)

    assert result is not None
    assert result.human_reviewed is True
    assert result.field_evidence == current.field_evidence
    assert submission_metadata_sha256(result) != submission_metadata_sha256(current)


def test_recheck_decision_requires_explicit_human_confirmation() -> None:
    current = _metadata()
    omitted = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256="a" * 64,
        values={field: "值" for field in SUBMISSION_METADATA_FIELDS},
        accepted_fields=[],
    )

    assert omitted.human_reviewed is False
    assert omitted.has_explicit_human_review_confirmation is False
    with pytest.raises(MetadataRecheckValidationError, match="必须明确确认"):
        apply_metadata_recheck_decision(current, current, omitted)

    explicitly_rejected = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256="a" * 64,
        values={field: "值" for field in SUBMISSION_METADATA_FIELDS},
        accepted_fields=[],
        human_reviewed=False,
    )
    assert explicitly_rejected.has_explicit_human_review_confirmation is False
    with pytest.raises(MetadataRecheckValidationError, match="必须明确确认"):
        apply_metadata_recheck_decision(current, current, explicitly_rejected)

    explicitly_confirmed = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256="a" * 64,
        values={field: "值" for field in SUBMISSION_METADATA_FIELDS},
        accepted_fields=[],
        human_reviewed=True,
    )
    assert explicitly_confirmed.has_explicit_human_review_confirmation is True
    assert apply_metadata_recheck_decision(current, current, explicitly_confirmed) is not None


def test_recheck_keeps_current_evidence_when_edited_suggestion_returns_to_current() -> None:
    current = _metadata(title="原题目")
    candidate = _metadata(
        title="新候选题目",
        title_source=SubmissionMetadataSource.VISIBLE_HEADING,
        title_confidence=0.96,
    )
    decision = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256=submission_metadata_sha256(current),
        values={"paper_title": current.paper_title},
        accepted_fields=["paper_title"],
        human_reviewed=True,
    )

    result = apply_metadata_recheck_decision(current, candidate, decision)

    assert result is not None
    assert result.paper_title == current.paper_title
    assert result.field_evidence["paper_title"] == current.field_evidence["paper_title"]


def test_recheck_copies_candidate_evidence_only_for_the_same_existing_value() -> None:
    current = _metadata(title="真实题目")
    candidate = _metadata(
        title="真实题目",
        title_source=SubmissionMetadataSource.VISIBLE_HEADING,
        title_confidence=0.96,
    )
    decision = MetadataRecheckDecision(
        item_id="item-1",
        base_metadata_sha256=submission_metadata_sha256(current),
        values={"paper_title": candidate.paper_title},
        accepted_fields=["paper_title"],
        human_reviewed=True,
    )

    result = apply_metadata_recheck_decision(current, candidate, decision)

    assert result is not None
    assert result.field_evidence["paper_title"] == candidate.field_evidence["paper_title"]


def test_known_boundary_anomalies_cover_name_title_and_extra_labels() -> None:
    name_polluted = _metadata().model_copy(update={"student_name": "张三 教师评语：优秀"})
    title_polluted = _metadata().model_copy(update={"paper_title": "课程论文 考核方式：闭卷"})

    assert metadata_requires_local_recheck(name_polluted)
    assert metadata_requires_local_recheck(title_polluted)
    assert not metadata_requires_local_recheck(_metadata(human_reviewed=True))
