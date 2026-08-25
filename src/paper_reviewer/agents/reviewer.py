from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from importlib.resources import files
from typing import cast

from jinja2 import Template

from paper_reviewer.agents.loop import AgentBudget, EventSink, run_bounded_agent
from paper_reviewer.config import ReviewerProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceRef
from paper_reviewer.domain.review import (
    HardRuleStatus,
    ReviewerResult,
    ReviewFinding,
)
from paper_reviewer.domain.rubric import HardRule, RubricDimension
from paper_reviewer.ports.model import ModelPort
from paper_reviewer.tools.evidence_reader import EvidenceReaderTools, register_evidence_tools
from paper_reviewer.tools.paper_reader import PaperReaderTools, register_paper_tools
from paper_reviewer.tools.registry import ToolRegistry
from paper_reviewer.tools.web_search import WebSearchTools, register_web_search_tools
from paper_reviewer.validation.audits import reviewer_reference_errors


async def run_reviewer(
    *,
    run_id: str,
    model: ModelPort,
    reviewer: ReviewerProfile,
    dimensions: list[RubricDimension],
    document: DocumentInfo,
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
    scoring_enabled: bool,
    max_repairs: int,
    repair_source: ReviewerResult | None = None,
    event_sink: EventSink | None = None,
    hard_rules: list[HardRule] | None = None,
    discipline_name: str | None = None,
    discipline_profile: str | None = None,
    web_search_tools: WebSearchTools | None = None,
) -> ReviewerResult:
    registry = ToolRegistry()
    register_paper_tools(registry, PaperReaderTools(blocks))
    register_evidence_tools(registry, EvidenceReaderTools(evidence))
    if web_search_tools is not None:
        register_web_search_tools(registry, web_search_tools)
    allowed = [
        name
        for name in reviewer.allowed_tools
        if name != "web_search" or web_search_tools is not None
    ]
    system_prompt = _reviewer_template().render(
        reviewer=reviewer,
        dimensions_json=json.dumps(
            [dimension.model_dump(mode="json") for dimension in dimensions],
            ensure_ascii=False,
            indent=2,
        ),
        hard_rules_json=json.dumps(
            [rule.model_dump(mode="json") for rule in (hard_rules or [])],
            ensure_ascii=False,
            indent=2,
        ),
        output_schema=json.dumps(ReviewerResult.model_json_schema(), ensure_ascii=False),
    )
    overview = [
        {
            "block_id": block.block_id,
            "page": block.page,
            "type": block.block_type.value,
            "text": block.text[:1200],
        }
        for block in blocks[:12]
    ]
    user_payload: dict[str, object] = {
        "run_id": run_id,
        "reviewer_id": reviewer.reviewer_id,
        "paper": document.model_dump(mode="json"),
        "paper_overview": overview,
        "scoring_enabled": scoring_enabled,
        "discipline_name": discipline_name,
        "discipline_profile": discipline_profile,
        "instruction": (
            "Review the assigned dimensions and hard rules and return a "
            "ReviewerResult. Read the complete rubric context and use tools to "
            "inspect all paper evidence needed for each assessment."
        ),
    }
    if repair_source is not None:
        user_payload["repair_source"] = repair_source.model_dump(mode="json")
        user_payload["instruction"] = (
            "Repair only invalid evidence references in repair_source. Return every original "
            "finding_id with the same reviewer, dimension, and severity. Preserve all already "
            "valid evidence references; do not drop findings or add new findings."
        )
    user_prompt = json.dumps(user_payload, ensure_ascii=False)

    block_ids = {block.block_id for block in blocks}
    block_by_id = {block.block_id: block for block in blocks}
    block_pages = {block.block_id: block.page for block in blocks}
    repair_baseline = (
        ReviewerResult.model_validate(repair_source.model_dump(mode="python"))
        if repair_source is not None
        else None
    )
    if repair_baseline is not None:
        duplicate_source_ids = _duplicate_finding_ids(repair_baseline)
        if duplicate_source_ids:
            raise ValueError(
                "repair_source finding_id values must be unique: "
                f"{duplicate_source_ids}"
            )

    dimension_by_id = {dimension.dimension_id: dimension for dimension in dimensions}
    hard_rule_ids = {rule.rule_id for rule in (hard_rules or [])}

    def validate_result(result: ReviewerResult) -> None:
        nonlocal repair_baseline
        evidence_ids = {item.evidence_id for item in evidence}
        errors: list[str] = []
        if result.reviewer_id != reviewer.reviewer_id:
            errors.append("reviewer result identity does not match the assigned reviewer")
        if not scoring_enabled and result.dimension_scores:
            errors.append("reviewer returned dimension scores while scoring is disabled")
        duplicate_ids = _duplicate_finding_ids(result)
        if duplicate_ids:
            errors.append(f"finding_id values must be unique: {duplicate_ids}")
        identity_is_valid = all(
            finding.reviewer_id == reviewer.reviewer_id for finding in result.findings
        )
        errors.extend(
            _policy_assessment_errors(
                result=result,
                reviewer_id=reviewer.reviewer_id,
                dimensions=dimension_by_id,
                hard_rule_ids=hard_rule_ids,
                scoring_enabled=scoring_enabled,
                block_pages=block_pages,
                evidence_ids=evidence_ids,
            )
        )
        if repair_baseline is not None:
            errors.extend(
                _repair_constraint_errors(
                    baseline=repair_baseline,
                    candidate=result,
                    block_ids=block_ids,
                    evidence_ids=evidence_ids,
                )
            )
        reference_errors = reviewer_reference_errors(
            result=result,
            block_ids=block_ids,
            evidence_ids=evidence_ids,
            block_by_id=block_by_id,
        )
        errors.extend(reference_errors)
        if errors:
            can_be_repair_baseline = (
                repair_baseline is None
                and result.reviewer_id == reviewer.reviewer_id
                and (scoring_enabled or not result.dimension_scores)
                and not duplicate_ids
                and identity_is_valid
                and not result.criterion_assessments
                and not result.hard_rule_assessments
                and bool(reference_errors)
            )
            if can_be_repair_baseline:
                repair_baseline = result.model_copy(deep=True)
            detail = "; ".join(errors)
            raise ValueError(f"reviewer output failed deterministic validation: {detail}")

    result = await run_bounded_agent(
        model=model,
        registry=registry,
        allowlist=allowed,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        output_type=ReviewerResult,
        trace_id=f"{run_id}:{reviewer.reviewer_id}",
        budget=AgentBudget(
            max_model_turns=reviewer.max_model_turns,
            max_tool_calls=reviewer.max_tool_calls,
            max_repairs=max_repairs,
            max_output_tokens=393_216,
            max_output_tokens_limit=393_216,
        ),
        event_sink=event_sink,
        output_validator=validate_result,
    )
    if repair_baseline is not None:
        evidence_ids = {item.evidence_id for item in evidence}
        return _merge_repaired_references(
            baseline=repair_baseline,
            candidate=result,
            block_ids=block_ids,
            evidence_ids=evidence_ids,
        )
    return result


