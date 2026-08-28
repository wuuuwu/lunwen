from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yaml import YAMLError

from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    HardRuleDecisionRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.application.app_state import AppPaths, PreferencesStore, read_json_lines
from paper_reviewer.application.artifacts import RunArtifactStore
from paper_reviewer.application.batch_errors import classify_batch_error
from paper_reviewer.application.batch_output import (
    BATCH_SUMMARY_FILENAME,
    allocate_report_path,
    build_report_filename,
    claim_batch_output_directory,
    is_allocated_report_filename,
    release_batch_output_directory_claim,
    write_batch_summary_csv,
)
from paper_reviewer.application.batch_store import (
    BatchLoadError,
    BatchSourceChangedError,
    BatchStore,
    scan_batch_sources,
    validate_source_snapshot,
)
from paper_reviewer.application.metadata_extractor import suggest_submission_metadata_locally
from paper_reviewer.application.metadata_recheck import (
    MetadataRecheckValidationError,
    apply_metadata_recheck_decision,
    build_metadata_suggestions,
    metadata_requires_local_recheck,
    submission_metadata_sha256,
)
from paper_reviewer.application.models import (
    BatchMetadataRecheckItem,
    BatchMetadataRecheckPreview,
    BatchMetadataRecheckResult,
    MetadataRecheckDecision,
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
    validate_provider_snapshot_identity,
)
from paper_reviewer.application.review_planner import build_review_plan
from paper_reviewer.application.run_events import RunEventView, project_run_event
from paper_reviewer.application.state_machine import transition
from paper_reviewer.application.unit_of_work import ApplicationUnitOfWork
from paper_reviewer.config import ReviewProfile, Settings, load_review_profile, load_rubric
from paper_reviewer.domain.batch import (
    BatchEvent,
    BatchItem,
    BatchItemStatus,
    BatchRecord,
    BatchReviewRequest,
    BatchStatus,
)
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
from paper_reviewer.domain.submission import SubmissionMetadata
from paper_reviewer.ports.model import Message, ModelRequest, ToolSpec
from paper_reviewer.reporting.presentation import load_presentation_profile
from paper_reviewer.reporting.renderer import render_markdown, write_report_bundle
from paper_reviewer.validation.audits import AuditReport, audit_evaluation_report
from paper_reviewer.validation.panel import (
    build_human_review_summary,
    decide_expert_panel,
    decide_panel,
)
from paper_reviewer.validation.scoring import aggregate_scores

EventSink = Callable[[RunEvent], None]
BatchEventSink = Callable[[BatchEvent], None]


class _BatchReportOutputError(OSError):
    """Internal marker for report destination failures; text is never persisted."""


# Keep these names as stable patch/injection points while delaying optional,
# heavyweight dependencies until the operation that actually needs them.
def PyMuPDFParser() -> Any:
    from paper_reviewer.adapters.documents.pymupdf_parser import (
        PyMuPDFParser as parser_type,
    )

    return parser_type()


def create_model_adapter(*args: Any, **kwargs: Any) -> Any:
    from paper_reviewer.adapters.models.factory import create_model_adapter as create_adapter

    return create_adapter(*args, **kwargs)


def review_runtime(**kwargs: Any) -> Any:
    from paper_reviewer.application.runtime import review_runtime as create_runtime

    return create_runtime(**kwargs)


def render_pdf(
    markdown: str,
    destination: Path,
    *,
    title: str,
    author: str = "Paper Reviewer",
) -> None:
    from paper_reviewer.reporting.exporter import render_pdf as render

    render(markdown, destination, title=title, author=author)


