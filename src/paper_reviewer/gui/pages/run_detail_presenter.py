"""Pure presentation helpers for the run-detail page.

This module deliberately contains no Qt imports.  Keeping the conversion from
legacy/v2 report shapes to human-readable text here makes the widget easier to
read and gives the formatter a small, deterministic surface for unit tests.
The leading-underscore names are imported by :mod:`run_detail` for backwards
compatibility with callers that historically reached into that module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from paper_reviewer.reporting.presentation import ReportPresentation


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value)).strip().casefold()


def _display_value(value: Any) -> str:
    if value is None:
        return "未提供"
    value = getattr(value, "value", value)
    return ("是" if value else "否") if isinstance(value, bool) else str(value)


def _score_text(value: float | int | None) -> str:
    """以适合界面阅读的精度显示课程分数。"""

    if value is None:
        return "暂无"
    numeric = float(value)
    return f"{numeric:.0f}" if numeric.is_integer() else f"{numeric:.1f}"


def _course_grade(total_score: float | int | None) -> str:
    """把课程总分映射到 Rubric 约定的五级锚点。"""

    if total_score is None:
        return "暂无"
    score = float(total_score)
    if score < 40:
        return "核心任务明显缺失"
    if score < 60:
        return "完成不足"
    if score < 75:
        return "达到基本要求"
    if score < 90:
        return "良好"
    return "优秀"


def _course_conclusion(
    total_score: float | int | None,
    passing_score: float | int | None,
    verdict: Any = None,
) -> str:
    """生成不暴露 ``pass``/``fail`` 机器值的课程结论。"""

    if total_score is not None and passing_score is not None:
        return (
            "达到课程论文基本要求"
            if float(total_score) >= float(passing_score)
            else "未达到课程论文基本要求"
        )
    normalized = _status_value(verdict)
    if normalized in {"pass", "passed", "qualified"}:
        return "达到课程论文基本要求"
    if normalized in {"fail", "failed", "unqualified"}:
        return "未达到课程论文基本要求"
    return "未设置结论"


def _first(source: Any, *names: str, default: Any = None) -> Any:
    if source is None:
        return default
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        try:
            value = getattr(source, name)
        except AttributeError:
            continue
        if value is not None:
            return value
    return default


def _items(value: Any) -> list[tuple[Any, Any]]:
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = []
        for item in value:
            key = _first(item, "group_id", "group", "dimension_id", "id")
            val = _first(item, "score", "value", "total", "rating")
            if key is not None and val is not None:
                result.append((key, val))
        return result
    return []


def _format_lines(
    value: Any, presentation: ReportPresentation | None = None
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [presentation.decision_step(value) if presentation is not None else value]
    if isinstance(value, Mapping):
        return [
            f"{presentation.decision_step(k) if presentation is not None else _display_value(k)}"
            f"：{presentation.panel_outcome(v) if presentation is not None else _display_value(v)}"
            for k, v in value.items()
        ]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            presentation.decision_step(item) if presentation is not None else _display_value(item)
            for item in value
        ]
    return [_display_value(value)]


def _pending_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return [value]
    result = []
    for item in value:
        status = _status_value(_first(item, "status", "state", default=""))
        if not status or status in {
            "suspected",
            "pending",
            "awaiting",
            "awaiting_human_confirmation",
            "not_assessable",
        }:
            result.append(item)
    return result


def _evidence_lines(value: Any, *, external: bool = False) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [str(value)]
    if not isinstance(value, Sequence) or isinstance(value, Mapping):
        value = [value]
    lines: list[str] = []
    for ref in value:
        page = _first(ref, "page", "page_number", "page_no")
        block = _first(ref, "block_id", "evidence_id", "id")
        quote = _first(ref, "quote", "excerpt", "snippet", "text")
        source = _first(ref, "title", "source", "url", "doi")
        if external:
            lines.append(f"• {source or block or '外部来源'}" + (f"：{quote}" if quote else ""))
        else:
            location = "；".join(
                x
                for x in (
                    f"第 {page} 页" if page is not None else "",
                    f"块 {block}" if block else "",
                )
                if x
            )
            lines.append(f"• {location or '论文证据'}" + (f"：{quote}" if quote else ""))
    return lines


def _format_hard_detail(
    rule: Any, presentation: ReportPresentation | None = None
) -> str:
    rule_id = _display_value(_first(rule, "rule_id", "id", default="未命名规则"))
    description = _display_value(_first(rule, "description", "title", "rule", default=""))
    raw_status = _first(rule, "status", "state", default="待复核")
    status = (
        presentation.hard_rule_status(raw_status)
        if presentation is not None
        else _display_value(raw_status)
    )
    judgment = _first(
        rule,
        "ai_judgment",
        "ai_judgement",
        "judgment",
        "assessment",
        "explanation",
        "rationale",
        default="未提供",
    )
    paper = _evidence_lines(_first(rule, "paper_evidence", "evidence", "paper_refs", "references"))
    external = _evidence_lines(
        _first(rule, "external_evidence", "external_refs", "sources"), external=True
    )
    paper_text = "\n".join(paper) if paper else "• 无"
    external_text = "\n".join(external) if external else "• 无"
    rule_header = (
        f"规则：{presentation.rule(rule_id)}"
        if presentation is not None
        else f"规则 ID：{rule_id}\n规则：{description}"
    )
    rendered_judgment = (
        presentation.narrative(judgment)
        if presentation is not None
        else _display_value(judgment)
    )
    return (
        f"{rule_header}\nAI 判断：{rendered_judgment}\n状态：{status}\n\n"
        f"论文页码与引文\n{paper_text}\n\n外部来源\n{external_text}"
    )


def _format_hard_report(
    assessments: Any,
    decisions: Any,
    presentation: ReportPresentation | None = None,
) -> str:
    lines: list[str] = []
    values = (
        []
        if assessments is None
        else (
            assessments
            if isinstance(assessments, Sequence) and not isinstance(assessments, (str, bytes))
            else [assessments]
        )
    )
    for rule in values:
        rule_id = _display_value(_first(rule, "rule_id", "id", default="未命名规则"))
        raw_status = _first(rule, "status", "state", default="未提供")
        status = (
            presentation.hard_rule_status(raw_status)
            if presentation is not None
            else _display_value(raw_status)
        )
        raw_judgment = _first(
            rule, "ai_judgment", "judgment", "assessment", "rationale", default=""
        )
        judgment = (
            presentation.narrative(raw_judgment)
            if presentation is not None
            else _display_value(raw_judgment)
        )
        rule_label = presentation.rule(rule_id) if presentation is not None else rule_id
        lines.append(
            f"{rule_label} · 状态：{status}"
            + (f" · AI 判断：{judgment}" if judgment else "")
        )
    values = (
        []
        if decisions is None
        else (
            decisions
            if isinstance(decisions, Sequence) and not isinstance(decisions, (str, bytes))
            else [decisions]
        )
    )
    if values:
        lines.append("人工处理记录：")
        for decision in values:
            rule_id = _display_value(_first(decision, "rule_id", "id", default="未命名规则"))
            raw_result = _first(decision, "decision", "status", default="未提供")
            result = (
                presentation.human_decision(raw_result)
                if presentation is not None
                else _display_value(raw_result)
            )
            reviewer = _display_value(_first(decision, "reviewer", "reviewer_id", default="未提供"))
            raw_reason = _first(decision, "reason", "rationale", default="未提供")
            reason = (
                presentation.narrative(raw_reason)
                if presentation is not None
                else _display_value(raw_reason)
            )
            rule_label = presentation.rule(rule_id) if presentation is not None else rule_id
            decided_at = _display_value(
                _first(decision, "decided_at", "reviewed_at", "timestamp", default="")
            )
            lines.append(
                f"• {rule_label} · {result} · 复核人：{reviewer} · {reason}"
                + (f" · 时间：{decided_at}" if decided_at else "")
            )
    return "\n".join(lines) or "暂无结构化否决项数据。"


def _format_panel_review_detail(
    source: Any, presentation: ReportPresentation | None = None
) -> str:
    opinions = _first(source, "expert_opinions", "opinions", default=[])
    if not isinstance(opinions, Sequence) or isinstance(opinions, (str, bytes)):
        opinions = [opinions] if opinions else []
    unable = [
        item
        for item in opinions
        if _status_value(_first(item, "verdict", "status", default=""))
        == "unable_to_assess"
    ]
    lines = ["人工面板复核原因：至少一名 AI 专家无法完成判断。"]
    lines.extend(
        _format_opinion(
            item,
            _display_value(_first(item, "round", default="专家")),
            index,
            presentation,
        )
        for index, item in enumerate(unable, start=1)
    )
    lines.append("请结合完整评分、Findings、论文证据和专家意见给出最终风险结论。")
    return "\n".join(lines)


def _format_panel(
    panel: Any, source: Any, presentation: ReportPresentation | None = None
) -> str:
    panel = panel or source
    lines: list[str] = []
    for phase, field in (("初评", "initial_opinions"), ("复评", "supplemental_opinions")):
        values = _first(panel, field)
        if values is None:
            continue
        values = (
            values
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
            else [values]
        )
        lines.extend(
            _format_opinion(item, phase, index, presentation)
            for index, item in enumerate(values, start=1)
        )
    opinions = _first(panel, "expert_opinions", "opinions", "reviewer_opinions")
    if opinions is None and panel is not source:
        opinions = _first(source, "expert_opinions", "opinions", "reviewer_opinions")
    if opinions is not None:
        opinions = (
            opinions
            if isinstance(opinions, Sequence) and not isinstance(opinions, (str, bytes))
            else [opinions]
        )
        round_counts: dict[str, int] = {}
        for item in opinions:
            phase = _display_value(_first(item, "phase", "round", default="专家"))
            round_counts[phase] = round_counts.get(phase, 0) + 1
            lines.append(
                _format_opinion(item, phase, round_counts[phase], presentation)
            )
    decision = _first(panel, "decision", "risk_conclusion", "verdict", "outcome")
    if decision is not None:
        rendered_decision = (
            presentation.panel_outcome(decision)
            if presentation is not None
            else _display_value(decision)
        )
        lines.append(f"面板结论：{rendered_decision}")
    reason = _first(panel, "reason", "rationale")
    if reason is not None:
        rendered_reason = (
            presentation.panel_reason(reason)
            if presentation is not None
            else _display_value(reason)
        )
        lines.append(f"面板理由：{rendered_reason}")
    initial_count = _first(panel, "initial_unqualified")
    supplemental_count = _first(panel, "supplemental_unqualified")
    if initial_count is not None or supplemental_count is not None:
        lines.append(
            f"不合格票数：初评 {_display_value(initial_count)}；"
            f"复评 {_display_value(supplemental_count)}"
        )
    return "\n".join(lines) or "暂无独立专家面板数据。"


def _format_opinion(
    opinion: Any,
    phase: str,
    index: int = 1,
    presentation: ReportPresentation | None = None,
) -> str:
    reviewer = _display_value(_first(opinion, "reviewer_id", "expert_id", "id", default="专家"))
    raw_verdict = _first(opinion, "verdict", "decision", "outcome", default="未提供")
    verdict = (
        presentation.expert_verdict(raw_verdict)
        if presentation is not None
        else _display_value(raw_verdict)
    )
    raw_summary = _first(opinion, "summary", "explanation", "rationale", default="")
    evidence = "; ".join(_evidence_lines(_first(opinion, "paper_evidence", "evidence")))
    phase = {"initial": "初评", "supplemental": "复评"}.get(phase, phase)
    finding_ids = _first(opinion, "finding_ids") or []
    aliases = {str(item): "对应问题" for item in finding_ids}
    summary = (
        presentation.narrative(raw_summary, extra_aliases=aliases)
        if presentation is not None
        else _display_value(raw_summary)
    )
    reviewer_label = (
        presentation.expert_label(
            _first(opinion, "round", "phase", default=phase), index, reviewer
        )
        if presentation is not None
        else reviewer
    )
    result = f"{phase} · {reviewer_label}：{verdict}"
    if summary and summary != "未提供":
        result += f" · {summary}"
    if finding_ids:
        result += (
            f" · 关联问题：{len(finding_ids)} 项"
            if presentation is not None
            else f" · Finding：{', '.join(str(item) for item in finding_ids)}"
        )
    return result + (f" · 证据：{evidence}" if evidence else "")


def _make_decision(data: dict[str, object]) -> Any:
    """Use the v2 value object when it is available, otherwise pass a dict."""
    for module_name in (
        "paper_reviewer.domain.evaluation",
        "paper_reviewer.domain.review",
        "paper_reviewer.application.models",
    ):
        try:
            module = __import__(module_name, fromlist=["HumanRuleDecision"])
            model = getattr(module, "HumanRuleDecision", None)
            if model is not None:
                try:
                    return (
                        model.model_validate(data)
                        if hasattr(model, "model_validate")
                        else model(**data)
                    )
                except Exception:
                    pass
        except (ImportError, AttributeError):
            pass
    return data
