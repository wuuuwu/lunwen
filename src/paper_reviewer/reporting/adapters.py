"""Adapters from persisted report models to :mod:`reporting.document`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from paper_reviewer.domain.provider import ProviderSnapshot
from paper_reviewer.domain.review import MetaReview
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.submission import SubmissionMetadata
from paper_reviewer.reporting.document import ReportDocument, ReportKind
from paper_reviewer.reporting.presentation import ReportPresentationProfile
from paper_reviewer.validation.audits import AuditReport


class LegacyReportAdapter:
    """Build a projection for v1/legacy ``MetaReview`` snapshots."""

    kind = ReportKind.LEGACY

    @classmethod
    def adapt(
        cls,
        rubric: RubricProfile,
        report: Any,
        audit: AuditReport,
        *,
        provider_snapshot: ProviderSnapshot | None = None,
        provider_ref: str | None = None,
        model: str | None = None,
        presentation_profile: ReportPresentationProfile = ReportPresentationProfile.LEGACY,
        submission_metadata: SubmissionMetadata | None = None,
        dimension_scores: Mapping[str, float] | None = None,
    ) -> ReportDocument:
        return ReportDocument(
            rubric=rubric,
            report=report,
            audit=audit,
            kind=cls.kind,
            presentation_profile=ReportPresentationProfile(presentation_profile),
            provider_snapshot=provider_snapshot,
            provider_ref=provider_ref,
            model=model,
            submission_metadata=submission_metadata,
            dimension_scores=(dict(dimension_scores) if dimension_scores is not None else None),
        )

    from_report = adapt


class EvaluationReportAdapter(LegacyReportAdapter):
    """Build a projection for v2 ``EvaluationReport`` snapshots."""

    kind = ReportKind.EVALUATION


def is_evaluation_report(value: Any) -> bool:
    """Detect an evaluation report without requiring a v2 import at runtime."""

    if isinstance(value, MetaReview):
        return False
    return any(
        _has_field(value, name)
        for name in (
            "diagnostic_score",
            "diagnostic_scores",
            "hard_rule_assessments",
            "human_rule_decisions",
            "initial_expert_opinions",
            "supplemental_expert_opinions",
            "panel_decision",
            "policy_context",
            "evaluation_mode",
        )
    )


def adapt_report(
    rubric: RubricProfile,
    report: Any,
    audit: AuditReport,
    *,
    provider_snapshot: ProviderSnapshot | None = None,
    provider_ref: str | None = None,
    model: str | None = None,
    presentation_profile: ReportPresentationProfile = ReportPresentationProfile.LEGACY,
    submission_metadata: SubmissionMetadata | None = None,
    dimension_scores: Mapping[str, float] | None = None,
) -> ReportDocument:
    """Select the compatible adapter while preserving legacy detection rules."""

    adapter = EvaluationReportAdapter if is_evaluation_report(report) else LegacyReportAdapter
    return adapter.adapt(
        rubric,
        report,
        audit,
        provider_snapshot=provider_snapshot,
        provider_ref=provider_ref,
        model=model,
        presentation_profile=presentation_profile,
        submission_metadata=submission_metadata,
        dimension_scores=dimension_scores,
    )


def _has_field(value: Any, name: str) -> bool:
    if isinstance(value, dict):
        return value.get(name) is not None
    return getattr(value, name, None) is not None
