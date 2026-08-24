from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, object]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, object]


class Message(BaseModel):
    role: str
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    continuation_items: list[dict[str, object]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )


class ModelRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    forced_tool_name: str | None = None
    max_output_tokens: int = 4096
    temperature: float = 0
    trace_id: str
    idempotency_key: str


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None


class ModelResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    response_id: str | None = None
    finish_reason: str | None = None
    reasoning_content_present: bool = False
    continuation_items: list[dict[str, object]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )


class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
