from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import HttpUrl

from paper_reviewer.domain.document import BlockType, DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceLevel
from paper_reviewer.domain.reference import (
    ReferenceCheck,
    ReferenceCheckReport,
    ReferenceEntry,
    ReferenceVerificationStatus,
    normalize_doi,
)
from paper_reviewer.ports.scholarly_search import ScholarlySearchPort, ScholarlyWork

if TYPE_CHECKING:
    from paper_reviewer.ports.web_search import WebSearchPort, WebSearchResult


MAX_EXTRACTED_REFERENCES = 200
MAX_REFERENCE_CHECKS = 200
SEARCH_QUERY_MAX_CHARS = 240

# Public, fixed thresholds make the automatic decision policy auditable.
VERIFIED_SCORE_THRESHOLD = 0.86
PROBABLE_SCORE_THRESHOLD = 0.58

_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)")
_NUMBER_PREFIX = re.compile(
    r"^\s*(?:\[\s*\d{1,3}\s*\]|\(\s*\d{1,3}\s*\)|\d{1,3}\s*[.)、])\s*"
)
_BRACKET_NUMBER_MARKER = re.compile(r"(?<!\w)\[\s*\d{1,3}\s*\]\s*")
_REFERENCE_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*)\s*)?"
    r"(?:references?|bibliography|works\s+cited|参考文献|参考书目|引用文献)"
    r"(?:\s*(?:\(continued\)|（续）))?$",
    re.IGNORECASE,
)


@dataclass
class _RawReference:
    text_parts: list[str]
    block_id: str
    page: int
    explicitly_numbered: bool

    @property
    def text(self) -> str:
        return " ".join(self.text_parts)


@dataclass(frozen=True)
class _Candidate:
    kind: Literal["scholarly", "web"]
    source: str
    source_id: str
    title: str
    content: str
    doi: str | None
    url: HttpUrl | None
    year: int | None
    level: EvidenceLevel
    metadata: dict[str, object]


@dataclass(frozen=True)
class _CandidateMatch:
    candidate: _Candidate
    score: float
    doi_match: bool
    doi_conflict: bool
    year_conflict: bool
    title_coverage: float


@dataclass(frozen=True)
class _SearchOutcome:
    label: str
    candidates: list[_Candidate] = field(default_factory=list)
    error_type: str | None = None


@dataclass(frozen=True)
class _CheckedReference:
    check: ReferenceCheck
    evidence: EvidenceItem | None
    warnings: list[str]


def extract_references(
    blocks: list[DocumentBlock],
    *,
    max_references: int = MAX_EXTRACTED_REFERENCES,
) -> list[ReferenceEntry]:
    """Extract and deduplicate bibliography entries while retaining their source block."""
    if max_references < 1:
        raise ValueError("max_references must be at least 1")

    raw_entries: list[_RawReference] = []
    pending_numbered: _RawReference | None = None

    def flush_pending() -> None:
        nonlocal pending_numbered
        if pending_numbered is not None:
            raw_entries.append(pending_numbered)
            pending_numbered = None

    for block in blocks:
        in_reference_section = any(
            _is_reference_heading(part) for part in block.section_path
        )
        if block.block_type is not BlockType.REFERENCE and not in_reference_section:
            flush_pending()
            continue

        text = _normalize_whitespace(block.text)
        if not text or _is_reference_heading(text):
            flush_pending()
            continue

        pieces = _split_numbered_references(text)
        for piece in pieces:
            explicitly_numbered = _NUMBER_PREFIX.match(piece) is not None
            cleaned = _NUMBER_PREFIX.sub("", piece, count=1).strip(" •")
            if not cleaned:
                continue

            is_continuation = (
                pending_numbered is not None
                and not explicitly_numbered
                and block.block_type is not BlockType.REFERENCE
                and in_reference_section
            )
            if is_continuation:
                assert pending_numbered is not None
                pending_numbered.text_parts.append(cleaned)
                continue

            flush_pending()
            current = _RawReference(
                text_parts=[cleaned],
                block_id=block.block_id,
                page=block.page,
                explicitly_numbered=explicitly_numbered,
            )
            if explicitly_numbered:
                pending_numbered = current
            else:
                raw_entries.append(current)

    flush_pending()

    entries_by_key: dict[str, ReferenceEntry] = {}
    ordered_keys: list[str] = []
    for raw in raw_entries:
        doi = _extract_doi(raw.text)
        year = _extract_year(raw.text)
        entry = ReferenceEntry.create(
            text=raw.text,
            block_id=raw.block_id,
            page=raw.page,
            doi=doi,
            year=year,
        )
        key = f"doi:{entry.doi}" if entry.doi else f"text:{_canonical_text(entry.text)}"
        existing = entries_by_key.get(key)
        if existing is not None:
            if len(entry.text) > len(existing.text):
                entries_by_key[key] = entry
            continue
        if len(ordered_keys) >= max_references:
            continue
        ordered_keys.append(key)
        entries_by_key[key] = entry

    return [entries_by_key[key] for key in ordered_keys]


