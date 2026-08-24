from __future__ import annotations

import os

from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter
from paper_reviewer.adapters.models.openai_responses import OpenAIResponsesAdapter
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
    normalized = provider.lower()
    resolved_protocol = protocol
    if resolved_protocol is None:
        resolved_protocol = (
            ModelApiProtocol.RESPONSES
            if normalized == "openai_responses"
            else ModelApiProtocol.CHAT_COMPLETIONS
        )

    if normalized in {"openai", "openai_responses"}:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OPENAI_API_KEY is not configured")
        resolved_base_url = base_url
    if normalized == "deepseek":
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not resolved_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        resolved_base_url = base_url or "https://api.deepseek.com"
    elif normalized.startswith("custom:"):
        if not api_key:
            raise ValueError("custom provider API Key is not configured")
        if protocol is None or base_url is None:
            raise ValueError("custom provider requires a protocol and Base URL snapshot")
        resolved_key = api_key
        resolved_base_url = base_url
    elif normalized not in {"openai", "openai_responses"}:
        raise ValueError(f"unsupported model provider: {provider}")

    adapter_type = (
        OpenAIResponsesAdapter
        if resolved_protocol is ModelApiProtocol.RESPONSES
        else OpenAICompatibleAdapter
    )
    if resolved_key is None:  # pragma: no cover - guarded by provider branches above
        raise ValueError("provider API Key is not configured")
    if adapter_type is OpenAIResponsesAdapter:
        return adapter_type(
            api_key=resolved_key,
            model=model,
            base_url=resolved_base_url,
            timeout=timeout,
            # Third-party Responses-compatible APIs do not consistently
            # accept OpenAI's optional ``include`` field. Their returned
            # output items are still replayed when present.
            include_encrypted_reasoning=not normalized.startswith("custom:"),
        )
    return adapter_type(
        api_key=resolved_key,
        model=model,
        base_url=resolved_base_url,
        timeout=timeout,
    )