def validate_pdf(path: Path, markdown: str) -> None:
    from paper_reviewer.reporting.exporter import validate_pdf as validate

    validate(path, markdown)


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
            if not request.discipline_name:
                raise ValueError("浙江双层评测必须填写专业名称。")
            panel_profile = load_review_profile(_resolve_panel_profile_path(request.profile))
        if request.provider.startswith("custom:"):
            profile_entry = self.providers.get(request.provider)
            if profile_entry.is_archived:
                raise ValueError("归档的自定义 Provider 不能用于创建新任务。")
        provider_snapshot = self.providers.snapshot(request.provider, request.model)
        return await self._start_review_from_snapshots(
            request,
            rubric=rubric,
            profile=profile,
            panel_profile=panel_profile,
            provider_snapshot=provider_snapshot,
            event_sink=event_sink,
        )

    async def _start_review_from_snapshots(
        self,
        request: ReviewRequest,
        *,
        rubric: RubricProfile,
        profile: ReviewProfile,
        panel_profile: ReviewProfile | None,
        provider_snapshot: ProviderSnapshot,
        event_sink: EventSink | None,
        expected_input_hash: str | None = None,
    ) -> RunRecord:
        """Start one run from immutable configuration snapshots.

        The public single-run entry point resolves current configuration first;
        course batches call this helper with the snapshots frozen in batch.json.
        """

        validate_provider_snapshot_identity(request.provider, request.model, provider_snapshot)
        api_key = self.providers.get_snapshot_api_key(provider_snapshot)
        if not api_key:
            raise ValueError("所选 Provider 未配置 API Key。")
        async with review_runtime(
            settings=self.settings,
            provider_snapshot=provider_snapshot,
            api_key=api_key,
            external_search=request.external_search,
        ) as runtime:
            orchestrator = self._orchestrator(
                runtime.model,
                runtime.sessions,
                runtime.scholarly_clients,
                runtime.web_search_client,
                event_sink,
            )
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
                expected_input_hash=expected_input_hash,
            )

    async def create_batch(self, request: BatchReviewRequest) -> BatchRecord:
        """Validate and atomically create a frozen course-paper batch."""

        _validate_batch_cloud_request(request)
        rubric = load_rubric(request.rubric)
        if getattr(rubric, "evaluation_mode", None) != "course_assessment":
            raise ValueError("课程批量版只支持 evaluation_mode=course_assessment 的 Rubric。")
        profile = load_review_profile(request.profile)
        build_review_plan(rubric, profile)
        if request.provider.startswith("custom:"):
            provider_profile = self.providers.get(request.provider)
            if provider_profile.is_archived:
                raise ValueError("归档的自定义 Provider 不能用于创建新批次。")
        provider_snapshot = self.providers.snapshot(request.provider, request.model)
        validate_provider_snapshot_identity(request.provider, request.model, provider_snapshot)
        if not self.providers.get_snapshot_api_key(provider_snapshot):
            raise ValueError("所选 Provider 未配置 API Key。")
        await asyncio.to_thread(_ensure_batch_output_directory, request.output_dir)
        items = await asyncio.to_thread(scan_batch_sources, request)
        batch_id = uuid.uuid4().hex
        record = BatchRecord(
            batch_id=batch_id,
            request=request,
            rubric_snapshot=rubric,
            profile_snapshot=profile,
            provider_snapshot=provider_snapshot,
            items=items,
        )
        _ensure_batch_summary_path(record)
        store = self._batch_store()
        # Claim and manifest persistence form one shielded thread operation.
        # A cancellation can therefore leave either no claim and no manifest,
        # or a matching claim and manifest, but never an orphaned owner marker.
        await _persist_batch_with_output_claim(store, record)
        try:
            await asyncio.to_thread(_write_batch_csv, record)
        except Exception as error:
            classified = classify_batch_error(error, context="output directory")
            record.status = BatchStatus.PAUSED
            record.error = classified.summary
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save, record)
        return record

    async def run_batch(
        self,
        batch_id: str,
        *,
        event_sink: BatchEventSink | None = None,
    ) -> BatchRecord:
        """Run queued course papers sequentially and persist after every item."""

        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            return await self._run_batch_locked(batch_id, event_sink=event_sink)

    async def _run_batch_locked(
        self,
        batch_id: str,
        *,
        event_sink: BatchEventSink | None,
        store: BatchStore | None = None,
    ) -> BatchRecord:
        store = store or self._batch_store()
        record = await asyncio.to_thread(store.load, batch_id)
        resumable_item_statuses = {
            BatchItemStatus.QUEUED,
            BatchItemStatus.RUNNING,
            BatchItemStatus.CANCELLED,
        }
        if record.status in {BatchStatus.COMPLETED, BatchStatus.COMPLETED_WITH_ERRORS} and not any(
            item.status in resumable_item_statuses
            for item in record.items
        ) and record.retry_item_ids is None:
            try:
                await asyncio.to_thread(_write_batch_csv, record)
                await asyncio.to_thread(store.save, record)
            except Exception as error:
                classified = classify_batch_error(error, context="output directory")
                record.status = BatchStatus.PAUSED
                record.error = classified.summary
                record.updated_at = datetime.now(UTC)
                await asyncio.to_thread(store.save, record)
                _emit_batch_event(
                    event_sink,
                    record,
                    event_type="batch_paused",
                    message=classified.summary,
                )
            return record
        preflight_steps: tuple[tuple[str, Callable[[], object]], ...] = (
            ("authorization", lambda: _validate_batch_cloud_request(record.request)),
            ("rubric", lambda: _validate_batch_rubric_snapshot(record)),
            ("protocol", lambda: _validate_batch_provider_snapshot(record)),
            ("credentials", lambda: _require_batch_api_key(self, record)),
            (
                "output directory",
                lambda: _ensure_batch_output_directory(record.request.output_dir),
            ),
        )
        for context, operation in preflight_steps:
            try:
                await asyncio.to_thread(operation)
            except Exception as error:
                classified = classify_batch_error(error, context=context)
                record.status = BatchStatus.PAUSED
                record.error = classified.summary
                record.updated_at = datetime.now(UTC)
                await asyncio.to_thread(store.save, record)
                _emit_batch_event(
                    event_sink,
                    record,
                    event_type="batch_paused",
                    message=classified.summary,
                )
                return record

        _ensure_batch_summary_path(record)
        try:
            await asyncio.to_thread(_write_batch_csv, record)
        except Exception as error:
            classified = classify_batch_error(error, context="output directory")
            record.status = BatchStatus.PAUSED
            record.error = classified.summary
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save, record)
            _emit_batch_event(
                event_sink,
                record,
                event_type="batch_paused",
                message=classified.summary,
            )
            return record

        record.status = BatchStatus.RUNNING
        record.error = None
        record.updated_at = datetime.now(UTC)
        await asyncio.to_thread(store.save, record)
        _emit_batch_event(
            event_sink,
            record,
            event_type="batch_started",
            message="课程论文批次已开始。",
        )

        selected_item_ids = (
            set(record.retry_item_ids) if record.retry_item_ids is not None else None
        )
        for item in record.items:
            if selected_item_ids is not None and item.item_id not in selected_item_ids:
                continue
            if item.status not in {
                BatchItemStatus.QUEUED,
                BatchItemStatus.RUNNING,
                BatchItemStatus.CANCELLED,
            }:
                continue
            record.current_item_id = item.item_id
            item.status = BatchItemStatus.RUNNING
            item.error = None
            item.updated_at = datetime.now(UTC)
            record.updated_at = item.updated_at
            await asyncio.to_thread(store.save, record)
            _emit_batch_event(
                event_sink,
                record,
                item=item,
                event_type="batch_item_started",
                message=f"正在评测：{item.source.filename}",
            )
            try:
                await asyncio.to_thread(validate_source_snapshot, item.source)
                await self._run_batch_item(record, item, store=store, event_sink=event_sink)
            except asyncio.CancelledError:
                item.status = BatchItemStatus.CANCELLED
                item.error = None
                item.updated_at = datetime.now(UTC)
                record.status = BatchStatus.PAUSED
                record.current_item_id = item.item_id
                record.error = None
                record.updated_at = item.updated_at
                await asyncio.to_thread(store.save, record)
                _emit_batch_event(
                    event_sink,
                    record,
                    item=item,
                    event_type="batch_paused",
                    message="批次已安全停止；当前任务检查点已保留。",
                )
                raise
            except BatchSourceChangedError:
                item.status = BatchItemStatus.SOURCE_CHANGED
                item.error = "源 PDF 在批次创建后发生变化，请重新创建批次。"
            except Exception as error:
                classified = classify_batch_error(
                    error,
                    context=_batch_item_error_context(error, record),
                )
                item.error = classified.summary
                item.updated_at = datetime.now(UTC)
                if classified.is_shared:
                    record.status = BatchStatus.PAUSED
                    record.error = classified.summary
                    record.current_item_id = item.item_id
                    record.updated_at = item.updated_at
                    await asyncio.to_thread(store.save, record)
                    try:
                        await asyncio.to_thread(_write_batch_csv, record)
                    except OSError:
                        # The shared failure is already durably recorded.  A
                        # later resume retries the summary before completion.
                        pass
                    _emit_batch_event(
                        event_sink,
                        record,
                        item=item,
                        event_type="batch_paused",
                        message=classified.summary,
                    )
                    return record
                item.status = BatchItemStatus.FAILED
            item.updated_at = datetime.now(UTC)
            record.updated_at = item.updated_at
            record.current_item_id = None
            await asyncio.to_thread(store.save, record)
            try:
                await asyncio.to_thread(_write_batch_csv, record)
            except Exception as error:
                classified = classify_batch_error(error, context="output directory")
                record.status = BatchStatus.PAUSED
                record.error = classified.summary
                record.updated_at = datetime.now(UTC)
                await asyncio.to_thread(store.save, record)
                _emit_batch_event(
                    event_sink,
                    record,
                    item=item,
                    event_type="batch_paused",
                    message=classified.summary,
                )
                return record
            _emit_batch_event(
                event_sink,
                record,
                item=item,
                event_type="batch_item_completed",
                message=_batch_item_completion_message(item),
            )

        final_record = record.model_copy(deep=True)
        final_record.current_item_id = None
        final_record.retry_item_ids = None
        final_record.error = None
        has_remaining = any(
            item.status
            in {BatchItemStatus.QUEUED, BatchItemStatus.RUNNING, BatchItemStatus.CANCELLED}
            for item in final_record.items
        )
        has_errors = any(
            item.status in {BatchItemStatus.FAILED, BatchItemStatus.SOURCE_CHANGED}
            for item in final_record.items
        )
        final_record.status = (
            BatchStatus.PAUSED
            if has_remaining
            else BatchStatus.COMPLETED_WITH_ERRORS
            if has_errors
            else BatchStatus.COMPLETED
        )
        final_record.updated_at = datetime.now(UTC)
        try:
            await asyncio.to_thread(_write_batch_csv, final_record)
        except Exception as error:
            classified = classify_batch_error(error, context="output directory")
            record.status = BatchStatus.PAUSED
            record.current_item_id = None
            record.error = classified.summary
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save, record)
            _emit_batch_event(
                event_sink,
                record,
                event_type="batch_paused",
                message=classified.summary,
            )
            return record
        await asyncio.to_thread(store.save, final_record)
        _emit_batch_event(
            event_sink,
            final_record,
            event_type="batch_completed",
            message=(
                "批次仍有排队论文，可继续执行。"
                if final_record.status is BatchStatus.PAUSED
                else "批次已完成，部分论文需要处理。"
                if final_record.status is BatchStatus.COMPLETED_WITH_ERRORS
                else "批次已全部完成。"
            ),
        )
        return final_record

    async def pause_batch(self, batch_id: str) -> BatchRecord:
        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            store = self._batch_store()
            record = await asyncio.to_thread(store.load, batch_id)
            record.status = BatchStatus.PAUSED
            if record.current_item_id:
                current = _batch_item(record, record.current_item_id)
                if current.status is BatchItemStatus.RUNNING:
                    current.status = BatchItemStatus.CANCELLED
                    current.updated_at = datetime.now(UTC)
            record.updated_at = datetime.now(UTC)
            await asyncio.to_thread(store.save, record)
            return record

    async def resume_batch(
        self,
        batch_id: str,
        *,
        event_sink: BatchEventSink | None = None,
    ) -> BatchRecord:
        return await self.run_batch(batch_id, event_sink=event_sink)

    async def retry_failed_items(
        self,
        batch_id: str,
        *,
        event_sink: BatchEventSink | None = None,
    ) -> BatchRecord:
        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            store = self._batch_store()
            record = await asyncio.to_thread(store.load, batch_id)
            if record.retry_item_ids is None:
                failed_item_ids = [
                    item.item_id for item in record.items if item.status is BatchItemStatus.FAILED
                ]
                if not failed_item_ids:
                    return record
                record.retry_item_ids = failed_item_ids
                for item in record.items:
                    if item.item_id in failed_item_ids:
                        item.status = BatchItemStatus.QUEUED
                        item.error = None
                        item.updated_at = datetime.now(UTC)
                record.status = BatchStatus.PAUSED
                record.error = None
                record.current_item_id = None
                record.updated_at = datetime.now(UTC)
                await asyncio.to_thread(store.save, record)
            return await self._run_batch_locked(
                batch_id,
                event_sink=event_sink,
                store=store,
            )

    async def get_batch(self, batch_id: str) -> BatchRecord:
        return await asyncio.to_thread(self._batch_store().load, batch_id)

    async def list_batches(self) -> list[BatchRecord]:
        return await asyncio.to_thread(self._batch_store().list_records)

    async def list_batch_load_errors(self) -> list[BatchLoadError]:
        """Return safe diagnostics for manifests omitted from ``list_batches``."""

        return await asyncio.to_thread(self._batch_store().list_load_errors)

    async def _run_batch_item(
        self,
        record: BatchRecord,
        item: BatchItem,
        *,
        store: BatchStore,
        event_sink: BatchEventSink | None,
    ) -> None:
        def run_event_sink(event: RunEvent) -> None:
            if item.run_id is None:
                item.run_id = event.run_id
                item.updated_at = datetime.now(UTC)
                record.updated_at = item.updated_at
                store.save(record)
            _emit_batch_event(
                event_sink,
                record,
                item=item,
                event_type="batch_run_event",
                message=event.message,
                payload={
                    "run_id": event.run_id,
                    "run_event_type": event.event_type,
                    "run_status": event.status.value if event.status is not None else None,
                    "stage": event.stage,
                },
            )

        run: RunRecord | None = None
        if item.run_id is not None:
            detail = await self.get_run(item.run_id)
            run = detail.run
            if run.status not in {
                RunStatus.REPORTED,
                RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
                RunStatus.FATAL_FAILURE,
            }:
                run = await self.resume_review(item.run_id, event_sink=run_event_sink)
        else:
            request = ReviewRequest(
                paper=item.source.path,
                provider=record.request.provider,
                model=record.request.model,
                rubric=record.request.rubric,
                profile=record.request.profile,
                discipline_name="",
                discipline_profile=None,
                cloud_processing_authorized=True,
                contains_classified_material=False,
                external_search=record.request.external_search,
            )
            run = await self._start_review_from_snapshots(
                request,
                rubric=record.rubric_snapshot,
                profile=record.profile_snapshot,
                panel_profile=None,
                provider_snapshot=record.provider_snapshot,
                event_sink=run_event_sink,
                expected_input_hash=item.source.sha256,
            )
        if item.run_id is None:
            item.run_id = run.run_id
        if run.status is RunStatus.FATAL_FAILURE:
            raise RuntimeError("course paper run entered a fatal failure state")
        if run.status not in {RunStatus.REPORTED, RunStatus.REPORTED_PENDING_HUMAN_REVIEW}:
            raise RuntimeError("course paper run did not produce a report")

        report = await self.load_report(run.run_id)
        metadata = report.submission_metadata
        if metadata is None:
            raise ValueError("course submission metadata checkpoint is missing")
        item.metadata = metadata
        item.dimension_scores = dict(report.dimension_scores)
        item.total_score = report.review.total_score
        item.grade = _course_grade(record.rubric_snapshot, item.total_score)
        item.conclusion = _course_conclusion(record.rubric_snapshot, item.total_score)
        for warning in metadata.warnings:
            if warning not in item.warnings:
                item.warnings.append(warning)

        if item.report_path is None:
            item.report_path = allocate_report_path(
                record.request.output_dir,
                metadata,
                run.run_id,
                source_filename=item.source.filename,
            )
            item.updated_at = datetime.now(UTC)
            record.updated_at = item.updated_at
            # Reserve the exact destination before exporting.  If the process
            # stops after the atomic PDF publish but before the terminal item
            # save, recovery reuses this path instead of allocating a suffixed
            # duplicate report.
            await asyncio.to_thread(store.save, record)
        destination = _validate_batch_output_path(
            record.request.output_dir,
            item.report_path,
            suffix=".pdf",
        )

        if not destination.is_file():
            try:
                result = await self.export_report(
                    run.run_id,
                    ReportExportFormat.PDF,
                    destination,
                    overwrite=False,
                )
            except OSError as error:
                raise _BatchReportOutputError("batch report output failed") from error
            item.report_path = result.path
        item.status = BatchItemStatus.COMPLETED
        item.error = None
        item.updated_at = datetime.now(UTC)
        record.updated_at = item.updated_at
        await asyncio.to_thread(store.save, record)

    async def preview_batch_metadata_recheck(
        self,
        batch_id: str,
        item_ids: Sequence[str] | None = None,
    ) -> BatchMetadataRecheckPreview:
        """Reparse historical sources and preview local-only metadata improvements."""

        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            record = await asyncio.to_thread(self._batch_store().load, batch_id)
            items: list[BatchMetadataRecheckItem] = []
            skipped: dict[str, str] = {}
            requested_ids = set(item_ids) if item_ids is not None else None
            if requested_ids is not None:
                known_ids = {item.item_id for item in record.items}
                for missing_id in sorted(requested_ids - known_ids):
                    skipped[missing_id] = "批次中不存在该论文。"
            for item in record.items:
                if requested_ids is not None and item.item_id not in requested_ids:
                    continue
                if (
                    requested_ids is None
                    and item.metadata is not None
                    and not metadata_requires_local_recheck(item.metadata)
                ):
                    continue
                try:
                    prepared = await _metadata_update_to_thread(
                        _prepare_batch_metadata_recheck_item,
                        self.settings.runs_dir,
                        item,
                    )
                except _MetadataRecheckUnavailable as error:
                    skipped[item.item_id] = error.public_message
                except Exception:
                    skipped[item.item_id] = "本地重检失败；源 PDF 或任务快照无法读取。"
                else:
                    items.append(prepared.preview)
            return BatchMetadataRecheckPreview(
                batch_id=batch_id,
                items=items,
                skipped=skipped,
            )

    async def apply_batch_metadata_recheck(
        self,
        batch_id: str,
        decisions: Sequence[MetadataRecheckDecision],
    ) -> BatchMetadataRecheckResult:
        """Apply verified local suggestions item by item without invoking a model."""

        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            store = self._batch_store()
            record = await asyncio.to_thread(store.load, batch_id)
            updated: list[str] = []
            failed: dict[str, str] = {}
            skipped: list[str] = []
            seen: set[str] = set()
            for decision in decisions:
                item_id = decision.item_id
                if item_id in seen:
                    failed[item_id] = "同一论文出现重复决定，请重新预检。"
                    continue
                seen.add(item_id)
                try:
                    item = _batch_item(record, item_id)
                except ValueError:
                    failed[item_id] = "批次中不存在该论文。"
                    continue
                try:
                    prepared = await _metadata_update_to_thread(
                        _prepare_batch_metadata_recheck_item,
                        self.settings.runs_dir,
                        item,
                    )
                except _MetadataRecheckUnavailable as error:
                    failed[item_id] = error.public_message
                    continue
                except Exception:
                    failed[item_id] = "本地重检失败；源 PDF 或任务快照无法读取。"
                    continue
                if decision.base_metadata_sha256 != prepared.preview.base_metadata_sha256:
                    failed[item_id] = "元数据已变化，请重新预检后再应用。"
                    continue
                try:
                    replacement = apply_metadata_recheck_decision(
                        prepared.current,
                        prepared.candidate,
                        decision,
                    )
                except MetadataRecheckValidationError as error:
                    failed[item_id] = str(error)
                    continue
                if replacement is None:
                    skipped.append(item_id)
                    continue
                try:
                    record = await self._update_submission_metadata_locked(
                        batch_id,
                        item_id,
                        replacement,
                    )
                except _MetadataUpdateRollbackError:
                    # The on-disk state may now need manual recovery. Continuing
                    # could build later updates on an inconsistent manifest.
                    raise
                except Exception:
                    failed[item_id] = "本地重检结果应用失败；原报告和批次记录已保持不变。"
                    continue
                updated.append(item_id)
            return BatchMetadataRecheckResult(
                batch_id=batch_id,
                updated_item_ids=updated,
                failed_items=failed,
                skipped_item_ids=skipped,
            )

    async def update_submission_metadata(
        self,
        batch_id: str,
        item_id: str,
        metadata: SubmissionMetadata,
        *,
        expected_metadata_sha256: str | None = None,
    ) -> BatchRecord:
        """Apply a local correction and rebuild Markdown, PDF and CSV without an LLM."""

        lock_store = BatchStore(self.paths.batches_dir)
        with lock_store.execution_lock(batch_id):
            return await self._update_submission_metadata_locked(
                batch_id,
                item_id,
                metadata,
                expected_metadata_sha256=expected_metadata_sha256,
            )

    async def _update_submission_metadata_locked(
        self,
        batch_id: str,
        item_id: str,
        metadata: SubmissionMetadata,
        *,
        expected_metadata_sha256: str | None = None,
    ) -> BatchRecord:
        """Stage and atomically publish one metadata correction."""

        store = self._batch_store()
        record = await asyncio.to_thread(store.load, batch_id)
        original_record = record.model_copy(deep=True)
        item = _batch_item(record, item_id)
        if expected_metadata_sha256 is not None:
            if (
                item.metadata is None
                or submission_metadata_sha256(item.metadata)
                != expected_metadata_sha256
            ):
                raise ValueError(
                    "论文信息已被其他窗口更新，请重新打开核对窗口。"
                )
        if item.run_id is None:
            raise ValueError("该论文尚未创建评测任务，不能修改报告信息。")

        run_dir = _validated_run_dir(self.settings.runs_dir, item.run_id)
        if not run_dir.is_dir():
            raise ValueError("该论文的任务快照目录不存在，不能修改报告信息。")
        report = await self.load_report(item.run_id)
        selected_report = report.evaluation or report.review
        output_dir = record.request.output_dir.resolve()
        await asyncio.to_thread(_ensure_batch_output_directory, output_dir)

        old_report_path = _managed_item_report_path(record, item)
        desired_name = build_report_filename(
            metadata,
            item.run_id,
            source_filename=item.source.filename,
        )
        same_destination = (
            old_report_path is not None
            and old_report_path.name.casefold() == desired_name.casefold()
        )
        destination = (
            old_report_path
            if same_destination and old_report_path is not None
            else allocate_report_path(
                output_dir,
                metadata,
                item.run_id,
                source_filename=item.source.filename,
            )
        )
        destination = _validated_export_destination(
            destination,
            export_format=ReportExportFormat.PDF,
            runs_dir=self.settings.runs_dir,
            overwrite=same_destination,
        )

        item.metadata = metadata
        item.report_path = destination
        item.error = None
        item.updated_at = datetime.now(UTC)
        record.updated_at = item.updated_at

        transaction = _MetadataUpdateFileTransaction()
        manifest_save_started = False
        try:
            await _metadata_update_to_thread(
                _stage_metadata_update_files,
                transaction=transaction,
                run_dir=run_dir,
                output_dir=output_dir,
                old_report_path=(
                    old_report_path
                    if old_report_path is not None and old_report_path != destination
                    else None
                ),
                destination=destination,
                destination_must_be_absent=not same_destination,
                run=report.run,
                rubric=report.rubric,
                selected_report=selected_report,
                audit=report.audit,
                evidence=report.evidence,
                presentation_profile=report.presentation_profile,
                metadata=metadata,
                dimension_scores=report.dimension_scores,
                batch=record,
            )
            await _metadata_update_to_thread(transaction.commit)
            manifest_save_started = True
            await _save_batch_manifest_for_metadata_update(store, record)
        except BaseException as error:
            restore_errors: list[BaseException] = []
            if manifest_save_started:
                try:
                    await _metadata_cleanup_to_thread(
                        _restore_batch_manifest_if_needed,
                        store,
                        original_record,
                    )
                except BaseException as restore_error:
                    restore_errors.append(restore_error)
            restore_errors.extend(
                await _metadata_cleanup_to_thread(transaction.rollback)
            )
            if restore_errors:
                raise _MetadataUpdateRollbackError(
                    "元数据修正失败，且自动回滚未完全完成；旧文件备份已保留，请勿继续操作该批次。"
                ) from error
            raise
        else:
            await _metadata_update_to_thread(transaction.finalize)
            return record

    async def resume_review(self, run_id: str, *, event_sink: EventSink | None = None) -> RunRecord:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            run = await RunRepository(sessions).get(run_id)
            if run is None:
                raise ValueError(f"未知任务：{run_id}")
            rubric, profile = load_run_snapshots(self.settings.runs_dir / run_id)
            panel_path = self.settings.runs_dir / run_id / "panel-profile.json"
            panel_profile = load_review_profile(panel_path) if panel_path.is_file() else None
            run_dir = self.settings.runs_dir / run_id
            provider_snapshot = load_provider_snapshot(run_dir)
            if provider_snapshot is None:
                if run.provider not in {"openai", "deepseek"}:
                    raise ValueError(
                        "该任务缺少 Provider 快照；自定义 Provider 或 Responses 任务"
                        "请使用桌面端重新创建。"
                    )
                provider_snapshot = self.providers.snapshot(run.provider, run.model)
            validate_provider_snapshot_identity(run.provider, run.model, provider_snapshot)
            api_key = self.providers.get_snapshot_api_key(provider_snapshot)
            if not api_key:
                raise ValueError("恢复任务所需的 API Key 不存在，请先在设置中重新配置。")
            request_context = load_run_request_context(run_dir)
            external_search = request_context.get("external_search", True) is not False
            async with review_runtime(
                settings=self.settings,
                provider_snapshot=provider_snapshot,
                api_key=api_key,
                external_search=external_search,
                sessions=sessions,
            ) as runtime:
                orchestrator = self._orchestrator(
                    runtime.model,
                    runtime.sessions,
                    runtime.scholarly_clients,
                    runtime.web_search_client,
                    event_sink,
                )
                return await orchestrator.execute(
                    run,
                    rubric=rubric,
                    profile=profile,
                    panel_profile=panel_profile,
                )

    async def get_pending_hard_rules(self, run_id: str) -> list[HardRuleAssessment]:
        """Return unresolved AI-suspected or unassessable hard rules.

        Hard-rule artifacts are deliberately stored with the run snapshot rather
        than inferred from trace messages, so a cancelled/restarted desktop app
        observes the same human-review gate.
        """

        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            await self._require_run(run_id, sessions=sessions)
            decisions = await self._human_rule_decisions(run_id, sessions=sessions)
            return await self._pending_hard_rules(
                run_id, sessions=sessions, decisions=decisions
            )

    async def get_pending_human_reviews(self, run_id: str) -> HumanReviewSummary:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            await self._require_run(run_id, sessions=sessions)
            decisions = await self._human_rule_decisions(run_id, sessions=sessions)
            pending_rules = await self._pending_hard_rules(
                run_id, sessions=sessions, decisions=decisions
            )
            return await self._human_review_summary(
                run_id, sessions=sessions, pending_rules=pending_rules
            )

    async def resolve_hard_rule(
        self, run_id: str, decision: HumanRuleDecision
    ) -> HumanRuleDecision:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            run = await self._require_run(run_id, sessions=sessions)
            normalized = HumanRuleDecision.model_validate(decision)
            decisions = await self._human_rule_decisions(run_id, sessions=sessions)
            pending = {
                item.rule_id
                for item in await self._pending_hard_rules(
                    run_id, sessions=sessions, decisions=decisions
                )
            }
            if normalized.rule_id not in pending:
                raise ValueError(f"否决项不在待确认列表中：{normalized.rule_id}")

            artifacts = RunArtifactStore(self.settings.runs_dir / run_id)
            if any(item.rule_id == normalized.rule_id for item in decisions):
                raise ValueError(f"否决项已经处理：{normalized.rule_id}")
            decisions.append(normalized)
            await HardRuleDecisionRepository(sessions).save_human_rule_decision(
                run_id,
                normalized,
                reason=normalized.rationale,
                timestamp=normalized.decided_at,
            )
            artifacts.write_model_list("human-rule-decisions.json", decisions)
            if artifacts.exists("evaluation-report.json"):
                await self._refresh_after_human_review(
                    run=run,
                    decisions=decisions,
                    sessions=sessions,
                )
            return normalized

    async def resolve_panel_review(
        self, run_id: str, decision: HumanPanelDecision
    ) -> HumanPanelDecision:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            run = await self._require_run(run_id, sessions=sessions)
            normalized = HumanPanelDecision.model_validate(decision)
            summary = await self._human_review_summary(run_id, sessions=sessions)
            if not summary.panel_review_required:
                raise ValueError("当前任务没有待处理的人工面板复核。")
            artifacts = RunArtifactStore(self.settings.runs_dir / run_id)
            if artifacts.exists("human-panel-decision.json"):
                raise ValueError("人工面板复核已经处理。")
            artifacts.write_model("human-panel-decision.json", normalized)
            if artifacts.exists("evaluation-report.json"):
                decisions = await self._human_rule_decisions(run_id, sessions=sessions)
                await self._refresh_after_human_review(
                    run=run,
                    decisions=decisions,
                    sessions=sessions,
                )
            return normalized

    async def refresh_after_human_review(self, run_id: str) -> RunRecord:
        """Recompute deterministic conclusions and reports without any model call."""

        unit_of_work = ApplicationUnitOfWork(self.settings.database_url)
        async with unit_of_work:
            sessions = unit_of_work.require_sessions()
            run = await self._require_run(run_id, sessions=sessions)
            decisions = await self._human_rule_decisions(run_id, sessions=sessions)
            return await self._refresh_after_human_review(
                run=run,
                decisions=decisions,
                sessions=sessions,
            )

    async def _refresh_after_human_review(
        self,
        *,
        run: RunRecord,
        decisions: list[HumanRuleDecision],
        sessions: async_sessionmaker[AsyncSession],
    ) -> RunRecord:
        run_id = run.run_id
        run_dir = self.settings.runs_dir / run_id
        artifacts = RunArtifactStore(run_dir)
        rubric, selected_report, audit = _load_export_report_snapshot(run_dir)
        if not isinstance(selected_report, EvaluationReport):
            raise ValueError("该任务不是可刷新人工复核的双层评测报告。")
        human_panel_decision = artifacts.load_optional_model(
            "human-panel-decision.json", HumanPanelDecision
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

        evidence = artifacts.load_model_list(
            "evidence.json",
            EvidenceItem,
        )
        artifacts.write_model("evaluation-report.json", updated)
        write_report_bundle(
            run_dir=run_dir,
            run=run,
            rubric=rubric,
            review=updated,
            audit=audit,
            evidence=evidence,
        )
        repository = ReviewRepository(sessions)
        await repository.save_panel_decision(run_id, panel_decision)
        await repository.save_evaluation_report(run_id, updated)
        await RunRepository(sessions).save(
            run,
            event_type="human_review_report_refreshed",
            payload={"status": run.status.value, "pending_count": summary.pending_count},
        )
        return run

    async def resume_after_human_review(
        self, run_id: str, *, event_sink: EventSink | None = None
    ) -> RunRecord:
        await self._require_run(run_id)
        if (self.settings.runs_dir / run_id / "evaluation-report.json").is_file():
            return await self.refresh_after_human_review(run_id)
        return await self.resume_review(run_id, event_sink=event_sink)

    async def cancel_review(self, run_id: str) -> RunRecord:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            repository = RunRepository(unit_of_work.require_sessions())
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

    async def list_runs(
        self, *, search: str = "", status: RunStatus | None = None
    ) -> list[RunSummary]:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            repository = RunRepository(unit_of_work.require_sessions())
            records = await repository.list(status=status)
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
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            return await self._get_run_detail(run_id, sessions=sessions)

    async def load_report(self, run_id: str) -> ReportView:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            sessions = unit_of_work.require_sessions()
            detail = await self._get_run_detail(run_id, sessions=sessions)
            run_dir = self.settings.runs_dir / run_id
            artifacts = RunArtifactStore(run_dir)
            rubric, selected_report, audit = _load_export_report_snapshot(run_dir)
            evaluation = (
                selected_report if isinstance(selected_report, EvaluationReport) else None
            )
            review = (
                selected_report.meta_review
                if isinstance(selected_report, EvaluationReport)
                else selected_report
            )
            document = artifacts.load_optional_model("document.json", DocumentInfo)
            submission_metadata = artifacts.load_optional_model(
                "submission-metadata.json", SubmissionMetadata
            )
            evidence = artifacts.load_model_list("evidence.json", EvidenceItem)
            results = await ReviewRepository(sessions).list_results(run_id)
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
            presentation_profile=load_presentation_profile(run_dir),
            submission_metadata=submission_metadata,
        )

    async def _get_run_detail(
        self,
        run_id: str,
        *,
        sessions: async_sessionmaker[AsyncSession],
    ) -> RunDetail:
        run = await RunRepository(sessions).get(run_id)
        if run is None:
            raise ValueError(f"未知任务：{run_id}")
        events = _load_trace_events(self.settings.runs_dir / run_id / "trace.jsonl", run_id)
        snapshot = _provider_snapshot_for_display(self.settings.runs_dir / run_id, run)
        decisions = await self._human_rule_decisions(run_id, sessions=sessions)
        pending_rules = await self._pending_hard_rules(
            run_id, sessions=sessions, decisions=decisions
        )
        human_review_summary = await self._human_review_summary(
            run_id, sessions=sessions, pending_rules=pending_rules
        )
        human_panel_decision = RunArtifactStore(
            self.settings.runs_dir / run_id
        ).load_optional_model(
            "human-panel-decision.json",
            HumanPanelDecision,
        )
        return RunDetail(
            run=run,
            provider_display_name=snapshot.display_name if snapshot else None,
            provider_protocol=snapshot.protocol if snapshot else None,
            events=events,
            pending_hard_rules=pending_rules,
            human_rule_decisions=decisions,
            human_review_summary=human_review_summary,
            human_panel_decision=human_panel_decision,
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
            artifacts = RunArtifactStore(run_dir)
            metadata = artifacts.load_optional_model(
                "submission-metadata.json", SubmissionMetadata
            )
            raw_dimension_scores = (
                artifacts.read_json("dimension-scores.json")
                if artifacts.exists("dimension-scores.json")
                else None
            )
            dimension_scores = (
                {str(key): float(value) for key, value in raw_dimension_scores.items()}
                if isinstance(raw_dimension_scores, dict)
                else None
            )
            markdown_bytes = render_markdown(
                rubric,
                selected_report,
                audit,
                provider_snapshot=load_provider_snapshot(run_dir),
                provider_ref=run.provider,
                model=run.model,
                presentation_profile=load_presentation_profile(run_dir),
                submission_metadata=metadata,
                dimension_scores=dimension_scores,
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

        async with ApplicationUnitOfWork(
            self.settings.database_url, initialize=False
        ) as unit_of_work:
            run = await RunRepository(unit_of_work.require_sessions()).get(run_id)
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

    def _batch_store(self) -> BatchStore:
        return BatchStore(self.paths.batches_dir)

    async def _require_run(
        self,
        run_id: str,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> RunRecord:
        if sessions is not None:
            run = await RunRepository(sessions).get(run_id)
            if run is None:
                raise ValueError(f"未知任务：{run_id}")
            return run
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            run = await RunRepository(unit_of_work.require_sessions()).get(run_id)
        if run is None:
            raise ValueError(f"未知任务：{run_id}")
        return run

    async def _provider_is_referenced(self, provider_ref: str) -> bool:
        async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
            records = await RunRepository(unit_of_work.require_sessions()).list()
        return any(record.provider == provider_ref for record in records)

    async def _pending_hard_rules(
        self,
        run_id: str,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        decisions: list[HumanRuleDecision] | None = None,
    ) -> list[HardRuleAssessment]:
        run_dir = self.settings.runs_dir / run_id
        assessments = RunArtifactStore(run_dir).load_model_list(
            "hard-rule-assessments.json",
            HardRuleAssessment,
            invalid_message="否决项评估快照格式无效。",
        )
        if decisions is None:
            decisions = await self._human_rule_decisions(run_id, sessions=sessions)
        resolved = {item.rule_id for item in decisions}

        return [
            item
            for item in assessments
            if item.status in {HardRuleStatus.SUSPECTED, HardRuleStatus.NOT_ASSESSABLE}
            and item.rule_id not in resolved
        ]

    async def _human_rule_decisions(
        self,
        run_id: str,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> list[HumanRuleDecision]:
        run_dir = self.settings.runs_dir / run_id
        decisions = RunArtifactStore(run_dir).load_model_list(
            "human-rule-decisions.json",
            HumanRuleDecision,
            invalid_message="人工复核快照格式无效。",
        )
        if sessions is None:
            async with ApplicationUnitOfWork(self.settings.database_url) as unit_of_work:
                persisted = await HardRuleDecisionRepository(
                    unit_of_work.require_sessions()
                ).list_human_rule_decisions(run_id)
        else:
            persisted = await HardRuleDecisionRepository(sessions).list_human_rule_decisions(
                run_id
            )
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

    async def _human_review_summary(
        self,
        run_id: str,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        pending_rules: list[HardRuleAssessment] | None = None,
    ) -> HumanReviewSummary:
        run_dir = self.settings.runs_dir / run_id
        artifacts = RunArtifactStore(run_dir)
        if pending_rules is None:
            pending_rules = await self._pending_hard_rules(run_id, sessions=sessions)
        human_panel_decision = artifacts.load_optional_model(
            "human-panel-decision.json", HumanPanelDecision
        )
        expert_panel_decision = artifacts.load_optional_model(
            "expert-panel-decision.json", PanelDecision
        )
        if expert_panel_decision is None:
            evaluation = artifacts.load_optional_model(
                "evaluation-report.json", EvaluationReport
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


def _validate_batch_cloud_request(request: BatchReviewRequest) -> None:
    if not request.cloud_processing_authorized:
        raise ValueError("开始云端批量评测前必须确认已获得全部论文的处理授权。")
    if request.contains_classified_material:
        raise ValueError("涉密材料不得提交云端批量评测。")
    if not request.pii_output_authorized:
        raise ValueError("必须确认批次输出将包含姓名、学号、专业和论文题目。")


def _validate_batch_snapshots(record: BatchRecord) -> None:
    """Compatibility helper validating every immutable batch prerequisite."""

    _validate_batch_cloud_request(record.request)
    _validate_batch_rubric_snapshot(record)
    _validate_batch_provider_snapshot(record)


def _validate_batch_rubric_snapshot(record: BatchRecord) -> None:
    if getattr(record.rubric_snapshot, "evaluation_mode", None) != "course_assessment":
        raise ValueError("batch rubric snapshot is not a course assessment rubric")
    build_review_plan(record.rubric_snapshot, record.profile_snapshot)


def _validate_batch_provider_snapshot(record: BatchRecord) -> None:
    validate_provider_snapshot_identity(
        record.request.provider,
        record.request.model,
        record.provider_snapshot,
    )


def _ensure_batch_output_directory(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise NotADirectoryError("batch output path is not a directory")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".course-output-", dir=output_dir)
    os.close(descriptor)
    Path(temporary_name).unlink(missing_ok=True)


async def _persist_batch_with_output_claim(store: BatchStore, record: BatchRecord) -> None:
    """Wait for the indivisible claim-and-manifest operation to settle."""

    operation = asyncio.create_task(
        asyncio.to_thread(_claim_and_create_batch_manifest, store, record)
    )
    caller_cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # ``to_thread`` cannot be stopped.  Releasing ownership here could
            # let another batch take the directory before this thread publishes
            # its manifest, so wait through repeated cancellation requests.
            caller_cancelled = True
        except BaseException:
            break

    try:
        operation.result()
    except BaseException:
        if caller_cancelled:
            raise asyncio.CancelledError from None
        raise
    if caller_cancelled:
        raise asyncio.CancelledError


def _claim_and_create_batch_manifest(store: BatchStore, record: BatchRecord) -> None:
    """Synchronously claim output ownership and publish the matching manifest."""

    claim_batch_output_directory(record.request.output_dir, record.batch_id)
    try:
        store.create(record)
    except BaseException:
        # Cleanup must never hide the persistence failure.
        try:
            release_batch_output_directory_claim(
                record.request.output_dir,
                record.batch_id,
            )
        except OSError:
            pass
        raise


def _require_batch_api_key(service: ReviewApplicationService, record: BatchRecord) -> None:
    if not service.providers.get_snapshot_api_key(record.provider_snapshot):
        raise ValueError("批次 Provider 的 API Key 不存在。")


def _validate_batch_output_path(output_dir: Path, path: Path, *, suffix: str) -> Path:
    root = output_dir.resolve(strict=False)
    candidate = path.resolve(strict=False)
    if candidate.parent != root or candidate.suffix.casefold() != suffix.casefold():
        raise ValueError("batch output path escapes the configured output directory")
    return candidate


def _ensure_batch_summary_path(record: BatchRecord) -> Path:
    if record.summary_path is not None:
        return _validate_batch_output_path(
            record.request.output_dir, record.summary_path, suffix=".csv"
        )
    output_dir = record.request.output_dir.resolve(strict=False)
    record.summary_path = output_dir / BATCH_SUMMARY_FILENAME
    return record.summary_path


def _batch_item_error_context(error: BaseException, record: BatchRecord) -> str | None:
    if isinstance(error, _BatchReportOutputError):
        return "output directory"
    if not isinstance(error, PermissionError):
        return None
    filename = getattr(error, "filename", None)
    if isinstance(filename, (str, os.PathLike)):
        candidate = Path(filename).resolve(strict=False)
        if candidate == record.request.source_dir or candidate.parent == record.request.source_dir:
            return "pdf"
        if candidate == record.request.output_dir or candidate.parent == record.request.output_dir:
            return "output directory"
    return "pdf"


def _write_batch_csv(record: BatchRecord) -> None:
    dimensions = [
        (dimension.dimension_id, dimension.title)
        for dimension in record.rubric_snapshot.dimensions
    ]
    write_batch_summary_csv(
        _ensure_batch_summary_path(record),
        record,
        dimensions,
    )


class _MetadataUpdateRollbackError(RuntimeError):
    """A fatal metadata update failure whose rollback was incomplete."""


class _MetadataRecheckUnavailable(ValueError):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class _PreparedMetadataRecheck:
    preview: BatchMetadataRecheckItem
    current: SubmissionMetadata
    candidate: SubmissionMetadata


def _prepare_batch_metadata_recheck_item(
    runs_dir: Path,
    item: BatchItem,
) -> _PreparedMetadataRecheck:
    if item.status is not BatchItemStatus.COMPLETED:
        raise _MetadataRecheckUnavailable("仅已完成评测的论文可以进行本地元数据重检。")
    if item.run_id is None or item.metadata is None:
        raise _MetadataRecheckUnavailable("该论文缺少已完成的元数据快照。")

    run_dir = _validated_run_dir(runs_dir, item.run_id)
    if not run_dir.is_dir():
        raise _MetadataRecheckUnavailable("该论文的任务快照目录不存在。")
    try:
        artifact_metadata = RunArtifactStore(run_dir).load_model(
            "submission-metadata.json",
            SubmissionMetadata,
        )
    except (OSError, UnicodeError, ValueError):
        raise _MetadataRecheckUnavailable("该论文的元数据快照缺失或无法读取。") from None

    current_hash = submission_metadata_sha256(item.metadata)
    if submission_metadata_sha256(artifact_metadata) != current_hash:
        raise _MetadataRecheckUnavailable("批次清单与任务元数据不一致，不能自动重检。")

    try:
        validate_source_snapshot(item.source)
        parsed = PyMuPDFParser().parse(item.source.path)
        validate_source_snapshot(item.source)
    except (OSError, UnicodeError, ValueError):
        raise _MetadataRecheckUnavailable("源 PDF 已变化、缺失或无法进行本地重检。") from None
    if parsed.info.sha256 != item.source.sha256:
        raise _MetadataRecheckUnavailable("源 PDF 哈希与批次快照不一致。")

    candidate = suggest_submission_metadata_locally(
        document=parsed.info,
        blocks=parsed.blocks,
        current=item.metadata,
    )
    suggestions, unresolved = build_metadata_suggestions(item.metadata, candidate)
    return _PreparedMetadataRecheck(
        preview=BatchMetadataRecheckItem(
            item_id=item.item_id,
            source_filename=item.source.filename,
            base_metadata_sha256=current_hash,
            suggestions=suggestions,
            unresolved_fields=unresolved,
        ),
        current=item.metadata.model_copy(deep=True),
        candidate=candidate,
    )


@dataclass(slots=True)
class _MetadataFileOperation:
    destination: Path
    prepared: Path | None
    backup: Path
    must_be_absent: bool = False
    original_moved: bool = False
    replacement_installed: bool = False


class _MetadataUpdateFileTransaction:
    """Best-effort multi-file commit with same-directory rollback copies."""

    def __init__(self) -> None:
        self._token = uuid.uuid4().hex
        self._operations: list[_MetadataFileOperation] = []
        self._destinations: set[str] = set()

    def stage_copy(self, destination: Path, source: Path) -> None:
        prepared = self.prepare_replacement(destination)
        try:
            with source.open("rb") as source_handle, prepared.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        except BaseException:
            prepared.unlink(missing_ok=True)
            raise

    def prepare_replacement(
        self,
        destination: Path,
        *,
        must_be_absent: bool = False,
    ) -> Path:
        destination = Path(os.path.abspath(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._reserve_destination(destination)
        prepared = destination.with_name(f".{destination.name}.{self._token}.new")
        if _path_present(prepared):
            raise FileExistsError(f"metadata update staging path already exists: {prepared.name}")
        self._operations.append(
            _MetadataFileOperation(
                destination=destination,
                prepared=prepared,
                backup=destination.with_name(f".{destination.name}.{self._token}.bak"),
                must_be_absent=must_be_absent,
            )
        )
        return prepared

    def stage_removal(self, destination: Path) -> None:
        destination = Path(os.path.abspath(destination))
        self._reserve_destination(destination)
        self._operations.append(
            _MetadataFileOperation(
                destination=destination,
                prepared=None,
                backup=destination.with_name(f".{destination.name}.{self._token}.bak"),
            )
        )

    def commit(self) -> None:
        for operation in self._operations:
            destination = operation.destination
            if destination.is_symlink():
                raise ValueError(f"refusing to replace symbolic link: {destination.name}")
            if _path_present(operation.backup):
                raise FileExistsError(
                    f"metadata update backup path already exists: {operation.backup.name}"
                )
            if operation.must_be_absent and _path_present(destination):
                raise FileExistsError(
                    f"metadata update target appeared after allocation: {destination.name}"
                )
            if _path_present(destination):
                if not destination.is_file():
                    raise ValueError(f"metadata update target is not a file: {destination.name}")
                os.replace(destination, operation.backup)
                operation.original_moved = True
            if operation.prepared is not None:
                if operation.prepared.is_symlink() or not operation.prepared.is_file():
                    raise FileNotFoundError(
                        f"metadata update staged file is missing: {destination.name}"
                    )
                if operation.must_be_absent:
                    # A hard-link publish is atomic and fails rather than
                    # replacing a file created after path allocation.
                    os.link(operation.prepared, destination)
                    operation.replacement_installed = True
                    operation.prepared.unlink()
                    continue
                os.replace(operation.prepared, destination)
                operation.replacement_installed = True

    def rollback(self) -> list[BaseException]:
        errors: list[BaseException] = []
        for operation in reversed(self._operations):
            try:
                if operation.original_moved and _path_present(operation.backup):
                    os.replace(operation.backup, operation.destination)
                    operation.original_moved = False
                    operation.replacement_installed = False
                elif operation.replacement_installed and _path_present(operation.destination):
                    operation.destination.unlink()
                    operation.replacement_installed = False
            except BaseException as error:
                errors.append(error)
            try:
                if operation.prepared is not None:
                    operation.prepared.unlink(missing_ok=True)
            except BaseException as error:
                errors.append(error)
        return errors

    def finalize(self) -> None:
        # A failed cleanup leaves an inert hidden backup rather than turning a
        # fully committed correction into a false failure.
        for operation in self._operations:
            try:
                operation.backup.unlink(missing_ok=True)
            except OSError:
                pass
            if operation.prepared is not None:
                try:
                    operation.prepared.unlink(missing_ok=True)
                except OSError:
                    pass

    def _reserve_destination(self, destination: Path) -> None:
        key = str(destination).casefold()
        if key in self._destinations:
            raise ValueError(f"duplicate metadata update target: {destination.name}")
        self._destinations.add(key)


def _stage_metadata_update_files(
    *,
    transaction: _MetadataUpdateFileTransaction,
    run_dir: Path,
    output_dir: Path,
    old_report_path: Path | None,
    destination: Path,
    destination_must_be_absent: bool,
    run: RunRecord,
    rubric: RubricProfile,
    selected_report: Any,
    audit: AuditReport,
    evidence: list[EvidenceItem],
    presentation_profile: Any,
    metadata: SubmissionMetadata,
    dimension_scores: Mapping[str, float],
    batch: BatchRecord,
) -> None:
    """Build every replacement before exposing any of them to readers."""

    with tempfile.TemporaryDirectory(prefix=".metadata-update-build-", dir=run_dir) as directory:
        build_dir = Path(directory)
        provider_source = run_dir / "provider.json"
        if provider_source.is_file() and not provider_source.is_symlink():
            shutil.copyfile(provider_source, build_dir / "provider.json")

        RunArtifactStore(build_dir).write_model("submission-metadata.json", metadata)
        write_report_bundle(
            run_dir=build_dir,
            run=run,
            rubric=rubric,
            review=selected_report,
            audit=audit,
            evidence=evidence,
            presentation_profile=presentation_profile,
            submission_metadata=metadata,
            dimension_scores=dimension_scores,
        )

        artifact_names = (
            "submission-metadata.json",
            "report.json",
            "report.md",
            "evidence.json",
            "run-summary.json",
        )
        for name in artifact_names:
            source = build_dir / name
            if not source.is_file():
                raise FileNotFoundError(f"报告重建未生成必要文件：{name}")
            transaction.stage_copy(run_dir / name, source)
        presentation_source = build_dir / "report-presentation.json"
        if presentation_source.is_file():
            transaction.stage_copy(run_dir / presentation_source.name, presentation_source)

        markdown = (build_dir / "report.md").read_text(encoding="utf-8")
        staged_pdf = transaction.prepare_replacement(
            destination,
            must_be_absent=destination_must_be_absent,
        )
        title = _markdown_title(markdown) or Path(run.input_path).stem
        render_pdf(markdown, staged_pdf, title=title)
        validate_pdf(staged_pdf, markdown)

        if batch.summary_path is not None and batch.summary_path.expanduser().is_symlink():
            raise ValueError("批次汇总文件不能是符号链接。")
        csv_destination = _ensure_batch_summary_path(batch)
        staged_csv = transaction.prepare_replacement(csv_destination)
        dimensions = [
            (dimension.dimension_id, dimension.title)
            for dimension in batch.rubric_snapshot.dimensions
        ]
        write_batch_summary_csv(staged_csv, batch, dimensions)

        if old_report_path is not None and old_report_path.is_file():
            transaction.stage_removal(old_report_path)


def _managed_item_report_path(record: BatchRecord, item: BatchItem) -> Path | None:
    """Return only a report path that the batch can safely replace or retire."""

    if item.report_path is None or item.run_id is None or item.metadata is None:
        return None
    requested = item.report_path.expanduser()
    if requested.is_symlink():
        return None
    candidate = requested.resolve(strict=False)
    output_dir = record.request.output_dir.resolve()
    if candidate.parent != output_dir or candidate.is_symlink():
        return None
    managed_metadata = [item.metadata]
    if item.metadata.schema_version == "1.0" and item.metadata.needs_review:
        # Before schema 1.1 introduced safe pending-review names, completed
        # reports always used the standard metadata-derived name. Accept that
        # one exact legacy name so it can be retired, while still rejecting
        # arbitrary files merely referenced by a modified manifest.
        legacy_confirmed = item.metadata.model_copy(update={"human_reviewed": True})
        managed_metadata.append(legacy_confirmed)
    if not any(
        is_allocated_report_filename(
            output_dir,
            candidate.name,
            metadata,
            item.run_id,
            source_filename=item.source.filename,
        )
        for metadata in managed_metadata
    ):
        return None
    if _path_present(candidate) and not candidate.is_file():
        return None
    return candidate


def _restore_batch_manifest_if_needed(store: BatchStore, original: BatchRecord) -> None:
    """Restore an old manifest if a save raised after replacing it."""

    try:
        current = store.load(original.batch_id)
    except (OSError, UnicodeError, ValueError):
        store.save(original)
        return
    if current != original:
        store.save(original)


async def _save_batch_manifest_for_metadata_update(
    store: BatchStore,
    record: BatchRecord,
) -> None:
    await _metadata_update_to_thread(store.save, record)


async def _metadata_update_to_thread(
    function: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Let an in-flight file operation settle before cancellation rollback."""

    operation_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancellation_requested = False
    while not operation_task.done():
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            # Repeated Task.cancel() calls must not let the caller escape while
            # the filesystem thread can still mutate files outside its lock.
            cancellation_requested = True
            continue
    if cancellation_requested:
        try:
            operation_task.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return operation_task.result()


async def _metadata_cleanup_to_thread(
    function: Callable[..., Any],
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Finish rollback work and preserve its result despite repeated cancellation."""

    operation_task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    while not operation_task.done():
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            continue
    return operation_task.result()


def _path_present(path: Path) -> bool:
    return os.path.lexists(path)


def _batch_item(record: BatchRecord, item_id: str) -> BatchItem:
    for item in record.items:
        if item.item_id == item_id:
            return item
    raise ValueError(f"未知批次论文：{item_id}")


def _emit_batch_event(
    sink: BatchEventSink | None,
    record: BatchRecord,
    *,
    event_type: str,
    message: str,
    item: BatchItem | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    if sink is None:
        return
    event_payload = dict(payload or {})
    if event_type != "batch_run_event":
        event_payload["record"] = record.model_dump(mode="json")
    sink(
        BatchEvent(
            batch_id=record.batch_id,
            event_type=event_type,
            status=record.status,
            item_id=item.item_id if item is not None else None,
            item_status=item.status if item is not None else None,
            message=message,
            payload=event_payload,
        )
    )


def _batch_item_completion_message(item: BatchItem) -> str:
    if item.status is BatchItemStatus.COMPLETED:
        return f"已完成：{item.source.filename}"
    if item.status is BatchItemStatus.SOURCE_CHANGED:
        return f"源文件已变化：{item.source.filename}"
    return f"评测失败：{item.source.filename}"


def _course_grade(rubric: RubricProfile, total_score: float | None) -> str | None:
    if total_score is None:
        return None
    if rubric.dimensions:
        for anchor in rubric.dimensions[0].anchors:
            if anchor.minimum <= total_score <= anchor.maximum:
                return anchor.label
    if total_score >= 90:
        return "优秀"
    if total_score >= 75:
        return "良好"
    if total_score >= 60:
        return "达到基本要求"
    if total_score >= 40:
        return "完成不足"
    return "核心任务明显缺失"


def _course_conclusion(rubric: RubricProfile, total_score: float | None) -> str | None:
    if total_score is None:
        return None
    passing_score = rubric.aggregation.passing_score if rubric.aggregation is not None else None
    if passing_score is None:
        return "仅提供诊断分，不设置及格结论"
    return "达到课程论文基本要求" if total_score >= passing_score else "未达到课程论文基本要求"


def _validation_messages(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        ]
    return [str(error)]


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

    artifacts = RunArtifactStore(run_dir)
    rubric_path = artifacts.path("rubric.json")
    audit_path = artifacts.path("audit.json")
    missing = [
        label
        for label, path in (("rubric", rubric_path), ("audit", audit_path))
        if not path.is_file()
    ]
    if missing:
        raise ValueError(f"报告尚不完整：{', '.join(missing)}")

    rubric = artifacts.load_model("rubric.json", RubricProfile)
    audit = artifacts.load_model("audit.json", AuditReport)
    candidates = ("evaluation-report.json", "report.json", "meta-review.json")
    invalid: list[str] = []
    for name in candidates:
        if not artifacts.exists(name):
            continue
        try:
            return rubric, artifacts.load_model(name, EvaluationReport), audit
        except ValidationError:
            try:
                return rubric, artifacts.load_model(name, MetaReview), audit
            except ValidationError:
                invalid.append(name)
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
        events.append(
            project_run_event(
                run_id=run_id,
                event_type=event_type,
                payload=row.get("payload"),
                timestamp=row.get("timestamp"),
                view=RunEventView.TRACE,
            )
        )
    return events
