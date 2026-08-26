from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from paper_reviewer.domain.document import DocumentBlock, DocumentInfo, normalize_text
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.ports.model import Message, ModelPort, ModelRequest, ToolCall, ToolSpec

SUBMIT_METADATA_TOOL = "submit_submission_metadata"
MAX_METADATA_INPUT_CHARACTERS = 20_000
MAX_METADATA_PAGE = 3

_PLACEHOLDERS = {
    "student_name": "未识别姓名",
    "student_id": "未识别学号",
    "major": "未识别专业",
    "paper_title": "未识别题目",
}
_LABELS = {
    "student_name": ("学生姓名", "姓名", "作者"),
    "student_id": ("学生学号", "学号", "学生编号"),
    "major": ("专业名称", "所学专业", "专业"),
    "paper_title": ("课程论文题目", "论文题目", "题目"),
}
_LABEL_PATTERN = {
    field: re.compile(
        rf"(?:^|[\s;；|])(?:{'|'.join(map(re.escape, labels))})\s*[:：]\s*"
        r"(?P<value>[^;；|]{1,200}?)(?=(?:\s+(?:姓名|学生姓名|作者|学号|学生学号|"
        r"学生编号|专业|专业名称|所学专业|题目|论文题目|课程论文题目)\s*[:：])|$)",
        flags=re.IGNORECASE,
    )
    for field, labels in _LABELS.items()
}
_IDENTITY_LABEL_PATTERN = re.compile(
    r"(?:学生姓名|姓名|作者|学生学号|学号|学生编号|专业名称|所学专业|专业)\s*[:：]",
    flags=re.IGNORECASE,
)
_TITLE_LABEL_PATTERN = re.compile(r"(?:课程论文题目|论文题目|题目)\s*[:：]", re.IGNORECASE)
_STUDENT_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]?\d{6,20})(?![A-Za-z0-9])")
_EXACT_LABEL_TO_FIELD = {
    normalize_text(label).casefold(): field
    for field, labels in _LABELS.items()
    for label in labels
}


class _ModelField(BaseModel):
    value: str = Field(min_length=1, max_length=300)
    block_id: str = Field(min_length=1)
    page: int = Field(ge=1, le=MAX_METADATA_PAGE)
    quote: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class _ModelSubmission(BaseModel):
    student_name: _ModelField | None = None
    student_id: _ModelField | None = None
    major: _ModelField | None = None
    paper_title: _ModelField | None = None


class _Candidate(BaseModel):
    value: str
    evidence: SubmissionFieldEvidence


async def extract_submission_metadata(
    *,
    model: ModelPort,
    document: DocumentInfo,
    blocks: Sequence[DocumentBlock],
    run_id: str,
    pdf_metadata: Mapping[str, str | None] | None = None,
) -> SubmissionMetadata:
    """Extract submission identity with exactly one logical model request.

    Transport/authentication errors intentionally propagate. A syntactically or
    semantically invalid model result is untrusted and simply loses to local
    extraction; response content is never copied into returned warnings.
    """

    ordered_blocks = _ordered_front_blocks(blocks)
    local = _cover_label_candidates(ordered_blocks)
    response = await model.complete(
        ModelRequest(
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=_metadata_prompt(ordered_blocks)),
            ],
            tools=[_metadata_tool()],
            forced_tool_name=SUBMIT_METADATA_TOOL,
            max_output_tokens=1024,
            temperature=0,
            trace_id=f"{run_id}:submission-metadata",
            idempotency_key=f"{run_id}:submission-metadata",
        )
    )
    model_candidates = _validated_model_candidates(response.tool_calls, ordered_blocks)
    pdf_candidates = _pdf_metadata_candidates(document, pdf_metadata)
    filename_candidates = _filename_candidates(Path(document.source_path))

    warnings: list[str] = []
    selected: dict[str, _Candidate] = {}
    for field in SUBMISSION_METADATA_FIELDS:
        candidate = (
            local.get(field)
            or model_candidates.get(field)
            or pdf_candidates.get(field)
            or filename_candidates.get(field)
        )
        if candidate is None:
            candidate = _Candidate(
                value=_PLACEHOLDERS[field],
                evidence=SubmissionFieldEvidence(
                    source=SubmissionMetadataSource.PLACEHOLDER,
                    confidence=0,
                ),
            )
            warnings.append(f"{_field_title(field)}未能自动识别，请人工核对")
        elif candidate.evidence.confidence < 0.75:
            warnings.append(f"{_field_title(field)}识别置信度较低，请人工核对")
        selected[field] = candidate

    return SubmissionMetadata(
        student_name=selected["student_name"].value,
        student_id=selected["student_id"].value,
        major=selected["major"].value,
        paper_title=selected["paper_title"].value,
        field_evidence={field: selected[field].evidence for field in SUBMISSION_METADATA_FIELDS},
        warnings=warnings,
    )


