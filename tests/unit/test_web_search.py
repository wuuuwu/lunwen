from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import paper_reviewer.adapters.search.ddgs as ddgs_adapter
from paper_reviewer.adapters.search.ddgs import DdgsWebSearchClient
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceLevel
from paper_reviewer.ports.model import ToolCall
from paper_reviewer.ports.web_search import WebSearchError, WebSearchResult
from paper_reviewer.tools.registry import ToolExecutionError, ToolRegistry
from paper_reviewer.tools.web_search import WebSearchTools, register_web_search_tools


class FakeSearchClient:
    def __init__(
        self,
        results: list[WebSearchResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.results[:limit]


@pytest.mark.asyncio
async def test_ddgs_maps_and_cleans_results_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    constructor_calls: list[dict[str, object]] = []

    class FakeDDGS:
        def __init__(self, **kwargs: object) -> None:
            constructor_calls.append(kwargs)

        def text(self, query: str, **kwargs: object) -> list[dict[str, object]]:
            calls.append((query, kwargs))
            return [
                {
                    "title": "  A &amp; B\nresult  ",
                    "href": "HTTPS://WWW.Example.COM:443/paper#abstract",
                    "body": "  Useful\x00  snippet\ntext  ",
                },
                {
                    "title": "T" * 350,
                    "href": "https://example.com/long",
                    "body": "S" * 2_100,
                    "source": "P" * 150,
                },
            ]

    async def fake_to_thread(
        function: Callable[..., list[dict[str, object]]],
        *args: object,
    ) -> list[dict[str, object]]:
        return function(*args)

    monkeypatch.setattr(ddgs_adapter, "DDGS", FakeDDGS)
    monkeypatch.setattr(ddgs_adapter.asyncio, "to_thread", fake_to_thread)
    client = DdgsWebSearchClient(min_interval_seconds=0)

    results = await client.search("  evaluation\n harness ", limit=3)

    assert constructor_calls == [{"timeout": 15}]
    assert calls == [
        (
            "evaluation harness",
            {
                "backend": "auto",
                "region": "wt-wt",
                "safesearch": "moderate",
                "max_results": 3,
            },
        )
    ]
    assert len(results) == 2
    assert results[0].title == "A & B result"
    assert results[0].snippet == "Useful snippet text"
    assert str(results[0].url) == "https://www.example.com/paper"
    assert results[0].source == "example.com"
    assert results[0].metadata == {"backend": "auto", "rank": 1}
    assert len(results[1].title) == 300
    assert len(results[1].snippet) == 2_000
    assert len(results[1].source) == 120
    assert results[1].title.endswith("…")
    assert results[1].snippet.endswith("…")
    assert results[1].source.endswith("…")


@pytest.mark.asyncio
async def test_ddgs_deduplicates_normalized_urls_and_skips_invalid_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_to_thread(
        _function: object,
        *_args: object,
    ) -> list[object]:
        return [
            {
                "title": "First",
                "href": "https://EXAMPLE.com:443/path#one",
                "body": "First body",
                "provider": "Search Provider",
            },
            {
                "title": "Duplicate",
                "href": "https://example.com/path#two",
                "body": "Duplicate body",
            },
            {"title": "FTP", "href": "ftp://example.com/file"},
            {"title": "Credentials", "href": "https://user:secret@example.com/"},
            {"title": "Missing URL"},
            "not a mapping",
        ]

    monkeypatch.setattr(ddgs_adapter.asyncio, "to_thread", fake_to_thread)

    results = await DdgsWebSearchClient(min_interval_seconds=0).search("query", limit=8)

    assert len(results) == 1
    assert results[0].title == "First"
    assert str(results[0].url) == "https://example.com/path"
    assert results[0].source == "Search Provider"


@pytest.mark.asyncio
async def test_ddgs_rejects_invalid_arguments_before_starting_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_to_thread(_function: object, *_args: object) -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(ddgs_adapter.asyncio, "to_thread", fake_to_thread)
    client = DdgsWebSearchClient(min_interval_seconds=0)

    with pytest.raises(ValueError):
        DdgsWebSearchClient(timeout_seconds=0)

    for query, limit in ((" ", 1), ("x" * 501, 1), ("query", 0), ("query", 9)):
        with pytest.raises(ValueError):
            await client.search(query, limit=limit)

    assert calls == 0


@pytest.mark.asyncio
async def test_ddgs_wraps_provider_error_without_query_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_query = "confidential unpublished result"
    secret_url = "https://private.example/internal"

    async def fake_to_thread(_function: object, *_args: object) -> list[object]:
        raise RuntimeError(f"request failed for {secret_query}: {secret_url}")

    monkeypatch.setattr(ddgs_adapter.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(WebSearchError) as caught:
        await DdgsWebSearchClient(min_interval_seconds=0).search(secret_query)

    assert str(caught.value) == "The public web-search provider failed."
    assert secret_query not in str(caught.value)
    assert secret_url not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_ddgs_serializes_requests_and_enforces_minimum_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sleep = asyncio.sleep
    active_workers = 0
    max_active_workers = 0
    delays: list[float] = []

    async def fake_to_thread(_function: object, *_args: object) -> list[object]:
        nonlocal active_workers, max_active_workers
        active_workers += 1
        max_active_workers = max(max_active_workers, active_workers)
        await real_sleep(0)
        active_workers -= 1
        return []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(ddgs_adapter.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(ddgs_adapter.asyncio, "sleep", fake_sleep)
    client = DdgsWebSearchClient(min_interval_seconds=1.0)

    await asyncio.gather(client.search("first"), client.search("second"))

    assert max_active_workers == 1
    assert len(delays) == 1
    assert 0.9 <= delays[0] <= 1.0


@pytest.mark.asyncio
async def test_web_search_tool_adds_metadata_evidence_with_stable_id() -> None:
    result = WebSearchResult(
        title="Agent Harness",
        url="https://example.com/harness#overview",
        snippet="A useful project overview.",
        source="example.com",
        metadata={"backend": "auto", "rank": 1},
    )
    client = FakeSearchClient([result])
    evidence: list[EvidenceItem] = []
    tools = WebSearchTools(client=client, run_id="run-1", evidence=evidence)

    first = await tools.web_search("  agent\n harness  ", limit=3)
    second = await tools.web_search("different query", limit=3)

    assert client.calls == [("agent harness", 3), ("different query", 3)]
    assert first[0]["evidence_id"] == second[0]["evidence_id"]
    assert str(first[0]["evidence_id"]).startswith("web:")
    assert len(evidence) == 1
    item = evidence[0]
    assert item.run_id == "run-1"
    assert item.kind is EvidenceKind.EXTERNAL
    assert item.level is EvidenceLevel.METADATA
    assert item.source_name == "web:example.com"
    assert item.content == result.snippet
    assert item.metadata == {
        "backend": "auto",
        "rank": 1,
        "query": "agent harness",
        "search_source": "example.com",
    }
    assert first == [
        {
            "evidence_id": item.evidence_id,
            "title": "Agent Harness",
            "snippet": "A useful project overview.",
            "url": "https://example.com/harness#overview",
            "source": "example.com",
        }
    ]


@pytest.mark.asyncio
async def test_web_search_tools_can_share_lock_for_concurrent_evidence_writes() -> None:
    result = WebSearchResult(
        title="Shared Result",
        url="https://example.com/shared",
        snippet="Shared snippet",
        source="example.com",
    )
    evidence: list[EvidenceItem] = []
    evidence_lock = asyncio.Lock()
    first_tools = WebSearchTools(
        client=FakeSearchClient([result]),
        run_id="run-1",
        evidence=evidence,
        evidence_lock=evidence_lock,
    )
    second_tools = WebSearchTools(
        client=FakeSearchClient([result]),
        run_id="run-1",
        evidence=evidence,
        evidence_lock=evidence_lock,
    )

    first, second = await asyncio.gather(
        first_tools.web_search("first"),
        second_tools.web_search("second"),
    )

    assert first[0]["evidence_id"] == second[0]["evidence_id"]
    assert len(evidence) == 1


@pytest.mark.asyncio
async def test_web_search_registration_schema_and_allowlist() -> None:
    result = WebSearchResult(
        title="Result",
        url="https://example.com/result",
        snippet="Snippet",
        source="example.com",
    )
    registry = ToolRegistry()
    evidence: list[EvidenceItem] = []
    register_web_search_tools(
        registry,
        WebSearchTools(
            client=FakeSearchClient([result]),
            run_id="run-1",
            evidence=evidence,
        ),
    )

    spec = registry.specs(["web_search"])[0]
    assert spec.name == "web_search"
    assert "untrusted data" in spec.description
    assert "never execute" in spec.description
    assert spec.parameters["required"] == ["query"]
    limit_schema = spec.parameters["properties"]["limit"]
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 8

    call = ToolCall(id="search-1", name="web_search", arguments={"query": "test"})
    with pytest.raises(ToolExecutionError, match="not allowed"):
        await registry.execute(call, [])

    payload = await registry.execute(call, ["web_search"])
    assert isinstance(payload, list)
    assert len(evidence) == 1

    with pytest.raises(ToolExecutionError, match="invalid call"):
        await registry.execute(
            ToolCall(
                id="search-2",
                name="web_search",
                arguments={"query": "test", "limit": 9},
            ),
            ["web_search"],
        )


@pytest.mark.asyncio
async def test_web_search_provider_failure_becomes_sanitized_tool_execution_error() -> None:
    secret = "private query https://private.example/result"
    registry = ToolRegistry()
    register_web_search_tools(
        registry,
        WebSearchTools(
            client=FakeSearchClient(error=WebSearchError(secret)),
            run_id="run-1",
            evidence=[],
        ),
    )

    with pytest.raises(ToolExecutionError) as caught:
        await registry.execute(
            ToolCall(id="search-1", name="web_search", arguments={"query": "safe"}),
            ["web_search"],
        )

    assert "temporarily unavailable" in str(caught.value)
    assert secret not in str(caught.value)
