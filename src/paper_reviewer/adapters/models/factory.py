from __future__ import annotations

import os

from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter


def create_model_adapter(
    provider: str, model: str, *, timeout: float = 120, api_key: str | None = None
) -> OpenAICompatibleAdapter:
    normalized = provider.lower()
    if normalized == "openai":
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("OPENAI_API_KEY is not configured")
        return OpenAICompatibleAdapter(api_key=resolved_key, model=model, timeout=timeout)
    if normalized == "deepseek":
        resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not resolved_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        return OpenAICompatibleAdapter(
            api_key=resolved_key,
            model=model,
            base_url="https://api.deepseek.com",
            timeout=timeout,
        )
    raise ValueError(f"unsupported model provider: {provider}")
