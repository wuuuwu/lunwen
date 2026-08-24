from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from paper_reviewer.ports.model import Message, ModelPort, ModelRequest, ModelResponse, ToolSpec
from paper_reviewer.tools.registry import ToolRegistry


class AgentBudgetExceeded(RuntimeError):
    pass


class InvalidAgentOutput(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentBudget:
    max_model_turns: int = 3
    max_tool_calls: int = 8
    max_repairs: int = 2
    max_output_tokens: int = 4096
    max_output_tokens_limit: int = 4096


EventSink = Callable[[str, dict[str, object]], None]


async def run_bounded_agent[OutputT: BaseModel](
    *,
    model: ModelPort,
    registry: ToolRegistry,
    allowlist: list[str],
    system_prompt: str,
    user_prompt: str,
    output_type: type[OutputT],
    trace_id: str,
    budget: AgentBudget,
    event_sink: EventSink | None = None,
    output_validator: Callable[[OutputT], None] | None = None,
) -> OutputT:
    if budget.max_model_turns < 1:
        raise AgentBudgetExceeded("model turn budget must be at least one")
    if budget.max_output_tokens < 1:
        raise AgentBudgetExceeded("output token budget must be at least one")
    if budget.max_output_tokens_limit < budget.max_output_tokens:
        raise AgentBudgetExceeded("output token limit must not be below the initial budget")

    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=user_prompt),
    ]
    allowed_tool_specs = registry.specs(allowlist)
    tool_count = 0
    repair_count = 0
    last_error: str | None = None
    last_truncation_error: str | None = None

    async def complete(
        *, phase: str, turn: int | str, tools: list[ToolSpec], max_output_tokens: int
    ) -> ModelResponse:
        if event_sink:
            event_sink(
                "model_call_started",
                {
                    "trace_id": trace_id,
                    "turn": turn,
                    "phase": phase,
                    "requested_max_output_tokens": max_output_tokens,
                },
            )
        response = await model.complete(
            ModelRequest(
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                trace_id=trace_id,
                idempotency_key=f"{trace_id}:turn:{turn}",
            )
        )
        if event_sink:
            event_sink(
                "model_call_completed",
                {
                    "trace_id": trace_id,
                    "turn": turn,
                    "phase": phase,
                    "response_id": response.response_id or "",
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "reasoning_tokens": response.usage.reasoning_tokens,
                    "reasoning_content_present": response.reasoning_content_present,
                    "finish_reason": response.finish_reason or "",
                    "requested_max_output_tokens": max_output_tokens,
                    "tool_call_count": len(response.tool_calls),
                },
            )
        # An empty assistant message is invalid in several OpenAI-compatible APIs
        # and provides no useful repair context. Reasoning text is deliberately
        # never represented in our message model or trace.
        if response.content or response.tool_calls:
            messages.append(
                Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
            )

        return response

    def parse_output(response: ModelResponse) -> tuple[OutputT | None, str | None]:
        if not response.content:
            return None, "model returned neither content nor tool calls"
        try:
            output = output_type.model_validate_json(_extract_json(response.content))
            if output_validator is not None:
                output_validator(output)
            return output, None
        except (ValidationError, ValueError) as error:
            return None, str(error)

    def exhausted_output_limit(response: ModelResponse, requested_limit: int) -> bool:
        if (response.finish_reason or "").casefold() == "length":
            return True
        return (
            not response.content
            and not response.tool_calls
            and response.usage.output_tokens >= requested_limit
        )

    # Reserve the final regular model turn for a response without tools. This
    # prevents a model that keeps gathering evidence from consuming the entire
    # budget before it ever gets a chance to produce the required JSON.
    collection_turns = budget.max_model_turns - 1 if allowed_tool_specs else 0
    tool_collection_turns = 0
    for turn in range(collection_turns):
        response = await complete(
            phase="tool_collection",
            turn=turn,
            tools=allowed_tool_specs,
            max_output_tokens=budget.max_output_tokens,
        )
        if response.tool_calls:
            for call in response.tool_calls:
                tool_count += 1
                if tool_count > budget.max_tool_calls:
                    raise AgentBudgetExceeded("tool call budget exceeded")
                if event_sink:
                    event_sink(
                        "tool_call_started",
                        {"trace_id": trace_id, "tool": call.name, "tool_call_id": call.id},
                    )
                result = await registry.execute(call, allowlist)
                if event_sink:
                    event_sink(
                        "tool_call_completed",
                        {"trace_id": trace_id, "tool": call.name, "tool_call_id": call.id},
                    )
                messages.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(result, ensure_ascii=False, default=str),
                    )
                )
            tool_collection_turns += 1
            continue

        parsed, error = parse_output(response)
        if parsed is not None:
            return parsed
        last_error = error

    if allowed_tool_specs:
        final_instruction = (
            "Tool collection is complete. Do not call any tools. Return only the final "
            "JSON object that conforms to the requested output schema."
        )
    else:
        final_instruction = (
            "Return only the final JSON object that conforms to the requested output schema."
        )
    messages.append(Message(role="user", content=final_instruction))
    final_turn = collection_turns
    requested_limit = budget.max_output_tokens
    repair_context = list(messages)
    response = await complete(
        phase="final",
        turn=final_turn,
        tools=[],
        max_output_tokens=requested_limit,
    )
    if response.tool_calls:
        # A model should not be able to call tools in this phase because the
        # request has no tool definitions. Treat a provider that nevertheless
        # returns tool calls as invalid output, without executing them.
        if tool_count + len(response.tool_calls) > budget.max_tool_calls:
            raise AgentBudgetExceeded("tool call budget exceeded")
        last_error = "final response requested tools after tool collection was disabled"
    else:
        parsed, error = parse_output(response)
        if parsed is not None:
            return parsed
        last_error = error

    output_limit_exhausted = exhausted_output_limit(response, requested_limit)
    if output_limit_exhausted:
        last_truncation_error = (
            f"model output was truncated at the requested token limit ({requested_limit})"
        )
        last_error = last_truncation_error
    if output_limit_exhausted and requested_limit >= budget.max_output_tokens_limit:
        raise InvalidAgentOutput(
            "model exhausted the maximum output token limit "
            f"({budget.max_output_tokens_limit}) without valid JSON (trace_id={trace_id})"
        )

    for repair_count in range(1, budget.max_repairs + 1):
        if output_limit_exhausted:
            requested_limit = min(requested_limit * 2, budget.max_output_tokens_limit)
        # Repairs always restart from the stable evidence/instruction context.
        # In particular, never feed a large truncated JSON response back to the
        # model, which would inflate every subsequent request and obscure the
        # original truncation failure.
        messages[:] = repair_context
        if event_sink:
            event_sink(
                "output_repair_requested",
                {
                    "trace_id": trace_id,
                    "repair": repair_count,
                    "error": last_error or "",
                    "requested_max_output_tokens": requested_limit,
                },
            )
        messages.append(
            Message(
                role="user",
                content=(
                    "Your previous final response did not validate or was truncated. "
                    "Return only complete corrected JSON. "
                    f"Validation error: {last_error}"
                ),
            )
        )
        response = await complete(
            phase="repair",
            turn=f"repair:{repair_count}",
            tools=[],
            max_output_tokens=requested_limit,
        )
        if response.tool_calls:
            if tool_count + len(response.tool_calls) > budget.max_tool_calls:
                raise AgentBudgetExceeded("tool call budget exceeded")
            last_error = "repair response requested tools after tool collection was disabled"
            continue
        parsed, error = parse_output(response)
        if parsed is not None:
            return parsed
        last_error = error
        output_limit_exhausted = exhausted_output_limit(response, requested_limit)
        if output_limit_exhausted:
            last_truncation_error = (
                f"model output was truncated at the requested token limit ({requested_limit})"
            )
            last_error = last_truncation_error
        if output_limit_exhausted and requested_limit >= budget.max_output_tokens_limit:
            raise InvalidAgentOutput(
                "model exhausted the maximum output token limit "
                f"({budget.max_output_tokens_limit}) without valid JSON (trace_id={trace_id})"
            )

    if tool_collection_turns == collection_turns and collection_turns > 0:
        detail = "tool collection turns exhausted without a final JSON response"
        if last_error:
            detail = f"{detail}; last error: {last_error}"
    else:
        detail = last_error or "agent did not produce valid output"
        if last_truncation_error:
            detail = last_truncation_error
            if last_error and last_error != last_truncation_error:
                detail = f"{detail}; subsequent repair failed: {last_error}"
    raise InvalidAgentOutput(f"{detail} (trace_id={trace_id})")


def _extract_json(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response does not contain a JSON object")
    return stripped[start : end + 1]
