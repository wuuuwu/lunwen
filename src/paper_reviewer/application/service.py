from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from yaml import YAMLError

from paper_reviewer.adapters.documents.pymupdf_parser import PyMuPDFParser
from paper_reviewer.adapters.models.factory import create_model_adapter
from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    HardRuleDecisionRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.adapters.scholarly.arxiv import ArxivClient
from paper_reviewer.adapters.scholarly.crossref import CrossrefClient
from paper_reviewer.adapters.scholarly.openalex import OpenAlexClient
from paper_reviewer.adapters.search.ddgs import DdgsWebSearchClient
from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.application.app_state import AppPaths, PreferencesStore, read_json_lines
from paper_reviewer.application.models import (
    ProviderCompatibilityResult,
    ProviderErrorDetails,
    ProviderResponseDiagnostics,
    ReportExportFormat,
    ReportExportResult,
    ReportView,
    ReviewRequest,
    RubricValidationResult,
    RunDetail,
    RunEvent,
    RunSummary,
)
from paper_reviewer.application.orchestrator import (
    ReviewOrchestrator,
    load_provider_snapshot,
    load_run_request_context,
    load_run_snapshots,
)
from paper_reviewer.application.providers import (
    CustomProviderRegistry,
    ProviderStore,
    builtin_provider_connections,
)
from paper_reviewer.application.review_planner import build_review_plan
from paper_reviewer.application.state_machine import transition
from paper_reviewer.config import Settings, load_review_profile, load_rubric
from paper_reviewer.domain.document import DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.provider import (
    CustomProviderProfile,
    ModelApiProtocol,
    ProviderConnection,
    ProviderSnapshot,
    normalize_base_url,
)
from paper_reviewer.domain.review import (
    EvaluationReport,
    HardRuleAssessment,
    HardRuleStatus,
    HumanPanelDecision,
    HumanReviewSummary,
    HumanRuleDecision,
    MetaReview,
    PanelDecision,
    PanelOutcome,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.ports.model import Message, ModelRequest, ToolSpec
from paper_reviewer.reporting.exporter import render_pdf, validate_pdf
from paper_reviewer.reporting.renderer import render_markdown, write_report_bundle
from paper_reviewer.validation.audits import AuditReport, audit_evaluation_report
from paper_reviewer.validation.panel import (
    build_human_review_summary,
    decide_expert_panel,
    decide_panel,
)
from paper_reviewer.validation.scoring import aggregate_scores

EventSink = Callable[[RunEvent], None]


def _validate_cloud_processing_request(request: ReviewRequest) -> None:
    if not request.cloud_processing_authorized:
        raise ValueError("开始云端评测前必须确认已获得论文处理授权。")
    if request.contains_classified_material:
        raise ValueError("涉密材料不得提交云端评测。")
    if request.discipline_profile is not None and not request.discipline_profile.is_file():
        raise ValueError(f"专业培养目标 YAML 不存在：{request.discipline_profile}")


def _is_dual_advisory_rubric(rubric: RubricProfile) -> bool:
    return (
        getattr(rubric, "evaluation_mode", None) == "dual_advisory" or rubric.schema_version == "2"
    )


def _resolve_panel_profile_path(review_profile_path: Path) -> Path:
    filename = "zhejiang_independent_panel_v1.yaml"
    packaged = Path(str(resources.files("paper_reviewer.resources").joinpath("configs", filename)))
    candidates = (
        review_profile_path.with_name(filename),
        Path(__file__).resolve().parents[3] / "configs" / "review_profiles" / filename,
        Path.cwd() / "configs" / "review_profiles" / filename,
        packaged,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"找不到独立专家面板配置：{filename}")


class ReviewApplicationService:
    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        credentials: SystemCredentialStore | None = None,
    ) -> None:
        self.paths = paths or AppPaths.for_current_user()
        self.paths.ensure()
        self.credentials = credentials or SystemCredentialStore()
        self.settings = Settings(
            database_url=self.paths.database_url,
            runs_dir=self.paths.runs_dir,
        )
        self.providers = CustomProviderRegistry(
            ProviderStore(self.paths.providers_path), self.credentials
        )

    def list_provider_connections(
        self, *, include_archived: bool = False
    ) -> list[ProviderConnection]:
        connections = list(builtin_provider_connections())
        connections.extend(
            self.providers.resolve(item.provider_ref)
            for item in self.providers.list(include_archived=include_archived)
        )
        return connections

    def list_custom_providers(
        self, *, include_archived: bool = False
    ) -> list[CustomProviderProfile]:
        return self.providers.list(include_archived=include_archived)

    def create_custom_provider(
        self,
        *,
        display_name: str,
        protocol: ModelApiProtocol,
        base_url: str,
        default_model: str,
        api_key: str,
    ) -> CustomProviderProfile:
        return self.providers.create(
            display_name=display_name,
            protocol=protocol,
            base_url=base_url,
            default_model=default_model,
            api_key=api_key,
        )

    def update_custom_provider(
        self,
        provider_ref: str,
        *,
        display_name: str | None = None,
        default_model: str | None = None,
    ) -> CustomProviderProfile:
        return self.providers.update(
            provider_ref, display_name=display_name, default_model=default_model
        )

    def archive_custom_provider(self, provider_ref: str) -> CustomProviderProfile:
        preferences = PreferencesStore(self.paths.preferences_path).load()
        if preferences.default_provider == provider_ref:
            raise ValueError("请先更换默认 Provider，再归档当前配置。")
        return self.providers.archive(provider_ref)

    def restore_custom_provider(self, provider_ref: str) -> CustomProviderProfile:
        return self.providers.restore(provider_ref)

    def replace_custom_provider_endpoint(
        self,
        provider_ref: str,
        *,
        protocol: ModelApiProtocol,
        base_url: str,
        api_key: str,
        display_name: str | None = None,
        default_model: str | None = None,
    ) -> CustomProviderProfile:
        return self.providers.replace_endpoint(
            provider_ref,
            protocol=protocol,
            base_url=base_url,
            api_key=api_key,
            display_name=display_name,
            default_model=default_model,
        )

    async def delete_custom_provider(self, provider_ref: str) -> None:
        if await self._provider_is_referenced(provider_ref):
            raise ValueError("该 Provider 仍被历史任务引用，不能永久删除")
        self.providers.delete(provider_ref)

    def rotate_custom_provider_key(self, provider_ref: str, api_key: str) -> None:
        self.providers.rotate_key(provider_ref, api_key)

    def delete_custom_provider_key(self, provider_ref: str) -> None:
        self.providers.delete_key(provider_ref)

    def provider_has_key(self, provider_ref: str) -> bool:
        return self.providers.has_key(provider_ref)

    async def test_provider_compatibility(
        self,
        provider_ref: str | None = None,
        *,
        protocol: ModelApiProtocol | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> ProviderCompatibilityResult:
        if provider_ref is not None:
            connection = self.providers.resolve(provider_ref)
            selected_model = (model or connection.default_model).strip()
            selected_protocol = connection.protocol
            selected_base_url = connection.base_url
            resolved_key = api_key or self.providers.get_api_key(provider_ref)
            adapter_provider_ref = provider_ref
        else:
            if protocol is None or base_url is None or model is None:
                raise ValueError("未保存的 Provider 测试需要协议、Base URL 和模型。")
            selected_protocol = protocol
            selected_base_url = normalize_base_url(base_url)
            selected_model = model.strip()
            resolved_key = api_key
            adapter_provider_ref = f"custom:{'0' * 32}"
        if not selected_model:
            raise ValueError("模型名称不能为空。")
        if not resolved_key:
            return ProviderCompatibilityResult(
                compatible=False,
                message="未配置 API Key。",
                protocol=selected_protocol,
            )
        adapter = create_model_adapter(
            adapter_provider_ref,
            selected_model,
            api_key=resolved_key,
            protocol=selected_protocol,
            base_url=selected_base_url,
            timeout=30,
        )
        try:
            async with asyncio.timeout(30):
                response = await adapter.complete_once(
                    ModelRequest(
                        messages=[
                            Message(
                                role="user",
                                content=(
                                    "Call the paper_reviewer_compatibility_probe tool "
                                    "exactly once with ok=true. Do not answer with text."
                                ),
                            )
                        ],
                        tools=[
                            ToolSpec(
                                name="paper_reviewer_compatibility_probe",
                                description="Verify function-tool compatibility.",
                                parameters={
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                    "required": ["ok"],
                                    "additionalProperties": False,
                                },
                            )
                        ],
                        max_output_tokens=1024,
                        trace_id="provider-compatibility-test",
                        idempotency_key="provider-compatibility-test",
                        forced_tool_name="paper_reviewer_compatibility_probe",
                    )
                )
            compatible = any(
                call.name == "paper_reviewer_compatibility_probe"
                and call.arguments.get("ok") is True
                for call in response.tool_calls
            )
            return ProviderCompatibilityResult(
                compatible=compatible,
                message=(
                    "Provider 支持所选协议和 Agent 工具调用。"
                    if compatible
                    else "Provider 可响应，但没有产生所需的 Agent 工具调用。"
                ),
                protocol=selected_protocol,
                response_diagnostics=_provider_response_diagnostics(response),
            )
        except Exception as error:
            error_details = _extract_provider_error_details(error, secrets=(resolved_key,))
            return ProviderCompatibilityResult(
                compatible=False,
                message=_sanitize_provider_error(error, secrets=(resolved_key,)),
                protocol=selected_protocol,
                error_details=error_details,
                response_diagnostics=_provider_error_response_diagnostics(error),
            )
        finally:
            await adapter.close()

    def validate_rubric(self, path: Path, *, profile_path: Path) -> RubricValidationResult:
        try:
            rubric = load_rubric(path)
        except (OSError, ValueError, ValidationError, YAMLError) as error:
            return RubricValidationResult(valid=False, errors=_validation_messages(error))
        warnings: list[str] = []
        errors: list[str] = []
        profile_compatible = False
        try:
            profile = load_review_profile(profile_path)
            build_review_plan(rubric, profile)
            if _is_dual_advisory_rubric(rubric):
                specialist_ids = [item.reviewer_id for item in profile.reviewers]
                if len(specialist_ids) != 5 or len(set(specialist_ids)) != 5:
                    raise ValueError("专业化 Reviewer 配置必须包含 5 个唯一角色。")
                panel_profile = load_review_profile(_resolve_panel_profile_path(profile_path))
                panel_ids = [item.reviewer_id for item in panel_profile.reviewers]
                if len(panel_ids) != 5 or len(set(panel_ids)) != 5:
                    raise ValueError(
                        "独立专家面板配置必须包含 5 个唯一专家（前 3 初评、后 2 复评）。"
                    )
                expected_dimensions = {item.dimension_id for item in rubric.dimensions}
                panel_plan = build_review_plan(rubric, panel_profile)
                if any(
                    set(assignment.dimension_ids) != expected_dimensions
                    for assignment in panel_plan.assignments
                ):
                    raise ValueError("每名独立专家必须覆盖当前 Rubric 的全部评分维度。")
            profile_compatible = True
        except (OSError, ValueError, ValidationError, YAMLError) as error:
            errors.extend(_validation_messages(error))
        if not rubric.scoring_enabled:
            warnings.append("当前 Rubric 未启用评分，只会生成评语。")
        if not rubric.applicable_levels:
            warnings.append("未声明适用学历层级。")
        if rubric.experimental:
            warnings.append(rubric.validation_notice or "当前 Rubric 未完成教育测量效度验证。")
        weight_total = sum(dimension.weight for dimension in rubric.dimensions)
        return RubricValidationResult(
            valid=not errors,
            rubric=rubric,
            errors=errors,
            warnings=warnings,
            weight_total=weight_total,
            profile_compatible=profile_compatible,
        )

    async def start_review(
        self, request: ReviewRequest, *, event_sink: EventSink | None = None
    ) -> RunRecord:
        _validate_cloud_processing_request(request)
        rubric = load_rubric(request.rubric)
        profile = load_review_profile(request.profile)
        build_review_plan(rubric, profile)
        panel_profile = None
        if _is_dual_advisory_rubric(rubric):
            panel_profile = load_review_profile(_resolve_panel_profile_path(request.profile))
        if request.provider.startswith("custom:"):
            profile_entry = self.providers.get(request.provider)
            if profile_entry.is_archived:
                raise ValueError("归档的自定义 Provider 不能用于创建新任务。")
        provider_snapshot = self.providers.snapshot(request.provider, request.model)
        api_key = self.providers.get_snapshot_api_key(provider_snapshot)
        if not api_key:
            raise ValueError("所选 Provider 未配置 API Key。")
        model = _adapter_from_snapshot(
            provider_snapshot,
            api_key=api_key,
            timeout=self.settings.request_timeout_seconds,
        )
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        sessions = create_session_factory(engine)
        async with httpx.AsyncClient(timeout=self.settings.external_timeout_seconds) as client:
            scholarly: list[Any] = (
                [OpenAlexClient(client), CrossrefClient(client), ArxivClient(client)]
                if request.external_search
                else []
            )
            web_search = _web_search_client(self.settings) if request.external_search else None
            orchestrator = self._orchestrator(
                model, sessions, scholarly, web_search, event_sink
            )
            try:
                return await orchestrator.create_and_execute(
                    input_path=request.paper,
                    rubric=rubric,
                    profile=profile,
                    provider=request.provider,
                    model_name=request.model,
                    discipline_name=request.discipline_name,
                    discipline_profile=request.discipline_profile,
                    panel_profile=panel_profile,
                    cloud_processing_authorized=request.cloud_processing_authorized,
                    contains_classified_material=request.contains_classified_material,
                    external_search=request.external_search,
                    provider_snapshot=provider_snapshot,
                )
            finally:
                await model.close()
                await engine.dispose()

    async def resume_review(self, run_id: str, *, event_sink: EventSink | None = None) -> RunRecord:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        sessions = create_session_factory(engine)
        repository = RunRepository(sessions)
        run = await repository.get(run_id)
        if run is None:
            await engine.dispose()
            raise ValueError(f"未知任务：{run_id}")
        rubric, profile = load_run_snapshots(self.settings.runs_dir / run_id)
        panel_path = self.settings.runs_dir / run_id / "panel-profile.json"
        panel_profile = load_review_profile(panel_path) if panel_path.is_file() else None
        run_dir = self.settings.runs_dir / run_id
        provider_snapshot = load_provider_snapshot(run_dir)
        if provider_snapshot is None:
            if run.provider not in {"openai", "deepseek"}:
                await engine.dispose()
                raise ValueError(
                    "该任务缺少 Provider 快照；自定义 Provider 或 Responses 任务"
                    "请使用桌面端重新创建。"
                )
            provider_snapshot = self.providers.snapshot(run.provider, run.model)
        api_key = self.providers.get_snapshot_api_key(provider_snapshot)
        if not api_key:
            await engine.dispose()
            raise ValueError("恢复任务所需的 API Key 不存在，请先在设置中重新配置。")
        model = _adapter_from_snapshot(
            provider_snapshot,
            api_key=api_key,
            timeout=self.settings.request_timeout_seconds,
        )
        request_context = load_run_request_context(run_dir)
        external_search = request_context.get("external_search", True) is not False
        async with httpx.AsyncClient(timeout=self.settings.external_timeout_seconds) as client:
            scholarly: list[Any] = (
                [OpenAlexClient(client), CrossrefClient(client), ArxivClient(client)]
                if external_search
                else []
            )
            web_search = _web_search_client(self.settings) if external_search else None
            orchestrator = self._orchestrator(
                model, sessions, scholarly, web_search, event_sink
            )
            try:
                return await orchestrator.execute(
                    run,
                    rubric=rubric,
                    profile=profile,
                    panel_profile=panel_profile,
                )
            finally:
                await model.close()
                await engine.dispose()

    async def get_pending_hard_rules(self, run_id: str) -> list[HardRuleAssessment]:
        """Return unresolved AI-suspected or unassessable hard rules.

        Hard-rule artifacts are deliberately stored with the run snapshot rather
        than inferred from trace messages, so a cancelled/restarted desktop app
        observes the same human-review gate.
        """

        await self._require_run(run_id)
        return await self._pending_hard_rules(run_id)

    async def get_pending_human_reviews(self, run_id: str) -> HumanReviewSummary:
        await self._require_run(run_id)
        return await self._human_review_summary(run_id)

    async def resolve_hard_rule(
        self, run_id: str, decision: HumanRuleDecision
    ) -> HumanRuleDecision:
        await self._require_run(run_id)
        normalized = HumanRuleDecision.model_validate(decision)
        pending = {item.rule_id for item in await self._pending_hard_rules(run_id)}
        if normalized.rule_id not in pending:
            raise ValueError(f"否决项不在待确认列表中：{normalized.rule_id}")

        path = self.settings.runs_dir / run_id / "human-rule-decisions.json"
        decisions = await self._human_rule_decisions(run_id)
        if any(item.rule_id == normalized.rule_id for item in decisions):
            raise ValueError(f"否决项已经处理：{normalized.rule_id}")
        decisions.append(normalized)
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        try:
            await HardRuleDecisionRepository(
                create_session_factory(engine)
            ).save_human_rule_decision(
                run_id,
                normalized,
                reason=normalized.rationale,
                timestamp=normalized.decided_at,
            )
        finally:
            await engine.dispose()
        _write_model_list(path, decisions)
        if (self.settings.runs_dir / run_id / "evaluation-report.json").is_file():
            await self.refresh_after_human_review(run_id)
        return normalized

    async def resolve_panel_review(
        self, run_id: str, decision: HumanPanelDecision
    ) -> HumanPanelDecision:
        await self._require_run(run_id)
        normalized = HumanPanelDecision.model_validate(decision)
        summary = await self._human_review_summary(run_id)
        if not summary.panel_review_required:
            raise ValueError("当前任务没有待处理的人工面板复核。")
        path = self.settings.runs_dir / run_id / "human-panel-decision.json"
        if path.is_file():
            raise ValueError("人工面板复核已经处理。")
        _write_model_atomic(path, normalized)
        if (self.settings.runs_dir / run_id / "evaluation-report.json").is_file():
            await self.refresh_after_human_review(run_id)
        return normalized

    async def refresh_after_human_review(self, run_id: str) -> RunRecord:
        """Recompute deterministic conclusions and reports without any model call."""

        run = await self._require_run(run_id)
        run_dir = self.settings.runs_dir / run_id
        rubric, selected_report, audit = _load_export_report_snapshot(run_dir)
        if not isinstance(selected_report, EvaluationReport):
            raise ValueError("该任务不是可刷新人工复核的双层评测报告。")
        decisions = await self._human_rule_decisions(run_id)
        human_panel_decision = _load_optional_model(
            run_dir / "human-panel-decision.json", HumanPanelDecision
        )
        opinions = selected_report.expert_opinions
        initial = [item for item in opinions if item.round == "initial"]
        supplemental = [item for item in opinions if item.round == "supplemental"]
        expert_panel_decision = decide_expert_panel(
            initial=initial,
            supplemental=supplemental,
        )
        panel_decision = decide_panel(
            initial=initial,
            supplemental=supplemental,
            hard_rules=selected_report.hard_rule_assessments,
            human_decisions=decisions,
            human_panel_decision=human_panel_decision,
        )
        summary = build_human_review_summary(
            hard_rules=selected_report.hard_rule_assessments,
            human_decisions=decisions,
            expert_panel_decision=expert_panel_decision,
            human_panel_decision=human_panel_decision,
        )
        updated = selected_report.model_copy(
            update={
                "human_rule_decisions": decisions,
                "expert_panel_decision": expert_panel_decision,
                "human_panel_decision": human_panel_decision,
                "human_review_summary": summary,
                "panel_decision": panel_decision,
            },
            deep=True,
        )
        report_audit = audit_evaluation_report(report=updated)
        if not report_audit.passed:
            raise ValueError(
                "人工复核后的确定性审计失败：" + "; ".join(report_audit.errors)
            )

        target_status = (
            RunStatus.REPORTED if summary.complete else RunStatus.REPORTED_PENDING_HUMAN_REVIEW
        )
        if run.status is RunStatus.REPORTED_PENDING_HUMAN_REVIEW and summary.complete:
            run.status = transition(run.status, target_status)
        elif run.status in {
            RunStatus.REPORTED,
            RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
        }:
            run.status = target_status
        else:
            raise ValueError("仅已生成报告的任务可以刷新人工复核结论。")
        run.error = None

        evidence_path = run_dir / "evidence.json"
        evidence = (
            [
                EvidenceItem.model_validate(item)
                for item in json.loads(evidence_path.read_text(encoding="utf-8"))
            ]
            if evidence_path.is_file()
            else []
        )
        _write_model_atomic(run_dir / "evaluation-report.json", updated)
        write_report_bundle(
            run_dir=run_dir,
            run=run,
            rubric=rubric,
            review=updated,
            audit=audit,
            evidence=evidence,
        )
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        sessions = create_session_factory(engine)
        try:
            repository = ReviewRepository(sessions)
            await repository.save_panel_decision(run_id, panel_decision)
            await repository.save_evaluation_report(run_id, updated)
            await RunRepository(sessions).save(
                run,
                event_type="human_review_report_refreshed",
                payload={"status": run.status.value, "pending_count": summary.pending_count},
            )
        finally:
            await engine.dispose()
        return run

    async def resume_after_human_review(
        self, run_id: str, *, event_sink: EventSink | None = None
    ) -> RunRecord:
        await self._require_run(run_id)
        if (self.settings.runs_dir / run_id / "evaluation-report.json").is_file():
            return await self.refresh_after_human_review(run_id)
        return await self.resume_review(run_id, event_sink=event_sink)

    async def cancel_review(self, run_id: str) -> RunRecord:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        repository = RunRepository(create_session_factory(engine))
        try:
            run = await repository.get(run_id)
            if run is None:
                raise ValueError(f"未知任务：{run_id}")
            if run.status in {
                RunStatus.REPORTED,
                RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
                RunStatus.FATAL_FAILURE,
                RunStatus.CANCELLED,
            }:
                return run
            run.status = transition(run.status, RunStatus.CANCELLED)
            run.error = None
            await repository.save(run, event_type="run_cancelled", payload={})
            return run
        finally:
            await engine.dispose()

    async def list_runs(
        self, *, search: str = "", status: RunStatus | None = None
    ) -> list[RunSummary]:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        repository = RunRepository(create_session_factory(engine))
        try:
            records = await repository.list(status=status)
        finally:
            await engine.dispose()
        needle = search.casefold().strip()
        summaries = []
        for record in records:
            snapshot = _provider_snapshot_for_display(
                self.settings.runs_dir / record.run_id, record
            )
            summaries.append(
                RunSummary.from_record(
                    record,
                    provider_display_name=snapshot.display_name if snapshot else None,
                    provider_protocol=snapshot.protocol if snapshot else None,
                )
            )
        if not needle:
            return summaries
        return [item for item in summaries if needle in item.paper_name.casefold()]

    async def get_run(self, run_id: str) -> RunDetail:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        repository = RunRepository(create_session_factory(engine))
        try:
            run = await repository.get(run_id)
        finally:
            await engine.dispose()
        if run is None:
            raise ValueError(f"未知任务：{run_id}")
        events = _load_trace_events(self.settings.runs_dir / run_id / "trace.jsonl", run_id)
        snapshot = _provider_snapshot_for_display(self.settings.runs_dir / run_id, run)
        human_review_summary = await self._human_review_summary(run_id)
        human_panel_decision = _load_optional_model(
            self.settings.runs_dir / run_id / "human-panel-decision.json",
            HumanPanelDecision,
        )
        return RunDetail(
            run=run,
            provider_display_name=snapshot.display_name if snapshot else None,
            provider_protocol=snapshot.protocol if snapshot else None,
            events=events,
            pending_hard_rules=await self._pending_hard_rules(run_id),
            human_rule_decisions=await self._human_rule_decisions(run_id),
            human_review_summary=human_review_summary,
            human_panel_decision=human_panel_decision,
        )

    async def load_report(self, run_id: str) -> ReportView:
        detail = await self.get_run(run_id)
        run_dir = self.settings.runs_dir / run_id
        rubric, selected_report, audit = _load_export_report_snapshot(run_dir)
        evaluation = selected_report if isinstance(selected_report, EvaluationReport) else None
        review = (
            selected_report.meta_review
            if isinstance(selected_report, EvaluationReport)
            else selected_report
        )
        document_path = run_dir / "document.json"
        evidence_path = run_dir / "evidence.json"
        document = (
            DocumentInfo.model_validate_json(document_path.read_text(encoding="utf-8"))
            if document_path.is_file()
            else None
        )
        evidence = (
            [
                EvidenceItem.model_validate(item)
                for item in json.loads(evidence_path.read_text("utf-8"))
            ]
            if evidence_path.is_file()
            else []
        )
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        try:
            results = await ReviewRepository(create_session_factory(engine)).list_results(run_id)
        finally:
            await engine.dispose()
        dimension_scores = (
            {
                item.criterion_id: float(item.rating)
                for item in evaluation.diagnostic_score.assessments
            }
            if evaluation is not None
            else (aggregate_scores(rubric, results).dimension_scores if results else {})
        )
        return ReportView(
            run=detail.run,
            provider_display_name=detail.provider_display_name,
            provider_protocol=detail.provider_protocol,
            document=document,
            rubric=rubric,
            review=review,
            audit=audit,
            evidence=evidence,
            dimension_scores=dimension_scores,
            report_markdown=run_dir / "report.md",
            report_json=run_dir / "report.json",
            evaluation=evaluation,
            human_review_summary=detail.human_review_summary,
            pending_hard_rules=detail.pending_hard_rules,
            human_panel_decision=detail.human_panel_decision,
        )

    async def export_report(
        self,
        run_id: str,
        export_format: ReportExportFormat,
        destination: Path,
        *,
        overwrite: bool = False,
    ) -> ReportExportResult:
        """Export an immutable report snapshot without changing run state."""

        try:
            normalized_format = ReportExportFormat(export_format)
        except ValueError as error:
            raise ValueError(f"不支持的报告导出格式：{export_format}") from error
        run_dir = _validated_run_dir(self.settings.runs_dir, run_id)
        run = await self._read_run_for_export(run_id)
        if run.status not in {
            RunStatus.REPORTED,
            RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
        }:
            raise ValueError("仅已生成报告的任务可以导出。")

        output = _validated_export_destination(
            destination,
            export_format=normalized_format,
            runs_dir=self.settings.runs_dir,
            overwrite=overwrite,
        )
        source = run_dir / "report.md"
        reconstructed = not source.is_file()
        if reconstructed:
            rubric, selected_report, audit = _load_export_report_snapshot(run_dir)
            markdown_bytes = render_markdown(
                rubric,
                selected_report,
                audit,
                provider_snapshot=load_provider_snapshot(run_dir),
                provider_ref=run.provider,
                model=run.model,
            ).encode("utf-8")
        else:
            markdown_bytes = source.read_bytes()

        temporary = _create_export_temporary(output)
        try:
            if normalized_format is ReportExportFormat.MARKDOWN:
                if reconstructed:
                    temporary.write_bytes(markdown_bytes)
                else:
                    shutil.copyfile(source, temporary)
            else:
                try:
                    markdown = markdown_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError("规范 Markdown 报告不是有效的 UTF-8 文本。") from error
                title = _markdown_title(markdown) or Path(run.input_path).stem
                render_pdf(markdown, temporary, title=title)
                validate_pdf(temporary, markdown)

            if output.exists() and not overwrite:
                raise FileExistsError(f"目标文件已存在：{output}")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

        return ReportExportResult(
            path=output,
            format=normalized_format,
            size_bytes=output.stat().st_size,
            reconstructed_from_snapshot=reconstructed,
        )

    async def _read_run_for_export(self, run_id: str) -> RunRecord:
        """Read export eligibility without running migrations or update hooks."""

        engine = create_engine(self.settings.database_url)
        try:
            run = await RunRepository(create_session_factory(engine)).get(run_id)
        finally:
            await engine.dispose()
        if run is None:
            raise ValueError(f"未知任务：{run_id}")
        return run

    def _orchestrator(
        self,
        model: Any,
        sessions: Any,
        scholarly: list[Any],
        web_search: Any | None,
        event_sink: EventSink | None,
    ) -> ReviewOrchestrator:
        return ReviewOrchestrator(
            settings=self.settings,
            model=model,
            parser=PyMuPDFParser(),
            run_repository=RunRepository(sessions),
            document_repository=DocumentRepository(sessions),
            evidence_repository=EvidenceRepository(sessions),
            review_repository=ReviewRepository(sessions),
            scholarly_clients=scholarly,
            web_search_client=web_search,
            event_sink=event_sink,
        )

    async def _require_run(self, run_id: str) -> RunRecord:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        try:
            run = await RunRepository(create_session_factory(engine)).get(run_id)
        finally:
            await engine.dispose()
        if run is None:
            raise ValueError(f"未知任务：{run_id}")
        return run

    async def _provider_is_referenced(self, provider_ref: str) -> bool:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        try:
            records = await RunRepository(create_session_factory(engine)).list()
        finally:
            await engine.dispose()
        return any(record.provider == provider_ref for record in records)

    async def _pending_hard_rules(self, run_id: str) -> list[HardRuleAssessment]:
        run_dir = self.settings.runs_dir / run_id
        assessments = _load_hard_rule_assessments(run_dir / "hard-rule-assessments.json")
        resolved = {item.rule_id for item in await self._human_rule_decisions(run_id)}

        return [
            item
            for item in assessments
            if item.status in {HardRuleStatus.SUSPECTED, HardRuleStatus.NOT_ASSESSABLE}
            and item.rule_id not in resolved
        ]

    async def _human_rule_decisions(self, run_id: str) -> list[HumanRuleDecision]:
        run_dir = self.settings.runs_dir / run_id
        decisions = _load_human_rule_decisions(run_dir / "human-rule-decisions.json")
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        try:
            persisted = await HardRuleDecisionRepository(
                create_session_factory(engine)
            ).list_human_rule_decisions(run_id)
        finally:
            await engine.dispose()
        by_rule = {item.rule_id: item for item in decisions}
        for item in persisted:
            decision = HumanRuleDecision.model_validate(
                {
                    "rule_id": item.get("rule_id"),
                    "decision": item.get("decision"),
                    "reviewer": item.get("reviewer"),
                    "rationale": item.get("reason"),
                    "decided_at": item.get("decided_at") or item.get("timestamp"),
                }
            )
            by_rule[decision.rule_id] = decision
        return list(by_rule.values())

    async def _human_review_summary(self, run_id: str) -> HumanReviewSummary:
        run_dir = self.settings.runs_dir / run_id
        pending_rules = await self._pending_hard_rules(run_id)
        human_panel_decision = _load_optional_model(
            run_dir / "human-panel-decision.json", HumanPanelDecision
        )
        expert_panel_decision = _load_optional_model(
            run_dir / "expert-panel-decision.json", PanelDecision
        )
        if expert_panel_decision is None:
            evaluation = _load_optional_model(
                run_dir / "evaluation-report.json", EvaluationReport
            )
            if evaluation is not None:
                expert_panel_decision = evaluation.expert_panel_decision
        return HumanReviewSummary(
            pending_hard_rule_ids=[item.rule_id for item in pending_rules],
            panel_review_required=(
                expert_panel_decision is not None
                and expert_panel_decision.outcome is PanelOutcome.AWAITING_PANEL_REVIEW
                and human_panel_decision is None
            ),
        )


def _web_search_client(settings: Settings) -> DdgsWebSearchClient:
    return DdgsWebSearchClient(
        backend=settings.web_search_backend,
        region=settings.web_search_region,
        safesearch=settings.web_search_safesearch,
        min_interval_seconds=settings.web_search_min_interval_seconds,
        timeout_seconds=settings.external_timeout_seconds,
    )


def _validation_messages(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        ]
    return [str(error)]


def _adapter_from_snapshot(
    snapshot: ProviderSnapshot, *, api_key: str, timeout: float
) -> Any:
    return create_model_adapter(
        snapshot.provider_ref,
        snapshot.model,
        timeout=timeout,
        api_key=api_key,
        protocol=snapshot.protocol,
        base_url=snapshot.base_url,
    )


def _provider_snapshot_for_display(
    run_dir: Path, run: RunRecord
) -> ProviderSnapshot | None:
    snapshot = load_provider_snapshot(run_dir)
    if snapshot is not None:
        return snapshot
    for connection in builtin_provider_connections():
        if connection.provider_ref == run.provider and run.provider in {"openai", "deepseek"}:
            return ProviderSnapshot(
                provider_ref=connection.provider_ref,
                display_name=connection.display_name,
                protocol=connection.protocol,
                base_url=connection.base_url,
                endpoint_fingerprint=connection.endpoint_fingerprint,
                model=run.model,
            )
    return None


_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LIKELY_KEY_PATTERN = re.compile(r"\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s\]\[(){}<>\"']+", re.IGNORECASE)
_NAMED_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|secret)\s*[:=]\s*)[^\s,;]+"
)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_PROVIDER_DETAIL_LIMITS = {"message": 300, "code": 80, "param": 120}
_RESPONSE_STATUSES = {
    "completed",
    "incomplete",
    "failed",
    "cancelled",
    "queued",
    "in_progress",
}
_INCOMPLETE_REASONS = {"max_output_tokens", "content_filter"}
_FINISH_REASONS = {"stop", "tool_calls", "length", "content_filter", "incomplete"}
_OUTPUT_ITEM_TYPES = {
    "message",
    "reasoning",
    "function_call",
    "function_call_output",
    "computer_call",
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "mcp_call",
    "mcp_list_tools",
    "local_shell_call",
    "custom_tool_call",
}


