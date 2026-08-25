from __future__ import annotations

from collections.abc import Sequence

from paper_reviewer.domain.review import (
    ExpertOpinion,
    ExpertVerdict,
    HardRuleAssessment,
    HardRuleStatus,
    HumanPanelDecision,
    HumanReviewSummary,
    HumanRuleDecision,
    HumanRuleDecisionValue,
    PanelDecision,
    PanelOutcome,
)


def decide_panel(
    *,
    initial: Sequence[ExpertOpinion],
    supplemental: Sequence[ExpertOpinion] = (),
    hard_rules: Sequence[HardRuleAssessment] = (),
    human_decisions: Sequence[HumanRuleDecision] = (),
    human_panel_decision: HumanPanelDecision | None = None,
) -> PanelDecision:
    """Apply the deterministic hard-rule and 3+2 panel policy.

    Partial panel input produces a resumable pending decision. Invalid round labels,
    duplicate experts, excess votes, or decisions for unknown rules are rejected.
    """

    expert_decision = decide_expert_panel(
        initial=initial,
        supplemental=supplemental,
        human_panel_decision=human_panel_decision,
    )
    rule_ids = {item.rule_id for item in hard_rules}
    decisions = _decision_map(human_decisions, rule_ids)

    confirmed = {
        rule_id
        for rule_id, decision in decisions.items()
        if decision.decision is HumanRuleDecisionValue.CONFIRMED
    }
    if confirmed:
        return PanelDecision(
            outcome=PanelOutcome.RISK_TRIGGERED,
            reason="A hard rule was confirmed by a human reviewer.",
            decisive_rule_ids=sorted(confirmed),
            decision_path=["hard_rule_confirmed", "risk_triggered"],
        )

    unresolved = {
        item.rule_id
        for item in hard_rules
        if item.status
        in {
            HardRuleStatus.SUSPECTED,
            HardRuleStatus.CONFIRMED,
            HardRuleStatus.DISMISSED,
            HardRuleStatus.NOT_ASSESSABLE,
        }
        and item.rule_id not in decisions
    }
    if unresolved:
        return PanelDecision(
            outcome=PanelOutcome.AWAITING_HARD_RULE_CONFIRMATION,
            reason="One or more hard rules require a human decision.",
            decisive_rule_ids=sorted(unresolved),
            decision_path=["hard_rule_human_confirmation_required"],
        )

    return expert_decision


