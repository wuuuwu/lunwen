from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    "paper_title": "未识别题目",
}
_LABELS = {
    "student_name": ("学生姓名", "姓名", "作者"),
    "student_id": ("学生学号", "学号", "学生编号"),
    "paper_title": ("课程论文题目", "论文题目", "题目"),
}
_SENSITIVE_IDENTITY_LABELS = ("专业名称", "所学专业", "专业")
_FIELD_BOUNDARY_LABELS = tuple(
    dict.fromkeys(
        (
            *(label for labels in _LABELS.values() for label in labels),
            *_SENSITIVE_IDENTITY_LABELS,
            "得分",
            "成绩",
            "分数",
            "评分",
            "班级",
            "任课教师",
            "考核方法",
            "考核方式",
            "教师评语",
        )
    )
)
_FIELD_BOUNDARY_ALTERNATION = "|".join(
    map(re.escape, sorted(_FIELD_BOUNDARY_LABELS, key=len, reverse=True))
)
_LAYOUT_BOUNDARY_PREFIX = r"(?:\s+|[;；|]\s*)"
_ADJACENT_BOUNDARY_PREFIX = r"\s*"
_LABEL_PATTERN = {
    field: re.compile(
        rf"(?:^|[\s;；|])(?:{'|'.join(map(re.escape, labels))})\s*[:：]\s*"
        rf"(?P<value>[^;；|]{{1,200}}?)(?=(?:"
        rf"{_LAYOUT_BOUNDARY_PREFIX if field == 'paper_title' else _ADJACENT_BOUNDARY_PREFIX}"
        rf"(?:{_FIELD_BOUNDARY_ALTERNATION})\s*[:：])|$)",
        flags=re.IGNORECASE,
    )
    for field, labels in _LABELS.items()
}
_SENSITIVE_LABEL_ALTERNATION = "|".join(
    map(re.escape, sorted(_SENSITIVE_IDENTITY_LABELS, key=len, reverse=True))
)
_SENSITIVE_INLINE_PATTERN = re.compile(
    rf"(?P<prefix>^|[\s;；|])(?:{_SENSITIVE_LABEL_ALTERNATION})\s*[:：]\s*"
    rf"[^;；|]{{0,200}}?(?=(?:{_ADJACENT_BOUNDARY_PREFIX}"
    rf"(?:{_FIELD_BOUNDARY_ALTERNATION})\s*[:：])|$)",
    flags=re.IGNORECASE,
)
_EXPLICIT_SENSITIVE_INLINE_PATTERN = re.compile(
    r"(?:^|[\s;；|])(?:专业名称|所学专业)\s*[:：]",
    flags=re.IGNORECASE,
)
_BARE_SENSITIVE_LINE_PATTERN = re.compile(
    r"^\s*专业\s*[:：]\s*(?P<value>\S(?:.{0,299}\S)?)\s*$",
    flags=re.IGNORECASE,
)
_TITLE_LIKE_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:研究|分析|探讨|机制|路径|影响|评价|改革|课程目标)"
)
_IDENTITY_LABEL_PATTERN = re.compile(
    r"(?:学生姓名|姓名|作者|学生学号|学号|学生编号)\s*[:：]",
    flags=re.IGNORECASE,
)
_COVER_CONTEXT_LABEL_PATTERN = re.compile(
    r"(?:学生姓名|姓名|作者|学生学号|学号|学生编号|课程论文题目|论文题目|题目|"
    r"班级|任课教师|考核方法|考核方式|教师评语|得分|成绩|分数|评分)\s*[:：]",
    flags=re.IGNORECASE,
)
_TITLE_LABEL_PATTERN = re.compile(r"(?:课程论文题目|论文题目|题目)\s*[:：]", re.IGNORECASE)
# A following field label is a boundary only when the layout separates it from
# the current value.  Matching at an arbitrary substring would truncate valid
# titles such as ``课程评分：……`` or ``专业：……的课程设计``.
_FIELD_BOUNDARY_PATTERN = re.compile(
    rf"{_LAYOUT_BOUNDARY_PREFIX}(?:{_FIELD_BOUNDARY_ALTERNATION})\s*[:：]",
    re.IGNORECASE,
)
_ADJACENT_FIELD_BOUNDARY_PATTERN = re.compile(
    rf"{_ADJACENT_BOUNDARY_PREFIX}(?:{_FIELD_BOUNDARY_ALTERNATION})\s*[:：]",
    re.IGNORECASE,
)
_METADATA_LABEL_AT_START_PATTERN = re.compile(
    rf"^(?:{_FIELD_BOUNDARY_ALTERNATION})\s*(?:[:：]|$)", re.IGNORECASE
)
_STUDENT_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]?\d{6,20})(?![A-Za-z0-9])")
_EXACT_LABEL_TO_FIELD = {
    normalize_text(label).casefold(): field
    for field, labels in _LABELS.items()
    for label in labels
}
_EXACT_SENSITIVE_LABELS = {
    normalize_text(label).casefold() for label in _SENSITIVE_IDENTITY_LABELS
}


