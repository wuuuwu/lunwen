from __future__ import annotations

from datetime import UTC, datetime

import pytest

from paper_reviewer.domain.review import (
    ExpertOpinion,
    HardRuleAssessment,
    HumanRuleDecision,
    PanelOutcome,
)
from paper_reviewer.validation.panel import decide_panel


def _opinion(expert_id: str, verdict: str, round_: str = "initial") -> ExpertOpinion:
    return ExpertOpinion.model_validate(
        {
            "expert_id": expert_id,
            "round": round_,
            "verdict": verdict,
            "rationale": "Independent full-paper assessment.",
            "finding_ids": ["major-1"] if verdict == "unqualified" else [],
        }
    )


@pytest.mark.parametrize(
    ("votes", "expected"),
    [
        (("qualified", "qualified", "qualified"), PanelOutcome.RISK_NOT_TRIGGERED),
        (("unqualified", "unqualified", "qualified"), PanelOutcome.RISK_TRIGGERED),
        (("unqualified", "unqualified", "unqualified"), PanelOutcome.RISK_TRIGGERED),
        (("qualified", "unable_to_assess", "qualified"), PanelOutcome.AWAITING_PANEL_REVIEW),
    ],
)
def test_initial_panel_decisions(votes: tuple[str, str, str], expected: PanelOutcome) -> None:
    decision = decide_panel(
        initial=[_opinion(f"expert-{index}", vote) for index, vote in enumerate(votes)]
    )
    assert decision.outcome is expected


def test_one_initial_unqualified_requires_two_supplemental_votes() -> None:
    initial = [
        _opinion("i-1", "unqualified"),
        _opinion("i-2", "qualified"),
        _opinion("i-3", "qualified"),
    ]
    assert decide_panel(initial=initial).outcome is PanelOutcome.SUPPLEMENTAL_REQUIRED
    passed = decide_panel(
        initial=initial,
        supplemental=[
            _opinion("s-1", "qualified", "supplemental"),
            _opinion("s-2", "qualified", "supplemental"),
        ],
    )
    assert passed.outcome is PanelOutcome.RISK_NOT_TRIGGERED
    failed = decide_panel(
        initial=initial,
        supplemental=[
            _opinion("s-1", "unqualified", "supplemental"),
            _opinion("s-2", "qualified", "supplemental"),
        ],
    )
    assert failed.outcome is PanelOutcome.RISK_TRIGGERED


def test_single_supplemental_unable_vote_immediately_requires_human_review() -> None:
    initial = [
        _opinion("i-1", "unqualified"),
        _opinion("i-2", "qualified"),
        _opinion("i-3", "qualified"),
    ]
    decision = decide_panel(
        initial=initial,
        supplemental=[_opinion("s-1", "unable_to_assess", "supplemental")],
    )
    assert decision.outcome is PanelOutcome.AWAITING_PANEL_REVIEW
    assert decision.decision_path == [
        "supplemental_unable_to_assess",
        "human_panel_review_required",
    ]


def test_incomplete_initial_panel_with_unable_vote_immediately_requires_human_review() -> None:
    decision = decide_panel(initial=[_opinion("i-1", "unable_to_assess")])
    assert decision.outcome is PanelOutcome.AWAITING_PANEL_REVIEW
    assert decision.decision_path == [
        "initial_unable_to_assess",
        "human_panel_review_required",
    ]


def test_confirmed_hard_rule_overrides_expert_votes() -> None:
    hard_rule = HardRuleAssessment(
        rule_id="integrity",
        status="suspected",
        rationale="Matching text requires human review.",
        external_evidence=[
            {
                "evidence_id": "source-1",
                "kind": "external",
                "title": "Potential source",
            }
        ],
    )
    decision = HumanRuleDecision(
        rule_id="integrity",
        decision="confirmed",
        reviewer="Teacher A",
        rationale="Confirmed after a documented offline check.",
        decided_at=datetime.now(UTC),
    )
    result = decide_panel(
        initial=[_opinion(f"expert-{index}", "qualified") for index in range(3)],
        hard_rules=[hard_rule],
        human_decisions=[decision],
    )
    assert result.outcome is PanelOutcome.RISK_TRIGGERED
    assert result.decisive_rule_ids == ["integrity"]


def test_unresolved_suspicion_pauses_before_panel() -> None:
    hard_rule = HardRuleAssessment(
        rule_id="integrity",
        status="suspected",
        rationale="Matching text requires human review.",
        external_evidence=[
            {"evidence_id": "source-1", "kind": "external", "title": "Source"}
        ],
    )
    assert (
        decide_panel(initial=[], hard_rules=[hard_rule]).outcome
        is PanelOutcome.AWAITING_HARD_RULE_CONFIRMATION
    )


def test_panel_rejects_non_independent_experts() -> None:
    with pytest.raises(ValueError, match="independent and unique"):
        decide_panel(
            initial=[
                _opinion("same", "qualified"),
                _opinion("same", "qualified"),
                _opinion("other", "qualified"),
            ]
        )
