from __future__ import annotations

import json
from importlib.resources import files

from jinja2 import Template
from pydantic import BaseModel, ConfigDict, Field

from paper_reviewer.agents.loop import AgentBudget, EventSink, run_bounded_agent
from paper_reviewer.domain.review import MetaReview, ReviewerResult, ReviewFinding
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.ports.model import ModelPort
from paper_reviewer.tools.registry import ToolRegistry
from paper_reviewer.validation.audits import AuditReport


class MetaReviewSelection(BaseModel):
    """The model's bounded decision; finding bodies remain deterministic data."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    overall_summary: str
    selected_finding_ids: list[str]
    disagreements: list[str] = Field(default_factory=list)
    human_checks: list[str] = Field(default_factory=list)


async def run_meta_reviewer(
    *,
    run_id: str,
    model: ModelPort,
    rubric: RubricProfile,
    results: list[ReviewerResult],
    audit: AuditReport,
    max_repairs: int,
    event_sink: EventSink | None = None,
) -> MetaReview:
    source_findings: dict[str, ReviewFinding] = {}
    duplicate_finding_ids: set[str] = set()
    for result in results:
        for finding in result.findings:
            if finding.finding_id in source_findings:
                duplicate_finding_ids.add(finding.finding_id)
            else:
                source_findings[finding.finding_id] = finding
    if duplicate_finding_ids:
        raise ValueError(
            "source reviewer finding_id values must be globally unique; duplicates: "
            f"{sorted(duplicate_finding_ids)}"
        )
    allowed_finding_ids = set(source_findings)

    def validate_selection(selection: MetaReviewSelection) -> None:
        if selection.run_id != run_id:
            raise ValueError("meta review run_id does not match the current run")
        unknown_ids = list(
            dict.fromkeys(
                finding_id
                for finding_id in selection.selected_finding_ids
                if finding_id not in allowed_finding_ids
            )
        )
        if unknown_ids:
            raise ValueError(
                "selected_finding_ids contains IDs not present in source reviews: "
                f"{unknown_ids}. Select only exact source IDs from: "
                f"{sorted(allowed_finding_ids)}"
            )

    template = Template(
        files("paper_reviewer.agents.prompts")
        .joinpath("meta_reviewer.txt")
        .read_text(encoding="utf-8")
    )
    system_prompt = template.render(
        rubric_json=rubric.model_dump_json(),
        reviewer_results_json=json.dumps(
            [result.model_dump(mode="json") for result in results],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        audit_json=audit.model_dump_json(),
        output_schema=json.dumps(
            MetaReviewSelection.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    selection = await run_bounded_agent(
        model=model,
        registry=ToolRegistry(),
        allowlist=[],
        system_prompt=system_prompt,
        user_prompt=json.dumps({"run_id": run_id, "instruction": "Produce the meta review."}),
        output_type=MetaReviewSelection,
        trace_id=f"{run_id}:meta",
        budget=AgentBudget(
            max_model_turns=1,
            max_tool_calls=0,
            max_repairs=max_repairs,
            max_output_tokens=8192,
            max_output_tokens_limit=16384,
        ),
        event_sink=event_sink,
        output_validator=validate_selection,
    )
    selected_ids = list(dict.fromkeys(selection.selected_finding_ids))
    return MetaReview(
        run_id=selection.run_id,
        overall_summary=selection.overall_summary,
        findings=[source_findings[finding_id].model_copy(deep=True) for finding_id in selected_ids],
        disagreements=selection.disagreements,
        human_checks=selection.human_checks,
    )
