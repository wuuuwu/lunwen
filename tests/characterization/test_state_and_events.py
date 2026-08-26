from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_reviewer.application.run_events import RunEventView, project_run_event
from paper_reviewer.application.service import _load_trace_events
from paper_reviewer.application.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    transition,
)
from paper_reviewer.domain.run import RunStatus


def test_transition_table_covers_every_status_and_keeps_legacy_review_gates() -> None:
    """The state graph is persisted data, so refactors must not drop old states."""

    assert set(ALLOWED_TRANSITIONS) == set(RunStatus)

    expected_transitions = (
        (RunStatus.CREATED, RunStatus.INGESTING),
        (RunStatus.INGESTING, RunStatus.INGESTED),
        (RunStatus.EVIDENCE_READY, RunStatus.REVIEWING),
        (RunStatus.EVIDENCE_READY, RunStatus.SCORING),
        (RunStatus.AWAITING_HARD_RULE_CONFIRMATION, RunStatus.PANEL_REVIEWING),
        (RunStatus.AWAITING_PANEL_REVIEW, RunStatus.SUPPLEMENTAL_REVIEWING),
        (RunStatus.VALIDATING, RunStatus.REPORTED_PENDING_HUMAN_REVIEW),
        (RunStatus.VALIDATING, RunStatus.REPORTED),
    )
    for current, target in expected_transitions:
        assert transition(current, target) is target

    with pytest.raises(InvalidTransitionError):
        transition(RunStatus.REPORTED, RunStatus.REVIEWING)


def test_trace_projection_preserves_status_stage_message_and_order(tmp_path: Path) -> None:
    """Trace reload is the public task-detail event projection after a restart."""

    trace = tmp_path / "trace.jsonl"
    rows = [
        {"event_type": "run_created", "payload": {"status": "created"}},
        {
            "event_type": "evidence_collection_started",
            "payload": {"status": "building_evidence"},
        },
        {
            "event_type": "hard_rule_confirmation_required",
            "payload": {"status": "awaiting_hard_rule_confirmation"},
        },
        {
            "event_type": "panel_expert_completed",
            "payload": {"status": "panel_reviewing"},
        },
        {"event_type": "future_event_name", "payload": {"status": "future_status"}},
        {"event_type": "malformed_payload", "payload": ["ignored"]},
    ]
    trace.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        "not-json\n",
        encoding="utf-8",
    )

    events = _load_trace_events(trace, "characterization-run")

    assert [event.event_type for event in events] == [row["event_type"] for row in rows]
    assert [event.status for event in events[:4]] == [
        RunStatus.CREATED,
        RunStatus.BUILDING_EVIDENCE,
        RunStatus.AWAITING_HARD_RULE_CONFIRMATION,
        RunStatus.PANEL_REVIEWING,
    ]
    assert [event.stage for event in events[:4]] == [
        None,
        "evidence",
        "hard_rule_gate",
        "panel",
    ]
    assert events[0].message == "已创建评测任务"
    assert events[1].message == "正在收集外部学术证据"
    assert events[2].message == "否决项需要人工确认"
    assert events[3].message == "独立专家评议完成"
    assert events[4].status is None
    assert events[4].stage is None
    assert events[4].message == "future event name"
    assert events[5].status is None
    assert events[5].stage is None
    assert events[5].message == "malformed payload"


def test_live_and_trace_projection_preserve_historical_repair_text_difference() -> None:
    live = project_run_event(
        run_id="run",
        event_type="review_reference_repair_started",
        payload={},
    )
    trace = project_run_event(
        run_id="run",
        event_type="review_reference_repair_started",
        payload={},
        view=RunEventView.TRACE,
    )

    assert live.message == "正在修复 Reviewer 的无效证据引用"
    assert trace.message == "review reference repair started"
    assert live.stage == trace.stage == "reviews"


@pytest.mark.parametrize(
    ("event_type", "expected_message"),
    [
        ("submission_metadata_started", "正在提取姓名、学号、专业和论文题目"),
        ("submission_metadata_completed", "学生与论文信息提取完成"),
    ],
)
def test_course_metadata_events_have_a_localized_stage_and_message(
    event_type: str,
    expected_message: str,
) -> None:
    event = project_run_event(
        run_id="course-run",
        event_type=event_type,
        payload={"status": "ingested"},
    )

    assert event.stage == "metadata"
    assert event.message == expected_message