async def check_references(
    *,
    run_id: str,
    entries: list[ReferenceEntry],
    web_search: WebSearchPort | None,
    scholarly_clients: list[ScholarlySearchPort],
    per_source_limit: int = 5,
    max_concurrency: int = 6,
) -> tuple[ReferenceCheckReport, list[EvidenceItem]]:
    """Verify references against web and scholarly sources without failing the batch."""
    if per_source_limit < 1:
        raise ValueError("per_source_limit must be at least 1")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")

    limited_entries = entries[:MAX_REFERENCE_CHECKS]
    initial_warnings: list[str] = []
    if len(entries) > MAX_REFERENCE_CHECKS:
        initial_warnings.append(
            f"参考文献超过自动核验上限 {MAX_REFERENCE_CHECKS} 条；"
            "未核验条目建议人工核对。"
        )

    semaphore = asyncio.Semaphore(max_concurrency)
    checked = await asyncio.gather(
        *(
            _check_one_reference(
                run_id=run_id,
                entry=entry,
                web_search=web_search,
                scholarly_clients=scholarly_clients,
                per_source_limit=per_source_limit,
                semaphore=semaphore,
            )
            for entry in limited_entries
        )
    )
    report = ReferenceCheckReport(
        checks=[result.check for result in checked],
        warnings=[
            *initial_warnings,
            *(warning for result in checked for warning in result.warnings),
        ],
    )
    evidence = [result.evidence for result in checked if result.evidence is not None]
    return report, evidence


async def _check_one_reference(
    *,
    run_id: str,
    entry: ReferenceEntry,
    web_search: WebSearchPort | None,
    scholarly_clients: list[ScholarlySearchPort],
    per_source_limit: int,
    semaphore: asyncio.Semaphore,
) -> _CheckedReference:
    query = entry.doi or entry.text[:SEARCH_QUERY_MAX_CHARS]
    jobs: list[Coroutine[Any, Any, _SearchOutcome]] = []
    if web_search is not None:
        jobs.append(
            _search_web(
                web_search,
                query=query,
                limit=per_source_limit,
                semaphore=semaphore,
            )
        )
    jobs.extend(
        _search_scholarly(
            client,
            query=query,
            limit=per_source_limit,
            semaphore=semaphore,
        )
        for client in scholarly_clients
    )
    outcomes = await asyncio.gather(*jobs)

    failed_backends = [outcome for outcome in outcomes if outcome.error_type is not None]
    warnings: list[str] = []
    candidates = [candidate for outcome in outcomes for candidate in outcome.candidates]
    if not candidates:
        warnings.extend(
            f"参考文献 {entry.reference_id}: {outcome.label} 检索失败"
            f"（{outcome.error_type}）；建议人工核对。"
            for outcome in failed_backends
        )
        warnings.append(
            f"参考文献 {entry.reference_id}: 未检索到可用候选；建议人工核对。"
        )
        return _CheckedReference(
            check=ReferenceCheck(
                entry=entry,
                status=ReferenceVerificationStatus.UNRESOLVED,
                score=0.0,
                message="未检索到可用候选，无法自动核验。",
            ),
            evidence=None,
            warnings=warnings,
        )

    matches = [_score_candidate(entry, candidate) for candidate in candidates]
    best = sorted(matches, key=_match_sort_key)[0]
    status = _status_for_match(best)
    warnings.extend(
        f"参考文献 {entry.reference_id}: {outcome.label} 检索失败"
        f"（{outcome.error_type}），已由其他来源完成核验。"
        for outcome in failed_backends
        if status is ReferenceVerificationStatus.VERIFIED
    )
    warnings.extend(
        f"参考文献 {entry.reference_id}: {outcome.label} 检索失败"
        f"（{outcome.error_type}）；建议人工核对。"
        for outcome in failed_backends
        if status is not ReferenceVerificationStatus.VERIFIED
    )
    if best.doi_conflict:
        warnings.append(
            f"参考文献 {entry.reference_id}: 候选 DOI 与原文 DOI 冲突；"
            "建议人工核对。"
        )
    if best.year_conflict:
        warnings.append(
            f"参考文献 {entry.reference_id}: 候选年份 {best.candidate.year} "
            f"与原文年份 {entry.year} 冲突；建议人工核对。"
        )
    if status is ReferenceVerificationStatus.PROBABLE:
        warnings.append(
            f"参考文献 {entry.reference_id}: 仅找到可能匹配（{best.score:.2f}）；"
            "建议人工核对。"
        )
    if status is ReferenceVerificationStatus.UNRESOLVED:
        warnings.append(
            f"参考文献 {entry.reference_id}: 最高匹配分 {best.score:.2f} 未达阈值；"
            "建议人工核对。"
        )

    evidence = (
        _to_evidence(run_id=run_id, entry=entry, query=query, match=best, status=status)
        if status is not ReferenceVerificationStatus.UNRESOLVED
        else None
    )
    evidence_ids = [evidence.evidence_id] if evidence is not None else []
    return _CheckedReference(
        check=ReferenceCheck(
            entry=entry,
            status=status,
            matched_evidence_ids=evidence_ids,
            score=round(best.score, 4),
            message=_match_message(best, status),
        ),
        evidence=evidence,
        warnings=warnings,
    )


