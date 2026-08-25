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


class OutputParser[OutputT: BaseModel]:
    """Validate structured output while retaining bounded repair context."""

    def __init__(
        self,
        output_type: type[OutputT],
        output_validator: Callable[[OutputT], None] | None,
    ) -> None:
        self._output_type = output_type
        self._output_validator = output_validator
        self.invalid_submission: object | None = None

    def parse_content(self, response: ModelResponse) -> tuple[OutputT | None, str | None]:
        if not response.content:
            return None, "model returned neither content nor tool calls"
        try:
            value = json.loads(extract_json(response.content))
        except (ValidationError, ValueError) as error:
            self.invalid_submission = response.content
            return None, str(error)
        return self.validate(value)

    def parse_final(self, response: ModelResponse) -> tuple[OutputT | None, str | None]:
        submissions = [
            call for call in response.tool_calls if call.name == FINAL_OUTPUT_TOOL_NAME
        ]
        if len(submissions) == 1 and len(response.tool_calls) == 1:
            return self.validate(submissions[0].arguments)
        if response.tool_calls:
            if len(submissions) > 1:
                return None, "model submitted more than one final result"
            names = sorted({call.name for call in response.tool_calls})
            return None, f"model called unexpected final-phase tools: {names}"
        # Compatibility fallback for providers that ignore forced tool choice.
        return self.parse_content(response)

    def validate(self, value: object) -> tuple[OutputT | None, str | None]:
        try:
            output = self._output_type.model_validate(value)
        except (ValidationError, ValueError) as error:
            self.invalid_submission = value
            return None, str(error)
        try:
            if self._output_validator is not None:
                self._output_validator(output)
        except (ValidationError, ValueError) as error:
            self.invalid_submission = output.model_dump(mode="json")
            return None, str(error)
        self.invalid_submission = None
        return output, None


class ModelTurnRunner:
    """Issue one model request and append only usable assistant context."""

    def __init__(
        self,
        *,
        model: ModelPort,
        messages: list[Message],
        trace_id: str,
        event_sink: EventSink | None,
    ) -> None:
        self._model = model
        self._messages = messages
        self._trace_id = trace_id
        self._event_sink = event_sink

    async def complete(
        self,
        *,
        phase: str,
        turn: int | str,
        tools: list[ToolSpec],
        max_output_tokens: int,
        forced_tool_name: str | None = None,
    ) -> ModelResponse:
        self._emit(
            "model_call_started",
            {
                "trace_id": self._trace_id,
                "turn": turn,
                "phase": phase,
                "requested_max_output_tokens": max_output_tokens,
            },
        )
        response = await self._model.complete(
            ModelRequest(
                messages=self._messages,
                tools=tools,
                forced_tool_name=forced_tool_name,
                max_output_tokens=max_output_tokens,
                trace_id=self._trace_id,
                idempotency_key=f"{self._trace_id}:turn:{turn}",
            )
        )
        self._emit(
            "model_call_completed",
            {
                "trace_id": self._trace_id,
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
        # Empty assistant messages are invalid for several compatible APIs and
        # provide no repair context. Reasoning text is never represented here.
        if response.content or response.tool_calls or response.continuation_items:
            self._messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    continuation_items=response.continuation_items,
                )
            )
        return response

    def _emit(self, name: str, payload: dict[str, object]) -> None:
        if self._event_sink is not None:
            self._event_sink(name, payload)


