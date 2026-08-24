from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import (
    ArtifactRepository,
    HardRuleDecisionRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.domain.run import RunRecord


def _run() -> RunRecord:
    return RunRecord(
        run_id="evaluation-run",
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="zhejiang@2",
        provider="fake",
        model="fake-model",
    )


@pytest.mark.asyncio
async def test_expert_checkpoints_and_json_reports_are_run_scoped(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'evaluation.db').as_posix()}")
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    await RunRepository(sessions).create(_run())
    reviews = ReviewRepository(sessions)

    await reviews.save_expert_opinion(
        "evaluation-run",
        {"expert_id": "panel-1", "role": "complete_panel", "vote": "qualified"},
    )
    await reviews.save_expert_opinion(
        "evaluation-run",
        {"expert_id": "panel-2", "role": "complete_panel", "vote": "unqualified"},
    )
    await reviews.save_expert_opinion(
        "evaluation-run",
        {"expert_id": "specialist-1", "role": "logic", "score": 3},
    )

    all_opinions = await reviews.list_expert_opinions("evaluation-run")
    panel_opinions = await reviews.list_expert_opinions(
        "evaluation-run", role="complete_panel"
    )
    assert [item["expert_id"] for item in all_opinions] == [
        "panel-1",
        "panel-2",
        "specialist-1",
    ]
    assert [item["expert_id"] for item in panel_opinions] == ["panel-1", "panel-2"]

    await reviews.save_diagnostic_score("evaluation-run", {"total": 75.0, "dimensions": {}})
    await reviews.save_evaluation_report("evaluation-run", {"decision": "advisory"})
    assert await reviews.get_diagnostic_score("evaluation-run") == {
        "dimensions": {},
        "total": 75.0,
    }
    assert await reviews.get_evaluation_report("evaluation-run") == {"decision": "advisory"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_hard_rule_decisions_are_append_only_and_latest_is_queryable(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'rules.db').as_posix()}")
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    await RunRepository(sessions).create(_run())
    rules = HardRuleDecisionRepository(sessions)
    first = await rules.save_decision(
        "evaluation-run",
        {
            "rule_id": "integrity-1",
            "decision": "confirmed",
            "reviewer": "teacher-a",
            "reason": "已核验原文证据",
        },
    )
    second = await rules.save_decision(
        "evaluation-run",
        {
            "rule_id": "integrity-1",
            "decision": "dismissed",
            "reviewer": "teacher-b",
            "reason": "复核后证据不足",
        },
    )
    assert first["confirmed"] is True
    assert second["dismissed"] is True
    assert len(await rules.list_decisions("evaluation-run", rule_id="integrity-1")) == 2
    assert (
        await rules.latest_decisions("evaluation-run")
    )["integrity-1"]["decision"] == "dismissed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_detection_report_payload_is_rejected_and_never_stored(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'guard.db').as_posix()}")
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    await RunRepository(sessions).create(_run())
    artifacts = ArtifactRepository(sessions)
    with pytest.raises(ValueError, match="not persisted"):
        await artifacts.save_json(
            "evaluation-run",
            "evaluation_report",
            {"integrityReportPath": "C:/private/check.html"},
        )
    assert await artifacts.list("evaluation-run") == []
    await engine.dispose()