def _provider_response_diagnostics(response: Any) -> ProviderResponseDiagnostics:
    """Build bounded response metadata without copying model-generated content."""

    status = _diagnostic_value(response.response_status, _RESPONSE_STATUSES)
    incomplete_reason = _diagnostic_value(
        response.incomplete_reason, _INCOMPLETE_REASONS
    )
    finish_reason = _diagnostic_value(response.finish_reason, _FINISH_REASONS)
    item_types: list[str] = []
    for raw_type in response.output_item_types[:12]:
        item_type = raw_type if raw_type in _OUTPUT_ITEM_TYPES else "unknown"
        if item_type not in item_types:
            item_types.append(item_type)
    return ProviderResponseDiagnostics(
        response_status=status,
        incomplete_reason=incomplete_reason,
        finish_reason=finish_reason,
        output_item_types=item_types,
        plain_text_only=response.plain_text_only,
    )


def _provider_error_response_diagnostics(
    error: BaseException,
) -> ProviderResponseDiagnostics | None:
    if not hasattr(error, "response_status"):
        return None
    return _provider_response_diagnostics(error)


def _diagnostic_value(value: object, allowed: set[str]) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "unknown"


def _extract_provider_error_details(
    error: BaseException, *, secrets: tuple[str, ...] = ()
) -> ProviderErrorDetails | None:
    """Extract only display-safe message/code/param fields from an SDK error.

    The response body is never stringified. Unknown fields, headers, request
    content, and nested objects are deliberately ignored.
    """

    body = getattr(error, "body", None)
    payload: Mapping[object, object] | None = body if isinstance(body, Mapping) else None
    if payload is not None:
        nested = payload.get("error")
        if isinstance(nested, Mapping):
            payload = nested

    values: dict[str, str | None] = {"message": None, "code": None, "param": None}
    for field, limit in _PROVIDER_DETAIL_LIMITS.items():
        candidate: object | None = payload.get(field) if payload is not None else None
        if candidate is None and field in {"code", "param"}:
            candidate = getattr(error, field, None)
        values[field] = _sanitize_provider_detail(candidate, secrets=secrets, limit=limit)

    details = ProviderErrorDetails.model_validate(values)
    return details if any((details.message, details.code, details.param)) else None


