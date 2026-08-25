from __future__ import annotations

import os
from dataclasses import dataclass, field

from paper_reviewer.domain.provider import ModelApiProtocol


@dataclass(frozen=True, slots=True)
class ResolvedAdapterConfig:
    """Connection settings resolved for one model adapter instance.

    The API key is deliberately kept in this short-lived value object only.
    It is never part of a provider snapshot or any persisted application
    model.  Resolving the configuration is separate from constructing the
    transport adapter so provider policy remains easy to test without
    creating an SDK client.
    """

    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None
    timeout: float
    protocol: ModelApiProtocol
    include_encrypted_reasoning: bool


def resolve_adapter_config(
    provider: str,
    model: str,
    *,
    timeout: float = 120,
    api_key: str | None = None,
    protocol: ModelApiProtocol | None = None,
    base_url: str | None = None,
) -> ResolvedAdapterConfig:
    """Resolve provider policy without constructing an SDK client.

    Built-in providers retain their environment-variable fallback. Custom
    providers intentionally do not fall back to an environment variable:
    their key and endpoint must come from the task's provider snapshot.
    """

    normalized = provider.lower()
    resolved_protocol = protocol if protocol is not None else _default_protocol(normalized)
    resolved_key, resolved_base_url = _resolve_connection(
        normalized,
        api_key=api_key,
        base_url=base_url,
        protocol=protocol,
    )
    return ResolvedAdapterConfig(
        provider=normalized,
        model=model,
        api_key=resolved_key,
        base_url=resolved_base_url,
        timeout=timeout,
        protocol=resolved_protocol,
        # Third-party Responses-compatible APIs do not consistently accept
        # OpenAI's optional encrypted-reasoning include field.
        include_encrypted_reasoning=(
            not normalized.startswith("custom:")
        ),
    )


def _default_protocol(provider: str) -> ModelApiProtocol:
    if provider == "openai_responses":
        return ModelApiProtocol.RESPONSES
    return ModelApiProtocol.CHAT_COMPLETIONS


def _resolve_connection(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    protocol: ModelApiProtocol | None,
) -> tuple[str, str | None]:
    if provider in {"openai", "openai_responses"}:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OPENAI_API_KEY is not configured")
        return resolved_key, base_url

    if provider == "deepseek":
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not resolved_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        return resolved_key, base_url or "https://api.deepseek.com"

    if provider.startswith("custom:"):
        if not api_key:
            raise ValueError("custom provider API Key is not configured")
        if protocol is None or base_url is None:
            # Keeping the endpoint and protocol check here makes it impossible
            # to accidentally instantiate a custom provider without its task
            # snapshot.
            raise ValueError("custom provider requires a protocol and Base URL snapshot")
        return api_key, base_url

    raise ValueError(f"unsupported model provider: {provider}")