async def _search_web(
    client: WebSearchPort,
    *,
    query: str,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> _SearchOutcome:
    label = f"web:{type(client).__name__}"
    try:
        async with semaphore:
            results = await client.search(query, limit=limit)
        return _SearchOutcome(
            label=label,
            candidates=[_candidate_from_web(result) for result in results],
        )
    except Exception as error:
        return _SearchOutcome(label=label, error_type=type(error).__name__)


async def _search_scholarly(
    client: ScholarlySearchPort,
    *,
    query: str,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> _SearchOutcome:
    label = f"scholarly:{type(client).__name__}"
    try:
        async with semaphore:
            results = await client.search(query, limit=limit)
        return _SearchOutcome(
            label=label,
            candidates=[_candidate_from_scholarly(result) for result in results],
        )
    except Exception as error:
        return _SearchOutcome(label=label, error_type=type(error).__name__)


def _candidate_from_web(result: WebSearchResult) -> _Candidate:
    metadata = dict(result.metadata)
    combined = " ".join((result.title, result.snippet, str(result.url)))
    metadata_doi = metadata.get("doi")
    doi = _extract_doi(str(metadata_doi)) if metadata_doi is not None else None
    year = _coerce_year(metadata.get("year")) or _extract_year(combined)
    return _Candidate(
        kind="web",
        source=result.source,
        source_id=str(result.url),
        title=result.title,
        content=result.snippet or "Web search metadata result.",
        doi=doi or _extract_doi(combined),
        url=result.url,
        year=year,
        level=EvidenceLevel.METADATA,
        metadata=metadata,
    )


def _candidate_from_scholarly(result: ScholarlyWork) -> _Candidate:
    doi = normalize_doi(result.doi) if result.doi else None
    return _Candidate(
        kind="scholarly",
        source=result.source,
        source_id=result.source_id,
        title=result.title,
        content=result.abstract or "Metadata-only scholarly result.",
        doi=doi,
        url=result.url,
        year=result.year,
        level=result.level,
        metadata={
            **result.metadata,
            "cited_by_count": result.cited_by_count,
        },
    )


def _score_candidate(entry: ReferenceEntry, candidate: _Candidate) -> _CandidateMatch:
    entry_doi = normalize_doi(entry.doi) if entry.doi else None
    candidate_doi = normalize_doi(candidate.doi) if candidate.doi else None
    doi_match = bool(entry_doi and candidate_doi and entry_doi == candidate_doi)
    doi_conflict = bool(entry_doi and candidate_doi and entry_doi != candidate_doi)
    year_conflict = bool(entry.year and candidate.year and entry.year != candidate.year)
    coverage = _title_coverage(entry.text, candidate.title)

    if doi_match:
        score = 1.0
    else:
        candidate_terms = _matching_terms(candidate.title)
        specificity = 0.65 + 0.35 * min(len(candidate_terms) / 4, 1.0)
        adjusted_coverage = coverage * specificity if len(candidate_terms) >= 2 else 0.0
        if entry.year is not None and candidate.year is not None:
            score = (
                0.88 * adjusted_coverage + 0.12
                if entry.year == candidate.year
                else 0.68 * adjusted_coverage
            )
        else:
            score = 0.9 * adjusted_coverage
        if doi_conflict:
            score = min(score, PROBABLE_SCORE_THRESHOLD - 0.01)

    return _CandidateMatch(
        candidate=candidate,
        score=max(0.0, min(score, 1.0)),
        doi_match=doi_match,
        doi_conflict=doi_conflict,
        year_conflict=year_conflict,
        title_coverage=coverage,
    )


def _match_sort_key(match: _CandidateMatch) -> tuple[float, int, int, str, str]:
    level_rank = {
        EvidenceLevel.FULL_TEXT: 0,
        EvidenceLevel.ABSTRACT: 1,
        EvidenceLevel.METADATA: 2,
    }
    return (
        -match.score,
        0 if match.candidate.kind == "scholarly" else 1,
        level_rank[match.candidate.level],
        match.candidate.source.casefold(),
        match.candidate.source_id.casefold(),
    )


def _status_for_match(match: _CandidateMatch) -> ReferenceVerificationStatus:
    if match.doi_match or match.score >= VERIFIED_SCORE_THRESHOLD:
        return ReferenceVerificationStatus.VERIFIED
    if match.score >= PROBABLE_SCORE_THRESHOLD:
        return ReferenceVerificationStatus.PROBABLE
    return ReferenceVerificationStatus.UNRESOLVED


def _match_message(
    match: _CandidateMatch, status: ReferenceVerificationStatus
) -> str:
    if match.doi_match:
        return f"DOI 精确匹配（{match.candidate.source}）。"
    label = {
        ReferenceVerificationStatus.VERIFIED: "高可信匹配",
        ReferenceVerificationStatus.PROBABLE: "可能匹配",
        ReferenceVerificationStatus.UNRESOLVED: "未解析",
    }[status]
    return (
        f"{label}（{match.candidate.source}）：标题覆盖率 "
        f"{match.title_coverage:.2f}，综合分 {match.score:.2f}。"
    )


def _to_evidence(
    *,
    run_id: str,
    entry: ReferenceEntry,
    query: str,
    match: _CandidateMatch,
    status: ReferenceVerificationStatus,
) -> EvidenceItem:
    candidate = match.candidate
    seed = (
        f"reference-verification|{entry.reference_id}|{candidate.source}|"
        f"{candidate.source_id}|{candidate.doi or ''}|{candidate.title}"
    )
    evidence_id = hashlib.sha256(seed.encode()).hexdigest()[:24]
    return EvidenceItem(
        evidence_id=evidence_id,
        run_id=run_id,
        kind=EvidenceKind.EXTERNAL,
        title=candidate.title,
        content=candidate.content,
        source_name=candidate.source,
        level=candidate.level,
        doi=candidate.doi,
        url=candidate.url,
        metadata={
            **candidate.metadata,
            "reference_id": entry.reference_id,
            "reference_text": entry.text,
            "verification_status": status.value,
            "match_score": round(match.score, 4),
            "query": query,
            "year": candidate.year,
        },
    )


def _split_numbered_references(text: str) -> list[str]:
    # Bracket markers are unambiguous after NFKC normalization. Bare ``2.`` markers
    # inside titles can instead be version or chapter numbers, so only strip those
    # when they are already at the beginning of an individual block.
    markers = list(_BRACKET_NUMBER_MARKER.finditer(text))
    if len(markers) < 2:
        return [text]
    prefix = text[: markers[0].start()].strip()
    pieces = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        pieces.append(text[marker.start() : end].strip())
    if prefix:
        pieces.insert(0, prefix)
    return [piece for piece in pieces if piece]


def _is_reference_heading(value: str) -> bool:
    normalized = _normalize_whitespace(value).strip(".:：")
    return _REFERENCE_HEADING.fullmatch(normalized) is not None


def _extract_doi(value: str) -> str | None:
    match = _DOI_PATTERN.search(unicodedata.normalize("NFKC", value))
    return normalize_doi(match.group(0)) if match else None


def _extract_year(value: str) -> int | None:
    match = _YEAR_PATTERN.search(unicodedata.normalize("NFKC", value))
    return int(match.group(0)) if match else None


def _coerce_year(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1800 <= value <= 2199:
        return value
    if isinstance(value, str):
        return _extract_year(value)
    return None


def _title_coverage(reference_text: str, candidate_title: str) -> float:
    reference_terms = _matching_terms(reference_text)
    candidate_terms = _matching_terms(candidate_title)
    if not candidate_terms:
        return 0.0
    return len(reference_terms & candidate_terms) / len(candidate_terms)


def _matching_terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    terms = set(re.findall(r"[a-z0-9]+", normalized))
    for sequence in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _canonical_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