def _sanitize_provider_detail(value: object, *, secrets: tuple[str, ...], limit: int) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return None
    if not isinstance(value, (str, int, float, bool)):
        return None
    message = _CONTROL_PATTERN.sub(" ", str(value))
    message = " ".join(message.split())
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[API Key 已隐藏]")
    message = _AUTHORIZATION_PATTERN.sub(r"\1[已隐藏]", message)
    message = _BEARER_PATTERN.sub("Bearer [已隐藏]", message)
    message = _LIKELY_KEY_PATTERN.sub("[API Key 已隐藏]", message)
    message = _NAMED_SECRET_PATTERN.sub(r"\1[已隐藏]", message)
    message = _URL_PATTERN.sub("[Provider URL 已隐藏]", message)
    message = message.strip()
    if not message:
        return None
    if len(message) > limit:
        return message[: limit - 1].rstrip() + "…"
    return message


def _sanitize_provider_error(error: BaseException, *, secrets: tuple[str, ...] = ()) -> str:
    """Return a bounded provider error without credentials, URLs, or response bodies."""

    module = type(error).__module__
    if module == "openai" or module.startswith("openai."):
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            reason = {
                400: "Provider 拒绝了请求；请检查所选协议、模型和请求参数。",
                401: "Provider 认证失败；请检查 API Key。",
                403: "Provider 拒绝访问；请检查账号或模型权限。",
                404: "Provider 未找到接口或模型；请检查 Base URL、协议和模型名称。",
                408: "Provider 请求超时。",
                409: "Provider 拒绝了当前请求状态。",
                422: "Provider 不支持当前协议或工具调用参数。",
                429: "Provider 已达到速率或额度限制。",
            }.get(status, "Provider 服务端请求失败。" if status >= 500 else "Provider 请求失败。")
            return f"{type(error).__name__}: {reason} (HTTP {status})"
        if "timeout" in type(error).__name__.casefold():
            return f"{type(error).__name__}: Provider 请求超时。"
        if "connection" in type(error).__name__.casefold():
            return f"{type(error).__name__}: 无法连接 Provider，请检查 Base URL 和网络。"
        return f"{type(error).__name__}: Provider 请求失败。"

    message = " ".join(str(error).split())
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<api-key>")
    message = _AUTHORIZATION_PATTERN.sub(r"\1<redacted>", message)
    message = _BEARER_PATTERN.sub("Bearer <redacted>", message)
    message = _LIKELY_KEY_PATTERN.sub("<api-key>", message)
    message = _URL_PATTERN.sub("<provider-url>", message)
    if not message:
        message = "Provider 请求失败。"
    return f"{type(error).__name__}: {message[:400]}"