def is_identity_only_block(
    block: DocumentBlock,
    metadata: SubmissionMetadata | None = None,
) -> bool:
    """Return whether a front-matter block contains identity data, not paper content."""

    if block.page > MAX_METADATA_PAGE or _TITLE_LABEL_PATTERN.search(block.text):
        return False
    if _IDENTITY_LABEL_PATTERN.search(block.text) or _exact_label_field(block.text) in {
        "student_name",
        "student_id",
        "major",
    }:
        return len(block.text) <= 500
    if metadata is None:
        return False
    normalized = normalize_text(block.text)
    identity_values = (
        metadata.student_name,
        metadata.student_id,
        metadata.major,
    )
    return len(normalized) <= 200 and any(
        detail.source is not SubmissionMetadataSource.PLACEHOLDER
        and normalize_text(value) == normalized
        for value, detail in zip(
            identity_values,
            (
                metadata.field_evidence["student_name"],
                metadata.field_evidence["student_id"],
                metadata.field_evidence["major"],
            ),
            strict=True,
        )
    )


def filter_identity_blocks(
    blocks: Sequence[DocumentBlock],
    metadata: SubmissionMetadata | None = None,
) -> list[DocumentBlock]:
    return [block for block in blocks if not is_identity_only_block(block, metadata)]


def _ordered_front_blocks(blocks: Sequence[DocumentBlock]) -> list[DocumentBlock]:
    indexed = enumerate(blocks)
    return [
        block
        for _, block in sorted(
            ((index, block) for index, block in indexed if block.page <= MAX_METADATA_PAGE),
            key=lambda item: (
                item[1].page,
                item[1].bbox[1] if item[1].bbox is not None else float("inf"),
                item[1].bbox[0] if item[1].bbox is not None else float("inf"),
                item[0],
            ),
        )
    ]


def _cover_label_candidates(blocks: Sequence[DocumentBlock]) -> dict[str, _Candidate]:
    candidates: dict[str, _Candidate] = {}
    for index, block in enumerate(blocks):
        exact_field = _exact_label_field(block.text)
        if exact_field is not None and exact_field not in candidates and index + 1 < len(blocks):
            value_block = blocks[index + 1]
            value = _clean_value(value_block.text)
            if (
                value_block.page == block.page
                and not _is_metadata_label(value_block.text)
                and _plausible(exact_field, value)
            ):
                candidates[exact_field] = _Candidate(
                    value=value,
                    evidence=SubmissionFieldEvidence(
                        source=SubmissionMetadataSource.COVER_LABEL,
                        confidence=0.97,
                        page=value_block.page,
                        block_id=value_block.block_id,
                        evidence=_bounded_evidence(value_block.text),
                    ),
                )
        for field, pattern in _LABEL_PATTERN.items():
            if field in candidates:
                continue
            match = pattern.search(block.text)
            if match is None:
                continue
            value = _clean_value(match.group("value"))
            if not _plausible(field, value):
                continue
            candidates[field] = _Candidate(
                value=value,
                evidence=SubmissionFieldEvidence(
                    source=SubmissionMetadataSource.COVER_LABEL,
                    confidence=0.99,
                    page=block.page,
                    block_id=block.block_id,
                    evidence=_bounded_evidence(match.group(0)),
                ),
            )
    return candidates


def _validated_model_candidates(
    tool_calls: Sequence[ToolCall],
    blocks: Sequence[DocumentBlock],
) -> dict[str, _Candidate]:
    submissions = [call for call in tool_calls if call.name == SUBMIT_METADATA_TOOL]
    if len(tool_calls) != 1 or len(submissions) != 1:
        return {}
    try:
        submission = _ModelSubmission.model_validate(submissions[0].arguments)
    except (ValidationError, ValueError):
        return {}
    by_id = {block.block_id: block for block in blocks}
    candidates: dict[str, _Candidate] = {}
    for field in SUBMISSION_METADATA_FIELDS:
        extracted = getattr(submission, field)
        if extracted is None:
            continue
        block = by_id.get(extracted.block_id)
        if block is None or block.page != extracted.page:
            continue
        quote = normalize_text(extracted.quote)
        value = _clean_value(extracted.value)
        if (
            not quote
            or quote not in block.text
            or value not in quote
            or not _plausible(field, value)
        ):
            continue
        candidates[field] = _Candidate(
            value=value,
            evidence=SubmissionFieldEvidence(
                source=SubmissionMetadataSource.MODEL_EVIDENCE,
                confidence=min(extracted.confidence, 0.9),
                page=block.page,
                block_id=block.block_id,
                evidence=_bounded_evidence(quote),
            ),
        )
    return candidates


