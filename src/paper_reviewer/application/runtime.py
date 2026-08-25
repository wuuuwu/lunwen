from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paper_reviewer.adapters.models.factory import create_model_adapter
from paper_reviewer.adapters.scholarly.arxiv import ArxivClient
from paper_reviewer.adapters.scholarly.crossref import CrossrefClient
from paper_reviewer.adapters.scholarly.openalex import OpenAlexClient
from paper_reviewer.adapters.search.ddgs import DdgsWebSearchClient
from paper_reviewer.application.unit_of_work import ApplicationUnitOfWork
from paper_reviewer.config import Settings
from paper_reviewer.domain.provider import ProviderSnapshot


@dataclass(frozen=True, slots=True)
class ReviewRuntime:
    """Resources owned by a single start/resume operation."""

    model: Any
    sessions: async_sessionmaker[AsyncSession]
    scholarly_clients: list[Any]
    web_search_client: DdgsWebSearchClient | None


@asynccontextmanager
async def review_runtime(
    *,
    settings: Settings,
    provider_snapshot: ProviderSnapshot,
    api_key: str,
    external_search: bool,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[ReviewRuntime]:
    """Create and deterministically close all resources for one review run."""

    async with AsyncExitStack() as stack:
        if sessions is None:
            unit_of_work = await stack.enter_async_context(
                ApplicationUnitOfWork(settings.database_url)
            )
            active_sessions = unit_of_work.require_sessions()
        else:
            active_sessions = sessions
        model = create_model_adapter(
            provider_snapshot.provider_ref,
            provider_snapshot.model,
            api_key=api_key,
            protocol=provider_snapshot.protocol,
            base_url=provider_snapshot.base_url,
            timeout=settings.request_timeout_seconds,
        )
        stack.push_async_callback(model.close)

        scholarly_clients: list[Any] = []
        web_search_client = None
        if external_search:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=settings.external_timeout_seconds)
            )
            scholarly_clients = [
                OpenAlexClient(http_client),
                CrossrefClient(http_client),
                ArxivClient(http_client),
            ]
            web_search_client = DdgsWebSearchClient(
                backend=settings.web_search_backend,
                region=settings.web_search_region,
                safesearch=settings.web_search_safesearch,
                min_interval_seconds=settings.web_search_min_interval_seconds,
                timeout_seconds=settings.external_timeout_seconds,
            )

        yield ReviewRuntime(
            model=model,
            sessions=active_sessions,
            scholarly_clients=scholarly_clients,
            web_search_client=web_search_client,
        )
