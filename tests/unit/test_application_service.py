from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.application.app_state import GuiPreferences, PreferencesStore
from paper_reviewer.application.models import ReviewRequest
from paper_reviewer.application.orchestrator import _run_config_hash
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.config import load_review_profile, load_rubric
from paper_reviewer.gui.app import run_database_self_test, run_packaging_resource_self_test

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ZHEJIANG_RUBRIC = PROJECT_ROOT / "configs" / "rubrics" / "zhejiang_undergraduate_thesis_v2.yaml"
ZHEJIANG_SPECIALISTS = (
    PROJECT_ROOT
    / "configs"
    / "review_profiles"
    / "zhejiang_undergraduate_specialists_v1.yaml"
)
ZHEJIANG_PANEL = (
    PROJECT_ROOT
    / "configs"
    / "review_profiles"
    / "zhejiang_independent_panel_v1.yaml"
)


class StubCredentials:
    def get(self, provider: str) -> str | None:
        return None

    def has(self, provider: str) -> bool:
        return False


def test_database_packaging_self_test_exercises_aiosqlite() -> None:
    run_database_self_test()


def test_packaging_resource_self_test_covers_policy_and_fluent_assets() -> None:
    run_packaging_resource_self_test()


def test_preferences_round_trip_without_secrets(tmp_path: Path) -> None:
    store = PreferencesStore(tmp_path / "config" / "preferences.json")
    preferences = GuiPreferences(default_provider="deepseek", default_model="deepseek-chat")

    store.save(preferences)

    loaded = store.load()
    assert loaded.default_provider == "deepseek"
    assert "api_key" not in store.path.read_text(encoding="utf-8").lower()


def test_preferences_sanitize_unknown_theme_without_losing_other_values(
    tmp_path: Path,
) -> None:
    store = PreferencesStore(tmp_path / "preferences.json")
    store.path.write_text(
        '{"theme":"future-theme","default_model":"kept-model"}',
        encoding="utf-8",
    )

    loaded = store.load()

    assert loaded.theme == "system"
    assert loaded.default_model == "kept-model"


def test_validate_rubric_returns_structured_errors(tmp_path: Path) -> None:
    rubric = tmp_path / "bad.yaml"
    rubric.write_text("rubric_id: bad\nversion: '1'\nscoring_enabled: true\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "profile_id: test\nversion: '1'\nreviewers:\n  - reviewer_id: r\n"
        "    title: R\n    description: R\n",
        encoding="utf-8",
    )
    service = ReviewApplicationService.__new__(ReviewApplicationService)

    result = service.validate_rubric(rubric, profile_path=profile)

    assert not result.valid
    assert result.errors


def test_validate_rubric_returns_structured_error_for_malformed_yaml(
    tmp_path: Path,
) -> None:
    rubric = tmp_path / "malformed.yaml"
    rubric.write_text("dimensions: [\n  invalid", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "profile_id: test\nversion: '1'\nreviewers:\n  - reviewer_id: r\n"
        "    title: R\n    description: R\n",
        encoding="utf-8",
    )
    service = ReviewApplicationService.__new__(ReviewApplicationService)

    result = service.validate_rubric(rubric, profile_path=profile)

    assert not result.valid
    assert result.errors


def test_validate_zhejiang_rubric_covers_all_criteria_and_warns_experimental() -> None:
    service = ReviewApplicationService.__new__(ReviewApplicationService)

    result = service.validate_rubric(
        ZHEJIANG_RUBRIC,
        profile_path=ZHEJIANG_SPECIALISTS,
    )

    assert result.valid
    assert result.profile_compatible
    assert result.weight_total == 100
    assert result.rubric is not None
    assert len(result.rubric.dimensions) == 9
    assert any("效度" in warning for warning in result.warnings)


def test_validate_zhejiang_rubric_rejects_duplicate_panel_experts(tmp_path: Path) -> None:
    specialists = tmp_path / ZHEJIANG_SPECIALISTS.name
    specialists.write_text(ZHEJIANG_SPECIALISTS.read_text(encoding="utf-8"), encoding="utf-8")
    panel = tmp_path / ZHEJIANG_PANEL.name
    panel.write_text(
        ZHEJIANG_PANEL.read_text(encoding="utf-8").replace(
            "initial-panel-expert-2",
            "initial-panel-expert-1",
        ),
        encoding="utf-8",
    )
    service = ReviewApplicationService.__new__(ReviewApplicationService)

    result = service.validate_rubric(ZHEJIANG_RUBRIC, profile_path=specialists)

    assert not result.valid
    assert not result.profile_compatible
    assert any("5 个唯一专家" in error for error in result.errors)


def test_run_config_hash_covers_panel_and_discipline_context() -> None:
    rubric = load_rubric(ZHEJIANG_RUBRIC)
    specialists = load_review_profile(ZHEJIANG_SPECIALISTS)
    panel = load_review_profile(ZHEJIANG_PANEL)
    baseline = _run_config_hash(
        rubric=rubric,
        profile=specialists,
        panel_profile=panel,
        discipline_name="计算机科学与技术",
        discipline_profile="培养目标 A",
        external_search=True,
    )

    assert baseline != _run_config_hash(
        rubric=rubric,
        profile=specialists,
        panel_profile=panel,
        discipline_name="汉语言文学",
        discipline_profile="培养目标 A",
        external_search=True,
    )
    assert baseline != _run_config_hash(
        rubric=rubric,
        profile=specialists,
        panel_profile=None,
        discipline_name="计算机科学与技术",
        discipline_profile="培养目标 A",
        external_search=True,
    )
    assert baseline != _run_config_hash(
        rubric=rubric,
        profile=specialists,
        panel_profile=panel,
        discipline_name="计算机科学与技术",
        discipline_profile="培养目标 B",
        external_search=False,
    )


@pytest.mark.asyncio
async def test_start_review_rejects_missing_cloud_authorization(tmp_path: Path) -> None:
    service = ReviewApplicationService.__new__(ReviewApplicationService)
    request = ReviewRequest(
        paper=tmp_path / "paper.pdf",
        provider="deepseek",
        model="deepseek-chat",
        rubric=tmp_path / "rubric.yaml",
        profile=tmp_path / "profile.yaml",
        discipline_name="计算机科学与技术",
        cloud_processing_authorized=False,
    )

    with pytest.raises(ValueError, match="处理授权"):
        await service.start_review(request)


@pytest.mark.asyncio
async def test_start_review_rejects_classified_material(tmp_path: Path) -> None:
    service = ReviewApplicationService.__new__(ReviewApplicationService)
    request = ReviewRequest(
        paper=tmp_path / "paper.pdf",
        provider="deepseek",
        model="deepseek-chat",
        rubric=tmp_path / "rubric.yaml",
        profile=tmp_path / "profile.yaml",
        discipline_name="计算机科学与技术",
        cloud_processing_authorized=True,
        contains_classified_material=True,
    )

    with pytest.raises(ValueError, match="涉密"):
        await service.start_review(request)
