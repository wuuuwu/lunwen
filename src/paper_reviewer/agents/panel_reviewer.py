from __future__ import annotations

import json
from importlib.resources import files
from typing import Literal, cast

from jinja2 import Template

from paper_reviewer.agents.loop import AgentBudget, EventSink, run_bounded_agent
from paper_reviewer.agents.reviewer_context import (
    build_reviewer_read_context,
    finding_evidence_blocks,
)
from paper_reviewer.config import ReviewerProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind
from paper_reviewer.domain.review import ExpertOpinion, ExpertVerdict, ReviewFinding, Severity
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.ports.model import ModelPort


async def run_panel_reviewer(
    *,
    run_id: str,
    model: ModelPort,
    expert: ReviewerProfile,
    round: Literal["initial", "supplemental"],
    rubric: RubricProfile,
    document: DocumentInfo,
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
    findings: list[ReviewFinding],
    discipline_name: str,
    discipline_profile: str | None,
    max_repairs: int,
    event_sink: EventSink | None = None,
) -> ExpertOpinion:
    """Run one isolated full-paper expert without exposing other experts' opinions."""

    read_context = build_reviewer_read_context(blocks=blocks, evidence=evidence)
    registry = read_context.registry
    system_prompt = _panel_template().render(
        expert=expert,
        rubric_json=json.dumps(rubric.model_dump(mode="json"), ensure_ascii=False, indent=2),
        output_schema=json.dumps(ExpertOpinion.model_json_schema(), ensure_ascii=False),
    )
    block_by_id = read_context.evidence_index.block_by_id
    cited_block_ids = {
        reference.block_id
        for finding in findings
        for reference in finding.paper_evidence
        if reference.block_id is not None
    }
    # Findings are deterministic inputs to voting. No ExpertOpinion collection is accepted
    # by this API, which enforces first/second-round reviewer isolation by construction.
    user_prompt = json.dumps(
        {
            "run_id": run_id,
            "expert_id": expert.reviewer_id,
            "round": round,
            "paper": document.model_dump(mode="json"),
            "paper_overview": read_context.paper_overview,
            "discipline_name": discipline_name,
            "discipline_profile": discipline_profile,
            "review_findings": [finding.model_dump(mode="json") for finding in findings],
            "finding_evidence_blocks": finding_evidence_blocks(
                finding_block_ids=cited_block_ids,
                index=read_context.evidence_index,
            ),
            "instruction": (
                "Independently assess the complete paper against the complete rubric. Use "
                "paper tools to inspect all evidence needed. Return your own ExpertOpinion; "
                "no other expert opinions are available."
            ),
        },
        ensure_ascii=False,
    )
    findings_by_id = {finding.finding_id: finding for finding in findings}
    def validate_result(result: ExpertOpinion) -> None:
        errors: list[str] = []
        if result.expert_id != expert.reviewer_id:
            errors.append("expert opinion identity does not match the assigned expert")
        if result.round != round:
            errors.append("expert opinion round does not match the assigned round")
        unknown = sorted(set(result.finding_ids) - set(findings_by_id))
        if unknown:
            errors.append(f"expert opinion references unknown finding ids: {unknown}")
        if result.verdict is ExpertVerdict.UNQUALIFIED:
            for finding_id in result.finding_ids:
                finding = findings_by_id.get(finding_id)
                if finding is None:
                    continue
                if finding.severity not in {Severity.MAJOR, Severity.CRITICAL}:
                    errors.append(
                        f"{finding_id}: unqualified vote requires a major or critical finding"
                    )
                if not finding.paper_evidence:
                    errors.append(
                        f"{finding_id}: unqualified vote requires paper evidence"
                    )
                for reference in finding.paper_evidence:
                    if reference.kind is not EvidenceKind.PAPER:
                        errors.append(
                            f"{finding_id}: unqualified vote contains non-paper evidence"
                        )
                        continue
                    block = block_by_id.get(reference.block_id or "")
                    if block is None:
                        errors.append(
                            f"{finding_id}: unqualified vote references unknown paper block "
                            f"{reference.block_id}"
                        )
                    elif (
                        reference.page is not None
                        and reference.page != block.page
                    ):
                        errors.append(
                            f"{finding_id}: paper evidence page does not match its block"
                        )
                    elif reference.quote and reference.quote not in block.text:
                        errors.append(
                            f"{finding_id}: paper evidence quote does not match its block"
                        )
        elif result.finding_ids:
            errors.append("only an unqualified opinion may cite decisive finding_ids")
        if errors:
            raise ValueError(
                "panel reviewer output failed deterministic validation: " + "; ".join(errors)
            )

    return await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=expert.allowed_tools,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_type=ExpertOpinion,
        trace_id=f"{run_id}:panel:{round}:{expert.reviewer_id}",
        budget=AgentBudget(
            max_model_turns=expert.max_model_turns,
            max_tool_calls=expert.max_tool_calls,
            max_repairs=max_repairs,
            max_output_tokens=393_216,
            max_output_tokens_limit=393_216,
        ),
        event_sink=event_sink,
        output_validator=validate_result,
    )


def _panel_template() -> Template:
    path = files("paper_reviewer.agents.prompts").joinpath("panel_reviewer.txt")
    return cast(Template, Template(path.read_text(encoding="utf-8")))
