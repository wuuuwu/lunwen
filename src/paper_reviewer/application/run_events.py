from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from paper_reviewer.application.models import RunEvent
from paper_reviewer.domain.run import RunStatus


class RunEventView(StrEnum):
    """The projection in which an event is shown.

    Trace replay intentionally retains a small historical difference from the
    live event stream.  Keeping that difference explicit prevents a refactor
    from silently changing text shown for existing runs.
    """

    LIVE = "live"
    TRACE = "trace"


@dataclass(frozen=True, slots=True)
class RunEventDescriptor:
    stage: str | None
    message: str
    trace_message: str | None = None

    def message_for(self, view: RunEventView) -> str:
        if view is RunEventView.TRACE and self.trace_message is not None:
            return self.trace_message
        return self.message


_STAGE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ingest", "ingest"),
    ("submission_metadata", "metadata"),
    ("evidence", "evidence"),
    ("reference", "evidence"),
    ("scoring", "scoring"),
    ("review", "reviews"),
    ("audit", "audit"),
    ("hard_rule", "hard_rule_gate"),
    ("panel", "panel"),
    ("supplemental", "supplemental"),
    ("meta", "meta"),
    ("report", "report"),
)


def stage_from_event(event_type: str) -> str | None:
    for prefix, stage in _STAGE_PREFIXES:
        if event_type.startswith(prefix):
            return stage
    return None


def _descriptor(
    event_type: str,
    message: str,
    *,
    trace_message: str | None = None,
) -> RunEventDescriptor:
    return RunEventDescriptor(
        stage=stage_from_event(event_type),
        message=message,
        trace_message=trace_message,
    )


RUN_EVENT_CATALOG: Mapping[str, RunEventDescriptor] = {
    "run_created": _descriptor("run_created", "已创建评测任务"),
    "ingest_started": _descriptor("ingest_started", "正在解析论文"),
    "ingest_completed": _descriptor("ingest_completed", "论文解析完成"),
    "submission_metadata_started": _descriptor(
        "submission_metadata_started", "正在提取姓名、学号、专业和论文题目"
    ),
    "submission_metadata_completed": _descriptor(
        "submission_metadata_completed", "学生与论文信息提取完成"
    ),
    "evidence_collection_started": _descriptor(
        "evidence_collection_started", "正在收集外部学术证据"
    ),
    "evidence_completed": _descriptor("evidence_completed", "外部证据收集完成"),
    "reference_check_started": _descriptor("reference_check_started", "正在自动核验参考文献"),
    "reference_check_completed": _descriptor("reference_check_completed", "参考文献自动核验完成"),
    "scoring_started": _descriptor(
        "scoring_started", "专业化 Reviewer 正在执行九项诊断评分"
    ),
    "scoring_completed": _descriptor("scoring_completed", "九项诊断评分完成"),
    "reviews_started": _descriptor("reviews_started", "多位 Reviewer 正在评测"),
    "reviews_completed": _descriptor("reviews_completed", "Reviewer 评测完成"),
    "review_reference_repair_started": _descriptor(
        "review_reference_repair_started",
        "正在修复 Reviewer 的无效证据引用",
        trace_message="review reference repair started",
    ),
    "review_reference_repair_completed": _descriptor(
        "review_reference_repair_completed",
        "Reviewer 证据引用修复完成",
        trace_message="review reference repair completed",
    ),
    "audit_started": _descriptor("audit_started", "正在执行确定性审计"),
    "audit_completed": _descriptor("audit_completed", "确定性审计完成"),
    "hard_rule_confirmation_required": _descriptor(
        "hard_rule_confirmation_required", "否决项需要人工确认"
    ),
    "panel_review_started": _descriptor("panel_review_started", "三名独立专家正在初评"),
    "panel_expert_completed": _descriptor("panel_expert_completed", "独立专家评议完成"),
    "supplemental_review_started": _descriptor(
        "supplemental_review_started", "两名独立专家正在复评"
    ),
    "panel_human_review_required": _descriptor(
        "panel_human_review_required", "专家无法判断，需要人工面板复核"
    ),
    "panel_completed": _descriptor("panel_completed", "独立专家面板评议完成"),
    "meta_review_started": _descriptor("meta_review_started", "正在汇总 Meta Review"),
    "meta_completed": _descriptor("meta_completed", "Meta Review 完成"),
    "report_validation_started": _descriptor(
        "report_validation_started", "正在验证并生成报告"
    ),
    "report_completed": _descriptor("report_completed", "评测报告已生成"),
    "stage_failed": _descriptor("stage_failed", "评测任务失败，可从检查点恢复"),
    "run_cancelled": _descriptor("run_cancelled", "评测任务已取消"),
}


def status_from_payload(payload: Mapping[str, object]) -> RunStatus | None:
    value = payload.get("status")
    if not isinstance(value, str):
        return None
    try:
        return RunStatus(value)
    except ValueError:
        return None


def event_message(event_type: str, *, view: RunEventView = RunEventView.LIVE) -> str:
    descriptor = RUN_EVENT_CATALOG.get(event_type)
    if descriptor is not None:
        return descriptor.message_for(view)
    return event_type.replace("_", " ")


def project_run_event(
    *,
    run_id: str,
    event_type: str,
    payload: object,
    timestamp: object = None,
    view: RunEventView = RunEventView.LIVE,
) -> RunEvent:
    normalized_payload = payload if isinstance(payload, dict) else {}
    event_data: dict[str, Any] = {
        "run_id": run_id,
        "event_type": event_type,
        "status": status_from_payload(normalized_payload),
        "stage": stage_from_event(event_type),
        "message": event_message(event_type, view=view),
        "payload": normalized_payload,
    }
    if isinstance(timestamp, str):
        event_data["timestamp"] = timestamp
    return RunEvent.model_validate(event_data)
