from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from paper_reviewer.ports.model import Message, ModelPort, ModelRequest, ModelResponse, ToolSpec
from paper_reviewer.tools.registry import ToolExecutionError, ToolRegistry


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
FINAL_OUTPUT_TOOL_NAME = "submit_final_result"


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
    repair_guidance: str | None = None,
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
    last_invalid_submission: object | None = None

    async def complete(
        *,
        phase: str,
        turn: int | str,
        tools: list[ToolSpec],
        max_output_tokens: int,
        forced_tool_name: str | None = None,
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
                forced_tool_name=forced_tool_name,
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
        if response.content or response.tool_calls or response.continuation_items:
            messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    continuation_items=response.continuation_items,
                )
            )

        return response

    def validate_output(value: object) -> tuple[OutputT | None, str | None]:
        nonlocal last_invalid_submission
        try:
            output = output_type.model_validate(value)
        except (ValidationError, ValueError) as error:
            last_invalid_submission = value
            return None, str(error)
        try:
            if output_validator is not None:
                output_validator(output)
        except (ValidationError, ValueError) as error:
            last_invalid_submission = output.model_dump(mode="json")
            return None, str(error)
        last_invalid_submission = None
        return output, None

    def parse_output(response: ModelResponse) -> tuple[OutputT | None, str | None]:
        nonlocal last_invalid_submission
        if not response.content:
            return None, "model returned neither content nor tool calls"
        try:
            value = json.loads(_extract_json(response.content))
        except (ValidationError, ValueError) as error:
            last_invalid_submission = response.content
            return None, str(error)
        return validate_output(value)

    def parse_final_output(response: ModelResponse) -> tuple[OutputT | None, str | None]:
        submissions = [call for call in response.tool_calls if call.name == FINAL_OUTPUT_TOOL_NAME]
        if len(submissions) == 1 and len(response.tool_calls) == 1:
            return validate_output(submissions[0].arguments)
        if response.tool_calls:
            if len(submissions) > 1:
                return None, "model submitted more than one final result"
            names = sorted({call.name for call in response.tool_calls})
            return None, f"model called unexpected final-phase tools: {names}"
        # Compatibility fallback for providers that ignore forced tool choice
        # but still honor the prompt and return a valid JSON object as text.
        return parse_output(response)

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
    final_tool = ToolSpec(
        name=FINAL_OUTPUT_TOOL_NAME,
        description=(
            "Submit the complete final result exactly once. The arguments must match "
            "the required output schema; do not return the result as prose."
        ),
        parameters=output_type.model_json_schema(),
    )
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
                try:
                    result = await registry.execute(call, allowlist)
                except ToolExecutionError as tool_error:
                    # Tool arguments are model output and can be malformed even when
                    # the provider accepted the JSON schema. Return a structured error
                    # to the same agent so one invalid call does not abort every
                    # concurrently running reviewer.
                    result = {
                        "ok": False,
                        "error": {
                            "type": "invalid_tool_call",
                            "message": str(tool_error),
                        },
                    }
                    if event_sink:
                        event_sink(
                            "tool_call_failed",
                            {
                                "trace_id": trace_id,
                                "tool": call.name,
                                "tool_call_id": call.id,
                                "error_type": "invalid_tool_call",
                                "message": str(tool_error),
                            },
                        )
                else:
                    if event_sink:
                        event_sink(
                            "tool_call_completed",
                            {
                                "trace_id": trace_id,
                                "tool": call.name,
                                "tool_call_id": call.id,
                            },
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
            f"Tool collection is complete. Call {FINAL_OUTPUT_TOOL_NAME} exactly once "
            "with the complete result matching the requested output schema. Do not call "
            "evidence tools and do not return ordinary prose."
        )
    else:
        final_instruction = (
            f"Call {FINAL_OUTPUT_TOOL_NAME} exactly once with the complete result matching "
            "the requested output schema. Do not return ordinary prose."
        )
    messages.append(Message(role="user", content=final_instruction))
    final_turn = collection_turns
    requested_limit = budget.max_output_tokens
    repair_context = list(messages)
    response = await complete(
        phase="final",
        turn=final_turn,
        tools=[final_tool],
        max_output_tokens=requested_limit,
        forced_tool_name=FINAL_OUTPUT_TOOL_NAME,
    )
    parsed, error = parse_final_output(response)
    if parsed is not None:
        return parsed
    last_error = error

    output_limit_exhausted = exhausted_output_limit(response, requested_limit)
    if output_limit_exhausted:
        last_invalid_submission = None
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
                content=_repair_instruction(
                    error=last_error,
                    invalid_submission=last_invalid_submission,
                    repair_guidance=repair_guidance,
                ),
            )
        )
        response = await complete(
            phase="repair",
            turn=f"repair:{repair_count}",
            tools=[final_tool],
            max_output_tokens=requested_limit,
            forced_tool_name=FINAL_OUTPUT_TOOL_NAME,
        )
        parsed, error = parse_final_output(response)
        if parsed is not None:
            return parsed
        last_error = error
        output_limit_exhausted = exhausted_output_limit(response, requested_limit)
        if output_limit_exhausted:
            last_invalid_submission = None
            last_truncation_error = (
                f"model output was truncated at the requested token limit ({requested_limit})"
            )
            last_error = last_truncation_error
        if output_limit_exhausted and requested_limit >= budget.max_output_tokens_limit:
            raise InvalidAgentOutput(
                "model exhausted the maximum output token limit "
                f"({budget.max_output_tokens_limit}) without valid JSON (trace_id={trace_id})"
            )

    detail = last_error or "agent did not produce valid output"
    if last_truncation_error:
        detail = last_truncation_error
        if last_error and last_error != last_truncation_error:
            detail = f"{detail}; subsequent repair failed: {last_error}"
    elif repair_count:
        detail = (
            "final result still failed validation after "
            f"{repair_count} repair attempts: {detail}"
        )
    elif tool_collection_turns == collection_turns and collection_turns > 0:
        detail = f"final result failed validation after evidence collection: {detail}"
    raise InvalidAgentOutput(f"{detail} (trace_id={trace_id})")


def _repair_instruction(
    *,
    error: str | None,
    invalid_submission: object | None,
    repair_guidance: str | None,
) -> str:
    parts = [
        "Your previous final submission did not validate or was truncated.",
        "Correct the previous submission instead of starting over.",
        f"Call {FINAL_OUTPUT_TOOL_NAME} exactly once with complete corrected arguments and "
        "do not return ordinary prose.",
        f"Validation error: {error or 'unknown validation error'}",
    ]
    if repair_guidance:
        parts.append(f"Task-specific repair rules: {repair_guidance}")
    candidate = _bounded_invalid_submission(invalid_submission)
    if candidate is not None:
        parts.extend(
            [
                "Previous invalid submission (preserve its valid content and correct only the "
                "reported defects):",
                candidate,
            ]
        )
    return "\n\n".join(parts)


def _bounded_invalid_submission(value: object | None, *, max_chars: int = 160_000) -> str | None:
    if value is None:
        return None
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) > max_chars:
        return None
    return rendered


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
