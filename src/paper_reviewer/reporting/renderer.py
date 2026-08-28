"""Render legacy and policy-aware paper review reports."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.provider import ProviderSnapshot
from paper_reviewer.domain.review import Severity
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord
from paper_reviewer.domain.submission import SubmissionMetadata
from paper_reviewer.reporting.adapters import adapt_report
from paper_reviewer.reporting.document import ReportDocument
from paper_reviewer.reporting.presentation import (
    REPORT_PRESENTATION_FILENAME,
    ReportPresentation,
    ReportPresentationMetadata,
    ReportPresentationProfile,
    load_presentation_profile,
)
from paper_reviewer.validation.audits import AuditReport

DISCLAIMER_LINES = (
    "本结果不是浙江省教育厅正式抽检结论。",
    "百分制和五级锚点为本项目自定义诊断规则。",
    "学术不端检测报告未由系统自动读取。",
    "模型置信度是未经校准的自评，不作为统计概率。",
)

COURSE_DISCLAIMER_LINES = (
    "本结果仅供教师评阅参考，不是教师正式成绩。",
    "当前通用课程论文 Rubric 是可替换的实验性模板，正式评分前应由任课教师依据课程大纲确认。",
    "姓名、学号、专业和题目由系统自动提取，使用和归档前必须人工核对。",
    "本系统仅检查引用对应关系与格式，不自动检测或认定抄袭、代写、伪造等学术不端。",
    "模型置信度是未经校准的自评，不作为统计概率。",
)

_METADATA_FIELD_LABELS = {
    "student_name": "姓名",
    "student_id": "学号",
    "major": "专业",
    "paper_title": "题目",
}
_METADATA_REPORT_LABELS = {
    **_METADATA_FIELD_LABELS,
    "paper_title": "论文题目",
}


def write_report_bundle(
    *,
    run_dir: Path,
    run: RunRecord,
    rubric: RubricProfile,
    review: Any | None = None,
    audit: AuditReport,
    evidence: list[EvidenceItem],
    evaluation_report: Any | None = None,
    report: Any | None = None,
    presentation_profile: ReportPresentationProfile | None = None,
    submission_metadata: SubmissionMetadata | None = None,
    dimension_scores: Mapping[str, float] | None = None,
) -> list[Path]:
    """Write JSON, Markdown, evidence and run summary artifacts."""
    selected = evaluation_report or report or review
    if selected is None:
        raise ValueError("a review or evaluation report is required")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_json = run_dir / "report.json"
    report_markdown = run_dir / "report.md"
    evidence_json = run_dir / "evidence.json"
    summary_json = run_dir / "run-summary.json"
    presentation_path = run_dir / REPORT_PRESENTATION_FILENAME
    existing_report = report_json.is_file() or report_markdown.is_file()
    selected_profile = (
        load_presentation_profile(run_dir)
        if presentation_path.is_file()
        else _default_presentation_profile(
            rubric,
            existing_report=existing_report,
            requested=presentation_profile,
        )
    )
    if selected_profile is not ReportPresentationProfile.LEGACY:
        _write_text_atomic(
            presentation_path,
            ReportPresentationMetadata(profile=selected_profile).model_dump_json(indent=2),
        )
    if (
        submission_metadata is None
        and selected_profile is ReportPresentationProfile.COURSE_ZH_CN_V1
    ):
        submission_metadata = _load_submission_metadata(run_dir)
    _write_text_atomic(report_json, _json_text(selected))
    provider_snapshot = _load_provider_snapshot(run_dir)
    _write_text_atomic(
        report_markdown,
        render_markdown(
            rubric,
            selected,
            audit,
            provider_snapshot=provider_snapshot,
            provider_ref=run.provider,
            model=run.model,
            presentation_profile=selected_profile,
            submission_metadata=submission_metadata,
            dimension_scores=dimension_scores,
        ),
    )
    _write_text_atomic(
        evidence_json,
        json.dumps(
            [item.model_dump(mode="json") for item in evidence], ensure_ascii=False, indent=2
        ),
    )
    _write_text_atomic(summary_json, run.model_dump_json(indent=2))
    return [report_markdown, report_json, evidence_json, summary_json]


def _default_presentation_profile(
    rubric: RubricProfile,
    *,
    existing_report: bool,
    requested: ReportPresentationProfile | None,
) -> ReportPresentationProfile:
    if requested is not None:
        return ReportPresentationProfile(requested)
    if existing_report:
        return ReportPresentationProfile.LEGACY
    if getattr(rubric, "evaluation_mode", None) == "course_assessment":
        return ReportPresentationProfile.COURSE_ZH_CN_V1
    return ReportPresentationProfile.ZH_CN_V1


def _write_text_atomic(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_markdown(
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
) -> str:
    """Render a legacy MetaReview or the new EvaluationReport.

    The public facade keeps its historical signature; the renderer consumes a
    read-only projection so future GUI/PDF consumers can share the same input
    boundary without reimplementing report-shape detection.
    """

    document = adapt_report(
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
    return render_document(document)


def render_document(document: ReportDocument) -> str:
    """Render a previously adapted report projection."""

    if document.is_course_report:
        return _render_course_document(document)
    if document.is_evaluation:
        return _render_evaluation_document(document)
    return _render_legacy_document(document)


def _render_legacy_document(document: ReportDocument) -> str:
    return _render_legacy_markdown(
        document.rubric,
        document.report,
        document.audit,
        list(document.provider_lines),
        document.presentation,
    )


def _render_evaluation_document(document: ReportDocument) -> str:
    return _render_evaluation_markdown(
        document.rubric,
        document.report,
        document.audit,
        list(document.provider_lines),
        document.presentation,
    )


def _render_course_document(document: ReportDocument) -> str:
    report = document.report
    metadata = document.submission_metadata
    presentation = document.presentation
    scores = document.dimension_scores
    if scores is None:
        candidate = _field(report, "dimension_scores", default={})
        scores = candidate if isinstance(candidate, Mapping) else {}
    total = _number(_field(report, "total_score", default=None))
    passing_score = 60.0
    if document.rubric.aggregation is not None:
        configured = document.rubric.aggregation.passing_score
        if configured is not None:
            passing_score = float(configured)
    pending_fields = _metadata_pending_fields(metadata)
    lines = [
        "# 课程论文 AI 辅助评测报告",
        "",
        f"- 任务编号：{_code(_field(report, 'run_id', default='unknown'))}",
        *document.provider_lines,
        f"- 评分规则：{_text(document.rubric.title)}（{_text(document.rubric.version)}）",
        f"- 证据审计：{'通过' if document.audit.passed else '未通过'}",
        "",
        "## 论文与学生信息",
        "",
        f"- {_metadata_label('student_name', pending_fields)}："
        f"{_metadata_text(metadata, 'student_name', '未识别姓名')}",
        f"- {_metadata_label('student_id', pending_fields)}："
        f"{_metadata_text(metadata, 'student_id', '未识别学号')}",
        f"- {_metadata_label('major', pending_fields)}（仅用于识别与文件命名）："
        f"{_metadata_text(metadata, 'major', '未识别专业')}",
        f"- {_metadata_label('paper_title', pending_fields)}："
        f"{_metadata_text(metadata, 'paper_title', '未识别题目')}",
        "- 元数据核对："
        + _metadata_review_status(metadata),
        "",
    ]
    if pending_fields:
        pending_labels = "、".join(_METADATA_FIELD_LABELS[field] for field in pending_fields)
        lines.extend(
            [
                "> **人工核对未完成：下列内容是自动提取的候选值，尚未经人工确认。**",
                ">",
                f"> 待核对字段：{pending_labels}",
                "",
            ]
        )
    if metadata is not None and metadata.warnings:
        lines.extend([">自动提取提示：", ">"])
        lines.extend(f"> - {_single_line(item)}" for item in metadata.warnings)
        lines.append("")
    lines.extend(
        [
            "## 总体评价",
            "",
            presentation.narrative(_field(report, "overall_summary", default="")),
            "",
            "## 课程评分",
            "",
            "| 评价维度 | 得分 | 权重 | 加权贡献 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for dimension in document.rubric.dimensions:
        score = _number(scores.get(dimension.dimension_id))
        contribution = _course_contribution(document.rubric, dimension, score)
        lines.append(
            f"| {presentation.dimension(dimension.dimension_id)} | "
            f"{_score_text(score)} | {_percentage_text(dimension.weight)} | "
            f"{_score_text(contribution)} |"
        )
    lines.extend(
        [
            "",
            f"- 总分：**{_score_text(total)}**",
            f"- 五级等级：**{_course_grade(document.rubric, total)}**",
            f"- 课程要求结论：**{_course_conclusion(total, passing_score)}**",
            f"- 及格参考线：{_score_text(passing_score)} 分",
            "",
        ]
    )
    findings = _field(report, "findings", default=[])
    _append_course_findings(lines, findings, presentation)
    _append_legacy_notes(lines, report, document.audit, presentation)
    lines.extend(["## 重要说明", ""])
    lines.extend(f"- {item}" for item in COURSE_DISCLAIMER_LINES)
    lines.append("")
    return "\n".join(lines)


def _metadata_text(
    metadata: SubmissionMetadata | None,
    field_name: str,
    fallback: str,
) -> str:
    if metadata is None:
        return fallback
    value = getattr(metadata, field_name, "")
    rendered = _single_line(value)
    return rendered or fallback


def _metadata_pending_fields(metadata: SubmissionMetadata | None) -> tuple[str, ...]:
    if metadata is None:
        return tuple(_METADATA_FIELD_LABELS)
    return metadata.pending_review_fields


def _metadata_label(field_name: str, pending_fields: tuple[str, ...]) -> str:
    label = _METADATA_REPORT_LABELS[field_name]
    return f"{label}（候选，待核对）" if field_name in pending_fields else label


def _metadata_review_status(metadata: SubmissionMetadata | None) -> str:
    if metadata is None or metadata.needs_review:
        return "需要人工核对"
    if metadata.human_reviewed:
        return "已由人工核对"
    return "自动提取置信度达标，使用前仍建议人工核对"


def _single_line(value: Any) -> str:
    return " ".join(str(value).split())


def _number(value: Any) -> float | None:
    candidate = _field(value, "score", default=value)
    if candidate is None or isinstance(candidate, bool):
        return None
    try:
        converted = float(candidate)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _score_text(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _percentage_text(value: float) -> str:
    return f"{_score_text(float(value))}%"


def _course_contribution(
    rubric: RubricProfile,
    dimension: Any,
    score: float | None,
) -> float | None:
    if score is None:
        return None
    minimum = float(dimension.minimum_score)
    maximum = float(dimension.maximum_score)
    if score < minimum or score > maximum or maximum <= minimum:
        return None
    maximum_total = rubric.aggregation.maximum_total if rubric.aggregation else 100
    normalized = (score - minimum) / (maximum - minimum)
    return round(normalized * float(dimension.weight) / 100 * maximum_total, 2)


def _course_grade(rubric: RubricProfile, total: float | None) -> str:
    if total is None:
        return "未评分"
    if rubric.dimensions:
        for anchor in rubric.dimensions[0].anchors:
            if anchor.minimum <= total <= anchor.maximum:
                return anchor.label
    if total >= 90:
        return "优秀"
    if total >= 75:
        return "良好"
    if total >= 60:
        return "达到基本要求"
    if total >= 40:
        return "完成不足"
    return "核心任务明显缺失"


def _course_conclusion(total: float | None, passing_score: float) -> str:
    if total is None:
        return "暂无法确定"
    if total >= passing_score:
        return "达到课程论文基本要求"
    return "未达到课程论文基本要求"


def _append_course_findings(
    lines: list[str], findings: Any, presentation: ReportPresentation
) -> None:
    lines.extend(["## 主要问题与修改建议", ""])
    severity_order = {
        Severity.CRITICAL.value: 0,
        Severity.MAJOR.value: 1,
        Severity.MINOR.value: 2,
        Severity.SUGGESTION.value: 3,
    }
    finding_items = _items(findings)
    finding_items.sort(
        key=lambda item: severity_order.get(
            _text(_field(item, "severity", default="")).casefold(), 99
        )
    )
    if not finding_items:
        lines.extend(["未发现需要单列的问题。", ""])
        return
    for finding in finding_items:
        severity = presentation.severity(_field(finding, "severity", default="finding"))
        claim = presentation.narrative(_field(finding, "claim", default=""))
        dimension = _field(finding, "dimension_id", "criterion_id", default="—")
        rationale = presentation.narrative(
            _field(finding, "rationale", "explanation", default="—")
        )
        recommendation = presentation.narrative(
            _field(finding, "recommendation", default="—")
        )
        lines.extend(
            [
                f"### [{severity}] {claim}",
                "",
                f"- 评价维度：{presentation.dimension(dimension)}",
                f"- 置信度：{_code(_field(finding, 'confidence', default='—'))}",
                f"- 问题说明：{rationale}",
                f"- 修改建议：{recommendation}",
            ]
        )
        _append_course_evidence(
            lines,
            "论文证据",
            _field(finding, "paper_evidence", "evidence", default=[]),
            external=False,
        )
        _append_course_evidence(
            lines,
            "外部证据",
            _field(finding, "external_evidence", default=[]),
            external=True,
        )
        if _field(finding, "needs_human_check", default=False):
            lines.append("- 需要人工核查：是")
        lines.append("")


def _append_course_evidence(
    lines: list[str],
    heading: str,
    evidence: Any,
    *,
    external: bool,
) -> None:
    items = _items(evidence)
    if not items:
        return
    lines.append(f"- {heading}：")
    for item in items:
        page = _field(item, "page", "page_number", default=None)
        quote = _single_line(_field(item, "quote", default=""))
        if external:
            source = _field(item, "title", "doi", "url", default="外部来源")
            description = _single_line(source) or "外部来源"
        else:
            description = f"第 {_text(page)} 页" if page is not None else "论文原文"
        if quote:
            description += f"：“{quote}”"
        lines.append(f"  - {description}")


def _render_legacy_markdown(
    rubric: RubricProfile,
    review: Any,
    audit: AuditReport,
    provider_lines: list[str],
    presentation: ReportPresentation,
) -> str:
    if presentation.localized:
        return _render_localized_legacy_markdown(
            rubric, review, audit, provider_lines, presentation
        )
    lines = [
        "# Academic Paper Review",
        "",
        f"- Run ID: {_code(_field(review, 'run_id', default='unknown'))}",
        *provider_lines,
        f"- Rubric: {_code(f'{rubric.rubric_id}@{rubric.version}')}",
        f"- Scoring: {_code('enabled' if rubric.scoring_enabled else 'disabled')}",
        f"- Evidence audit: {_code('passed' if audit.passed else 'failed')}",
        "",
        "## Overall assessment",
        "",
        _text(_field(review, "overall_summary", default="")),
        "",
    ]
    if rubric.scoring_enabled:
        lines.extend(
            [
                "## Score and verdict",
                "",
                f"- Total score: {_text(_field(review, 'total_score', default='—'))}",
                f"- Verdict: {_text(_field(review, 'verdict', default='—'))}",
                "",
            ]
        )
    else:
        lines.extend(["## Score and verdict", "", "Not scored: no validated scoring rubric.", ""])
    _append_findings(lines, _field(review, "findings", default=[]), presentation)
    _append_legacy_notes(lines, review, audit, presentation)
    return "\n".join(lines)


def _render_evaluation_markdown(
    rubric: RubricProfile,
    report: Any,
    audit: AuditReport,
    provider_lines: list[str],
    presentation: ReportPresentation,
) -> str:
    policy = _field(report, "policy_context", "policy", default=None)
    meta = _field(report, "meta_review", default=None)
    title = _field(report, "paper_title", "title", default="浙江省本科毕业论文 AI 辅助评测报告")
    lines = [
        f"# {_text(title)}",
        "",
    ]
    human_review = _field(report, "human_review_summary", default=None)
    if human_review is not None and not bool(
        _field(human_review, "complete", default=True)
    ):
        lines.extend(
            [
                "> **AI 评测已完成。**",
                ">",
                "> **人工复核尚未完成，当前风险结论待定。**",
                "",
            ]
        )
    if presentation.localized:
        lines.extend(
            [
                f"- 任务编号：{_code(_field(report, 'run_id', default='unknown'))}",
                *provider_lines,
                f"- 评分规则：{_text(rubric.title)}（{_text(rubric.version)}）",
                "- 评测模式：诊断评分与抽检风险评议",
                f"- 证据审计：{'通过' if audit.passed else '未通过'}",
            ]
        )
    else:
        lines.extend(
            [
                f"- Run ID: {_code(_field(report, 'run_id', default='unknown'))}",
                *provider_lines,
                f"- Rubric: {_code(f'{rubric.rubric_id}@{rubric.version}')}",
                "- Evaluation mode: "
                f"{_code(_field(report, 'evaluation_mode', default='dual_advisory'))}",
                f"- Evidence audit: {_code('passed' if audit.passed else 'failed')}",
            ]
        )
    discipline = _field(report, "discipline_name", "discipline", default=None)
    if discipline:
        lines.append(f"- 专业：{_code(discipline)}")
    source = _field(policy, "source", "source_title", "document", default=None)
    if source:
        lines.append(f"- 政策来源：{_text(source)}")
    lines.extend(
        [
            "",
            "## 总体评价",
            "",
            presentation.narrative(
                _field(
                    report,
                    "overall_summary",
                    "summary",
                    default=_field(meta, "overall_summary", default=""),
                )
            ),
            "",
        ]
    )
    _append_diagnostic_scores(lines, report, presentation)
    _append_hard_rules(lines, report, presentation)
    _append_expert_panel(lines, report, presentation)
    _append_decision_path(lines, report, presentation)
    findings = _field(report, "findings", default=_field(meta, "findings", default=[]))
    if findings:
        _append_findings(lines, findings, presentation)
    _append_audit_notes(lines, audit, presentation)
    lines.extend(["## 重要说明", ""])
    disclaimers = _field(report, "disclaimers", default=DISCLAIMER_LINES)
    lines.extend(f"- {_text(item)}" for item in _items(disclaimers))
    lines.append("")
    return "\n".join(lines)


def _render_localized_legacy_markdown(
    rubric: RubricProfile,
    review: Any,
    audit: AuditReport,
    provider_lines: list[str],
    presentation: ReportPresentation,
) -> str:
    lines = [
        "# AI 辅助论文评测报告",
        "",
        f"- 任务编号：{_code(_field(review, 'run_id', default='unknown'))}",
        *provider_lines,
        f"- 评分规则：{_text(rubric.title)}（{_text(rubric.version)}）",
        f"- 计分状态：{'启用' if rubric.scoring_enabled else '未启用'}",
        f"- 证据审计：{'通过' if audit.passed else '未通过'}",
        "",
        "## 总体评价",
        "",
        presentation.narrative(_field(review, "overall_summary", default="")),
        "",
    ]
    if rubric.scoring_enabled:
        lines.extend(
            [
                "## 分数与结论",
                "",
                f"- 总分：{_text(_field(review, 'total_score', default='—'))}",
                f"- 结论：{presentation.narrative(_field(review, 'verdict', default='—'))}",
                "",
            ]
        )
    else:
        lines.extend(["## 分数与结论", "", "当前评分规则未启用计分。", ""])
    _append_findings(lines, _field(review, "findings", default=[]), presentation)
    _append_legacy_notes(lines, review, audit, presentation)
    return "\n".join(lines)


def _append_diagnostic_scores(
    lines: list[str], report: Any, presentation: ReportPresentation
) -> None:
    container = _field(
        report, "diagnostic_score", "diagnostic_scores", "diagnostic", "score", default=None
    )
    if container is None:
        return
    items = _items(_field(container, "assessments", "items", default=[]))
    total = _field(
        container,
        "total_score",
        "diagnostic_total",
        "percentage_score",
        default=_field(report, "diagnostic_total", "total_score", default=None),
    )
    lines.extend(["## 九项诊断评分", ""])
    if total is not None:
        lines.append(f"诊断总分（实验性百分制）：**{_text(total)}**")
    lines.extend(
        [
            "",
            "| 一级指标/二级指标 | 等级（0–4） | 权重 | 加权贡献 | 证据状态 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    if not items:
        lines.append("| （暂无评分记录） | — | — | — | — |")
    for item in items:
        criterion = _field(item, "criterion_id", "dimension_id", "id", default="—")
        title = _field(item, "title", "criterion_title", "name", default=None)
        label = (
            presentation.dimension(criterion)
            if presentation.localized
            else _text(criterion) + (f" · {_text(title)}" if title else "")
        )
        group = _field(item, "group", "group_title", "category", "parent_title", default=None)
        if group is None:
            dimension = next(
                (
                    value
                    for value in presentation.rubric.dimensions
                    if value.dimension_id == _text(criterion)
                ),
                None,
            )
            group = dimension.group_id if dimension is not None else None
        if group:
            group_label = presentation.group(group) if presentation.localized else _text(group)
            label = f"{group_label} / {label}"
        score = _field(item, "score", "level", "rating", "grade", default="—")
        weight = _field(item, "weight", default="—")
        contribution = _field(item, "contribution", "weighted_contribution", default="—")
        evidence = _field(item, "evidence_status", "evidence_state", default=None)
        if evidence is None:
            evidence = "有" if _field(item, "evidence", "paper_evidence", default=[]) else "无"
        rendered_evidence = _text(evidence)
        lines.append(
            f"| {label} | {_text(score)} | {_text(weight)} | "
            f"{_text(contribution)} | {rendered_evidence} |"
        )
    lines.append("")
    groups = _field(container, "group_scores", "grouped_scores", "groups", default=None)
    if groups:
        lines.extend(["### 分组得分", "", "| 一级指标 | 得分 |", "| --- | ---: |"])
        if isinstance(groups, Mapping):
            for group_id, score in groups.items():
                label = presentation.group(group_id) if presentation.localized else _text(group_id)
                lines.append(f"| {label} | {_text(score)} |")
        else:
            for group in _items(groups):
                group_id = _field(group, "group_id", "id", default="—")
                title = _field(group, "title", default=None)
                label = (
                    presentation.group(group_id)
                    if presentation.localized
                    else _text(title or group_id)
                )
                lines.append(
                    f"| {label} | "
                    f"{_text(_field(group, 'score', 'total_score', default='—'))} |"
                )
        lines.append("")


def _append_hard_rules(
    lines: list[str], report: Any, presentation: ReportPresentation
) -> None:
    rules = _field(
        report,
        "hard_rule_assessments",
        "hard_rules",
        "hard_rule_results",
        "rule_assessments",
        default=None,
    )
    decisions = _field(
        report, "human_rule_decisions", "human_decisions", "rule_decisions", default=[]
    )
    if rules is None and not decisions:
        return
    lines.extend(
        [
            "## 否决项与人工复核",
            "",
            "| 规则 | AI 判断 | 状态 | 论文证据 | 人工决定 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    by_rule = {
        _text(_field(item, "rule_id", "id", default="")): item
        for item in _items(decisions)
        if _field(item, "rule_id", "id", default=None)
    }
    for item in _items(rules):
        rule_id = _field(item, "rule_id", "id", default="—")
        nested = _field(item, "human_decision", "decision", default=None)
        decision = nested or by_rule.get(_text(rule_id))
        status = _field(item, "status", "assessment", "ai_status", "outcome", default="—")
        judgement = _field(item, "ai_judgement", "judgement", "finding", "rationale", default="—")
        evidence = _format_evidence(
            _field(item, "paper_evidence", "evidence", "evidence_refs", default=[])
        )
        rule_label = presentation.rule(rule_id) if presentation.localized else _text(rule_id)
        rendered_judgement = presentation.narrative(judgement)
        rendered_status = presentation.hard_rule_status(status)
        lines.append(
            f"| {rule_label} | {rendered_judgement} | {rendered_status} | "
            f"{evidence} | {_format_human_decision(decision, presentation)} |"
        )
    known_ids = {_text(_field(item, "rule_id", "id", default="")) for item in _items(rules)}
    for item in _items(decisions):
        rule_id = _field(item, "rule_id", "id", default="—")
        if _text(rule_id) not in known_ids:
            rule_label = presentation.rule(rule_id) if presentation.localized else _text(rule_id)
            lines.append(
                f"| {rule_label} | — | — | — | "
                f"{_format_human_decision(item, presentation)} |"
            )
    lines.append("")


def _append_expert_panel(
    lines: list[str], report: Any, presentation: ReportPresentation
) -> None:
    initial = _field(
        report, "initial_expert_opinions", "initial_panel_opinions", "initial_panel", default=None
    )
    supplemental = _field(
        report,
        "supplemental_expert_opinions",
        "supplemental_panel_opinions",
        "supplemental_panel",
        default=None,
    )
    all_opinions = _field(report, "expert_opinions", "panel_opinions", default=None)
    if all_opinions is not None and initial is None and supplemental is None:
        initial_items: list[Any] = []
        supplemental_items: list[Any] = []
        for opinion in _items(all_opinions):
            phase = _text(_field(opinion, "phase", "round", default="initial")).casefold()
            (supplemental_items if "supp" in phase or "复评" in phase else initial_items).append(
                opinion
            )
        initial, supplemental = initial_items, supplemental_items
    decision = _field(report, "panel_decision", "panel_result", default=None)
    if initial is None and supplemental is None and decision is None:
        return
    lines.extend(["## 独立专家面板", ""])
    if decision is not None:
        conclusion = _field(
            decision,
            "risk_conclusion",
            "conclusion",
            "decision",
            "verdict",
            "outcome",
            default="—",
        )
        triggered = _field(decision, "risk_triggered", "triggered", default=None)
        rendered_conclusion = presentation.panel_outcome(conclusion)
        lines.append(f"面板结论：**{rendered_conclusion}**")
        if triggered is not None:
            lines.append(f"；触发存在问题风险：**{_text(triggered)}**")
        lines.append("")
    for heading, opinions in (("初评专家意见", initial), ("条件性复评专家意见", supplemental)):
        if opinions is None:
            continue
        lines.extend(
            [
                f"### {heading}",
                "",
                "| 专家 | 结论 | 主要意见 | 证据/问题 |",
                "| --- | --- | --- | --- |",
            ]
        )
        opinion_items = _items(opinions)
        if not opinion_items:
            lines.append("| （无） | — | — | — |")
        for index, opinion in enumerate(opinion_items, start=1):
            expert = _field(opinion, "expert_id", "reviewer_id", "opinion_id", "id", default="—")
            round_value = _field(opinion, "round", "phase", default="initial")
            verdict = _field(opinion, "verdict", "decision", "qualification", "status", default="—")
            summary = _field(opinion, "summary", "rationale", "explanation", default="—")
            finding_ids = _items(
                _field(opinion, "finding_ids", "evidence", "paper_evidence", default=[])
            )
            aliases = {_text(item): "对应问题" for item in finding_ids if _text(item)}
            evidence = _format_evidence(
                _field(opinion, "evidence", "paper_evidence", "finding_ids", default=[])
            )
            expert_label = presentation.expert_label(round_value, index, expert)
            verdict_label = presentation.expert_verdict(verdict)
            summary_text = presentation.narrative(summary, extra_aliases=aliases)
            evidence_text = "关联问题" if presentation.localized and finding_ids else evidence
            lines.append(
                f"| {expert_label} | {verdict_label} | {summary_text} | {evidence_text} |"
            )
        lines.append("")


def _append_decision_path(
    lines: list[str], report: Any, presentation: ReportPresentation
) -> None:
    panel_decision = _field(report, "panel_decision", "panel_result", default=None)
    path = _field(
        report,
        "deterministic_decision_path",
        "decision_path",
        "decision_trace",
        "determination_path",
        default=_field(panel_decision, "decision_path", default=None),
    )
    if path is None:
        return
    lines.extend(["## 确定性决策路径", ""])
    items = _items(path)
    if not items:
        lines.append("（暂无决策步骤）")
    for index, item in enumerate(items, start=1):
        if isinstance(item, Mapping):
            step = _field(item, "step", "stage", "rule", "message", default="")
            result = _field(item, "result", "outcome", "status", default=None)
            rendered_step = presentation.decision_step(step)
            rendered_result = presentation.panel_outcome(result)
            text = rendered_step + (f"：{rendered_result}" if result is not None else "")
        else:
            text = presentation.decision_step(item)
        lines.append(f"{index}. {text}")
        lines.append("")


def _append_findings(
    lines: list[str], findings: Any, presentation: ReportPresentation
) -> None:
    lines.extend(["## 主要问题" if presentation.localized else "## Findings", ""])
    severity_order = {
        Severity.CRITICAL.value: 0,
        Severity.MAJOR.value: 1,
        Severity.MINOR.value: 2,
        Severity.SUGGESTION.value: 3,
    }
    finding_items = _items(findings)
    finding_items.sort(
        key=lambda item: severity_order.get(
            _text(_field(item, "severity", default="")).casefold(), 99
        )
    )
    for finding in finding_items:
        severity = _field(finding, "severity", default="finding")
        claim = _field(finding, "claim", "title", "summary", default="")
        severity_label = presentation.severity(severity)
        heading_severity = severity_label if presentation.localized else severity_label.upper()
        lines.extend(
            [f"### [{heading_severity}] {presentation.narrative(claim)}", ""]
        )
        dimension = _field(finding, "dimension_id", "criterion_id", default="—")
        rationale = presentation.narrative(
            _field(finding, "rationale", "explanation", default="—")
        )
        recommendation = presentation.narrative(
            _field(finding, "recommendation", default="—")
        )
        if presentation.localized:
            lines.extend(
                [
                    f"- 指标：{presentation.dimension(dimension)}",
                    f"- 置信度：{_code(_field(finding, 'confidence', default='—'))}",
                    f"- 问题说明：{rationale}",
                    f"- 修改建议：{recommendation}",
                ]
            )
        else:
            lines.extend(
                [
                    f"- Dimension: {_code(dimension)}",
                    f"- Confidence: {_code(_field(finding, 'confidence', default='—'))}",
                    f"- Rationale: {rationale}",
                    f"- Recommendation: {recommendation}",
                ]
            )
        refs = _field(finding, "paper_evidence", "evidence", default=[])
        if refs:
            prefix = "论文证据" if presentation.localized else "Paper evidence"
            lines.append(f"- {prefix}: {_format_evidence(refs)}")
        external = _field(finding, "external_evidence", default=[])
        if external:
            prefix = "外部证据" if presentation.localized else "External evidence"
            lines.append(f"- {prefix}: {_format_evidence(external)}")
        if _field(finding, "needs_human_check", default=False):
            lines.append(
                "- 需要人工核查：是"
                if presentation.localized
                else "- Human verification required: yes"
            )
        lines.append("")


def _append_legacy_notes(
    lines: list[str],
    review: Any,
    audit: AuditReport,
    presentation: ReportPresentation,
) -> None:
    disagreements = _field(review, "disagreements", default=[])
    human_checks = _field(review, "human_checks", default=[])
    if disagreements:
        lines.extend(
            ["## 评阅分歧" if presentation.localized else "## Reviewer disagreements", ""]
        )
        lines.extend(f"- {presentation.narrative(item)}" for item in _items(disagreements))
        lines.append("")
    if human_checks:
        lines.extend(["## 人工核查" if presentation.localized else "## Human checks", ""])
        lines.extend(f"- {presentation.narrative(item)}" for item in _items(human_checks))
        lines.append("")
    _append_audit_notes(lines, audit, presentation)


def _append_audit_notes(
    lines: list[str], audit: AuditReport, presentation: ReportPresentation
) -> None:
    if audit.errors or audit.warnings:
        lines.extend(["## 审计说明" if presentation.localized else "## Audit notes", ""])
        error_prefix = "错误" if presentation.localized else "ERROR"
        warning_prefix = "警告" if presentation.localized else "WARNING"
        lines.extend(
            f"- {error_prefix}: {presentation.narrative(item)}" for item in audit.errors
        )
        lines.extend(
            f"- {warning_prefix}: {presentation.narrative(item)}" for item in audit.warnings
        )
        lines.append("")


def _format_human_decision(
    decision: Any, presentation: ReportPresentation
) -> str:
    if decision is None:
        return "未处理"
    status = _field(decision, "decision", "status", "outcome", "confirmed", default="—")
    reviewer = _field(decision, "reviewer", "reviewer_id", "reviewer_name", default=None)
    reason = _field(decision, "reason", "rationale", "review_reason", default=None)
    result = presentation.human_decision(status)
    if reviewer:
        result += f"（复核人：{_text(reviewer)}）"
    if reason:
        result += f"：{presentation.narrative(reason)}"
    return result


def _format_evidence(evidence: Any) -> str:
    if evidence is None:
        return "—"
    items = _items(evidence)
    if not items:
        return "—"
    rendered: list[str] = []
    for item in items:
        page = _field(item, "page", "page_number", default=None)
        block = _field(item, "block_id", default=None)
        evidence_id = _field(item, "evidence_id", "finding_id", default=None)
        label = _text(evidence_id or block or "证据")
        if page is not None:
            label += f"（第 {_text(page)} 页）"
        rendered.append(label)
    return ", ".join(rendered)


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("items", "assessments", "criteria", "opinions", "steps", "values"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                return list(nested)
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        try:
            candidate = getattr(value, name)
        except AttributeError:
            continue
        if candidate is not None:
            return candidate
    return default


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(getattr(value, "value", value))


def _code(value: Any) -> str:
    return chr(96) + _text(value) + chr(96)


def _json_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = value
    else:
        dump = getattr(value, "model_dump", None)
        payload = dump(mode="json") if callable(dump) else value
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _load_provider_snapshot(run_dir: Path) -> ProviderSnapshot | None:
    path = run_dir / "provider.json"
    if not path.is_file():
        return None
    return ProviderSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def _load_submission_metadata(run_dir: Path) -> SubmissionMetadata | None:
    path = run_dir / "submission-metadata.json"
    if not path.is_file():
        return None
    return SubmissionMetadata.model_validate_json(path.read_text(encoding="utf-8"))
