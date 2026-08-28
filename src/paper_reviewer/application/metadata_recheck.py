from __future__ import annotations

import hashlib
import json
import re
from typing import cast

from paper_reviewer.application.models import (
    MetadataFieldName,
    MetadataFieldSuggestion,
    MetadataRecheckDecision,
)
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)

_FIELD_TITLES = {
    "student_name": "姓名",
    "student_id": "学号",
    "major": "专业",
    "paper_title": "题目",
}
_SOURCE_REASONS = {
    SubmissionMetadataSource.COVER_LABEL: "检测到明确的封面字段标签。",
    SubmissionMetadataSource.VISIBLE_HEADING: "检测到可验证的正文题目。",
    SubmissionMetadataSource.MODEL_EVIDENCE: "既有提取值仍可由当前原文证据验证。",
    SubmissionMetadataSource.PDF_METADATA: "检测到 PDF 内嵌元数据。",
    SubmissionMetadataSource.FILE_NAME: "检测到结构化文件名信息。",
    SubmissionMetadataSource.HUMAN_CORRECTION: "保留既有人工修正。",
    SubmissionMetadataSource.PLACEHOLDER: "当前本地证据不足。",
}


class MetadataRecheckValidationError(ValueError):
    """A display-safe validation failure for one recheck decision."""


_LEGACY_METADATA_BOUNDARY_PATTERN = re.compile(
    r"(?:得分|成绩|分数|评分|班级|专业|学号|姓名|任课教师|考核方法|考核方式|教师评语)"
    r"\s*[:：]",
    re.IGNORECASE,
)


def metadata_requires_local_recheck(metadata: SubmissionMetadata) -> bool:
    """Select low-confidence records and known pre-1.1 extraction anomalies."""

    return bool(metadata_recheck_fields(metadata))


def metadata_recheck_fields(metadata: SubmissionMetadata) -> tuple[str, ...]:
    """Return the metadata fields that should be presented for local recheck.

    ``SubmissionMetadata.needs_review`` intentionally only represents current
    field-level confidence.  Batch records created by earlier versions can
    also contain values from PDF hidden metadata or a following cover-field
    label.  Keep those compatibility rules in this application-level helper so
    the service and GUI make the same decision and can identify the exact
    affected column.
    """

    if metadata.human_reviewed:
        return ()

    fields: list[str] = list(metadata.pending_review_fields)
    if (
        metadata.field_evidence["paper_title"].source
        is SubmissionMetadataSource.PDF_METADATA
    ):
        fields.append("paper_title")
    for field in SUBMISSION_METADATA_FIELDS:
        if _LEGACY_METADATA_BOUNDARY_PATTERN.search(getattr(metadata, field)):
            fields.append(field)
    # Preserve the schema field order even when a field is identified by more
    # than one compatibility rule.  This keeps UI text deterministic.
    return tuple(field for field in SUBMISSION_METADATA_FIELDS if field in fields)