def _validated_run_dir(runs_dir: Path, run_id: str) -> Path:
    root = runs_dir.resolve()
    candidate = (root / run_id).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("任务目录超出允许范围。") from error
    if not relative.parts or len(relative.parts) != 1:
        raise ValueError("任务标识无效。")
    return candidate


def _validated_export_destination(
    destination: Path,
    *,
    export_format: ReportExportFormat,
    runs_dir: Path,
    overwrite: bool,
) -> Path:
    expected_suffix = ".md" if export_format is ReportExportFormat.MARKDOWN else ".pdf"
    requested = Path(destination).expanduser()
    if requested.suffix.casefold() != expected_suffix:
        raise ValueError(f"导出文件扩展名必须是 {expected_suffix}。")
    if requested.is_symlink():
        raise ValueError("导出目标不能是符号链接。")
    output = requested.resolve()
    parent = output.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"导出目录不存在：{parent}")
    if not os.access(parent, os.W_OK):
        raise PermissionError(f"导出目录不可写：{parent}")
    runs_root = runs_dir.resolve()
    if output == runs_root or output.is_relative_to(runs_root):
        raise ValueError("导出目标不能位于任何任务快照目录内。")
    if output.exists():
        if not output.is_file():
            raise ValueError(f"导出目标不是普通文件：{output}")
        if not overwrite:
            raise FileExistsError(f"目标文件已存在：{output}")
    return output


