from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from pydantic import BaseModel

from paper_reviewer.ports.model import ToolCall, ToolSpec


class ToolExecutionError(RuntimeError):
    pass


class RegisteredTool(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    spec: ToolSpec
    handler: Callable[..., object]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, object],
        handler: Callable[..., object],
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = RegisteredTool(
            spec=ToolSpec(name=name, description=description, parameters=parameters),
            handler=handler,
        )

    def specs(self, allowlist: list[str]) -> list[ToolSpec]:
        unknown = set(allowlist) - self._tools.keys()
        if unknown:
            raise ValueError(f"unknown tools in allowlist: {sorted(unknown)}")
        return [self._tools[name].spec for name in allowlist]

    async def execute(self, call: ToolCall, allowlist: list[str]) -> object:
        if call.name not in allowlist:
            raise ToolExecutionError(f"tool is not allowed: {call.name}")
        registered = self._tools.get(call.name)
        if registered is None:
            raise ToolExecutionError(f"tool is not registered: {call.name}")
        try:
            validate(instance=call.arguments, schema=registered.spec.parameters)
            result = registered.handler(**call.arguments)
            if inspect.isawaitable(result):
                return await _await_result(result)
            return result
        except JsonSchemaValidationError as error:
            detail = _schema_validation_detail(error)
            raise ToolExecutionError(f"invalid call to {call.name}: {detail}") from error
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(f"invalid call to {call.name}: {error}") from error


async def _await_result(result: Awaitable[Any]) -> Any:
    return await result


def _schema_validation_detail(error: JsonSchemaValidationError) -> str:
    """Return a bounded schema error without echoing model-supplied arguments."""

    path = ".".join(str(part) for part in error.absolute_path) or "arguments"
    if error.validator == "maxItems":
        return f"{path} must contain at most {error.validator_value} items"
    if error.validator == "minItems":
        return f"{path} must contain at least {error.validator_value} items"
    if error.validator == "required":
        return "a required argument is missing"
    if error.validator == "additionalProperties":
        return "arguments contain unsupported fields"
    if error.validator == "type":
        return f"{path} must have type {error.validator_value}"
    return f"{path} does not match the tool schema"
