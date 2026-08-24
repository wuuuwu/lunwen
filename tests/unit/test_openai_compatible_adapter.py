from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter
from paper_reviewer.ports.model import Message, ModelRequest, ToolSpec


def _request(
    *, max_output_tokens: int = 321, forced_tool_name: str | None = None
) -> ModelRequest:
    return ModelRequest(
        messages=[Message(role="user", content="Return JSON.")],
        tools=(
            [
                ToolSpec(
                    name=forced_tool_name,
                    description="Probe",
                    parameters={"type": "object", "properties": {}},
                )
            ]
            if forced_tool_name
            else []
        ),
        max_output_tokens=max_output_tokens,
        temperature=0.2,
        forced_tool_name=forced_tool_name,
        trace_id="trace-test",
        idempotency_key="idem-test",
    )


def _adapter_with_response(response: object) -> tuple[OpenAICompatibleAdapter, AsyncMock]:
    adapter = OpenAICompatibleAdapter(api_key="test-key", model="deepseek-chat")
    create = AsyncMock(return_value=response)
    adapter.client.chat.completions.create = create
    return adapter, create


def test_sdk_retries_are_disabled() -> None:
    adapter = OpenAICompatibleAdapter(api_key="test-key", model="deepseek-chat")

    assert adapter.client.max_retries == 0


@pytest.mark.asyncio
async def test_complete_maps_metadata_and_preserves_max_tokens() -> None:
    response = SimpleNamespace(
        id="response-1",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content='{"result":"ok"}',
                    tool_calls=[],
                    reasoning_content="private chain of thought must not be retained",
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=17,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=13),
        ),
    )
    adapter, create = _adapter_with_response(response)

    result = await adapter.complete(_request(max_output_tokens=777))

    assert result.content == '{"result":"ok"}'
    assert result.finish_reason == "stop"
    assert result.reasoning_content_present is True
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 17
    assert result.usage.reasoning_tokens == 13
    assert "private chain of thought" not in result.model_dump_json()
    create.assert_awaited_once()
    assert create.await_args.kwargs["max_tokens"] == 777
    assert create.await_args.kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_complete_defaults_optional_metadata_without_reasoning_text() -> None:
    response = SimpleNamespace(
        id="response-2",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="answer", tool_calls=[]),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3),
    )
    adapter, _ = _adapter_with_response(response)

    result = await adapter.complete(_request())

    assert result.finish_reason is None
    assert result.reasoning_content_present is False
    assert result.usage.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_complete_maps_named_tool_choice() -> None:
    response = SimpleNamespace(
        id="response-3",
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer", tool_calls=[]))],
        usage=None,
    )
    adapter, create = _adapter_with_response(response)

    await adapter.complete(_request(forced_tool_name="probe"))

    assert create.await_args.kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "probe"},
    }
