from __future__ import annotations

from paper_reviewer.domain.run import RunStatus


class InvalidTransitionError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.INGESTING, RunStatus.CANCELLED, RunStatus.FATAL_FAILURE},
    RunStatus.INGESTING: {
        RunStatus.INGESTED,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.INGESTED: {
        RunStatus.BUILDING_EVIDENCE,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.CANCELLED,
    },
    RunStatus.BUILDING_EVIDENCE: {
        RunStatus.EVIDENCE_READY,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.EVIDENCE_READY: {
        RunStatus.REVIEWING,
        RunStatus.SCORING,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.CANCELLED,
    },
    RunStatus.REVIEWING: {
        RunStatus.AUDITING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.SCORING: {
        RunStatus.AUDITING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.AUDITING: {
        RunStatus.META_REVIEWING,
        RunStatus.AWAITING_HARD_RULE_CONFIRMATION,
        RunStatus.PANEL_REVIEWING,
        RunStatus.SYNTHESIZING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.AWAITING_HARD_RULE_CONFIRMATION: {
        RunStatus.PANEL_REVIEWING,
        RunStatus.SYNTHESIZING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.PANEL_REVIEWING: {
        RunStatus.SUPPLEMENTAL_REVIEWING,
        RunStatus.AWAITING_PANEL_REVIEW,
        RunStatus.SYNTHESIZING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.SUPPLEMENTAL_REVIEWING: {
        RunStatus.AWAITING_PANEL_REVIEW,
        RunStatus.SYNTHESIZING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.AWAITING_PANEL_REVIEW: {
        RunStatus.PANEL_REVIEWING,
        RunStatus.SUPPLEMENTAL_REVIEWING,
        RunStatus.SYNTHESIZING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.SYNTHESIZING: {
        RunStatus.VALIDATING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.META_REVIEWING: {
        RunStatus.VALIDATING,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.VALIDATING: {
        RunStatus.REPORTED,
        RunStatus.CANCELLED,
        RunStatus.RETRYABLE_FAILURE,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.RETRYABLE_FAILURE: {
        RunStatus.INGESTING,
        RunStatus.BUILDING_EVIDENCE,
        RunStatus.REVIEWING,
        RunStatus.SCORING,
        RunStatus.AUDITING,
        RunStatus.AWAITING_HARD_RULE_CONFIRMATION,
        RunStatus.PANEL_REVIEWING,
        RunStatus.SUPPLEMENTAL_REVIEWING,
        RunStatus.AWAITING_PANEL_REVIEW,
        RunStatus.SYNTHESIZING,
        RunStatus.META_REVIEWING,
        RunStatus.VALIDATING,
        RunStatus.CANCELLED,
        RunStatus.FATAL_FAILURE,
    },
    RunStatus.REPORTED: set(),
    RunStatus.FATAL_FAILURE: set(),
    RunStatus.CANCELLED: {
        RunStatus.INGESTING,
        RunStatus.BUILDING_EVIDENCE,
        RunStatus.REVIEWING,
        RunStatus.SCORING,
        RunStatus.AUDITING,
        RunStatus.PANEL_REVIEWING,
        RunStatus.SUPPLEMENTAL_REVIEWING,
        RunStatus.SYNTHESIZING,
        RunStatus.META_REVIEWING,
        RunStatus.VALIDATING,
    },
}


def transition(current: RunStatus, target: RunStatus) -> RunStatus:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"invalid run transition: {current} -> {target}")
    return target
