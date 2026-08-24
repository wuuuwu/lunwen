from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx
from pydantic import HttpUrl

from paper_reviewer.domain.evidence import EvidenceLevel
from paper_reviewer.ports.scholarly_search import ScholarlyWork


class ArxivClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def search(self, query: str, *, limit: int = 10) -> list[ScholarlyWork]:
        response = await self.client.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": min(limit, 25)},
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        namespace = {"a": "http://www.w3.org/2005/Atom"}
        works: list[ScholarlyWork] = []
        for entry in root.findall("a:entry", namespace):
            identifier = _text(entry, "a:id", namespace)
            title = " ".join((_text(entry, "a:title", namespace) or "Untitled").split())
            abstract = " ".join((_text(entry, "a:summary", namespace) or "").split()) or None
            published = _text(entry, "a:published", namespace)
            works.append(
                ScholarlyWork(
                    source="arxiv",
                    source_id=identifier or title,
                    title=title,
                    abstract=abstract,
                    url=HttpUrl(identifier) if identifier else None,
                    year=int(published[:4]) if published else None,
                    level=EvidenceLevel.ABSTRACT,
                )
            )
        return works


def _text(node: ET.Element, path: str, namespace: dict[str, str]) -> str | None:
    found = node.find(path, namespace)
    return found.text if found is not None else None