def _load_export_report_snapshot(
    run_dir: Path,
) -> tuple[RubricProfile, EvaluationReport | MetaReview, AuditReport]:
    """Load the immutable inputs needed for deterministic Markdown rebuilding."""

    rubric_path = run_dir / "rubric.json"
    audit_path = run_dir / "audit.json"
    missing = [
        label
        for label, path in (("rubric", rubric_path), ("audit", audit_path))
        if not path.is_file()
    ]
    if missing:
        raise ValueError(f"报告尚不完整：{', '.join(missing)}")

    rubric = RubricProfile.model_validate_json(rubric_path.read_text(encoding="utf-8"))
    audit = AuditReport.model_validate_json(audit_path.read_text(encoding="utf-8"))
    candidates = (
        run_dir / "evaluation-report.json",
        run_dir / "report.json",
        run_dir / "meta-review.json",
    )
    invalid: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        payload = path.read_text(encoding="utf-8")
        try:
            return rubric, EvaluationReport.model_validate_json(payload), audit
        except ValidationError:
            try:
                return rubric, MetaReview.model_validate_json(payload), audit
            except ValidationError:
                invalid.append(path.name)
    if invalid:
        raise ValueError("报告快照格式无效：" + "、".join(invalid))
    raise ValueError("报告尚不完整：evaluation/meta review")


