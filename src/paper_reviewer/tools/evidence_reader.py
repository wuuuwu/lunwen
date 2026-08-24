from __future__ import annotations

import re

from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.tools.registry import ToolRegistry


class EvidenceReaderTools:
    def __init__(self, evidence: list[EvidenceItem]) -> None:
        self.evidence = evidence
        self.by_id = {item.evidence_id: item for item in evidence}

    def search_evidence(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        terms = set(_tokens(query))
        ranked: list[tuple[int, EvidenceItem]] = []
        for item in self.evidence:
            haystack = _tokens(f"{item.title} {item.content}")
            score = sum(haystack.count(term) for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [_payload(item) for _, item in ranked[: min(max(limit, 1), 12)]]

    def read_evidence(self, evidence_ids: list[str]) -> list[dict[str, object]]:
        if len(evidence_ids) > 12:
            raise ValueError("at most 12 evidence items can be read at once")
        return [
            _payload(item) for identifier in evidence_ids if (item := self.by_id.get(identifier))
        ]


def register_evidence_tools(registry: ToolRegistry, tools: EvidenceReaderTools) -> None:
    registry.register(
        name="search_evidence",
        description="Search previously collected external scholarly evidence.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=tools.search_evidence,
    )
    registry.register(
        name="read_evidence",
        description="Read scholarly evidence items by stable evidence id.",
        parameters={
            "type": "object",
            "properties": {
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 12,
                }
            },
            "required": ["evidence_ids"],
            "additionalProperties": False,
        },
        handler=tools.read_evidence,
    )


def _payload(item: EvidenceItem) -> dict[str, object]:
    return {
        "evidence_id": item.evidence_id,
        "title": item.title,
        "content": item.content,
        "source": item.source_name,
        "level": item.level.value,
        "doi": item.doi,
        "url": str(item.url) if item.url else None,
        "metadata": item.metadata,
    }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]", text.lower())
