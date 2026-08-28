from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.reporting.presentation import ReportPresentationProfile
from paper_reviewer.reporting.renderer import render_markdown
from paper_reviewer.validation.audits import AuditReport

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COURSE_RUBRIC = PROJECT_ROOT / "configs" / "rubrics" / "course_paper_v1.yaml"


def _metadata(
    *,
    field_confidences: dict[str, float] | None = None,
    human_reviewed: bool = False,
) -> SubmissionMetadata:
    return SubmissionMetadata(
        student_name="张三",
        student_id="20260001",
        major="公共管理",
        paper_title="公共治理课程案例分析",
        field_evidence={
            field: SubmissionFieldEvidence(
                source=SubmissionMetadataSource.COVER_LABEL,
                confidence=(field_confidences or {}).get(field, 0.95),
            )
            for field in SUBMISSION_METADATA_FIELDS
        },
        human_reviewed=human_reviewed,
    )


@pytest.mark.parametrize(
    ("score", "grade"),
    [
        (0, "核心任务明显缺失"),
        (39, "核心任务明显缺失"),
        (40, "完成不足"),
        (59, "完成不足"),
        (60, "达到基本要求"),
        (74, "达到基本要求"),
        (75, "良好"),
        (89, "良好"),
        (90, "优秀"),
        (100, "优秀"),
    ],
)
def test_course_markdown_uses_configured_five_level_anchors(
    score: int,
    grade: str,
) -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    report = {
        "run_id": "run-course",
        "overall_summary": "课程论文完成情况摘要。",
        "findings": [],
        "disagreements": [],
        "human_checks": [],
        "total_score": score,
    }
    dimension_scores = {
        dimension.dimension_id: float(score) for dimension in rubric.dimensions
    }

    markdown = render_markdown(
        rubric,
        report,
        AuditReport(),
        provider_ref="openai",
        model="test-model",
        presentation_profile=ReportPresentationProfile.COURSE_ZH_CN_V1,
        submission_metadata=_metadata(),
        dimension_scores=dimension_scores,
    )

    assert f"五级等级：**{grade}**" in markdown
    assert "课程论文 AI 辅助评测报告" in markdown
    assert "姓名：张三" in markdown
    assert "学号：20260001" in markdown
    assert "专业（仅用于识别与文件命名）：公共管理" in markdown
    assert "论文题目：公共治理课程案例分析" in markdown
    for dimension in rubric.dimensions:
        assert dimension.title in markdown
        assert dimension.dimension_id not in markdown
    for prohibited in (
        "浙江省教育厅",
        "否决项",
        "独立专家面板",
        "抽检风险",
        "risk_triggered",
        "hard_rule",
    ):
        assert prohibited not in markdown
    assert "本结果仅供教师评阅参考，不是教师正式成绩" in markdown


def test_course_markdown_marks_unconfirmed_values_as_candidates() -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    metadata = _metadata(
        field_confidences={"student_name": 0.4, "paper_title": 0.6}
    )

    markdown = render_markdown(
        rubric,
        {
            "run_id": "run-pending",
            "overall_summary": "摘要",
            "findings": [],
            "total_score": 75,
        },
        AuditReport(),
        presentation_profile=ReportPresentationProfile.COURSE_ZH_CN_V1,
        submission_metadata=metadata,
        dimension_scores={item.dimension_id: 75 for item in rubric.dimensions},
    )

    assert "姓名（候选，待核对）：张三" in markdown
    assert "论文题目（候选，待核对）：公共治理课程案例分析" in markdown
    assert "人工核对未完成" in markdown
    assert "待核对字段：姓名、题目" in markdown
    assert "尚未经人工确认" in markdown


def test_course_markdown_marks_human_review_without_candidate_warning() -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    metadata = _metadata(
        field_confidences={"student_name": 0.4, "paper_title": 0.6},
        human_reviewed=True,
    )

    markdown = render_markdown(
        rubric,
        {"run_id": "run-reviewed", "overall_summary": "摘要", "findings": []},
        AuditReport(),
        presentation_profile=ReportPresentationProfile.COURSE_ZH_CN_V1,
        submission_metadata=metadata,
    )

    assert "元数据核对：已由人工核对" in markdown
    assert "候选，待核对" not in markdown
    assert "尚未经人工确认" not in markdown
