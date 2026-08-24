from __future__ import annotations

import asyncio
import html
import math
import re
from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit, urlunsplit

from ddgs import DDGS
from pydantic import HttpUrl, TypeAdapter, ValidationError

from paper_reviewer.ports.web_search import WebSearchError, WebSearchResult

MAX_QUERY_LENGTH = 500
MAX_TITLE_LENGTH = 300
MAX_SNIPPET_LENGTH = 2_000
MAX_SOURCE_LENGTH = 120
MAX_URL_LENGTH = 2_048

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


class DdgsWebSearchClient:
    """Keyless public web search backed by the synchronous ``ddgs`` package."""

    def __init__(
        self,
        *,
        backend: str = "auto",
        region: str = "wt-wt",
        safesearch: str = "moderate",
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not backend.strip():
            raise ValueError("backend must not be empty")
        if not region.strip():
            raise ValueError("region must not be empty")
        if safesearch not in {"on", "moderate", "off"}:
            raise ValueError("safesearch must be on, moderate, or off")
        if not math.isfinite(min_interval_seconds) or min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be a finite non-negative number")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        self.backend = backend.strip()
        self.region = region.strip()
        self.safesearch = safesearch
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._request_lock = asyncio.Lock()
        self._last_request_started_at: float | None = None

    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        normalized_query = _normalize_query(query)
        _validate_limit(limit)

        failure: WebSearchError | None = None
        raw_results: list[dict[str, object]] = []
        try:
            async with self._request_lock:
                await self._wait_for_rate_limit()
                self._last_request_started_at = asyncio.get_running_loop().time()
                raw_results = await asyncio.to_thread(
                    self._search_sync,
                    normalized_query,
                    limit,
                )
        except Exception:
            # Deliberately discard the provider exception: ddgs exceptions can echo the
            # query or result URLs and therefore must not cross this adapter boundary.
            failure = WebSearchError("The public web-search provider failed.")
        if failure is not None:
            raise failure

        results: list[WebSearchResult] = []
        seen_urls: set[str] = set()
        for rank, raw_result in enumerate(raw_results, start=1):
            if not isinstance(raw_result, Mapping):
                continue
            mapped = self._map_result(raw_result, rank=rank)
            if mapped is None:
                continue
            normalized_url = str(mapped.url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            results.append(mapped)
            if len(results) >= limit:
                break
        return results

    def _search_sync(self, query: str, limit: int) -> list[dict[str, object]]:
        results = DDGS(timeout=max(1, math.ceil(self.timeout_seconds))).text(
            query,
            backend=self.backend,
            region=self.region,
            safesearch=self.safesearch,
            max_results=limit,
        )
        return cast(list[dict[str, object]], results)

    async def _wait_for_rate_limit(self) -> None:
        if self._last_request_started_at is None or self.min_interval_seconds == 0:
            return
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request_started_at
        delay = self.min_interval_seconds - elapsed
        if delay > 0:
            await asyncio.sleep(delay)

    def _map_result(
        self,
        raw_result: Mapping[str, object],
        *,
        rank: int,
    ) -> WebSearchResult | None:
        raw_url = raw_result.get("href") or raw_result.get("url")
        url = _normalize_http_url(raw_url)
        if url is None:
            return None
        hostname = (urlsplit(str(url)).hostname or "web").removeprefix("www.")
        title = _clean_text(raw_result.get("title"), limit=MAX_TITLE_LENGTH) or hostname
        snippet = _clean_text(
            raw_result.get("body")
            or raw_result.get("snippet")
            or raw_result.get("description"),
            limit=MAX_SNIPPET_LENGTH,
        )
        source = _clean_text(
            raw_result.get("source") or raw_result.get("provider"),
            limit=MAX_SOURCE_LENGTH,
        ) or hostname
        return WebSearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source=source,
            metadata={"backend": self.backend, "rank": rank},
        )


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must not exceed {MAX_QUERY_LENGTH} characters")
    return normalized


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError("limit must be an integer between 1 and 8")


def _normalize_http_url(value: object) -> HttpUrl | None:
    if not isinstance(value, str):
        return None
    candidate = html.unescape(value).strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        default_port = (parts.scheme.lower() == "http" and port == 80) or (
            parts.scheme.lower() == "https" and port == 443
        )
        netloc = host if port is None or default_port else f"{host}:{port}"
        normalized = urlunsplit(
            (
                parts.scheme.lower(),
                netloc,
                parts.path or "/",
                parts.query,
                "",
            )
        )
        return _HTTP_URL_ADAPTER.validate_python(normalized)
    except (ValueError, ValidationError):
        return None


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = html.unescape(value)
    normalized = _CONTROL_CHARACTERS.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"
