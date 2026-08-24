from __future__ import annotations

import httpx

from paper_reviewer.domain.evidence import EvidenceLevel
from paper_reviewer.ports.scholarly_search import ScholarlyWork


class OpenAlexClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def search(self, query: str, *, limit: int = 10) -> list[ScholarlyWork]:
        response = await self.client.get(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": min(limit, 25)},
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        works: list[ScholarlyWork] = []
        for item in results:
            abstract = _decode_abstract(item.get("abstract_inverted_index"))
            primary = item.get("primary_location") or {}
            works.append(
                ScholarlyWork(
                    source="openalex",
                    source_id=str(item.get("id", "")),
                    title=str(item.get("display_name") or "Untitled"),
                    abstract=abstract,
                    doi=item.get("doi"),
                    url=primary.get("landing_page_url") or item.get("id"),
                    year=item.get("publication_year"),
                    cited_by_count=item.get("cited_by_count"),
                    level=EvidenceLevel.ABSTRACT if abstract else EvidenceLevel.METADATA,
                    metadata={"type": item.get("type")},
                )
            )
        return works


def _decode_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    positioned = [(position, word) for word, positions in index.items() for position in positions]
    return " ".join(word for _, word in sorted(positioned))
