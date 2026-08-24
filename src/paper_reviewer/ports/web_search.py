from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field, HttpUrl


class WebSearchError(RuntimeError):
    """A sanitized failure reported by a web-search provider."""


class WebSearchResult(BaseModel):
    title: str
    url: HttpUrl
    snippet: str
    source: str
    metadata: dict[str, object] = Field(default_factory=dict)


class WebSearchPort(Protocol):
    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]: ...
