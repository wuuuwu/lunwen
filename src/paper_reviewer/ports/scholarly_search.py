from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field, HttpUrl

from paper_reviewer.domain.evidence import EvidenceLevel


class ScholarlyWork(BaseModel):
    source: str
    source_id: str
    title: str
    abstract: str | None = None
    doi: str | None = None
    url: HttpUrl | None = None
    year: int | None = None
    cited_by_count: int | None = None
    level: EvidenceLevel = EvidenceLevel.METADATA
    metadata: dict[str, object] = Field(default_factory=dict)


class ScholarlySearchPort(Protocol):
    async def search(self, query: str, *, limit: int = 10) -> list[ScholarlyWork]: ...
