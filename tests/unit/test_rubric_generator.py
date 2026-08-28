from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.application import service as service_module
from paper_reviewer.application.app_state import AppPaths
from paper_reviewer.application.review_planner import build_review_plan
from paper_reviewer.application.rubric_generator import (
    RubricPackageStore,
    compile_rubric_generation,
    default_rubric_draft,
    generate_rubric_with_model,
    resolve_companion_profile,
)
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.config import load_review_profile, load_rubric
from paper_reviewer.domain.rubric_generation import (
    CourseAssessmentBrief,
    DimensionPreference,
    RubricGenerationRequest,
    ScoringSettings,
    SubjectAssessmentMode,
)
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall


def _request() -> RubricGenerationRequest:
    return RubricGenerationRequest(
        brief=CourseAssessmentBrief(
            course_name="数据库原理",
            assignment_requirements="完成一篇数据库设计与分析课程论文。",
            learning_outcomes=["能够运用关系模型分析并设计数据库"],
            subject_assessment_mode=SubjectAssessmentMode.SPECIALIST,
            subject_name="计算机科学与技术",
            core_topics=["关系模型", "数据库规范化"],
            dimension_preferences=[
                DimensionPreference(
                    title="课程任务完成度",
                    weight=20,
                    reviewer_role="course_requirements",
                ),
                DimensionPreference(
                    title="数据库专业内容",
                    weight=50,
                    reviewer_role="subject_matter",
                ),
                DimensionPreference(
                    title="论证与表达",
                    weight=30,
                    reviewer_role="argumentation",
                ),
            ],
        ),
        scoring=ScoringSettings(
            minimum_score=0,
            maximum_score=5,
            anchor_count=5,
            passing_score=60,
        ),
    )


def test_compile_builds_course_rubric_and_dynamic_reviewer_profile() -> None:
    request = _request()
    result = compile_rubric_generation(request, default_rubric_draft(request))

    assert result.rubric.evaluation_mode == "course_assessment"
    assert result.rubric.aggregation is not None
    assert result.rubric.aggregation.method == "weighted_mean"
    assert sum(item.weight for item in result.rubric.dimensions) == 100
    assert [item.reviewer_tags for item in result.rubric.dimensions] == [
        ["course_requirements"],
        ["subject_matter"],
        ["argumentation"],
    ]
    assert all(not item.dimension_ids for item in result.profile.reviewers)
    assert len(build_review_plan(result.rubric, result.profile).assignments) == 3
    for dimension in result.rubric.dimensions:
        assert [(item.minimum, item.maximum) for item in dimension.anchors] == [
            (0, 1),
            (2, 2),
            (3, 3),
            (4, 4),
            (5, 5),
        ]


def test_compile_rejects_model_changes_to_teacher_confirmed_weight() -> None:
    request = _request()
    draft = default_rubric_draft(request)
    draft.dimensions[0].weight = 25

    with pytest.raises(ValueError, match="权重被模型擅自修改"):
        compile_rubric_generation(request, draft)


def test_default_hundred_point_scale_uses_teacher_familiar_anchors() -> None:
    request = _request().model_copy(update={"scoring": ScoringSettings()})

    result = compile_rubric_generation(request, default_rubric_draft(request))

    assert [(item.minimum, item.maximum) for item in result.rubric.dimensions[0].anchors] == [
        (0, 39),
        (40, 59),
        (60, 74),
        (75, 89),
        (90, 100),
    ]


def test_package_round_trip_and_companion_resolution(tmp_path: Path) -> None:
    request = _request()
    result = compile_rubric_generation(request, default_rubric_draft(request))
    store = RubricPackageStore(tmp_path / "rubric_packages")

    saved = store.save(result, provider_ref="openai", model="test-model")
    second = store.save(
        result,
        provider_ref="openai",
        model="test-model",
        parent_package_id=saved.manifest.package_id,
    )

    assert saved.manifest.provider_ref == "openai"
    assert saved.manifest.model == "test-model"
    assert saved.manifest.version == "0.1.0-experimental"
    assert second.manifest.version == "0.1.1-experimental"
    assert second.manifest.parent_package_id == saved.manifest.package_id
    assert store.list()[0].manifest.package_id == second.manifest.package_id
    rubric = load_rubric(saved.rubric_path)
    profile = load_review_profile(saved.profile_path)
    build_review_plan(rubric, profile)
    assert resolve_companion_profile(saved.rubric_path) == saved.profile_path


def test_package_tampering_is_rejected(tmp_path: Path) -> None:
    request = _request()
    result = compile_rubric_generation(request, default_rubric_draft(request))
    store = RubricPackageStore(tmp_path / "rubric_packages")
    saved = store.save(result)
    saved.profile_path.write_text("profile_id: changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="文件校验失败"):
        store.load(saved.root)
    assert resolve_companion_profile(saved.rubric_path) is None


class _FakeModel:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="draft",
                    name="submit_rubric_draft",
                    arguments=self.arguments,
                )
            ]
        )


@pytest.mark.asyncio
async def test_model_generation_uses_forced_structured_output() -> None:
    request = _request()
    draft = default_rubric_draft(request)
    model = _FakeModel(draft.model_dump(mode="json"))

    result = await generate_rubric_with_model(
        model=model,
        request=request,
        trace_id="test-rubric",
    )

    assert result.draft == draft
    assert len(model.requests) == 1
    sent = model.requests[0]
    assert sent.forced_tool_name == "submit_rubric_draft"
    assert sent.temperature == 0
    assert sent.tools[0].parameters["additionalProperties"] is False


class _KeyCredentials:
    def get(self, _provider: str) -> str:
        return "test-secret"

    def has(self, _provider: str) -> bool:
        return True


class _ClosableFakeModel(_FakeModel):
    def __init__(self, arguments: dict[str, object]) -> None:
        super().__init__(arguments)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_application_service_generates_saves_and_resolves_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    model = _ClosableFakeModel(default_rubric_draft(request).model_dump(mode="json"))
    monkeypatch.setattr(service_module, "create_model_adapter", lambda *_args, **_kwargs: model)
    paths = AppPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        runs_dir=tmp_path / "runs",
        logs_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
    )
    service = ReviewApplicationService(
        paths=paths,
        credentials=_KeyCredentials(),  # type: ignore[arg-type]
    )

    result = await service.generate_rubric(
        request,
        provider_ref="openai",
        model="test-model",
    )
    saved = service.save_rubric_generation(
        result,
        provider_ref="openai",
        model="test-model",
    )

    assert model.closed
    assert service.list_rubric_packages()[0].rubric_path == saved.rubric_path
    fallback = tmp_path / "fallback.yaml"
    assert service.resolve_profile_for_rubric(
        saved.rubric_path,
        fallback_profile_path=fallback,
    ) == saved.profile_path
