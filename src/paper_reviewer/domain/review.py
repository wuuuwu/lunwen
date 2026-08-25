from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from paper_reviewer.domain.evidence import EvidenceRef


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    SUGGESTION = "suggestion"


class ScoreProposal(BaseModel):
    score: float
    explanation: str


class ReviewFinding(BaseModel):
    finding_id: str
    reviewer_id: str
    dimension_id: str
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    claim: str
    rationale: str
    paper_evidence: list[EvidenceRef] = Field(default_factory=list)
    external_evidence: list[EvidenceRef] = Field(default_factory=list)
    recommendation: str
    needs_human_check: bool = False
    score_proposal: ScoreProposal | None = None

    @model_validator(mode="after")
    def require_evidence(self) -> ReviewFinding:
        if self.severity in {Severity.CRITICAL, Severity.MAJOR} and not self.paper_evidence:
            raise ValueError("critical and major findings require paper evidence")
        return self


class ReviewerResult(BaseModel):
    reviewer_id: str
    summary: str
    findings: list[ReviewFinding]
    dimension_scores: dict[str, ScoreProposal] = Field(default_factory=dict)
    criterion_assessments: list[CriterionAssessment] = Field(default_factory=list)
    hard_rule_assessments: list[HardRuleAssessment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class MetaReview(BaseModel):
    run_id: str
    overall_summary: str
    findings: list[ReviewFinding]
    disagreements: list[str] = Field(default_factory=list)
    human_checks: list[str] = Field(default_factory=list)
    total_score: float | None = None
    verdict: str | None = None


class PolicyContext(BaseModel):
    """Traceable policy metadata captured with an evaluation run."""

    source: str
    document_number: str
    effective_date: date
    source_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class CriterionAssessment(BaseModel):
    """One reviewer's evidence-grounded assessment of one rubric criterion."""

    criterion_id: str
    reviewer_id: str
    rating: int = Field(strict=True, ge=0, le=4)
    weight: float = Field(gt=0, le=100)
    rationale: str = Field(min_length=1)
    paper_evidence: list[EvidenceRef] = Field(default_factory=list)
    external_evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @property
    def weighted_contribution(self) -> float:
        return self.rating / 4 * self.weight


class HardRuleStatus(StrEnum):
    NOT_DETECTED = "not_detected"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    NOT_ASSESSABLE = "not_assessable"


class HardRuleAssessment(BaseModel):
    rule_id: str
    reviewer_id: str = ""
    status: HardRuleStatus
    rationale: str = Field(min_length=1)
    paper_evidence: list[EvidenceRef] = Field(default_factory=list)
    external_evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def suspected_rule_requires_evidence(self) -> HardRuleAssessment:
        if self.status is HardRuleStatus.SUSPECTED and not (
            self.paper_evidence or self.external_evidence
        ):
            raise ValueError("suspected hard rule requires evidence")
        return self


class HumanRuleDecisionValue(StrEnum):
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class HumanRuleDecision(BaseModel):
    rule_id: str
    decision: HumanRuleDecisionValue
    reviewer: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    decided_at: datetime


class ExpertVerdict(StrEnum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    UNABLE_TO_ASSESS = "unable_to_assess"


class ExpertOpinion(BaseModel):
    expert_id: str
    round: Literal["initial", "supplemental"]
    verdict: ExpertVerdict
    rationale: str = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def unqualified_requires_findings(self) -> ExpertOpinion:
        if self.verdict is ExpertVerdict.UNQUALIFIED and not self.finding_ids:
            raise ValueError("unqualified expert opinion requires finding_ids")
        if len(self.finding_ids) != len(set(self.finding_ids)):
            raise ValueError("expert opinion finding_ids must be unique")
        return self


class DiagnosticScore(BaseModel):
    assessments: list[CriterionAssessment]
    group_scores: dict[str, float] = Field(default_factory=dict)
    total_score: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def total_matches_contributions(self) -> DiagnosticScore:
        criterion_ids = [item.criterion_id for item in self.assessments]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("diagnostic assessments must have unique criterion ids")
        total_weight = sum(item.weight for item in self.assessments)
        if abs(total_weight - 100) > 0.01:
            raise ValueError(f"diagnostic assessment weights must total 100, got {total_weight}")
        expected = sum(item.weighted_contribution for item in self.assessments)
        if abs(expected - self.total_score) > 0.01:
            raise ValueError(
                f"diagnostic total_score {self.total_score} does not match "
                f"weighted contributions {expected}"
            )
        return self


class PanelOutcome(StrEnum):
    RISK_TRIGGERED = "risk_triggered"
    RISK_NOT_TRIGGERED = "risk_not_triggered"
    AWAITING_HARD_RULE_CONFIRMATION = "awaiting_hard_rule_confirmation"
    SUPPLEMENTAL_REQUIRED = "supplemental_required"
    AWAITING_PANEL_REVIEW = "awaiting_panel_review"


class PanelDecision(BaseModel):
    outcome: PanelOutcome
    reason: str
    initial_unqualified: int = Field(default=0, ge=0, le=3)
    supplemental_unqualified: int = Field(default=0, ge=0, le=2)
    decisive_rule_ids: list[str] = Field(default_factory=list)
    decision_path: list[str] = Field(default_factory=list)


class HumanPanelDecision(BaseModel):
    """A human panel's direct resolution when AI experts cannot assess."""

    outcome: Literal["risk_triggered", "risk_not_triggered"]
    reviewer: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    decided_at: datetime


class HumanReviewSummary(BaseModel):
    """Pending post-report review work, independent from model execution."""

    pending_hard_rule_ids: list[str] = Field(default_factory=list)
    panel_review_required: bool = False
    pending_count: int = Field(default=0, ge=0)
    complete: bool = True

    @model_validator(mode="after")
    def normalize_summary(self) -> HumanReviewSummary:
        unique_rule_ids = list(dict.fromkeys(self.pending_hard_rule_ids))
        count = len(unique_rule_ids) + int(self.panel_review_required)
        self.pending_hard_rule_ids = unique_rule_ids
        self.pending_count = count
        self.complete = count == 0
        return self


class EvaluationReport(BaseModel):
    """New dual-advisory report; legacy MetaReview remains independently readable."""

    run_id: str
    policy_context: PolicyContext
    diagnostic_score: DiagnosticScore
    hard_rule_assessments: list[HardRuleAssessment] = Field(default_factory=list)
    human_rule_decisions: list[HumanRuleDecision] = Field(default_factory=list)
    expert_opinions: list[ExpertOpinion] = Field(default_factory=list)
    expert_panel_decision: PanelDecision | None = None
    human_panel_decision: HumanPanelDecision | None = None
    human_review_summary: HumanReviewSummary = Field(default_factory=HumanReviewSummary)
    panel_decision: PanelDecision
    meta_review: MetaReview
    experimental: bool = True
    disclaimers: list[str] = Field(
        default_factory=lambda: [
            "本结果不是浙江省教育厅正式抽检结论。",
            "百分制和五级锚点为本项目自定义诊断规则。",
            "学术不端检测报告未由系统自动读取。",
            "模型置信度是未经校准的自评，不作为统计概率。",
        ]
    )

    @model_validator(mode="after")
    def meta_is_summary_only(self) -> EvaluationReport:
        if self.meta_review.run_id != self.run_id:
            raise ValueError("meta review run_id does not match evaluation report run_id")
        if self.meta_review.total_score is not None or self.meta_review.verdict is not None:
            raise ValueError("meta reviewer cannot set diagnostic score or panel verdict")
        return self


# ReviewerResult intentionally precedes the v2 assessment models so its legacy
# location and shape remain easy to inspect. Resolve its two optional v2 fields now.
ReviewerResult.model_rebuild()
