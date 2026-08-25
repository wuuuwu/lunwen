from __future__ import annotations

from pathlib import Path

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.rubric import RubricDimension, RubricGroup, RubricProfile
from paper_reviewer.reporting.presentation import (
    ReportPresentation,
    ReportPresentationProfile,
    load_presentation_profile,
)


def test_zh_cn_presentation_resolves_rubric_and_status_labels() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    presentation = ReportPresentation(rubric, ReportPresentationProfile.ZH_CN_V1)

    assert presentation.dimension("hierarchy_system") == "层次体系"
    assert presentation.group("logical_construction") == "逻辑构建"
    assert presentation.rule("academic_integrity") == "学术诚信"
    assert presentation.severity("critical") == "严重"
    assert presentation.hard_rule_status("not_detected") == "未发现"
    assert presentation.expert_verdict("unable_to_assess") == "无法判断"
    assert presentation.panel_outcome("risk_triggered") == "触发存在问题风险"
    assert (
        presentation.decision_step("initial_unqualified_at_least_two")
        == "三名初评专家中至少两名判定不合格"
    )


def test_narrative_replaces_only_known_unprotected_identifiers() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    presentation = ReportPresentation(rubric, ReportPresentationProfile.ZH_CN_V1)
    source = (
        "hierarchy_system 存在问题；finding_id: finding-1；"
        "https://example.test/hierarchy_system；`logical_structure`"
    )

    rendered = presentation.narrative(
        source,
        extra_aliases={"finding-1": "对应问题"},
    )

    assert "层次体系 存在问题" in rendered
    assert "关联问题: 对应问题" in rendered
    assert "https://example.test/hierarchy_system" in rendered
    assert "`logical_structure`" in rendered


def test_dynamic_rubric_uses_its_own_titles_and_hides_unknown_ids() -> None:
    rubric = RubricProfile(
        rubric_id="dynamic",
        version="1",
        title="动态规则",
        groups=[
            RubricGroup(
                group_id="research_quality",
                title="研究质量",
                description="研究质量说明",
                weight=100,
                dimensions=["method_quality"],
            )
        ],
        dimensions=[
            RubricDimension(
                dimension_id="method_quality",
                group_id="research_quality",
                title="研究方法质量",
                description="方法是否恰当",
                weight=100,
                maximum_score=4,
                checks=["检查方法"],
            )
        ],
    )
    presentation = ReportPresentation(rubric, ReportPresentationProfile.ZH_CN_V1)

    assert presentation.dimension("method_quality") == "研究方法质量"
    assert presentation.group("research_quality") == "研究质量"
    assert presentation.dimension("unknown_dimension") == "未命名指标"
    assert presentation.group("unknown_group") == "未命名分组"
    assert presentation.rule("unknown_rule") == "未命名规则"


def test_missing_presentation_metadata_defaults_to_legacy(tmp_path: Path) -> None:
    assert load_presentation_profile(tmp_path) is ReportPresentationProfile.LEGACY
