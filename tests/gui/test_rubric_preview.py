from __future__ import annotations

from types import SimpleNamespace

from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.gui.widgets import RubricPreview


def test_rubric_preview_is_schema_driven(qtbot: object) -> None:
    rubric = RubricProfile.model_validate(
        {
            "schema_version": "1",
            "rubric_id": "undergraduate",
            "version": "2.0",
            "title": "本科论文评分",
            "scoring_enabled": True,
            "dimensions": [
                {
                    "dimension_id": "methods",
                    "title": "研究方法",
                    "description": "方法是否合适",
                    "weight": 100,
                    "minimum_score": 0,
                    "maximum_score": 10,
                    "checks": ["方法与问题匹配"],
                    "anchors": [
                        {
                            "label": "不足",
                            "minimum": 0,
                            "maximum": 4,
                            "description": "存在明显问题",
                        },
                        {
                            "label": "良好",
                            "minimum": 5,
                            "maximum": 10,
                            "description": "总体可靠",
                        },
                    ],
                }
            ],
            "aggregation": {"method": "weighted_mean", "maximum_total": 100},
        }
    )
    preview = RubricPreview()
    qtbot.addWidget(preview)

    preview.set_result(
        RubricValidationResult(
            valid=True,
            rubric=rubric,
            weight_total=100,
            profile_compatible=True,
        )
    )

    assert "本科论文评分" in preview.title.text()
    assert "Schema 1" in preview.metadata.text()
    assert "权重 100%" in preview.model.item(0).child(0).text()


def test_rubric_preview_renders_zhejiang_v2_policy_and_panel(qtbot: object) -> None:
    def anchor(score: int, description: str) -> SimpleNamespace:
        return SimpleNamespace(
            label=str(score), minimum=score, maximum=score, description=description
        )
    dimensions = [
        SimpleNamespace(
            dimension_id=f"criterion-{index}",
            title=title,
            description="本科论文二级指标",
            weight=weight,
            minimum_score=0,
            maximum_score=4,
            checks=["有论文证据"],
            anchors=[anchor(score, f"等级 {score}") for score in range(5)],
            reviewer_tags=["specialist"],
        )
        for index, (title, weight) in enumerate(
            (("选题目的", 10), ("研究意义", 10)), start=1
        )
    ]
    rubric = SimpleNamespace(
        schema_version="2",
        rubric_id="zhejiang-undergraduate",
        version="0.1-experimental",
        title="浙江省本科毕业论文诊断 Rubric",
        scoring_enabled=True,
        applicable_levels=["本科"],
        dimensions=dimensions,
        groups=[
            SimpleNamespace(
                group_id="topic",
                title="选题意义",
                description="选题目的与研究价值",
                dimensions=["criterion-1", "criterion-2"],
            )
        ],
        policy_context=SimpleNamespace(
            source="浙江省教育厅",
            document_number="浙教高教(2021)33号",
            effective_date="2021-11-01",
            source_sha256="a" * 64,
        ),
        rating_scale=[anchor(score, f"等级 {score}") for score in range(5)],
        hard_rules=[
            SimpleNamespace(
                rule_id="integrity",
                description="学术诚信",
                outcome="存在问题风险",
                evidence_required=True,
                requires_human_confirmation=True,
            )
        ],
        panel_strategy=SimpleNamespace(
            initial_count=3,
            supplemental_count=2,
            supplemental_trigger="首轮恰 1 人不合格",
        ),
        evaluation_mode="dual_advisory",
        experimental=True,
        aggregation=SimpleNamespace(method="weighted_rating", passing_score=None),
    )
    preview = RubricPreview()
    qtbot.addWidget(preview)
    preview.set_result(
        RubricValidationResult.model_construct(
            valid=True,
            rubric=rubric,
            weight_total=20,
            profile_compatible=True,
        )
    )

    assert "Schema 2" in preview.metadata.text()
    assert "政策来源：浙江省教育厅" in preview.details.text()
    assert "实验性 Rubric" in preview.details.text()
    assert "Reviewer Profile 覆盖：完整" in preview.details.text()
    assert "一级指标分组" in preview.model.item(0).text()
    assert "选题意义" in preview.model.item(0).child(0).text()
    assert "选题目的" in preview.model.item(0).child(0).child(0).text()
    texts = [preview.model.item(index).text() for index in range(preview.model.rowCount())]
    assert any("硬性规则" in text and "人工确认" in text for text in texts)
    assert any("独立专家面板" in text for text in texts)
