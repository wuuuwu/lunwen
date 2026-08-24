from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from paper_reviewer.ports.model import (
    Message,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)


class OpenAICompatibleAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120,
    ) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    @retry(
        retry=retry_if_exception_type(
            (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages = [_message_payload(message) for message in request.messages]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid tool arguments for {call.function.name}") from error
            if not isinstance(arguments, dict):
                raise ValueError(f"tool arguments for {call.function.name} must be an object")
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))
        usage = response.usage
        return ModelResponse(
            # Only expose the model's final answer.  Some compatible providers
            # attach a reasoning_content field to the message; detect its
            # presence for diagnostics without ever copying its value.
            content=message.content,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                reasoning_tokens=_reasoning_tokens(usage),
            ),
            response_id=response.id,
            finish_reason=_optional_value(choice, "finish_reason"),
            reasoning_content_present=_has_optional_field(message, "reasoning_content"),
        )

    async def close(self) -> None:
        await self.client.close()


def _message_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _optional_value(value: Any, field: str) -> Any:
    """Read an optional SDK field without assuming a concrete response type."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(field)
    return getattr(value, field, None)


def _has_optional_field(value: Any, field: str) -> bool:
    """Return whether an optional SDK field is present, without reading it."""

    if value is None:
        return False
    if isinstance(value, Mapping):
        return field in value
    try:
        return hasattr(value, field)
    except Exception:
        # A third-party SDK object may implement dynamic attributes.  Presence
        # metadata must never make an otherwise valid model response fail.
        return False


def _reasoning_tokens(usage: Any) -> int:
    """Extract usage reasoning-token metadata, defaulting safely to zero."""

    details = _optional_value(usage, "completion_tokens_details")
    tokens = _optional_value(details, "reasoning_tokens")
    return tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
