from __future__ import annotations

import asyncio
import hashlib
import re

from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind
from paper_reviewer.ports.scholarly_search import ScholarlySearchPort, ScholarlyWork


async def build_external_evidence(
    *,
    run_id: str,
    query: str,
    clients: list[ScholarlySearchPort],
    per_source_limit: int = 5,
) -> tuple[list[EvidenceItem], list[str]]:
    results = await asyncio.gather(
        *(client.search(query, limit=per_source_limit) for client in clients),
        return_exceptions=True,
    )
    warnings: list[str] = []
    works: list[ScholarlyWork] = []
    for client, result in zip(clients, results, strict=True):
        if isinstance(result, BaseException):
            warnings.append(f"{type(client).__name__}: {result}")
        else:
            works.extend(result)
    deduplicated: dict[str, ScholarlyWork] = {}
    for work in works:
        key = (work.doi or _normalize_title(work.title)).lower()
        existing = deduplicated.get(key)
        if existing is None or (existing.abstract is None and work.abstract):
            deduplicated[key] = work
    items = [_to_evidence(run_id, work) for work in deduplicated.values()]
    return items, warnings


def _to_evidence(run_id: str, work: ScholarlyWork) -> EvidenceItem:
    seed = f"{work.source}|{work.source_id}|{work.doi or ''}|{work.title}"
    evidence_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return EvidenceItem(
        evidence_id=evidence_id,
        run_id=run_id,
        kind=EvidenceKind.EXTERNAL,
        title=work.title,
        content=work.abstract or "Metadata-only scholarly result.",
        source_name=work.source,
        level=work.level,
        doi=work.doi,
        url=work.url,
        metadata={
            "year": work.year,
            "cited_by_count": work.cited_by_count,
            **work.metadata,
        },
    )


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())
