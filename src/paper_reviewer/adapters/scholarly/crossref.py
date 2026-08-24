from __future__ import annotations

import re

import httpx

from paper_reviewer.domain.evidence import EvidenceLevel
from paper_reviewer.ports.scholarly_search import ScholarlyWork


class CrossrefClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def search(self, query: str, *, limit: int = 10) -> list[ScholarlyWork]:
        response = await self.client.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": query, "rows": min(limit, 25)},
        )
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
        works: list[ScholarlyWork] = []
        for item in items:
            title_values = item.get("title") or ["Untitled"]
            abstract = item.get("abstract")
            if abstract:
                abstract = re.sub(r"<[^>]+>", " ", abstract)
                abstract = re.sub(r"\s+", " ", abstract).strip()
            year_parts = (item.get("published") or {}).get("date-parts") or []
            year = year_parts[0][0] if year_parts and year_parts[0] else None
            works.append(
                ScholarlyWork(
                    source="crossref",
                    source_id=str(item.get("DOI", "")),
                    title=str(title_values[0]),
                    abstract=abstract,
                    doi=item.get("DOI"),
                    url=item.get("URL"),
                    year=year,
                    level=EvidenceLevel.ABSTRACT if abstract else EvidenceLevel.METADATA,
                    metadata={"publisher": item.get("publisher")},
                )
            )
        return works
