from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paper_reviewer.adapters.models.openai_responses import (
    OpenAIResponsesAdapter,
    ResponsesAPIError,
)
from paper_reviewer.ports.model import Message, ModelRequest, ToolCall, ToolSpec


def _request(*, messages: list[Message] | None = None) -> ModelRequest:
    return ModelRequest(
        messages=messages
        or [
            Message(role="system", content="Follow the rubric."),
            Message(role="user", content="Review the paper."),
        ],
        tools=[
            ToolSpec(
                name="lookup",
                description="Look up evidence",
                parameters={"type": "object", "properties": {}},
            )
        ],
        forced_tool_name="lookup",
        max_output_tokens=321,
        temperature=0.7,
        trace_id="trace-test",
        idempotency_key="idem-test",
    )


def _adapter_with_response(
    response: object, *, include_encrypted_reasoning: bool = True
) -> tuple[OpenAIResponsesAdapter, AsyncMock]:
    adapter = OpenAIResponsesAdapter(
        api_key="test-key",
        model="gpt-test",
        include_encrypted_reasoning=include_encrypted_reasoning,
    )
    create = AsyncMock(return_value=response)
    adapter.client.responses.create = create
    return adapter, create


@pytest.mark.asyncio
async def test_complete_uses_responses_wire_format_and_maps_output() -> None:
    output = [
        {
            "id": "rs_1",
            "type": "reasoning",
            "encrypted_content": "opaque-private-value",
            "summary": [],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"query":"source"}',
            "status": "completed",
        },
    ]
    response = SimpleNamespace(
        id="resp_1",
        status="completed",
        output_text="",
        output=output,
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=17,
            output_tokens_details=SimpleNamespace(reasoning_tokens=13),
        ),
        incomplete_details=None,
    )
    adapter, create = _adapter_with_response(response)

    result = await adapter.complete(_request())

    assert result.tool_calls == [
        ToolCall(id="call_1", name="lookup", arguments={"query": "source"})
    ]
    assert result.finish_reason == "tool_calls"
    assert result.response_status == "completed"
    assert result.incomplete_reason is None
    assert result.output_item_types == ["reasoning", "function_call"]
    assert result.plain_text_only is False
    assert result.reasoning_content_present is True
    assert result.response_id is None
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 17
    assert result.usage.reasoning_tokens == 13
    assert result.continuation_items == output
    assert "opaque-private-value" not in result.model_dump_json()

    kwargs = create.await_args.kwargs
    assert kwargs["instructions"] == "Follow the rubric."
    assert kwargs["input"] == [{"role": "user", "content": "Review the paper."}]
    assert kwargs["store"] is False
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["max_output_tokens"] == 321
    assert kwargs["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up evidence",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]
    assert kwargs["tool_choice"] == {"type": "function", "name": "lookup"}
    assert "temperature" not in kwargs
    assert "previous_response_id" not in kwargs
    assert adapter.client.max_retries == 0


@pytest.mark.asyncio
async def test_custom_compatible_request_can_omit_openai_include_field() -> None:
    adapter, create = _adapter_with_response(
        {
            "status": "completed",
            "output": [],
            "output_text": "ok",
            "usage": {},
        },
        include_encrypted_reasoning=False,
    )

    result = await adapter.complete_once(
        ModelRequest(
            messages=[Message(role="user", content="test")],
            max_output_tokens=32,
            trace_id="custom-responses-test",
            idempotency_key="custom-responses-test",
        )
    )

    kwargs = create.await_args.kwargs
    assert kwargs["store"] is False
    assert "include" not in kwargs
    assert "temperature" not in kwargs
    assert "previous_response_id" not in kwargs
    assert adapter.client.max_retries == 0
    assert result.response_status == "completed"
    assert result.output_item_types == []
    assert result.plain_text_only is True


@pytest.mark.asyncio
async def test_continuation_and_tool_output_are_replayed_without_duplication() -> None:
    continuation = [
        {"id": "rs_1", "type": "reasoning", "encrypted_content": "opaque"},
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    response = SimpleNamespace(
        id="resp_2",
        status="completed",
        output_text='{"value":"ok"}',
        output=[
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": '{"value":"ok"}'}],
            }
        ],
        usage=None,
        incomplete_details=None,
    )
    adapter, create = _adapter_with_response(response)
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="user"),
        Message(
            role="assistant",
            content="must not be duplicated",
            tool_calls=[ToolCall(id="call_1", name="lookup", arguments={})],
            continuation_items=continuation,
        ),
        Message(role="tool", tool_call_id="call_1", content='{"answer":42}'),
    ]

    result = await adapter.complete(_request(messages=messages))

    assert result.content == '{"value":"ok"}'
    assert create.await_args.kwargs["input"] == [
        {"role": "user", "content": "user"},
        *continuation,
        {"type": "function_call_output", "call_id": "call_1", "output": '{"answer":42}'},
    ]


@pytest.mark.asyncio
async def test_incomplete_max_tokens_maps_to_length() -> None:
    response = SimpleNamespace(
        id="resp_3",
        status="incomplete",
        output_text="partial",
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "private partial text"}],
            }
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=321),
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    adapter, _ = _adapter_with_response(response)

    result = await adapter.complete(_request())

    assert result.finish_reason == "length"
    assert result.response_status == "incomplete"
    assert result.incomplete_reason == "max_output_tokens"
    assert result.output_item_types == ["message"]
    assert result.plain_text_only is True
    assert "private partial text" not in result.model_dump_json(
        exclude={"content", "continuation_items"}
    )


@pytest.mark.asyncio
async def test_failed_status_raises_structured_error_without_server_message() -> None:
    response = SimpleNamespace(
        id="resp_4",
        status="failed",
        error=SimpleNamespace(code="server_error", message="secret response body"),
    )
    adapter, _ = _adapter_with_response(response)

    with pytest.raises(ResponsesAPIError, match=r"failed \(server_error\)") as caught:
        await adapter.complete(_request())

    assert "secret response body" not in str(caught.value)
    assert caught.value.response_status == "failed"
    assert caught.value.incomplete_reason is None
    assert caught.value.finish_reason is None
    assert caught.value.output_item_types == []
    assert caught.value.plain_text_only is False


@pytest.mark.asyncio
async def test_invalid_function_arguments_are_rejected() -> None:
    response = SimpleNamespace(
        id="resp_5",
        status="completed",
        output_text="",
        output=[
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "not-json",
            }
        ],
        usage=None,
        incomplete_details=None,
    )
    adapter, _ = _adapter_with_response(response)

    with pytest.raises(ValueError, match="invalid tool arguments for lookup"):
        await adapter.complete(_request())
