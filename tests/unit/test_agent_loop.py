from __future__ import annotations

from collections import deque

import pytest
from pydantic import BaseModel

from paper_reviewer.agents.loop import (
    AgentBudget,
    AgentBudgetExceeded,
    InvalidAgentOutput,
    run_bounded_agent,
)
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall, Usage
from paper_reviewer.tools.registry import ToolExecutionError, ToolRegistry


class Output(BaseModel):
    value: str


class FakeModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.popleft()


@pytest.mark.asyncio
async def test_agent_without_tools_uses_one_regular_final_call() -> None:
    model = FakeModel([ModelResponse(content='{"value":"ok"}')])

    result = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=3, max_tool_calls=0, max_repairs=2),
    )

    assert result.value == "ok"
    assert len(model.requests) == 1
    assert model.requests[0].tools == []


@pytest.mark.asyncio
async def test_semantic_output_validator_uses_the_existing_repair_loop() -> None:
    model = FakeModel(
        [
            ModelResponse(content='{"value":"invented"}'),
            ModelResponse(content='{"value":"source-id"}'),
        ]
    )

    def require_source_id(output: Output) -> None:
        if output.value != "source-id":
            raise ValueError("value must be an exact source ID")

    result = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=1, max_tool_calls=0, max_repairs=1),
        output_validator=require_source_id,
    )

    assert result.value == "source-id"
    assert "value must be an exact source ID" in (
        model.requests[1].messages[-1].content or ""
    )


@pytest.mark.asyncio
async def test_length_exhaustion_grows_output_limit_and_uses_stable_context() -> None:
    first_truncated = '{"value":"' + ("a" * 5000)
    second_truncated = '{"value":"' + ("b" * 9000)
    model = FakeModel(
        [
            ModelResponse(
                content=first_truncated,
                finish_reason="length",
                usage=Usage(output_tokens=4096),
            ),
            ModelResponse(
                content=second_truncated,
                finish_reason="length",
                usage=Usage(output_tokens=8192),
            ),
            ModelResponse(content='{"value":"complete"}', finish_reason="stop"),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(
            max_model_turns=1,
            max_tool_calls=0,
            max_repairs=2,
            max_output_tokens=4096,
            max_output_tokens_limit=16384,
        ),
    )

    assert result.value == "complete"
    assert [request.max_output_tokens for request in model.requests] == [4096, 8192, 16384]
    assert all(
        first_truncated not in (message.content or "")
        and second_truncated not in (message.content or "")
        for request in model.requests[1:]
        for message in request.messages
    )


@pytest.mark.asyncio
async def test_length_exhaustion_at_output_limit_has_explicit_error() -> None:
    model = FakeModel(
        [
            ModelResponse(finish_reason="length", usage=Usage(output_tokens=4096)),
            ModelResponse(finish_reason="length", usage=Usage(output_tokens=8192)),
        ]
    )

    with pytest.raises(
        InvalidAgentOutput,
        match=r"exhausted the maximum output token limit \(8192\).*trace_id=trace",
    ):
        await run_bounded_agent(
            model=model,
            registry=ToolRegistry(),
            allowlist=[],
            system_prompt="system",
            user_prompt="user",
            output_type=Output,
            trace_id="trace",
            budget=AgentBudget(
                max_model_turns=1,
                max_tool_calls=0,
                max_repairs=2,
                max_output_tokens=4096,
                max_output_tokens_limit=8192,
            ),
        )

    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_later_schema_error_does_not_hide_earlier_truncation() -> None:
    model = FakeModel(
        [
            ModelResponse(
                content='{"value":"truncated',
                finish_reason="length",
                usage=Usage(output_tokens=4096),
            ),
            ModelResponse(content='{"wrong":"shape"}', finish_reason="stop"),
        ]
    )

    with pytest.raises(InvalidAgentOutput) as caught:
        await run_bounded_agent(
            model=model,
            registry=ToolRegistry(),
            allowlist=[],
            system_prompt="system",
            user_prompt="user",
            output_type=Output,
            trace_id="trace",
            budget=AgentBudget(
                max_model_turns=1,
                max_tool_calls=0,
                max_repairs=1,
                max_output_tokens=4096,
                max_output_tokens_limit=16384,
            ),
        )

    message = str(caught.value)
    assert "truncated at the requested token limit (4096)" in message
    assert "subsequent repair failed:" in message
    assert "trace_id=trace" in message


@pytest.mark.asyncio
async def test_trace_records_reasoning_presence_without_reasoning_content() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    model = FakeModel(
        [
            ModelResponse(
                content='{"value":"ok"}',
                finish_reason="stop",
                reasoning_content_present=True,
                usage=Usage(output_tokens=12, reasoning_tokens=7),
                continuation_items=[
                    {"type": "reasoning", "encrypted_content": "opaque-private-value"}
                ],
            )
        ]
    )

    await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=1, max_tool_calls=0, max_repairs=0),
        event_sink=lambda name, payload: events.append((name, payload)),
    )

    completed = next(payload for name, payload in events if name == "model_call_completed")
    assert completed["finish_reason"] == "stop"
    assert completed["reasoning_tokens"] == 7
    assert completed["reasoning_content_present"] is True
    assert "reasoning_content" not in completed
    assert "opaque-private-value" not in str(events)