def _policy_assessment_errors(
    *,
    result: ReviewerResult,
    reviewer_id: str,
    dimensions: dict[str, RubricDimension],
    hard_rule_ids: set[str],
    scoring_enabled: bool,
    block_pages: dict[str, int],
    evidence_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    assessments = result.criterion_assessments
    assessment_ids = [item.criterion_id for item in assessments]
    if len(assessment_ids) != len(set(assessment_ids)):
        errors.append("criterion assessment ids must be unique")
    unknown = sorted(set(assessment_ids) - set(dimensions))
    if unknown:
        errors.append(f"criterion assessments contain unassigned dimensions: {unknown}")
    # A policy-aware scored invocation must assess every assigned 0-4 criterion.
    policy_dimensions = {
        identifier
        for identifier, dimension in dimensions.items()
        if dimension.minimum_score == 0 and dimension.maximum_score == 4
    }
    if scoring_enabled and policy_dimensions and set(assessment_ids) != policy_dimensions:
        errors.append(
            "criterion assessments must cover every assigned 0-4 dimension; "
            f"missing={sorted(policy_dimensions - set(assessment_ids))}, "
            f"extra={sorted(set(assessment_ids) - policy_dimensions)}"
        )
    for assessment in assessments:
        dimension = dimensions.get(assessment.criterion_id)
        if assessment.reviewer_id != reviewer_id:
            errors.append(
                f"{assessment.criterion_id}: criterion reviewer_id does not match assignment"
            )
        if dimension is None:
            continue
        if abs(assessment.weight - dimension.weight) > 0.001:
            errors.append(
                f"{assessment.criterion_id}: assessment weight {assessment.weight} "
                f"does not match rubric weight {dimension.weight}"
            )
        errors.extend(
            _reference_list_errors(
                label=f"criterion {assessment.criterion_id}",
                paper_references=assessment.paper_evidence,
                external_references=assessment.external_evidence,
                block_pages=block_pages,
                evidence_ids=evidence_ids,
            )
        )
        policy = dimension.evidence_policy
        if policy.paper_evidence_required and not assessment.paper_evidence:
            errors.append(f"{assessment.criterion_id}: paper evidence is required")
        if policy.external_evidence_required and not assessment.external_evidence:
            errors.append(f"{assessment.criterion_id}: external evidence is required")
        if (
            len(assessment.paper_evidence) + len(assessment.external_evidence)
            < policy.minimum_references
        ):
            errors.append(
                f"{assessment.criterion_id}: evidence count is below the rubric minimum"
            )

    rule_ids = [assessment.rule_id for assessment in result.hard_rule_assessments]
    if len(rule_ids) != len(set(rule_ids)):
        errors.append("hard rule assessment ids must be unique")
    unknown_rules = sorted(set(rule_ids) - hard_rule_ids)
    if unknown_rules:
        errors.append(f"hard rule assessments contain unknown rules: {unknown_rules}")
    if hard_rule_ids and set(rule_ids) != hard_rule_ids:
        errors.append(
            "hard rule assessments must cover every assigned rule; "
            f"missing={sorted(hard_rule_ids - set(rule_ids))}"
        )
    prohibited = {HardRuleStatus.CONFIRMED, HardRuleStatus.DISMISSED}
    for rule_assessment in result.hard_rule_assessments:
        if rule_assessment.reviewer_id != reviewer_id:
            errors.append(
                f"{rule_assessment.rule_id}: hard rule reviewer_id does not match assignment"
            )
        if rule_assessment.status in prohibited:
            errors.append(
                f"{rule_assessment.rule_id}: AI reviewer cannot set human-confirmed status "
                f"{rule_assessment.status.value}"
            )
        errors.extend(
            _reference_list_errors(
                label=f"hard rule {rule_assessment.rule_id}",
                paper_references=rule_assessment.paper_evidence,
                external_references=rule_assessment.external_evidence,
                block_pages=block_pages,
                evidence_ids=evidence_ids,
            )
        )
    return errors


def _reference_list_errors(
    *,
    label: str,
    paper_references: list[EvidenceRef],
    external_references: list[EvidenceRef],
    block_pages: dict[str, int],
    evidence_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    for reference in paper_references:
        if reference.kind is not EvidenceKind.PAPER:
            errors.append(f"{label}: paper_evidence contains non-paper reference")
        elif reference.block_id not in block_pages:
            errors.append(f"{label}: unknown paper block {reference.block_id}")
        elif reference.page is not None and reference.page != block_pages[reference.block_id]:
            errors.append(
                f"{label}: paper evidence page {reference.page} does not match "
                f"block page {block_pages[reference.block_id]}"
            )
    for reference in external_references:
        if reference.kind is not EvidenceKind.EXTERNAL:
            errors.append(f"{label}: external_evidence contains non-external reference")
        elif reference.evidence_id not in evidence_ids:
            errors.append(f"{label}: unknown external evidence {reference.evidence_id}")
    return errors


def _repair_constraint_errors(
    *,
    baseline: ReviewerResult,
    candidate: ReviewerResult,
    block_ids: set[str],
    evidence_ids: set[str],
) -> list[str]:
    errors: list[str] = []
    baseline_by_id = {finding.finding_id: finding for finding in baseline.findings}
    candidate_by_id = {finding.finding_id: finding for finding in candidate.findings}
    baseline_ids = set(baseline_by_id)
    candidate_ids = set(candidate_by_id)
    if baseline_ids != candidate_ids:
        errors.append(
            "evidence repair must preserve the exact finding_id set; "
            f"missing={sorted(baseline_ids - candidate_ids)}, "
            f"added={sorted(candidate_ids - baseline_ids)}"
        )
    for finding_id in sorted(baseline_ids & candidate_ids):
        source = baseline_by_id[finding_id]
        repaired = candidate_by_id[finding_id]
        if repaired.reviewer_id != source.reviewer_id:
            errors.append(f"{finding_id}: evidence repair changed reviewer_id")
        if repaired.dimension_id != source.dimension_id:
            errors.append(f"{finding_id}: evidence repair changed dimension_id")
        if repaired.severity != source.severity:
            errors.append(f"{finding_id}: evidence repair changed severity")
        required_blocks = {
            reference.block_id
            for reference in source.paper_evidence
            if reference.kind is EvidenceKind.PAPER and reference.block_id in block_ids
        }
        candidate_blocks = {reference.block_id for reference in repaired.paper_evidence}
        missing_blocks = sorted(required_blocks - candidate_blocks)
        if missing_blocks:
            errors.append(
                f"{finding_id}: evidence repair removed valid paper blocks {missing_blocks}"
            )
        required_evidence = {
            reference.evidence_id
            for reference in source.external_evidence
            if reference.kind is EvidenceKind.EXTERNAL
            and reference.evidence_id in evidence_ids
        }
        candidate_evidence = {
            reference.evidence_id for reference in repaired.external_evidence
        }
        missing_evidence = sorted(required_evidence - candidate_evidence)
        if missing_evidence:
            errors.append(
                f"{finding_id}: evidence repair removed valid external evidence "
                f"{missing_evidence}"
            )
    return errors


def _merge_repaired_references(
    *,
    baseline: ReviewerResult,
    candidate: ReviewerResult,
    block_ids: set[str],
    evidence_ids: set[str],
) -> ReviewerResult:
    candidate_by_id = {finding.finding_id: finding for finding in candidate.findings}
    merged_findings: list[ReviewFinding] = []
    for source in baseline.findings:
        repaired = candidate_by_id[source.finding_id]
        paper_references = _merge_reference_lists(
            [
                ref
                for ref in source.paper_evidence
                if ref.kind is EvidenceKind.PAPER and ref.block_id in block_ids
            ],
            repaired.paper_evidence,
            key=lambda ref: ref.block_id or "",
        )
        external_references = _merge_reference_lists(
            [
                ref
                for ref in source.external_evidence
                if ref.kind is EvidenceKind.EXTERNAL and ref.evidence_id in evidence_ids
            ],
            repaired.external_evidence,
            key=lambda ref: ref.evidence_id,
        )
        merged_findings.append(
            source.model_copy(
                update={
                    "paper_evidence": paper_references,
                    "external_evidence": external_references,
                },
                deep=True,
            )
        )
    return baseline.model_copy(update={"findings": merged_findings}, deep=True)


def _merge_reference_lists(
    first: list[EvidenceRef],
    second: list[EvidenceRef],
    *,
    key: Callable[[EvidenceRef], str],
) -> list[EvidenceRef]:
    merged: list[EvidenceRef] = []
    seen: set[str] = set()
    for reference in [*first, *second]:
        identifier = key(reference)
        if identifier not in seen:
            seen.add(identifier)
            merged.append(reference.model_copy(deep=True))
    return merged


def _duplicate_finding_ids(result: ReviewerResult) -> list[str]:
    counts = Counter(finding.finding_id for finding in result.findings)
    return sorted(finding_id for finding_id, count in counts.items() if count > 1)


def _reviewer_template() -> Template:
    path = files("paper_reviewer.agents.prompts").joinpath("reviewer.txt")
    return cast(Template, Template(path.read_text(encoding="utf-8")))
