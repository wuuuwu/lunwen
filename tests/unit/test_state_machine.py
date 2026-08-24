from __future__ import annotations

from itertools import pairwise

import pytest

from paper_reviewer.application.state_machine import InvalidTransitionError, transition
from paper_reviewer.domain.run import RunStatus


def test_happy_path_transition() -> None:
    assert transition(RunStatus.CREATED, RunStatus.INGESTING) is RunStatus.INGESTING


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.CREATED, RunStatus.REPORTED)


def test_retryable_failure_can_resume_reviewing() -> None:
    assert transition(RunStatus.RETRYABLE_FAILURE, RunStatus.REVIEWING) is RunStatus.REVIEWING


def test_dual_advisory_happy_path_transitions() -> None:
    states = [
        RunStatus.EVIDENCE_READY,
        RunStatus.SCORING,
        RunStatus.AUDITING,
        RunStatus.PANEL_REVIEWING,
        RunStatus.SUPPLEMENTAL_REVIEWING,
        RunStatus.SYNTHESIZING,
        RunStatus.VALIDATING,
        RunStatus.REPORTED,
    ]
    for current, target in pairwise(states):
        assert transition(current, target) is target


def test_hard_rule_gate_cannot_skip_directly_to_report() -> None:
    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.AWAITING_HARD_RULE_CONFIRMATION, RunStatus.REPORTED)