class _ModelField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=300)
    block_id: str = Field(min_length=1)
    page: int = Field(ge=1, le=MAX_METADATA_PAGE)
    quote: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class _ModelSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    student_name: _ModelField | None = None
    student_id: _ModelField | None = None
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
                Message(
                    role="user",
                    content=_metadata_prompt(document, ordered_blocks),
                ),
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
    return _build_submission_metadata(
        document=document,
        local_candidates=local,
        model_candidates=model_candidates,
        pdf_metadata=pdf_metadata,
    )


def suggest_submission_metadata_locally(
    *,
    document: DocumentInfo,
    blocks: Sequence[DocumentBlock],
    current: SubmissionMetadata | None = None,
    pdf_metadata: Mapping[str, str | None] | None = None,
) -> SubmissionMetadata:
    """Recheck metadata from local evidence without making a model request.

    Previously validated model evidence may be reused only when its page, block ID,
    quote, and value all remain verifiable against the freshly supplied blocks.
    """

    ordered_blocks = _ordered_front_blocks(blocks)
    return _build_submission_metadata(
        document=document,
        local_candidates=_cover_label_candidates(ordered_blocks),
        model_candidates=_revalidated_current_model_candidates(current, ordered_blocks),
        pdf_metadata=pdf_metadata,
    )


