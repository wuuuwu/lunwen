from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, Field

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind
from paper_reviewer.domain.review import (
    CriterionAssessment,
    EvaluationReport,
    ExpertOpinion,
    ExpertVerdict,
    HardRuleAssessment,
    HardRuleStatus,
    HumanRuleDecision,
    HumanRuleDecisionValue,
    MetaReview,
    ReviewerResult,
    ReviewFinding,
    Severity,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.validation.evidence_references import (
    EvidenceIndex,
    evidence_reference_errors,
)
from paper_reviewer.validation.panel import (
    build_human_review_summary,
    decide_expert_panel,
    decide_panel,
)


class AuditReport(BaseModel):
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    covered_dimensions: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def reviewer_reference_errors(
    *,
    result: ReviewerResult,
    block_ids: Collection[str],
    evidence_ids: Collection[str],
    block_by_id: dict[str, DocumentBlock] | None = None,
) -> list[str]:
    """Return deterministic reference errors for one reviewer result.

    ``block_ids`` keeps the lightweight legacy validation available to callers
    that only have identifiers.  Supplying ``block_by_id`` additionally checks
    page and verbatim quote integrity across findings, criterion assessments,
    and hard-rule assessments.
    """

    errors: list[str] = []
    known_evidence_ids = frozenset(evidence_ids)
    detailed_index = (
        EvidenceIndex(
            block_by_id=block_by_id,
            block_ids=frozenset(block_ids),
            block_pages={block_id: block.page for block_id, block in block_by_id.items()},
            evidence_ids=known_evidence_ids,
        )
        if block_by_id is not None
        else None
    )
    for finding in result.findings:
        if finding.reviewer_id != result.reviewer_id:
            errors.append(
                f"{finding.finding_id}: finding reviewer_id {finding.reviewer_id} "
                f"does not match result reviewer_id {result.reviewer_id}"
            )
        for reference in finding.paper_evidence:
            if reference.kind is not EvidenceKind.PAPER:
                errors.append(
                    f"{finding.finding_id}: paper_evidence contains non-paper reference "
                    f"{reference.evidence_id}"
                )
            if reference.block_id not in block_ids:
                errors.append(
                    f"{finding.finding_id}: unknown paper block {reference.block_id}"
                )
        for reference in finding.external_evidence:
            if reference.kind is not EvidenceKind.EXTERNAL:
                errors.append(
                    f"{finding.finding_id}: external_evidence contains non-external reference "
                    f"{reference.evidence_id}"
                )
            if reference.evidence_id not in evidence_ids:
                errors.append(
                    f"{finding.finding_id}: unknown external evidence {reference.evidence_id}"
                )
        is_major = finding.severity in {Severity.CRITICAL, Severity.MAJOR}
        if is_major and not finding.paper_evidence:
            errors.append(f"{finding.finding_id}: major finding lacks paper evidence")
    if detailed_index is not None:
        for finding in result.findings:
            errors.extend(
                evidence_reference_errors(
                    owner=f"finding {finding.finding_id}",
                    paper_evidence=finding.paper_evidence,
                    external_evidence=finding.external_evidence,
                    index=detailed_index,
                )
            )
        for criterion_assessment in result.criterion_assessments:
            errors.extend(
                evidence_reference_errors(
                    owner=f"criterion {criterion_assessment.criterion_id}",
                    paper_evidence=criterion_assessment.paper_evidence,
                    external_evidence=criterion_assessment.external_evidence,
                    index=detailed_index,
                )
            )
        for hard_rule_assessment in result.hard_rule_assessments:
            errors.extend(
                evidence_reference_errors(
                    owner=f"hard rule {hard_rule_assessment.rule_id}",
                    paper_evidence=hard_rule_assessment.paper_evidence,
                    external_evidence=hard_rule_assessment.external_evidence,
                    index=detailed_index,
                )
            )
    return errors


def audit_reviews(
    *,
    results: list[ReviewerResult],
    rubric: RubricProfile,
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
) -> AuditReport:
    report = AuditReport()
    index = EvidenceIndex.build(blocks=blocks, evidence=evidence)
    covered: set[str] = set()
    for result in results:
        report.errors.extend(
            reviewer_reference_errors(
                result=result,
                block_ids=index.block_ids,
                evidence_ids=index.evidence_ids,
                block_by_id=index.block_by_id,
            )
        )
        covered.update(result.dimension_scores)
        for finding in result.findings:
            covered.add(finding.dimension_id)
    required = {dimension.dimension_id for dimension in rubric.dimensions}
    missing = required - covered
    if missing:
        report.errors.append(f"rubric dimensions lack review coverage: {sorted(missing)}")
    report.covered_dimensions = sorted(covered)
    return report


def audit_meta_review(
    *,
    meta: MetaReview,
    source_results: list[ReviewerResult],
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
    scoring_enabled: bool,
) -> AuditReport:
    report = AuditReport()
    source_ids = {finding.finding_id for result in source_results for finding in result.findings}
    block_ids = {block.block_id for block in blocks}
    evidence_ids = {item.evidence_id for item in evidence}
    for finding in meta.findings:
        if finding.finding_id not in source_ids:
            report.errors.append(
                "meta review invented finding id not present in source reviews: "
                f"{finding.finding_id}"
            )
        for reference in finding.paper_evidence:
            if reference.kind is not EvidenceKind.PAPER:
                report.errors.append(
                    f"{finding.finding_id}: meta paper_evidence contains non-paper reference "
                    f"{reference.evidence_id}"
                )
            if reference.block_id not in block_ids:
                report.errors.append(
                    f"{finding.finding_id}: meta review references unknown block "
                    f"{reference.block_id}"
                )
        for reference in finding.external_evidence:
            if reference.kind is not EvidenceKind.EXTERNAL:
                report.errors.append(
                    f"{finding.finding_id}: meta external_evidence contains non-external "
                    f"reference {reference.evidence_id}"
                )
            if reference.evidence_id not in evidence_ids:
                report.errors.append(
                    f"{finding.finding_id}: meta review references unknown evidence "
                    f"{reference.evidence_id}"
                )
    if not scoring_enabled and (meta.total_score is not None or meta.verdict is not None):
        report.errors.append("unscored meta review contains a score or verdict")
    return report


def audit_criterion_assessments(
    *,
    assessments: list[CriterionAssessment],
    rubric: RubricProfile,
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
    reviewer_dimensions: dict[str, Collection[str]] | None = None,
) -> AuditReport:
    """Audit the complete discrete 0-4 diagnostic assessment set."""

    report = AuditReport()
    dimensions = {item.dimension_id: item for item in rubric.dimensions}
    index = EvidenceIndex.build(blocks=blocks, evidence=evidence)
    seen: set[str] = set()
    for assessment in assessments:
        criterion_id = assessment.criterion_id
        dimension = dimensions.get(criterion_id)
        if dimension is None:
            report.errors.append(f"unknown rubric criterion {criterion_id}")
            continue
        if criterion_id in seen:
            report.errors.append(f"criterion {criterion_id} has multiple assessments")
        seen.add(criterion_id)
        if abs(assessment.weight - dimension.weight) > 0.001:
            report.errors.append(
                f"criterion {criterion_id} weight {assessment.weight} does not match "
                f"rubric weight {dimension.weight}"
            )
        if reviewer_dimensions is not None:
            assigned = reviewer_dimensions.get(assessment.reviewer_id, ())
            if criterion_id not in assigned:
                report.errors.append(
                    f"criterion {criterion_id} is not assigned to reviewer "
                    f"{assessment.reviewer_id}"
                )
        report.errors.extend(
            evidence_reference_errors(
                owner=f"criterion {criterion_id}",
                paper_evidence=assessment.paper_evidence,
                external_evidence=assessment.external_evidence,
                index=index,
            )
        )
        policy = dimension.evidence_policy
        if policy.paper_evidence_required and not assessment.paper_evidence:
            report.errors.append(f"criterion {criterion_id} requires paper evidence")
        if policy.external_evidence_required and not assessment.external_evidence:
            report.errors.append(f"criterion {criterion_id} requires external evidence")
        reference_count = len(assessment.paper_evidence) + len(assessment.external_evidence)
        if reference_count < policy.minimum_references:
            report.errors.append(
                f"criterion {criterion_id} requires at least "
                f"{policy.minimum_references} evidence references"
            )
    missing = set(dimensions) - seen
    if missing:
        report.errors.append(f"rubric criteria lack diagnostic assessments: {sorted(missing)}")
    report.covered_dimensions = sorted(seen)
    return report


def audit_hard_rule_assessments(
    *,
    assessments: list[HardRuleAssessment],
    known_rule_ids: Collection[str],
    human_decisions: list[HumanRuleDecision],
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
) -> AuditReport:
    report = AuditReport()
    known = set(known_rule_ids)
    index = EvidenceIndex.build(blocks=blocks, evidence=evidence)
    assessment_ids: set[str] = set()
    for assessment in assessments:
        if not assessment.reviewer_id.strip():
            report.errors.append(
                f"hard rule {assessment.rule_id} is missing its reviewer_id"
            )
        if assessment.rule_id not in known:
            report.errors.append(f"unknown hard rule {assessment.rule_id}")
        if assessment.rule_id in assessment_ids:
            report.errors.append(f"hard rule {assessment.rule_id} has multiple assessments")
        assessment_ids.add(assessment.rule_id)
        report.errors.extend(
            evidence_reference_errors(
                owner=f"hard rule {assessment.rule_id}",
                paper_evidence=assessment.paper_evidence,
                external_evidence=assessment.external_evidence,
                index=index,
            )
        )

    decisions: dict[str, HumanRuleDecision] = {}
    for decision in human_decisions:
        if decision.rule_id not in assessment_ids:
            report.errors.append(
                f"human decision references unknown or unassessed hard rule {decision.rule_id}"
            )
        if decision.rule_id in decisions:
            report.errors.append(f"hard rule {decision.rule_id} has multiple human decisions")
        decisions[decision.rule_id] = decision
    missing = known - assessment_ids
    if missing:
        report.errors.append(f"hard rules lack assessments: {sorted(missing)}")
    for assessment in assessments:
        resolved_decision = decisions.get(assessment.rule_id)
        if assessment.status is HardRuleStatus.NOT_DETECTED and resolved_decision is not None:
            report.errors.append(
                f"hard rule {assessment.rule_id} was not suspected and cannot receive "
                "a human resolution"
            )
        if assessment.status is HardRuleStatus.CONFIRMED and (
            resolved_decision is None
            or resolved_decision.decision is not HumanRuleDecisionValue.CONFIRMED
        ):
            report.errors.append(
                f"hard rule {assessment.rule_id} cannot be confirmed without a matching "
                "human decision"
            )
        if assessment.status is HardRuleStatus.DISMISSED and (
            resolved_decision is None
            or resolved_decision.decision is not HumanRuleDecisionValue.DISMISSED
        ):
            report.errors.append(
                f"hard rule {assessment.rule_id} cannot be dismissed without a matching "
                "human decision"
            )
    return report


def audit_expert_opinions(
    *,
    opinions: list[ExpertOpinion],
    findings: list[ReviewFinding],
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
) -> AuditReport:
    """Reject invented findings and unsupported unqualified opinions."""

    report = AuditReport()
    finding_by_id = {item.finding_id: item for item in findings}
    index = EvidenceIndex.build(blocks=blocks, evidence=evidence)
    for opinion in opinions:
        referenced: list[ReviewFinding] = []
        for finding_id in opinion.finding_ids:
            finding = finding_by_id.get(finding_id)
            if finding is None:
                report.errors.append(
                    f"expert {opinion.expert_id} references unknown finding {finding_id}"
                )
            else:
                referenced.append(finding)
        if opinion.verdict is not ExpertVerdict.UNQUALIFIED:
            continue
        major = [
            item for item in referenced if item.severity in {Severity.CRITICAL, Severity.MAJOR}
        ]
        if not major:
            report.errors.append(
                f"unqualified expert {opinion.expert_id} does not cite a major finding"
            )
            continue
        evidence_is_valid = False
        for finding in major:
            reference_errors = evidence_reference_errors(
                owner=f"finding {finding.finding_id}",
                paper_evidence=finding.paper_evidence,
                external_evidence=finding.external_evidence,
                index=index,
            )
            report.errors.extend(reference_errors)
            if finding.paper_evidence and not any(
                error.startswith(f"finding {finding.finding_id}: paper")
                for error in reference_errors
            ):
                evidence_is_valid = True
        if not evidence_is_valid:
            report.errors.append(
                f"unqualified expert {opinion.expert_id} lacks valid paper evidence"
            )
    return report


def audit_evaluation_report(
    *,
    report: EvaluationReport,
) -> AuditReport:
    """Ensure synthesis did not replace deterministic decisions or votes."""

    audit = AuditReport()
    if report.meta_review.total_score is not None or report.meta_review.verdict is not None:
        audit.errors.append("meta reviewer modified a deterministic score or verdict")
    initial = [item for item in report.expert_opinions if item.round == "initial"]
    supplemental = [item for item in report.expert_opinions if item.round == "supplemental"]
    try:
        expected_expert = decide_expert_panel(
            initial=initial,
            supplemental=supplemental,
        )
        expected = decide_panel(
            initial=initial,
            supplemental=supplemental,
            hard_rules=report.hard_rule_assessments,
            human_decisions=report.human_rule_decisions,
            human_panel_decision=report.human_panel_decision,
        )
        expected_summary = build_human_review_summary(
            hard_rules=report.hard_rule_assessments,
            human_decisions=report.human_rule_decisions,
            expert_panel_decision=expected_expert,
            human_panel_decision=report.human_panel_decision,
        )
    except ValueError as exc:
        audit.errors.append(f"invalid deterministic panel inputs: {exc}")
        return audit
    if (
        report.expert_panel_decision is not None
        and report.expert_panel_decision != expected_expert
    ):
        audit.errors.append("stored expert panel decision does not match 3+2 policy")
    if report.panel_decision != expected:
        audit.errors.append("stored panel decision does not match deterministic panel policy")
    if report.human_review_summary != expected_summary:
        audit.errors.append("stored human review summary does not match pending decisions")
    return audit