def submission_metadata_sha256(metadata: SubmissionMetadata) -> str:
    payload = metadata.model_dump(mode="json", exclude_computed_fields=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_metadata_suggestions(
    current: SubmissionMetadata,
    candidate: SubmissionMetadata,
) -> tuple[list[MetadataFieldSuggestion], list[MetadataFieldName]]:
    suggestions: list[MetadataFieldSuggestion] = []
    for field in SUBMISSION_METADATA_FIELDS:
        typed_field = cast(MetadataFieldName, field)
        current_value = getattr(current, field)
        suggested_value = getattr(candidate, field)
        current_evidence = current.field_evidence[field]
        evidence = candidate.field_evidence[field]
        candidate_requires_review = field in candidate.pending_review_fields
        if (
            current_value == suggested_value
            and current_evidence == evidence
            and not candidate_requires_review
        ):
            continue
        selected_by_default = (
            current_evidence.source is not SubmissionMetadataSource.HUMAN_CORRECTION
            and evidence.source is not SubmissionMetadataSource.PLACEHOLDER
        )
        reason = _SOURCE_REASONS[evidence.source]
        if candidate_requires_review:
            reason = (
                f"当前仍无法可靠识别{_FIELD_TITLES[field]}，"
                "请对照论文原文编辑后再采用。"
            )
        if not selected_by_default:
            if current_evidence.source is SubmissionMetadataSource.HUMAN_CORRECTION:
                reason = f"当前{_FIELD_TITLES[field]}由人工修正，默认保留；{reason}"
        suggestions.append(
            MetadataFieldSuggestion(
                field=typed_field,
                current_value=current_value,
                suggested_value=suggested_value,
                evidence=evidence.model_copy(deep=True),
                reason=reason,
                selected_by_default=selected_by_default,
            )
        )
    unresolved = [
        cast(MetadataFieldName, field) for field in candidate.pending_review_fields
    ]
    return suggestions, unresolved


def apply_metadata_recheck_decision(
    current: SubmissionMetadata,
    candidate: SubmissionMetadata,
    decision: MetadataRecheckDecision,
) -> SubmissionMetadata | None:
    if not decision.has_explicit_human_review_confirmation:
        raise MetadataRecheckValidationError(
            "应用重新检查结果前，必须明确确认已人工核对。"
        )
    accepted = decision.accepted_fields
    if len(accepted) != len(set(accepted)):
        raise MetadataRecheckValidationError("接受字段存在重复项，请重新预检。")
    known_fields = set(SUBMISSION_METADATA_FIELDS)
    if unknown := sorted((set(accepted) | set(decision.values)) - known_fields):
        del unknown
        raise MetadataRecheckValidationError("重检决定包含未知字段，请重新预检。")

    values = {field: getattr(current, field) for field in SUBMISSION_METADATA_FIELDS}
    evidence = {
        field: current.field_evidence[field].model_copy(deep=True)
        for field in SUBMISSION_METADATA_FIELDS
    }
    warnings = list(current.warnings)
    changed_fields: list[str] = []
    for field in accepted:
        proposed = decision.values.get(field, "").strip()
        if not proposed:
            raise MetadataRecheckValidationError("接受字段缺少待应用值，请重新预检。")
        current_value = getattr(current, field)
        values[field] = proposed
        candidate_evidence = candidate.field_evidence[field]
        if proposed != current_value:
            changed_fields.append(field)
            evidence[field] = SubmissionFieldEvidence(
                source=SubmissionMetadataSource.HUMAN_CORRECTION,
                confidence=1.0,
                page=candidate_evidence.page,
                block_id=candidate_evidence.block_id,
                block_ids=(
                    list(candidate_evidence.block_ids)
                    if candidate_evidence.block_ids is not None
                    else None
                ),
                evidence=candidate_evidence.evidence,
            )
            warnings = _clear_field_warnings(warnings, field)
        elif (
            proposed == getattr(candidate, field)
            and candidate_evidence != current.field_evidence[field]
        ):
            evidence[field] = candidate_evidence.model_copy(deep=True)
            warnings = _refresh_field_warnings(warnings, field, candidate)

    if changed_fields and "信息已人工修改" not in warnings:
        warnings.append("信息已人工修改")

    if not accepted and current.human_reviewed == decision.human_reviewed:
        return None
    if (
        all(values[field] == getattr(current, field) for field in SUBMISSION_METADATA_FIELDS)
        and all(
            evidence[field] == current.field_evidence[field]
            for field in SUBMISSION_METADATA_FIELDS
        )
        and current.human_reviewed == decision.human_reviewed
    ):
        return None
    return SubmissionMetadata(
        schema_version=candidate.schema_version if accepted else current.schema_version,
        student_name=values["student_name"],
        student_id=values["student_id"],
        major=values["major"],
        paper_title=values["paper_title"],
        field_evidence=evidence,
        warnings=warnings,
        human_reviewed=decision.human_reviewed,
    )


def _refresh_field_warnings(
    warnings: list[str],
    field: str,
    candidate: SubmissionMetadata,
) -> list[str]:
    title = _FIELD_TITLES[field]
    refreshed = _clear_field_warnings(warnings, field)
    detail = candidate.field_evidence[field]
    if detail.source is SubmissionMetadataSource.PLACEHOLDER:
        refreshed.append(f"{title}未能自动识别，请人工核对")
    elif detail.confidence < 0.75:
        refreshed.append(f"{title}识别置信度较低，请人工核对")
    return list(dict.fromkeys(refreshed))


def _clear_field_warnings(warnings: list[str], field: str) -> list[str]:
    title = _FIELD_TITLES[field]
    stale = {
        f"{title}未能自动识别，请人工核对",
        f"{title}识别置信度较低，请人工核对",
    }
    return [warning for warning in warnings if warning not in stale]