def _pdf_metadata_candidates(
    document: DocumentInfo,
    metadata: Mapping[str, str | None] | None,
) -> dict[str, _Candidate]:
    raw = metadata or {}
    values = {
        "student_name": raw.get("author"),
        "paper_title": raw.get("title") or document.title,
    }
    return {
        field: _Candidate(
            value=clean,
            evidence=SubmissionFieldEvidence(
                source=SubmissionMetadataSource.PDF_METADATA,
                confidence=0.65,
            ),
        )
        for field, value in values.items()
        if value and (clean := _clean_value(value)) and _plausible(field, clean)
    }


def _filename_candidates(path: Path) -> dict[str, _Candidate]:
    stem = unicodedata.normalize("NFKC", path.stem).strip()
    parts = [_clean_value(part) for part in re.split(r"_+", stem) if part.strip()]
    values: dict[str, str] = {}
    if len(parts) >= 4:
        values = {
            "student_name": parts[0],
            "student_id": parts[1],
            "major": parts[2],
            "paper_title": "_".join(parts[3:]),
        }
    else:
        match = _STUDENT_ID_PATTERN.search(stem)
        if match is not None:
            values["student_id"] = match.group(1)
        if stem:
            values["paper_title"] = stem
    return {
        field: _Candidate(
            value=value,
            evidence=SubmissionFieldEvidence(
                source=SubmissionMetadataSource.FILE_NAME,
                confidence=0.55,
            ),
        )
        for field, value in values.items()
        if _plausible(field, value)
    }


def _metadata_prompt(blocks: Sequence[DocumentBlock]) -> str:
    sections: list[str] = []
    used = 0
    for block in blocks:
        prefix = f"[page={block.page} block_id={block.block_id}] "
        available = MAX_METADATA_INPUT_CHARACTERS - used - len(prefix) - 1
        if available <= 0:
            break
        text = block.text[:available]
        sections.append(prefix + text)
        used += len(prefix) + len(text) + 1
    return (
        "请仅从下列论文前置页面证据提取姓名、学号、专业和题目。每个非空字段必须给出"
        "原始 block_id、页码、逐字证据短句和置信度；无法确认的字段返回 null。不要猜测。\n\n"
        + "\n".join(sections)
    )


def _metadata_tool() -> ToolSpec:
    field_schema: dict[str, object] = {
        "type": ["object", "null"],
        "properties": {
            "value": {"type": "string", "minLength": 1, "maxLength": 300},
            "block_id": {"type": "string", "minLength": 1},
            "page": {"type": "integer", "minimum": 1, "maximum": MAX_METADATA_PAGE},
            "quote": {"type": "string", "minLength": 1, "maxLength": 500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["value", "block_id", "page", "quote", "confidence"],
        "additionalProperties": False,
    }
    return ToolSpec(
        name=SUBMIT_METADATA_TOOL,
        description="提交课程论文封面中可由原文验证的学生及论文元数据。",
        parameters={
            "type": "object",
            "properties": {field: field_schema for field in SUBMISSION_METADATA_FIELDS},
            "required": list(SUBMISSION_METADATA_FIELDS),
            "additionalProperties": False,
        },
    )


def _clean_value(value: str) -> str:
    return unicodedata.normalize("NFKC", normalize_text(value)).strip(" :：;；|_-")


def _exact_label_field(value: str) -> str | None:
    normalized = normalize_text(value).strip(" :：").casefold()
    return _EXACT_LABEL_TO_FIELD.get(normalized)


def _is_metadata_label(value: str) -> bool:
    """Reject a neighbouring label block before it can be mistaken for a value."""

    if _exact_label_field(value) is not None:
        return True
    normalized = unicodedata.normalize("NFKC", normalize_text(value))
    return bool(
        _IDENTITY_LABEL_PATTERN.search(normalized)
        or _TITLE_LABEL_PATTERN.search(normalized)
    )


def _plausible(field: str, value: str) -> bool:
    if not value or len(value) > 300:
        return False
    if field == "student_id":
        return _STUDENT_ID_PATTERN.fullmatch(value) is not None
    if field == "student_name":
        return len(value) <= 80 and not any(char.isdigit() for char in value)
    return len(value) >= 2


def _bounded_evidence(value: str) -> str:
    return normalize_text(value)[:500]


def _field_title(field: str) -> str:
    return {
        "student_name": "姓名",
        "student_id": "学号",
        "major": "专业",
        "paper_title": "题目",
    }[field]


_SYSTEM_PROMPT = (
    "你是课程论文提交信息提取器。你只能提取原文中明确出现的信息，不能根据姓名、题目或"
    "其他线索推断专业或身份。必须且只能调用 submit_submission_metadata 一次。"
)