def _create_export_temporary(destination: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(temporary_name)


def _markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            return title or None
    return None


def _load_trace_events(path: Path, run_id: str) -> list[RunEvent]:
    events: list[RunEvent] = []
    for row in read_json_lines(path):
        event_type = str(row.get("event_type", "event"))
        payload = row.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}
        timestamp = row.get("timestamp")
        event_data: dict[str, object] = {
            "run_id": run_id,
            "event_type": event_type,
            "status": _status_from_payload(normalized_payload),
            "stage": _stage_from_event(event_type),
            "message": _TRACE_MESSAGES.get(event_type, event_type.replace("_", " ")),
            "payload": normalized_payload,
        }
        if isinstance(timestamp, str):
            event_data["timestamp"] = timestamp
        events.append(RunEvent.model_validate(event_data))
    return events


def _status_from_payload(payload: dict[str, object]) -> RunStatus | None:
    value = payload.get("status")
    if not isinstance(value, str):
        return None
    try:
        return RunStatus(value)
    except ValueError:
        return None


def _stage_from_event(event_type: str) -> str | None:
    for prefix, stage in (
        ("ingest", "ingest"),
        ("evidence", "evidence"),
        ("reference", "evidence"),
        ("scoring", "scoring"),
        ("review", "reviews"),
        ("audit", "audit"),
        ("hard_rule", "hard_rule_gate"),
        ("panel", "panel"),
        ("supplemental", "supplemental"),
        ("meta", "meta"),
        ("report", "report"),
    ):
        if event_type.startswith(prefix):
            return stage
    return None


