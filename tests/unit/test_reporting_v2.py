from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.provider import ModelApiProtocol, ProviderSnapshot, endpoint_fingerprint
from paper_reviewer.domain.review import (
    CriterionAssessment,
    EvaluationReport,
    ExpertOpinion,
    HardRuleAssessment,
    HardRuleStatus,
    HumanReviewSummary,
    MetaReview,
)
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.reporting.presentation import (
    REPORT_PRESENTATION_FILENAME,
    ReportPresentationMetadata,
    ReportPresentationProfile,
)
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


def test_zh_cn_report_contains_labels_without_machine_identifiers() -> None:
    rubric, evaluation = _evaluation()
    markdown = render_markdown(
        rubric,  # type: ignore[arg-type]
        evaluation,
        AuditReport(),
        presentation_profile=ReportPresentationProfile.ZH_CN_V1,
    )

    assert "层次体系" in markdown
    assert "逻辑构建" in markdown
    assert "学术诚信" in markdown
    assert "未发现" in markdown
    assert "合格" in markdown
    assert "未触发存在问题风险" in markdown
    assert "三名初评专家均判定合格" in markdown
    assert "初评专家 1" in markdown
    forbidden = {
        *(item.dimension_id for item in rubric.dimensions),  # type: ignore[union-attr]
        *(item.group_id for item in rubric.groups),  # type: ignore[union-attr]
        *(item.rule_id for item in rubric.hard_rules),  # type: ignore[union-attr]
        "not_detected",
        "qualified",
        "risk_not_triggered",
        "initial_unqualified_zero",
        "initial-1",
    }
    assert not forbidden.intersection(markdown.split())
    for value in forbidden:
        assert value not in markdown


def test_pending_human_review_marker_is_at_top_of_markdown() -> None:
    rubric, evaluation = _evaluation()
    pending = evaluation.model_copy(
        update={
            "human_review_summary": HumanReviewSummary(
                pending_hard_rule_ids=["academic_integrity"]
            )
        },
        deep=True,
    )

    markdown = render_markdown(rubric, pending, AuditReport())  # type: ignore[arg-type]

    assert "**AI 评测已完成。**" in markdown.split("## 总体评价", 1)[0]
    assert "**人工复核尚未完成，当前风险结论待定。**" in markdown


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
    assert payload["diagnostic_score"]["assessments"][0]["criterion_id"] in {
        item.dimension_id for item in rubric.dimensions  # type: ignore[union-attr]
    }
    assert payload["panel_decision"]["outcome"] == "risk_not_triggered"
    assert payload["meta_review"]["verdict"] is None
    metadata = ReportPresentationMetadata.model_validate_json(
        (tmp_path / REPORT_PRESENTATION_FILENAME).read_text(encoding="utf-8")
    )
    assert metadata.profile is ReportPresentationProfile.ZH_CN_V1
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "层次体系" in markdown
    assert "hierarchy_system" not in markdown


def test_existing_report_without_marker_keeps_legacy_presentation(tmp_path: Path) -> None:
    rubric, evaluation = _evaluation()
    (tmp_path / "report.md").write_text("# Existing report", encoding="utf-8")
    run = RunRecord(
        run_id="run-v2",
        status=RunStatus.REPORTED,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="zhejiang@0.1",
        provider="deepseek",
        model="deepseek-chat",
    )

    write_report_bundle(
        run_dir=tmp_path,
        run=run,
        rubric=rubric,  # type: ignore[arg-type]
        evaluation_report=evaluation,
        audit=AuditReport(),
        evidence=[],
    )

    assert not (tmp_path / REPORT_PRESENTATION_FILENAME).exists()
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "hierarchy_system" in markdown
    assert "risk_not_triggered" in markdown


def test_report_header_uses_snapshot_name_model_and_protocol_without_endpoint_or_id(
    tmp_path: Path,
) -> None:
    rubric, evaluation = _evaluation()
    provider_id = "a" * 32
    base_url = "https://private.example/v1"
    snapshot = ProviderSnapshot(
        provider_ref=f"custom:{provider_id}",
        display_name="校内模型服务",
        protocol=ModelApiProtocol.RESPONSES,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(base_url, ModelApiProtocol.RESPONSES),
        model="review-model-v2",
    )
    (tmp_path / "provider.json").write_text(
        snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    run = RunRecord(
        run_id="run-v2",
        status=RunStatus.REPORTED,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="zhejiang@0.1",
        provider=snapshot.provider_ref,
        model=snapshot.model,
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

    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Provider：校内模型服务" in markdown
    assert "接口协议：Responses API" in markdown
    assert "模型：review-model-v2" in markdown
    assert provider_id not in markdown
    assert "private.example" not in markdown