@pytest.mark.asyncio
async def test_empty_response_is_not_added_as_an_assistant_message() -> None:
    model = FakeModel(
        [
            ModelResponse(content=""),
            ModelResponse(content='{"value":"repaired"}'),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=1, max_tool_calls=0, max_repairs=1),
    )

    assert result.value == "repaired"
    assert not any(
        message.role == "assistant" and not message.content and not message.tool_calls
        for message in model.requests[1].messages
    )


@pytest.mark.asyncio
async def test_responses_continuation_is_forwarded_but_not_serialized() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda: {"answer": 42},
    )
    continuation = [
        {"type": "reasoning", "encrypted_content": "opaque-private-value"},
        {
            "type": "function_call",
            "call_id": "1",
            "name": "lookup",
            "arguments": "{}",
        },
    ]
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="lookup", arguments={})],
                continuation_items=continuation,
            ),
            ModelResponse(content='{"value":"ok"}'),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=["lookup"],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=2, max_tool_calls=1, max_repairs=0),
    )

    assert result.value == "ok"
    assistant = next(
        message for message in model.requests[1].messages if message.role == "assistant"
    )
    assert assistant.continuation_items == continuation
    assert "opaque-private-value" not in assistant.model_dump_json()
    assert "continuation_items" not in assistant.model_dump()


@pytest.mark.asyncio
async def test_invalid_final_continuation_is_excluded_from_repair_context() -> None:
    model = FakeModel(
        [
            ModelResponse(
                content='{"wrong":"shape"}',
                continuation_items=[{"type": "reasoning", "encrypted_content": "discard-me"}],
            ),
            ModelResponse(content='{"value":"repaired"}'),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=1, max_tool_calls=0, max_repairs=1),
    )

    assert result.value == "repaired"
    assert not any(message.continuation_items for message in model.requests[1].messages)


@pytest.mark.asyncio
async def test_agent_executes_allowed_tool_then_returns_valid_json() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda: {"answer": 42},
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="lookup", arguments={})]),
            ModelResponse(content='{"value":"ok"}'),
        ]
    )
    result = await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=["lookup"],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=2, max_tool_calls=1, max_repairs=0),
    )
    assert result.value == "ok"


@pytest.mark.asyncio
async def test_agent_enforces_tool_budget() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}},
        handler=lambda: {},
    )
    model = FakeModel([ModelResponse(tool_calls=[ToolCall(id="1", name="lookup", arguments={})])])
    with pytest.raises(AgentBudgetExceeded):
        await run_bounded_agent(
            model=model,
            registry=registry,
            allowlist=["lookup"],
            system_prompt="system",
            user_prompt="user",
            output_type=Output,
            trace_id="trace",
            budget=AgentBudget(max_model_turns=2, max_tool_calls=0, max_repairs=0),
        )


@pytest.mark.asyncio
async def test_agent_reserves_last_model_turn_for_final_json() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda: {"answer": 42},
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="lookup", arguments={})]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="lookup", arguments={})]),
            ModelResponse(content='{"value":"ok"}'),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=["lookup"],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=3, max_tool_calls=2, max_repairs=0),
    )

    assert result.value == "ok"
    assert [request.tools for request in model.requests[:2]] == [
        registry.specs(["lookup"]),
        registry.specs(["lookup"]),
    ]
    assert model.requests[-1].tools == []
    assert "Tool collection is complete" in (model.requests[-1].messages[-1].content or "")


@pytest.mark.asyncio
async def test_repairs_are_extra_no_tool_turns() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda: {"answer": 42},
    )
    model = FakeModel(
        [
            ModelResponse(content='{"wrong":"shape"}'),
            ModelResponse(content='{"value":"repaired"}'),
        ]
    )

    result = await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=["lookup"],
        system_prompt="system",
        user_prompt="user",
        output_type=Output,
        trace_id="trace",
        budget=AgentBudget(max_model_turns=1, max_tool_calls=0, max_repairs=1),
    )

    assert result.value == "repaired"
    assert [request.tools for request in model.requests] == [[], []]
    assert "Validation error" in (model.requests[-1].messages[-1].content or "")


@pytest.mark.asyncio
async def test_tool_collection_exhaustion_has_traceable_error() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda: {"answer": 42},
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="lookup", arguments={})]),
            ModelResponse(tool_calls=[ToolCall(id="2", name="lookup", arguments={})]),
            ModelResponse(),
        ]
    )

    with pytest.raises(
        InvalidAgentOutput,
        match=r"tool collection turns exhausted without a final JSON response.*trace_id=trace",
    ):
        await run_bounded_agent(
            model=model,
            registry=registry,
            allowlist=["lookup"],
            system_prompt="system",
            user_prompt="user",
            output_type=Output,
            trace_id="trace",
            budget=AgentBudget(max_model_turns=3, max_tool_calls=2, max_repairs=0),
        )


@pytest.mark.asyncio
async def test_tool_arguments_are_validated_against_json_schema() -> None:
    registry = ToolRegistry()
    registry.register(
        name="lookup",
        description="Lookup",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lambda query: {"query": query},
    )
    with pytest.raises(ToolExecutionError, match="invalid call"):
        await registry.execute(
            ToolCall(id="bad", name="lookup", arguments={"unexpected": True}),
            ["lookup"],
        )