def _build_submission_metadata(
    *,
    document: DocumentInfo,
    local_candidates: Mapping[str, _Candidate],
    model_candidates: Mapping[str, _Candidate],
    pdf_metadata: Mapping[str, str | None] | None,
) -> SubmissionMetadata:
    visible_title_candidate = _visible_title_candidate(document)
    filename_candidates = _filename_candidates(Path(document.source_path))

    warnings: list[str] = []
    selected: dict[str, _Candidate] = {}
    for field in SUBMISSION_METADATA_FIELDS:
        if field == "paper_title":
            candidate = (
                local_candidates.get(field)
                or visible_title_candidate
                or model_candidates.get(field)
                or filename_candidates.get(field)
            )
        else:
            candidate = (
                local_candidates.get(field)
                or model_candidates.get(field)
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

    embedded_title = _embedded_title(document, pdf_metadata)
    selected_title = selected["paper_title"]
    if (
        embedded_title is not None
        and selected_title.evidence.source
        in {
            SubmissionMetadataSource.COVER_LABEL,
            SubmissionMetadataSource.VISIBLE_HEADING,
            SubmissionMetadataSource.MODEL_EVIDENCE,
        }
        and not _same_title(embedded_title, selected_title.value)
    ):
        warnings.append("PDF 隐藏标题与正文题目不一致")

    return SubmissionMetadata(
        student_name=selected["student_name"].value,
        student_id=selected["student_id"].value,
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
    if (
        _IDENTITY_LABEL_PATTERN.search(block.text)
        or _exact_label_field(block.text) in {"student_name", "student_id"}
        or _is_sensitive_identity_label(block.text)
    ):
        return len(block.text) <= 500
    if metadata is None:
        return False
    normalized = normalize_text(block.text)
    identity_values = (
        metadata.student_name,
        metadata.student_id,
    )
    return len(normalized) <= 200 and any(
        detail.source is not SubmissionMetadataSource.PLACEHOLDER
        and normalize_text(value) == normalized
        for value, detail in zip(
            identity_values,
            (
                metadata.field_evidence["student_name"],
                metadata.field_evidence["student_id"],
            ),
            strict=True,
        )
    )


def filter_identity_blocks(
    blocks: Sequence[DocumentBlock],
    metadata: SubmissionMetadata | None = None,
) -> list[DocumentBlock]:
    protected_title_block_ids = _protected_title_block_ids(metadata)
    dropped = {
        index
        for index, block in enumerate(blocks)
        if is_identity_only_block(block, metadata)
    }
    dropped.update(
        _sensitive_cover_block_indexes(
            blocks,
            protected_block_ids=protected_title_block_ids,
        )
    )
    return [block for index, block in enumerate(blocks) if index not in dropped]


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
            value = _clean_field_value(exact_field, value_block.text)
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
            value = _clean_field_value(field, match.group("value"))
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
        quote = _normalize_layout_whitespace(extracted.quote)
        block_text = _normalize_layout_whitespace(block.text)
        value = _clean_field_value(field, extracted.value)
        normalized_value = _normalize_layout_whitespace(value)
        if (
            not quote
            or quote not in block_text
            or normalized_value not in quote
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
                evidence=_bounded_evidence(extracted.quote),
            ),
        )
    return candidates


def _revalidated_current_model_candidates(
    current: SubmissionMetadata | None,
    blocks: Sequence[DocumentBlock],
) -> dict[str, _Candidate]:
    if current is None:
        return {}
    by_id = {block.block_id: block for block in blocks}
    candidates: dict[str, _Candidate] = {}
    for field in SUBMISSION_METADATA_FIELDS:
        evidence = current.field_evidence[field]
        if (
            evidence.source is not SubmissionMetadataSource.MODEL_EVIDENCE
            or evidence.block_id is None
            or evidence.page is None
            or evidence.evidence is None
        ):
            continue
        block = by_id.get(evidence.block_id)
        if block is None or block.page != evidence.page:
            continue
        value = _clean_field_value(field, getattr(current, field))
        quote = _normalize_layout_whitespace(evidence.evidence)
        if (
            not quote
            or quote not in _normalize_layout_whitespace(block.text)
            or _normalize_layout_whitespace(value) not in quote
            or not _plausible(field, value)
        ):
            continue
        candidates[field] = _Candidate(value=value, evidence=evidence)
    return candidates


def _visible_title_candidate(document: DocumentInfo) -> _Candidate | None:
    raw_title = getattr(document, "visible_title", None)
    if not isinstance(raw_title, str):
        return None
    title = _clean_title_text(raw_title)
    if not _plausible("paper_title", title):
        return None

    raw_page = getattr(document, "visible_title_page", None)
    page = raw_page if isinstance(raw_page, int) and raw_page >= 1 else None
    raw_block_ids = getattr(document, "visible_title_block_ids", None)
    block_ids = (
        list(dict.fromkeys(item for item in raw_block_ids if isinstance(item, str) and item))
        if isinstance(raw_block_ids, Sequence) and not isinstance(raw_block_ids, str)
        else []
    )
    return _Candidate(
        value=title,
        evidence=SubmissionFieldEvidence(
            source=SubmissionMetadataSource.VISIBLE_HEADING,
            confidence=0.96,
            page=page,
            block_id=block_ids[0] if block_ids else None,
            block_ids=block_ids or None,
            evidence=_bounded_evidence(raw_title),
        ),
    )


def _embedded_title(
    document: DocumentInfo,
    metadata: Mapping[str, str | None] | None,
) -> str | None:
    raw = metadata or {}
    for value in (raw.get("title"), getattr(document, "embedded_title", None)):
        if isinstance(value, str) and (clean := _clean_title_text(value)):
            return clean
    return None


def _filename_candidates(path: Path) -> dict[str, _Candidate]:
    stem = unicodedata.normalize("NFKC", path.stem).strip()
    parts = [_clean_value(part) for part in re.split(r"_+", stem) if part.strip()]
    values: dict[str, str] = {}
    if len(parts) == 3 and _STUDENT_ID_PATTERN.fullmatch(parts[1]) is not None:
        values = {
            "student_name": parts[0],
            "student_id": parts[1],
            "paper_title": parts[2],
        }
    else:
        match = _STUDENT_ID_PATTERN.search(stem)
        if match is not None:
            values["student_id"] = match.group(1)
    return {
        field: _Candidate(
            value=clean,
            evidence=SubmissionFieldEvidence(
                source=SubmissionMetadataSource.FILE_NAME,
                confidence=0.55,
            ),
        )
        for field, value in values.items()
        if (
            clean := (
                _clean_title_text(value)
                if field == "paper_title"
                else _clean_field_value(field, value)
            )
        )
        and _plausible(field, clean)
    }


def _metadata_prompt(
    document: DocumentInfo,
    blocks: Sequence[DocumentBlock],
) -> str:
    sections: list[str] = []
    used = 0
    protected_block_ids = {
        block_id
        for block_id in (getattr(document, "visible_title_block_ids", None) or [])
        if isinstance(block_id, str) and block_id
    }
    sensitive_value_indexes = _sensitive_label_value_block_indexes(
        blocks,
        protected_block_ids=protected_block_ids,
    )
    cover_pages = _cover_context_pages(blocks)
    for index, block in enumerate(blocks):
        prefix = f"[page={block.page} block_id={block.block_id}] "
        available = MAX_METADATA_INPUT_CHARACTERS - used - len(prefix) - 1
        if available <= 0:
            break
        if index in sensitive_value_indexes:
            text = "[已移除字段]"
        elif _is_sensitive_inline_cover_field(
            block,
            cover_pages=cover_pages,
        ):
            text = _redact_sensitive_identity_fields(block.text)
        else:
            text = block.text
        text = text[:available]
        sections.append(prefix + text)
        used += len(prefix) + len(text) + 1
    return (
        "请仅从下列论文前置页面证据提取姓名、学号和题目。每个非空字段必须给出"
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


def _clean_field_value(field: str, value: str) -> str:
    if field == "paper_title":
        # Visible headings are report-facing text.  Keep their original
        # punctuation (notably Chinese full-width punctuation) and only remove
        # layout whitespace introduced between adjacent Chinese characters.
        normalized_title = normalize_text(value)
        boundary = _FIELD_BOUNDARY_PATTERN.search(normalized_title)
        if boundary is not None:
            normalized_title = normalized_title[: boundary.start()]
        return _clean_title_text(normalized_title)
    normalized = unicodedata.normalize("NFKC", normalize_text(value))
    boundary = _ADJACENT_FIELD_BOUNDARY_PATTERN.search(normalized)
    if boundary is not None:
        normalized = normalized[: boundary.start()]
    return _clean_value(normalized)


def _normalize_layout_whitespace(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", normalize_text(value))
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)


def _normalize_title_layout_whitespace(value: str) -> str:
    normalized = normalize_text(value)
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", normalized)


def _clean_title_text(value: str) -> str:
    clean = normalize_text(value).strip(" :：;；|_-")
    return _normalize_title_layout_whitespace(clean)


def _same_title(left: str, right: str) -> bool:
    return _normalize_layout_whitespace(left).casefold() == _normalize_layout_whitespace(
        right
    ).casefold()


def _exact_label_field(value: str) -> str | None:
    normalized = normalize_text(value).strip(" :：").casefold()
    return _EXACT_LABEL_TO_FIELD.get(normalized)


def _is_sensitive_identity_label(value: str) -> bool:
    normalized = normalize_text(value).strip(" :：").casefold()
    return normalized in _EXACT_SENSITIVE_LABELS


def _sensitive_cover_block_indexes(
    blocks: Sequence[DocumentBlock],
    *,
    protected_block_ids: set[str],
) -> set[int]:
    """Locate professional cover fields without treating paper prose as identity.

    A bare ``专业：……`` phrase is ambiguous in Chinese: it may be a cover field,
    but it may also be a legitimate title or sentence.  Inline forms therefore
    require a cover-field signal, an explicit label, or a short early-page value
    that is not title-like.  An exact label block and its adjacent short value are
    always removed.  Known visible-title evidence is protected first.
    """

    indexes = _sensitive_label_value_block_indexes(
        blocks,
        protected_block_ids=protected_block_ids,
    )
    cover_pages = _cover_context_pages(blocks)
    for index, block in enumerate(blocks):
        if _is_sensitive_inline_cover_field(
            block,
            cover_pages=cover_pages,
        ):
            indexes.add(index)
    return indexes


def _sensitive_label_value_block_indexes(
    blocks: Sequence[DocumentBlock],
    *,
    protected_block_ids: set[str],
) -> set[int]:
    indexes: set[int] = set()
    for index, block in enumerate(blocks):
        if block.block_id in protected_block_ids or not _is_sensitive_identity_label(
            block.text
        ):
            continue
        indexes.add(index)
        if index + 1 < len(blocks):
            value_block = blocks[index + 1]
            if (
                value_block.block_id not in protected_block_ids
                and value_block.page == block.page
                and len(value_block.text) <= 200
                and not _is_metadata_label(value_block.text)
                and not _TITLE_LABEL_PATTERN.search(value_block.text)
            ):
                indexes.add(index + 1)
    return indexes


def _cover_context_pages(blocks: Sequence[DocumentBlock]) -> set[int]:
    return {
        block.page for block in blocks if _COVER_CONTEXT_LABEL_PATTERN.search(block.text)
    }


def _is_sensitive_inline_cover_field(
    block: DocumentBlock,
    *,
    cover_pages: set[int],
) -> bool:
    if _SENSITIVE_INLINE_PATTERN.search(block.text) is None:
        return False
    bare = _BARE_SENSITIVE_LINE_PATTERN.fullmatch(normalize_text(block.text))
    if (
        bare is not None
        and _TITLE_LIKE_SENSITIVE_VALUE_PATTERN.search(bare.group("value")) is not None
    ):
        return False
    if (
        block.page in cover_pages
        or _EXPLICIT_SENSITIVE_INLINE_PATTERN.search(block.text)
    ):
        return True
    return bool(
        bare is not None
        and block.page <= MAX_METADATA_PAGE
        and _TITLE_LIKE_SENSITIVE_VALUE_PATTERN.search(bare.group("value")) is None
    )


def _redact_sensitive_identity_fields(value: str) -> str:
    return _SENSITIVE_INLINE_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[已移除字段]",
        value,
    )


def _protected_title_block_ids(metadata: SubmissionMetadata | None) -> set[str]:
    if metadata is None:
        return set()
    evidence = metadata.field_evidence["paper_title"]
    return {
        block_id
        for block_id in ([evidence.block_id] + list(evidence.block_ids or []))
        if block_id
    }


def _is_metadata_label(value: str) -> bool:
    """Reject a neighbouring label block before it can be mistaken for a value."""

    if _exact_label_field(value) is not None:
        return True
    normalized = unicodedata.normalize("NFKC", normalize_text(value))
    return _METADATA_LABEL_AT_START_PATTERN.search(normalized) is not None


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
        "paper_title": "题目",
    }[field]


_SYSTEM_PROMPT = (
    "你是课程论文提交信息提取器。你只能提取原文中明确出现的信息，不能根据姓名、题目或"
    "其他线索推断额外身份信息。必须且只能调用 submit_submission_metadata 一次。"
)
