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

from paper_reviewer.ports.model import Message, ModelRequest, ModelResponse, ToolCall, Usage


class ResponsesAPIError(RuntimeError):
    """A Responses API lifecycle failure safe for application error handling."""


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120,
    ) -> None:
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    @retry(
        retry=retry_if_exception_type(
            (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await self.complete_once(request)

    async def complete_once(self, request: ModelRequest) -> ModelResponse:
        instructions, input_items = _request_input(request.messages)
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in request.tools
        ]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "store": False,
            "max_output_tokens": request.max_output_tokens,
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = tools
        if request.forced_tool_name:
            if not any(tool.name == request.forced_tool_name for tool in request.tools):
                raise ValueError("tool choice must name a declared tool")
            kwargs["tool_choice"] = {
                "type": "function",
                "name": request.forced_tool_name,
            }

        response = await self.client.responses.create(**kwargs)
        status = _optional_value(response, "status")
        _validate_status(response, status)

        raw_output = _optional_value(response, "output")
        if raw_output is None:
            output: list[object] = []
        elif isinstance(raw_output, list):
            output = raw_output
        else:
            raise ResponsesAPIError("Responses API returned an invalid output collection")
        continuation_items = [_json_safe_item(item) for item in output]
        tool_calls = _tool_calls(output)
        usage = _optional_value(response, "usage")
        incomplete_details = _optional_value(response, "incomplete_details")
        incomplete_reason = _optional_value(incomplete_details, "reason")

        return ModelResponse(
            content=_string_field(response, "output_text"),
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=_integer_field(usage, "input_tokens"),
                output_tokens=_integer_field(usage, "output_tokens"),
                reasoning_tokens=_reasoning_tokens(usage),
            ),
            # Stateless Responses calls never expose the remote response ID to
            # the agent loop, preventing it from entering persistent traces.
            response_id=None,
            finish_reason=_finish_reason(status, incomplete_reason, bool(tool_calls)),
            reasoning_content_present=any(
                _optional_value(item, "type") == "reasoning" for item in output
            ),
            continuation_items=continuation_items,
        )

    async def close(self) -> None:
        await self.client.close()


def _request_input(messages: list[Message]) -> tuple[str | None, list[dict[str, object]]]:
    instructions: list[str] = []
    input_items: list[dict[str, object]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                instructions.append(message.content)
            continue
        if message.continuation_items:
            input_items.extend(_json_safe_item(item) for item in message.continuation_items)
            continue
        if message.role == "tool":
            if not message.tool_call_id:
                raise ValueError("Responses function output requires a tool call ID")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content or "",
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            if message.content:
                input_items.append({"role": "assistant", "content": message.content})
            input_items.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
            continue
        input_items.append({"role": message.role, "content": message.content or ""})
    return "\n\n".join(instructions) or None, input_items


def _tool_calls(output: object) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(output, list):
        return calls
    for item in output:
        if _optional_value(item, "type") != "function_call":
            continue
        name = _optional_value(item, "name")
        call_id = _optional_value(item, "call_id") or _optional_value(item, "id")
        raw_arguments = _optional_value(item, "arguments")
        if not isinstance(name, str) or not isinstance(call_id, str):
            raise ValueError("Responses API returned an invalid function call")
        try:
            arguments = (
                json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            )
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid tool arguments for {name}") from error
        if not isinstance(arguments, dict):
            raise ValueError(f"tool arguments for {name} must be an object")
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return calls


def _validate_status(response: object, status: object) -> None:
    if status in {None, "completed", "incomplete"}:
        return
    if status == "failed":
        error = _optional_value(response, "error")
        code = _optional_value(error, "code")
        suffix = f" ({code})" if isinstance(code, str) and code else ""
        raise ResponsesAPIError(f"Responses API request failed{suffix}")
    if status in {"cancelled", "canceled"}:
        raise ResponsesAPIError("Responses API request was cancelled")
    if status in {"queued", "in_progress"}:
        raise ResponsesAPIError(f"Responses API returned non-terminal status: {status}")
    raise ResponsesAPIError("Responses API returned an unknown status")


def _finish_reason(status: object, incomplete_reason: object, has_tools: bool) -> str:
    if status == "incomplete":
        if incomplete_reason == "max_output_tokens":
            return "length"
        if incomplete_reason == "content_filter":
            return "content_filter"
        return "incomplete"
    return "tool_calls" if has_tools else "stop"


def _json_safe_item(item: object) -> dict[str, object]:
    if isinstance(item, Mapping):
        raw: object = dict(item)
    else:
        model_dump = getattr(item, "model_dump", None)
        if not callable(model_dump):
            raise ValueError("Responses API returned a non-serializable output item")
        raw = model_dump(mode="json", exclude_none=True)
    try:
        value = json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("Responses API returned a non-JSON output item") from error
    if not isinstance(value, dict):
        raise ValueError("Responses API output item must be an object")
    return value


def _optional_value(value: object, field: str) -> object:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(field)
    return getattr(value, field, None)


def _integer_field(value: object, field: str) -> int:
    candidate = _optional_value(value, field)
    return candidate if isinstance(candidate, int) and not isinstance(candidate, bool) else 0


def _string_field(value: object, field: str) -> str | None:
    candidate = _optional_value(value, field)
    return candidate if isinstance(candidate, str) and candidate else None


def _reasoning_tokens(usage: object) -> int:
    details = _optional_value(usage, "output_tokens_details")
    return _integer_field(details, "reasoning_tokens")