_TRACE_MESSAGES = {
    "run_created": "已创建评测任务",
    "ingest_started": "正在解析论文",
    "ingest_completed": "论文解析完成",
    "evidence_collection_started": "正在收集外部学术证据",
    "evidence_completed": "外部证据收集完成",
    "reference_check_started": "正在自动核验参考文献",
    "reference_check_completed": "参考文献自动核验完成",
    "scoring_started": "专业化 Reviewer 正在执行九项诊断评分",
    "scoring_completed": "九项诊断评分完成",
    "reviews_started": "多位 Reviewer 正在评测",
    "reviews_completed": "Reviewer 评测完成",
    "audit_started": "正在执行确定性审计",
    "audit_completed": "确定性审计完成",
    "hard_rule_confirmation_required": "否决项需要人工确认",
    "panel_review_started": "三名独立专家正在初评",
    "panel_expert_completed": "独立专家评议完成",
    "supplemental_review_started": "两名独立专家正在复评",
    "panel_human_review_required": "专家无法判断，需要人工面板复核",
    "panel_completed": "独立专家面板评议完成",
    "meta_review_started": "正在汇总 Meta Review",
    "meta_completed": "Meta Review 完成",
    "report_validation_started": "正在验证并生成报告",
    "report_completed": "评测报告已生成",
    "stage_failed": "评测任务失败，可从检查点恢复",
    "run_cancelled": "评测任务已取消",
}


def _load_hard_rule_assessments(path: Path) -> list[HardRuleAssessment]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("否决项评估快照格式无效。")
    return [HardRuleAssessment.model_validate(item) for item in payload]


def _load_human_rule_decisions(path: Path) -> list[HumanRuleDecision]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("人工复核快照格式无效。")
    return [HumanRuleDecision.model_validate(item) for item in payload]


def _write_model_list(path: Path, items: list[HumanRuleDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in items]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_model_atomic(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_optional_model[ModelT: BaseModel](
    path: Path, model_type: type[ModelT]
) -> ModelT | None:
    if not path.is_file():
        return None
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))
