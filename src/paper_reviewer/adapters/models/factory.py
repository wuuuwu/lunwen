from __future__ import annotations

from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter
from paper_reviewer.adapters.models.openai_responses import OpenAIResponsesAdapter
from paper_reviewer.adapters.models.resolved_config import (
    ResolvedAdapterConfig,
    resolve_adapter_config,
)
from paper_reviewer.domain.provider import ModelApiProtocol


def create_model_adapter(
    provider: str,
    model: str,
    *,
    timeout: float = 120,
    api_key: str | None = None,
    protocol: ModelApiProtocol | None = None,
    base_url: str | None = None,
) -> OpenAICompatibleAdapter | OpenAIResponsesAdapter:
    config = resolve_adapter_config(
        provider,
        model,
        timeout=timeout,
        api_key=api_key,
        protocol=protocol,
        base_url=base_url,
    )
    return _instantiate_adapter(config)


def _instantiate_adapter(
    config: ResolvedAdapterConfig,
) -> OpenAICompatibleAdapter | OpenAIResponsesAdapter:
    if config.protocol is ModelApiProtocol.RESPONSES:
        return OpenAIResponsesAdapter(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url,
            timeout=config.timeout,
            include_encrypted_reasoning=config.include_encrypted_reasoning,
        )
    return OpenAICompatibleAdapter(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        timeout=config.timeout,
    )