class ToolBatchRunner:
    """Execute tool calls in response order and enforce the shared call budget."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        allowlist: list[str],
        messages: list[Message],
        trace_id: str,
        max_tool_calls: int,
        event_sink: EventSink | None,
    ) -> None:
        self._registry = registry
        self._allowlist = allowlist
        self._messages = messages
        self._trace_id = trace_id
        self._max_tool_calls = max_tool_calls
        self._event_sink = event_sink
        self._tool_count = 0

    async def execute(self, response: ModelResponse) -> None:
        for call in response.tool_calls:
            self._tool_count += 1
            if self._tool_count > self._max_tool_calls:
                raise AgentBudgetExceeded("tool call budget exceeded")
            self._emit(
                "tool_call_started",
                {"trace_id": self._trace_id, "tool": call.name, "tool_call_id": call.id},
            )
            try:
                result = await self._registry.execute(call, self._allowlist)
            except ToolExecutionError as tool_error:
                result = {
                    "ok": False,
                    "error": {"type": "invalid_tool_call", "message": str(tool_error)},
                }
                self._emit(
                    "tool_call_failed",
                    {
                        "trace_id": self._trace_id,
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "error_type": "invalid_tool_call",
                        "message": str(tool_error),
                    },
                )
            else:
                self._emit(
                    "tool_call_completed",
                    {
                        "trace_id": self._trace_id,
                        "tool": call.name,
                        "tool_call_id": call.id,
                    },
                )
            self._messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    content=json.dumps(result, ensure_ascii=False, default=str),
                )
            )

    def _emit(self, name: str, payload: dict[str, object]) -> None:
        if self._event_sink is not None:
            self._event_sink(name, payload)


@dataclass
class CollectionResult[OutputT: BaseModel]:
    output: OutputT | None = None
    last_error: str | None = None
    completed_tool_turns: int = 0


@dataclass
class RepairState:
    requested_limit: int
    last_error: str | None = None
    last_truncation_error: str | None = None
    output_limit_exhausted: bool = False

    def record_response[OutputT: BaseModel](
        self,
        response: ModelResponse,
        error: str | None,
        parser: OutputParser[OutputT],
    ) -> None:
        self.last_error = error
        self.output_limit_exhausted = exhausted_output_limit(response, self.requested_limit)
        if self.output_limit_exhausted:
            parser.invalid_submission = None
            self.last_truncation_error = (
                "model output was truncated at the requested token limit "
                f"({self.requested_limit})"
            )
            self.last_error = self.last_truncation_error


class BoundedAgentRunner[OutputT: BaseModel]:
    def __init__(
        self,
        *,
        model: ModelPort,
        registry: ToolRegistry,
        allowlist: list[str],
        system_prompt: str,
        user_prompt: str,
        output_type: type[OutputT],
        trace_id: str,
        budget: AgentBudget,
        event_sink: EventSink | None,
        output_validator: Callable[[OutputT], None] | None,
        repair_guidance: str | None,
    ) -> None:
        validate_budget(budget)
        self._budget = budget
        self._trace_id = trace_id
        self._event_sink = event_sink
        self._repair_guidance = repair_guidance
        self._messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ]
        self._allowed_tool_specs = registry.specs(allowlist)
        self._collection_turns = budget.max_model_turns - 1 if self._allowed_tool_specs else 0
        self._final_tool = make_final_tool(output_type)
        self._parser = OutputParser(output_type, output_validator)
        self._model_turn = ModelTurnRunner(
            model=model,
            messages=self._messages,
            trace_id=trace_id,
            event_sink=event_sink,
        )
        self._tools = ToolBatchRunner(
            registry=registry,
            allowlist=allowlist,
            messages=self._messages,
            trace_id=trace_id,
            max_tool_calls=budget.max_tool_calls,
            event_sink=event_sink,
        )

    async def run(self) -> OutputT:
        collection = await self._collect_tools()
        if collection.output is not None:
            return collection.output

        self._messages.append(
            Message(role="user", content=final_instruction(bool(self._allowed_tool_specs)))
        )
        # Capture before the final response: invalid final output is excluded from repair.
        repair_context = list(self._messages)
        state = RepairState(
            requested_limit=self._budget.max_output_tokens,
            last_error=collection.last_error,
        )
        response = await self._complete_final(
            phase="final", turn=self._collection_turns, state=state
        )
        parsed, error = self._parser.parse_final(response)
        if parsed is not None:
            return parsed
        state.record_response(response, error, self._parser)
        self._raise_if_maximum_output_exhausted(state)

        repairs_used, repaired = await self._repair(repair_context, state)
        if repaired is not None:
            return repaired
        detail = final_error_detail(
            state=state,
            repairs_used=repairs_used,
            completed_tool_turns=collection.completed_tool_turns,
            collection_turns=self._collection_turns,
        )
        raise InvalidAgentOutput(f"{detail} (trace_id={self._trace_id})")

    async def _collect_tools(self) -> CollectionResult[OutputT]:
        result: CollectionResult[OutputT] = CollectionResult()
        for turn in range(self._collection_turns):
            response = await self._model_turn.complete(
                phase="tool_collection",
                turn=turn,
                tools=self._allowed_tool_specs,
                max_output_tokens=self._budget.max_output_tokens,
            )
            if response.tool_calls:
                await self._tools.execute(response)
                result.completed_tool_turns += 1
                continue
            parsed, error = self._parser.parse_content(response)
            if parsed is not None:
                result.output = parsed
                return result
            result.last_error = error
        return result

    async def _repair(
        self, repair_context: list[Message], state: RepairState
    ) -> tuple[int, OutputT | None]:
        repairs_used = 0
        for repairs_used in range(1, self._budget.max_repairs + 1):
            if state.output_limit_exhausted:
                state.requested_limit = min(
                    state.requested_limit * 2,
                    self._budget.max_output_tokens_limit,
                )
            self._messages[:] = repair_context
            self._emit_repair_requested(repairs_used, state)
            self._messages.append(
                Message(
                    role="user",
                    content=repair_instruction(
                        error=state.last_error,
                        invalid_submission=self._parser.invalid_submission,
                        repair_guidance=self._repair_guidance,
                    ),
                )
            )
            response = await self._complete_final(
                phase="repair", turn=f"repair:{repairs_used}", state=state
            )
            parsed, error = self._parser.parse_final(response)
            if parsed is not None:
                return repairs_used, parsed
            state.record_response(response, error, self._parser)
            self._raise_if_maximum_output_exhausted(state)
        return repairs_used, None

    async def _complete_final(
        self, *, phase: str, turn: int | str, state: RepairState
    ) -> ModelResponse:
        return await self._model_turn.complete(
            phase=phase,
            turn=turn,
            tools=[self._final_tool],
            max_output_tokens=state.requested_limit,
            forced_tool_name=FINAL_OUTPUT_TOOL_NAME,
        )

    def _emit_repair_requested(self, repair: int, state: RepairState) -> None:
        if self._event_sink is not None:
            self._event_sink(
                "output_repair_requested",
                {
                    "trace_id": self._trace_id,
                    "repair": repair,
                    "error": state.last_error or "",
                    "requested_max_output_tokens": state.requested_limit,
                },
            )

    def _raise_if_maximum_output_exhausted(self, state: RepairState) -> None:
        if (
            state.output_limit_exhausted
            and state.requested_limit >= self._budget.max_output_tokens_limit
        ):
            raise InvalidAgentOutput(
                "model exhausted the maximum output token limit "
                f"({self._budget.max_output_tokens_limit}) without valid JSON "
                f"(trace_id={self._trace_id})"
            )


def validate_budget(budget: AgentBudget) -> None:
    if budget.max_model_turns < 1:
        raise AgentBudgetExceeded("model turn budget must be at least one")
    if budget.max_output_tokens < 1:
        raise AgentBudgetExceeded("output token budget must be at least one")
    if budget.max_output_tokens_limit < budget.max_output_tokens:
        raise AgentBudgetExceeded("output token limit must not be below the initial budget")


def make_final_tool(output_type: type[BaseModel]) -> ToolSpec:
    return ToolSpec(
        name=FINAL_OUTPUT_TOOL_NAME,
        description=(
            "Submit the complete final result exactly once. The arguments must match "
            "the required output schema; do not return the result as prose."
        ),
        parameters=output_type.model_json_schema(),
    )


def final_instruction(has_collection_tools: bool) -> str:
    if has_collection_tools:
        return (
            f"Tool collection is complete. Call {FINAL_OUTPUT_TOOL_NAME} exactly once "
            "with the complete result matching the requested output schema. Do not call "
            "evidence tools and do not return ordinary prose."
        )
    return (
        f"Call {FINAL_OUTPUT_TOOL_NAME} exactly once with the complete result matching "
        "the requested output schema. Do not return ordinary prose."
    )


def exhausted_output_limit(response: ModelResponse, requested_limit: int) -> bool:
    if (response.finish_reason or "").casefold() == "length":
        return True
    return (
        not response.content
        and not response.tool_calls
        and response.usage.output_tokens >= requested_limit
    )


def final_error_detail(
    *,
    state: RepairState,
    repairs_used: int,
    completed_tool_turns: int,
    collection_turns: int,
) -> str:
    detail = state.last_error or "agent did not produce valid output"
    if state.last_truncation_error:
        detail = state.last_truncation_error
        if state.last_error and state.last_error != state.last_truncation_error:
            detail = f"{detail}; subsequent repair failed: {state.last_error}"
    elif repairs_used:
        detail = (
            "final result still failed validation after "
            f"{repairs_used} repair attempts: {detail}"
        )
    elif completed_tool_turns == collection_turns and collection_turns > 0:
        detail = f"final result failed validation after evidence collection: {detail}"
    return detail


def repair_instruction(
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
    candidate = bounded_invalid_submission(invalid_submission)
    if candidate is not None:
        parts.extend(
            [
                "Previous invalid submission (preserve its valid content and correct only the "
                "reported defects):",
                candidate,
            ]
        )
    return "\n\n".join(parts)


def bounded_invalid_submission(value: object | None, *, max_chars: int = 160_000) -> str | None:
    if value is None:
        return None
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) > max_chars:
        return None
    return rendered


def extract_json(content: str) -> str:
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
