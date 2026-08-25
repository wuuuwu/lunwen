from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from paper_reviewer.agents.loop_components import (
    FINAL_OUTPUT_TOOL_NAME,
    AgentBudget,
    AgentBudgetExceeded,
    BoundedAgentRunner,
    EventSink,
    InvalidAgentOutput,
    bounded_invalid_submission,
    extract_json,
    repair_instruction,
)
from paper_reviewer.ports.model import ModelPort
from paper_reviewer.tools.registry import ToolRegistry

__all__ = [
    "FINAL_OUTPUT_TOOL_NAME",
    "AgentBudget",
    "AgentBudgetExceeded",
    "EventSink",
    "InvalidAgentOutput",
    "run_bounded_agent",
]


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
    """Run a bounded tool-using agent without persisting model continuation state."""
    runner = BoundedAgentRunner(
        model=model,
        registry=registry,
        allowlist=allowlist,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_type=output_type,
        trace_id=trace_id,
        budget=budget,
        event_sink=event_sink,
        output_validator=output_validator,
        repair_guidance=repair_guidance,
    )
    return await runner.run()


# Preserve the existing private names for in-package callers and downstream tests.
_repair_instruction = repair_instruction
_bounded_invalid_submission = bounded_invalid_submission
_extract_json = extract_json
