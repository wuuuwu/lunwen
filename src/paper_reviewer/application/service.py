from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
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
from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.application.app_state import AppPaths, read_json_lines
from paper_reviewer.application.models import (
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
    load_run_request_context,
    load_run_snapshots,
)
from paper_reviewer.application.review_planner import build_review_plan
from paper_reviewer.application.state_machine import transition
from paper_reviewer.config import Settings, load_review_profile, load_rubric
from paper_reviewer.domain.document import DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.review import (
    EvaluationReport,
    HardRuleAssessment,
    HardRuleStatus,
    HumanRuleDecision,
    MetaReview,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.reporting.exporter import render_pdf, validate_pdf
from paper_reviewer.reporting.renderer import render_markdown
from paper_reviewer.validation.audits import AuditReport
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
        api_key = self.credentials.get(request.provider)
        model = create_model_adapter(
            request.provider,
            request.model,
            timeout=self.settings.request_timeout_seconds,
            api_key=api_key,
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
            orchestrator = self._orchestrator(model, sessions, scholarly, event_sink)
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
        pending = await self._pending_hard_rules(run_id)
        if pending:
            await engine.dispose()
            raise ValueError("仍有否决项等待人工确认，不能恢复评测。")
        rubric, profile = load_run_snapshots(self.settings.runs_dir / run_id)
        panel_path = self.settings.runs_dir / run_id / "panel-profile.json"
        panel_profile = load_review_profile(panel_path) if panel_path.is_file() else None
        api_key = self.credentials.get(run.provider)
        model = create_model_adapter(
            run.provider,
            run.model,
            timeout=self.settings.request_timeout_seconds,
            api_key=api_key,
        )
        request_context = load_run_request_context(self.settings.runs_dir / run_id)
        external_search = request_context.get("external_search", True) is not False
        async with httpx.AsyncClient(timeout=self.settings.external_timeout_seconds) as client:
            scholarly: list[Any] = (
                [OpenAlexClient(client), CrossrefClient(client), ArxivClient(client)]
                if external_search
                else []
            )
            orchestrator = self._orchestrator(model, sessions, scholarly, event_sink)
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
        return normalized

    async def resume_after_human_review(
        self, run_id: str, *, event_sink: EventSink | None = None
    ) -> RunRecord:
        await self._require_run(run_id)
        if await self._pending_hard_rules(run_id):
            raise ValueError("必须处理全部否决项嫌疑后才能继续评测。")
        return await self.resume_review(run_id, event_sink=event_sink)

    async def cancel_review(self, run_id: str) -> RunRecord:
        engine = create_engine(self.settings.database_url)
        await initialize_database(engine)
        repository = RunRepository(create_session_factory(engine))
        try:
            run = await repository.get(run_id)
            if run is None:
                raise ValueError(f"未知任务：{run_id}")
            if run.status in {RunStatus.REPORTED, RunStatus.FATAL_FAILURE, RunStatus.CANCELLED}:
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
        summaries = [RunSummary.from_record(record) for record in records]
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
        return RunDetail(
            run=run,
            events=events,
            pending_hard_rules=await self._pending_hard_rules(run_id),
            human_rule_decisions=await self._human_rule_decisions(run_id),
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
            document=document,
            rubric=rubric,
            review=review,
            audit=audit,
            evidence=evidence,
            dimension_scores=dimension_scores,
            report_markdown=run_dir / "report.md",
            report_json=run_dir / "report.json",
            evaluation=evaluation,
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
        if run.status is not RunStatus.REPORTED:
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


def _validation_messages(error: Exception) -> list[str]:
    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        ]
    return [str(error)]


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