def decide_expert_panel(
    *,
    initial: Sequence[ExpertOpinion],
    supplemental: Sequence[ExpertOpinion] = (),
    human_panel_decision: HumanPanelDecision | None = None,
) -> PanelDecision:
    """Apply only the independent 3+2 panel policy.

    A human panel decision is accepted only when at least one AI expert was
    unable to assess.  It resolves the complete panel rather than fabricating
    a replacement AI vote.
    """

    _validate_panel_members(initial, supplemental)
    unable_to_assess = any(
        item.verdict is ExpertVerdict.UNABLE_TO_ASSESS
        for item in (*initial, *supplemental)
    )
    if human_panel_decision is not None:
        if not unable_to_assess:
            raise ValueError("human panel decision is not allowed without unable_to_assess")
        return PanelDecision(
            outcome=PanelOutcome(human_panel_decision.outcome),
            reason="A human panel resolved the AI experts' inability to assess.",
            decision_path=[
                "expert_panel_unable_to_assess",
                f"human_panel_{human_panel_decision.outcome}",
            ],
        )

    if any(item.verdict is ExpertVerdict.UNABLE_TO_ASSESS for item in initial):
        if supplemental:
            raise ValueError("supplemental opinions are not allowed after unable_to_assess")
        return PanelDecision(
            outcome=PanelOutcome.AWAITING_PANEL_REVIEW,
            reason="An initial expert was unable to assess the paper.",
            decision_path=["initial_unable_to_assess", "human_panel_review_required"],
        )
    if len(initial) < 3:
        if supplemental:
            raise ValueError("supplemental opinions cannot precede a complete initial panel")
        return PanelDecision(
            outcome=PanelOutcome.AWAITING_PANEL_REVIEW,
            reason="Three independent initial opinions are required.",
            decision_path=["initial_panel_incomplete"],
        )

    initial_unqualified = sum(
        item.verdict is ExpertVerdict.UNQUALIFIED for item in initial
    )
    if initial_unqualified >= 2:
        if supplemental:
            raise ValueError(
                "supplemental opinions are only allowed after exactly one initial "
                "unqualified vote"
            )
        return PanelDecision(
            outcome=PanelOutcome.RISK_TRIGGERED,
            reason="At least two of three initial experts found the paper unqualified.",
            initial_unqualified=initial_unqualified,
            decision_path=["initial_unqualified_at_least_two", "risk_triggered"],
        )
    if initial_unqualified == 0:
        if supplemental:
            raise ValueError(
                "supplemental opinions are only allowed after exactly one initial "
                "unqualified vote"
            )
        return PanelDecision(
            outcome=PanelOutcome.RISK_NOT_TRIGGERED,
            reason="All three initial experts found the paper qualified.",
            decision_path=["initial_unqualified_zero", "risk_not_triggered"],
        )

    if any(item.verdict is ExpertVerdict.UNABLE_TO_ASSESS for item in supplemental):
        return PanelDecision(
            outcome=PanelOutcome.AWAITING_PANEL_REVIEW,
            reason="A supplemental expert was unable to assess the paper.",
            initial_unqualified=1,
            decision_path=["supplemental_unable_to_assess", "human_panel_review_required"],
        )
    if len(supplemental) < 2:
        return PanelDecision(
            outcome=PanelOutcome.SUPPLEMENTAL_REQUIRED,
            reason=(
                "Exactly one initial expert voted unqualified; "
                "two supplemental opinions are required."
            ),
            initial_unqualified=1,
            decision_path=["initial_unqualified_one", "supplemental_required"],
        )
    supplemental_unqualified = sum(
        item.verdict is ExpertVerdict.UNQUALIFIED for item in supplemental
    )
    return PanelDecision(
        outcome=(
            PanelOutcome.RISK_TRIGGERED
            if supplemental_unqualified >= 1
            else PanelOutcome.RISK_NOT_TRIGGERED
        ),
        reason=(
            "At least one supplemental expert found the paper unqualified."
            if supplemental_unqualified >= 1
            else "Both supplemental experts found the paper qualified."
        ),
        initial_unqualified=1,
        supplemental_unqualified=supplemental_unqualified,
        decision_path=[
            "initial_unqualified_one",
            (
                "supplemental_unqualified_at_least_one"
                if supplemental_unqualified >= 1
                else "supplemental_unqualified_zero"
            ),
            (
                "risk_triggered"
                if supplemental_unqualified >= 1
                else "risk_not_triggered"
            ),
        ],
    )


def build_human_review_summary(
    *,
    hard_rules: Sequence[HardRuleAssessment],
    human_decisions: Sequence[HumanRuleDecision],
    expert_panel_decision: PanelDecision,
    human_panel_decision: HumanPanelDecision | None = None,
) -> HumanReviewSummary:
    decided_rule_ids = {item.rule_id for item in human_decisions}
    pending_rule_ids = sorted(
        item.rule_id
        for item in hard_rules
        if item.status in {HardRuleStatus.SUSPECTED, HardRuleStatus.NOT_ASSESSABLE}
        and item.rule_id not in decided_rule_ids
    )
    return HumanReviewSummary(
        pending_hard_rule_ids=pending_rule_ids,
        panel_review_required=(
            expert_panel_decision.outcome is PanelOutcome.AWAITING_PANEL_REVIEW
            and human_panel_decision is None
        ),
    )


def _validate_panel_members(
    initial: Sequence[ExpertOpinion], supplemental: Sequence[ExpertOpinion]
) -> None:
    if len(initial) > 3:
        raise ValueError("initial panel cannot contain more than three opinions")
    if len(supplemental) > 2:
        raise ValueError("supplemental panel cannot contain more than two opinions")
    if any(item.round != "initial" for item in initial):
        raise ValueError("initial panel contains an opinion with the wrong round")
    if any(item.round != "supplemental" for item in supplemental):
        raise ValueError("supplemental panel contains an opinion with the wrong round")
    expert_ids = [item.expert_id for item in (*initial, *supplemental)]
    if len(expert_ids) != len(set(expert_ids)):
        raise ValueError("panel experts must be independent and unique")


def _decision_map(
    human_decisions: Sequence[HumanRuleDecision], rule_ids: set[str]
) -> dict[str, HumanRuleDecision]:
    decisions: dict[str, HumanRuleDecision] = {}
    for decision in human_decisions:
        if decision.rule_id not in rule_ids:
            raise ValueError(f"human decision references unknown hard rule {decision.rule_id}")
        if decision.rule_id in decisions:
            raise ValueError(f"hard rule {decision.rule_id} has multiple human decisions")
        decisions[decision.rule_id] = decision
    return decisions
