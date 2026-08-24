from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.review import (
    CriterionAssessment,
    EvaluationReport,
    ExpertOpinion,
    HardRuleAssessment,
    HardRuleStatus,
    MetaReview,
)
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.reporting.renderer import render_markdown, write_report_bundle
from paper_reviewer.validation.audits import AuditReport
from paper_reviewer.validation.panel import decide_panel
from paper_reviewer.validation.scoring import calculate_diagnostic_score


def _evaluation() -> tuple[object, EvaluationReport]:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    assessments = [
        CriterionAssessment(
            criterion_id=item.dimension_id,
            reviewer_id="specialist",
            rating=2,
            weight=item.weight,
            rationale="达到本科基本要求",
            confidence=0.5,
        )
        for item in rubric.dimensions
    ]
    diagnostic = calculate_diagnostic_score(rubric, assessments)
    hard_rules = [
        HardRuleAssessment(
            rule_id=item.rule_id,
            reviewer_id="compliance-integrity-specialist",
            status=HardRuleStatus.NOT_DETECTED,
            rationale="未在现有材料中发现可验证嫌疑",
        )
        for item in rubric.hard_rules
    ]
    opinions = [
        ExpertOpinion(
            expert_id=f"initial-{index}",
            round="initial",
            verdict="qualified",
            rationale="现有重大问题不足以支持不合格意见",
        )
        for index in range(1, 4)
    ]
    return rubric, EvaluationReport(
        run_id="run-v2",
        policy_context=rubric.policy_context,  # type: ignore[arg-type]
        diagnostic_score=diagnostic,
        hard_rule_assessments=hard_rules,
        expert_opinions=opinions,
        panel_decision=decide_panel(initial=opinions, hard_rules=hard_rules),
        meta_review=MetaReview(
            run_id="run-v2",
            overall_summary="论文基本达到本科毕业论文要求。",
            findings=[],
        ),
    )


def test_actual_evaluation_report_renders_scores_panel_path_and_disclaimers() -> None:
    rubric, evaluation = _evaluation()
    markdown = render_markdown(rubric, evaluation, AuditReport())  # type: ignore[arg-type]
    assert "论文基本达到本科毕业论文要求" in markdown
    assert "诊断总分（实验性百分制）：**50**" in markdown
    assert "topic_significance" in markdown
    assert "risk_not_triggered" in markdown
    assert "initial_unqualified_zero" in markdown
    assert "本结果不是浙江省教育厅正式抽检结论" in markdown
    assert "学术不端检测报告未由系统自动读取" in markdown


def test_v2_report_json_contains_the_full_evaluation(tmp_path: Path) -> None:
    rubric, evaluation = _evaluation()
    run = RunRecord(
        run_id="run-v2",
        status=RunStatus.REPORTED,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="zhejiang@0.1",
        provider="deepseek",
        model="deepseek-chat",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    write_report_bundle(
        run_dir=tmp_path,
        run=run,
        rubric=rubric,  # type: ignore[arg-type]
        evaluation_report=evaluation,
        audit=AuditReport(),
        evidence=[],
    )
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["diagnostic_score"]["total_score"] == 50
    assert payload["panel_decision"]["outcome"] == "risk_not_triggered"
    assert payload["meta_review"]["verdict"] is None
