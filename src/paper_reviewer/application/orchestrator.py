from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import StatementError

from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.agents.meta_reviewer import run_meta_reviewer
from paper_reviewer.agents.panel_reviewer import run_panel_reviewer
from paper_reviewer.agents.reviewer import run_reviewer
from paper_reviewer.application.artifacts import RunArtifactStore
from paper_reviewer.application.evidence_builder import build_external_evidence
from paper_reviewer.application.models import RunEvent
from paper_reviewer.application.pipeline import PipelineContext
from paper_reviewer.application.providers import (
    builtin_provider_connections,
    validate_provider_snapshot_identity,
)
from paper_reviewer.application.reference_checker import (
    MAX_EXTRACTED_REFERENCES,
    check_references,
    extract_references,
)
from paper_reviewer.application.review_planner import ReviewPlan, build_review_plan
from paper_reviewer.application.run_events import project_run_event
from paper_reviewer.application.state_machine import transition
from paper_reviewer.config import ReviewerProfile, ReviewProfile, Settings
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.provider import ProviderSnapshot
from paper_reviewer.domain.reference import ReferenceCheckReport
from paper_reviewer.domain.review import (
    CriterionAssessment,
    DiagnosticScore,
    EvaluationReport,
    ExpertOpinion,
    HardRuleAssessment,
    HardRuleStatus,
    HumanPanelDecision,
    HumanRuleDecision,
    MetaReview,
    PanelOutcome,
    PolicyContext,
    ReviewerResult,
    ReviewFinding,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.ports.document_parser import DocumentParserPort
from paper_reviewer.ports.model import ModelPort
from paper_reviewer.ports.scholarly_search import ScholarlySearchPort
from paper_reviewer.ports.web_search import WebSearchPort
from paper_reviewer.reporting.renderer import write_report_bundle
from paper_reviewer.tools.web_search import WebSearchTools
from paper_reviewer.validation.audits import (
    AuditReport,
    audit_criterion_assessments,
    audit_evaluation_report,
    audit_expert_opinions,
    audit_hard_rule_assessments,
    audit_meta_review,
    audit_reviews,
    reviewer_reference_errors,
)
from paper_reviewer.validation.panel import (
    build_human_review_summary,
    decide_expert_panel,
    decide_panel,
)
from paper_reviewer.validation.scoring import aggregate_scores


class SanitizedDatabaseError(RuntimeError):
    """Public error for database failures without SQL statement or bound values.

    The original exception is retained for in-process diagnostics through
    ``original_error``.  It is deliberately not chained when this exception is
    raised from the orchestrator: the GUI worker renders a full traceback, and
    a normal exception chain would include SQL parameters and document text.
    """

    def __init__(self, message: str, *, original_error: BaseException) -> None:
        super().__init__(message)
        self.original_error = original_error


class ReviewOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        model: ModelPort,
        parser: DocumentParserPort,
        run_repository: RunRepository,
        document_repository: DocumentRepository,
        evidence_repository: EvidenceRepository,
        review_repository: ReviewRepository,
        scholarly_clients: list[ScholarlySearchPort] | None = None,
        web_search_client: WebSearchPort | None = None,
        event_sink: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self.settings = settings
        self.model = model
        self.parser = parser
        self.runs = run_repository
        self.documents = document_repository
        self.evidence = evidence_repository
        self.reviews = review_repository
        self.scholarly_clients = scholarly_clients or []
        self.web_search_client = web_search_client
        self.event_sink = event_sink

    async def create_and_execute(
        self,
        *,
        input_path: Path,
        rubric: RubricProfile,
        profile: ReviewProfile,
        provider: str,
        model_name: str,
        discipline_name: str = "",
        discipline_profile: Path | None = None,
        panel_profile: ReviewProfile | None = None,
        cloud_processing_authorized: bool | None = None,
        contains_classified_material: bool = False,
        external_search: bool = True,
        provider_snapshot: ProviderSnapshot | None = None,
    ) -> RunRecord:
        if provider_snapshot is None:
            provider_snapshot = _builtin_provider_snapshot(provider, model_name)
        if provider_snapshot is not None:
            validate_provider_snapshot_identity(provider, model_name, provider_snapshot)
        if provider.startswith("custom:") and provider_snapshot is None:
            raise ValueError("自定义 Provider 任务必须由桌面端提供不可变 Provider 快照。")
        if _is_dual_advisory(rubric):
            if cloud_processing_authorized is not True:
                raise ValueError("cloud processing authorization is required")
            if contains_classified_material:
                raise ValueError("classified material cannot be processed in the cloud")
        resolved_input = await asyncio.to_thread(input_path.resolve)
        input_hash = await asyncio.to_thread(_file_hash, input_path)
        discipline_profile_text = (
            await asyncio.to_thread(discipline_profile.read_text, encoding="utf-8")
            if discipline_profile is not None
            else None
        )
        run = RunRecord(
            run_id=uuid.uuid4().hex,
            input_path=str(resolved_input),
            input_hash=input_hash,
            config_hash=_run_config_hash(
                rubric=rubric,
                profile=profile,
                panel_profile=panel_profile,
                discipline_name=discipline_name,
                discipline_profile=discipline_profile_text,
                external_search=external_search,
                provider_snapshot=provider_snapshot,
            ),
            rubric_id=f"{rubric.rubric_id}@{rubric.version}",
            provider=provider,
            model=model_name,
        )
        await self.runs.create(run)
        run_dir = self.settings.runs_dir / run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts = RunArtifactStore(run_dir)
        if provider_snapshot is not None:
            artifacts.write_json("provider.json", provider_snapshot.model_dump(mode="json"))
        artifacts.write_model("rubric.json", rubric)
        artifacts.write_model("review-profile.json", profile)
        if panel_profile is not None:
            artifacts.write_model("panel-profile.json", panel_profile)
        artifacts.write_json(
            "request-context.json",
            {
                "discipline_name": discipline_name,
                "discipline_profile": discipline_profile_text,
                "cloud_processing_authorized": bool(cloud_processing_authorized),
                "contains_classified_material": contains_classified_material,
                "external_search": external_search,
            },
        )
        self._append_trace(run.run_id, "run_created", {"status": run.status.value})
        return await self.execute(
            run,
            rubric=rubric,
            profile=profile,
            panel_profile=panel_profile,
        )

    async def execute(
        self,
        run: RunRecord,
        *,
        rubric: RubricProfile,
        profile: ReviewProfile,
        panel_profile: ReviewProfile | None = None,
    ) -> RunRecord:
        run_dir = self.settings.runs_dir / run.run_id
        plan = build_review_plan(rubric, profile)
        dual_advisory = _is_dual_advisory(rubric)
        context = load_run_request_context(run_dir)
        discipline_name = str(context.get("discipline_name", ""))
        discipline_profile = context.get("discipline_profile")
        if discipline_profile is not None and not isinstance(discipline_profile, str):
            discipline_profile = None
        pipeline = PipelineContext(
            run=run,
            run_dir=run_dir,
            rubric=rubric,
            panel_profile=panel_profile,
            plan=plan,
            dual_advisory=dual_advisory,
            discipline_name=discipline_name,
            discipline_profile=discipline_profile,
        )
        try:
            await self._execute_ingest_stage(pipeline)
            await self._execute_evidence_stage(pipeline)
            await self._execute_reviews_stage(pipeline)
            await self._execute_audit_stage(pipeline)
            await self._execute_panel_stage(pipeline)
            await self._execute_meta_stage(pipeline)
            await self._execute_report_stage(pipeline)
            return run
        except asyncio.CancelledError:
            run.error = None
            if run.status not in {
                RunStatus.CANCELLED,
                RunStatus.REPORTED,
                RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
            }:
                run.status = transition(run.status, RunStatus.CANCELLED)
                await self.runs.save(run, event_type="run_cancelled", payload={})
                self._append_trace(run.run_id, "run_cancelled", {"status": run.status.value})
            raise
        except Exception as error:
            safe_message = _safe_error_message(error)
            run.error = safe_message
            if run.status not in {
                RunStatus.RETRYABLE_FAILURE,
                RunStatus.FATAL_FAILURE,
                RunStatus.REPORTED,
                RunStatus.REPORTED_PENDING_HUMAN_REVIEW,
            }:
                run.status = transition(run.status, RunStatus.RETRYABLE_FAILURE)
                await self.runs.save(
                    run,
                    event_type="stage_failed",
                    payload={
                        "error_type": type(error).__name__,
                        "message": safe_message,
                        "status": run.status.value,
                    },
                )
                self._append_trace(
                    run.run_id,
                    "stage_failed",
                    {
                        "error_type": type(error).__name__,
                        "message": safe_message,
                        "status": run.status.value,
                    },
                )
            if _is_database_error(error):
                raise SanitizedDatabaseError(safe_message, original_error=error) from None
            raise

    async def _execute_ingest_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        artifacts = RunArtifactStore(pipeline.run_dir)
        if "ingest" not in run.completed_stages:
            await self._set_status(run, RunStatus.INGESTING, "ingest_started")
            parsed = await asyncio.to_thread(self.parser.parse, Path(run.input_path))
            await self.documents.add_blocks(run.run_id, parsed.blocks)
            artifacts.write_model("document.json", parsed.info)
            await self._complete_stage(run, "ingest", RunStatus.INGESTED)
            pipeline.document = parsed.info
        else:
            pipeline.document = artifacts.load_model("document.json", DocumentInfo)
        pipeline.blocks = await self.documents.list_blocks(run.run_id)

    async def _execute_evidence_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        assert pipeline.document is not None
        artifacts = RunArtifactStore(pipeline.run_dir)
        reference_report_path = pipeline.run_dir / "reference-checks.json"
        reference_report = ReferenceCheckReport()
        if "evidence" not in run.completed_stages:
            await self._set_status(run, RunStatus.BUILDING_EVIDENCE, "evidence_collection_started")
            evidence, warnings = await build_external_evidence(
                run_id=run.run_id,
                query=pipeline.document.title or "academic paper",
                clients=self.scholarly_clients,
            )
            extracted_references = extract_references(
                pipeline.blocks,
                max_references=min(
                    self.settings.max_reference_checks + 1,
                    MAX_EXTRACTED_REFERENCES,
                ),
            )
            reference_entries = extracted_references[: self.settings.max_reference_checks]
            if reference_entries and (self.web_search_client is not None or self.scholarly_clients):
                self._append_trace(
                    run.run_id,
                    "reference_check_started",
                    {"count": len(reference_entries)},
                )
                reference_report, reference_evidence = await check_references(
                    run_id=run.run_id,
                    entries=reference_entries,
                    web_search=self.web_search_client,
                    scholarly_clients=self.scholarly_clients,
                    per_source_limit=self.settings.reference_search_results,
                    max_concurrency=self.settings.reference_check_concurrency,
                )
                if len(extracted_references) > len(reference_entries):
                    reference_report.warnings.insert(
                        0,
                        "参考文献超过本次自动核验上限 "
                        f"{self.settings.max_reference_checks} 条；其余条目建议人工核对。",
                    )
                evidence_by_id = {item.evidence_id: item for item in evidence}
                for item in reference_evidence:
                    evidence_by_id.setdefault(item.evidence_id, item)
                evidence = list(evidence_by_id.values())
                warnings.extend(reference_report.warnings)
                self._append_trace(
                    run.run_id,
                    "reference_check_completed",
                    {
                        "count": len(reference_report.checks),
                        "verified": reference_report.verified_count,
                        "probable": reference_report.probable_count,
                        "unresolved": reference_report.unresolved_count,
                    },
                )
            artifacts.write_json("reference-checks.json", reference_report.model_dump(mode="json"))
            await self.evidence.replace(run.run_id, evidence)
            await self._complete_stage(
                run,
                "evidence",
                RunStatus.EVIDENCE_READY,
                payload={
                    "count": len(evidence),
                    "reference_checks": len(reference_report.checks),
                    "reference_verified": reference_report.verified_count,
                    "reference_probable": reference_report.probable_count,
                    "reference_unresolved": reference_report.unresolved_count,
                    "warnings": warnings,
                },
            )
        elif reference_report_path.is_file():
            reference_report = artifacts.load_model("reference-checks.json", ReferenceCheckReport)
        pipeline.reference_report = reference_report
        pipeline.evidence = await self.evidence.list(run.run_id)

    async def _execute_reviews_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        assert pipeline.document is not None
        scoring_stage = "scoring" if pipeline.dual_advisory else "reviews"
        scoring_status = RunStatus.SCORING if pipeline.dual_advisory else RunStatus.REVIEWING
        assigned_ids = {item.reviewer.reviewer_id for item in pipeline.plan.assignments}
        if scoring_stage not in run.completed_stages:
            await self._set_status(
                run,
                scoring_status,
                "scoring_started" if pipeline.dual_advisory else "reviews_started",
            )
            stored_results = await self.reviews.list_results(run.run_id)
            completed_ids = {
                item.reviewer_id for item in stored_results if item.reviewer_id in assigned_ids
            }
            await self._run_reviewers(
                run=run,
                plan=pipeline.plan,
                rubric=pipeline.rubric,
                document=pipeline.document,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
                reviewer_ids=assigned_ids - completed_ids,
                discipline_name=pipeline.discipline_name,
                discipline_profile=pipeline.discipline_profile,
                persist_results=True,
            )
            await self._complete_stage(
                run,
                scoring_stage,
                RunStatus.AUDITING,
                payload={"count": len(assigned_ids)},
            )
        stored_results = await self.reviews.list_results(run.run_id)
        pipeline.results = [item for item in stored_results if item.reviewer_id in assigned_ids]

    async def _execute_audit_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        assert pipeline.document is not None
        artifacts = RunArtifactStore(pipeline.run_dir)
        if "audit" in run.completed_stages:
            pipeline.audit = artifacts.load_model("audit.json", AuditReport)
            return
        if run.status != RunStatus.AUDITING:
            await self._set_status(run, RunStatus.AUDITING, "audit_started")
        pipeline.results = await self._repair_invalid_reviewer_references(
            run=run,
            plan=pipeline.plan,
            rubric=pipeline.rubric,
            document=pipeline.document,
            blocks=pipeline.blocks,
            evidence=pipeline.evidence,
            results=pipeline.results,
            discipline_name=pipeline.discipline_name,
            discipline_profile=pipeline.discipline_profile,
        )
        if pipeline.dual_advisory:
            criterion_assessments = [
                assessment
                for result in pipeline.results
                for assessment in result.criterion_assessments
            ]
            hard_rules = _collect_hard_rule_assessments(pipeline.results)
            reviewer_dimensions: dict[str, Collection[str]] = {
                item.reviewer.reviewer_id: item.dimension_ids for item in pipeline.plan.assignments
            }
            reference_audit = AuditReport()
            block_ids = {item.block_id for item in pipeline.blocks}
            block_by_id = {item.block_id: item for item in pipeline.blocks}
            evidence_ids = {item.evidence_id for item in pipeline.evidence}
            for result in pipeline.results:
                reference_audit.errors.extend(
                    reviewer_reference_errors(
                        result=result,
                        block_ids=block_ids,
                        evidence_ids=evidence_ids,
                        block_by_id=block_by_id,
                    )
                )
            criterion_audit = audit_criterion_assessments(
                assessments=criterion_assessments,
                rubric=pipeline.rubric,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
                reviewer_dimensions=reviewer_dimensions,
            )
            hard_rule_audit = audit_hard_rule_assessments(
                assessments=hard_rules,
                known_rule_ids={item.rule_id for item in pipeline.rubric.hard_rules},
                human_decisions=[],
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
            )
            audit = _combine_audits(reference_audit, criterion_audit, hard_rule_audit)
            diagnostic = DiagnosticScore(
                assessments=criterion_assessments,
                group_scores=_diagnostic_group_scores(pipeline.rubric, criterion_assessments),
                total_score=round(
                    sum(item.weighted_contribution for item in criterion_assessments),
                    2,
                ),
            )
            await self.reviews.save_diagnostic_score(run.run_id, diagnostic)
            artifacts.write_model("diagnostic-score.json", diagnostic)
            await self.reviews.artifacts.save_json(
                run.run_id,
                "hard_rule_assessments",
                hard_rules,
                replace=True,
            )
            artifacts.write_model_list("hard-rule-assessments.json", hard_rules)
        else:
            audit = audit_reviews(
                results=pipeline.results,
                rubric=pipeline.rubric,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
            )
        for warning in pipeline.reference_report.warnings:
            if warning not in audit.warnings:
                audit.warnings.append(warning)
        artifacts.write_model("audit.json", audit)
        if not audit.passed:
            raise ValueError("deterministic review audit failed: " + "; ".join(audit.errors))
        pipeline.audit = audit
        target = RunStatus.PANEL_REVIEWING if pipeline.dual_advisory else RunStatus.META_REVIEWING
        await self._complete_stage(run, "audit", target)

    async def _execute_panel_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        if not pipeline.dual_advisory or "panel" in run.completed_stages:
            return
        assert pipeline.document is not None
        artifacts = RunArtifactStore(pipeline.run_dir)
        hard_rules_path = pipeline.run_dir / "hard-rule-assessments.json"
        hard_rules = artifacts.load_model_list("hard-rule-assessments.json", HardRuleAssessment)
        if not hard_rules_path.is_file():
            stored_hard_rules = await self.reviews.artifacts.get_json(
                run.run_id, artifact_type="hard_rule_assessments"
            )
            if isinstance(stored_hard_rules, list):
                hard_rules = [HardRuleAssessment.model_validate(item) for item in stored_hard_rules]
        human_decisions = _merge_human_decisions(
            artifacts.load_model_list("human-rule-decisions.json", HumanRuleDecision),
            [
                _human_decision_from_repository(item)
                for item in await self.reviews.hard_rules.list_human_rule_decisions(run.run_id)
            ],
        )
        opinions_path = pipeline.run_dir / "expert-opinions.json"
        opinions = _merge_expert_opinions(
            artifacts.load_model_list("expert-opinions.json", ExpertOpinion),
            [
                ExpertOpinion.model_validate(item)
                for item in await self.reviews.list_expert_opinion_payloads(run.run_id)
            ],
        )
        if pipeline.panel_profile is None:
            pipeline.panel_profile = _load_panel_profile_snapshot(pipeline.run_dir)
        if pipeline.panel_profile is None or len(pipeline.panel_profile.reviewers) < 5:
            raise ValueError("dual-advisory evaluation requires a five-member panel profile")
        if run.status is not RunStatus.PANEL_REVIEWING:
            await self._set_status(run, RunStatus.PANEL_REVIEWING, "panel_review_started")
        findings = [finding for item in pipeline.results for finding in item.findings]
        opinions = await self._run_panel_round(
            run=run,
            experts=pipeline.panel_profile.reviewers[:3],
            round_name="initial",
            rubric=pipeline.rubric,
            document=pipeline.document,
            blocks=pipeline.blocks,
            evidence=pipeline.evidence,
            findings=findings,
            discipline_name=pipeline.discipline_name,
            discipline_profile=pipeline.discipline_profile,
            existing=opinions,
            output_path=opinions_path,
        )
        initial = [item for item in opinions if item.round == "initial"]
        supplemental = [item for item in opinions if item.round == "supplemental"]
        expert_decision = decide_expert_panel(
            initial=initial,
            supplemental=supplemental,
        )
        if expert_decision.outcome is PanelOutcome.SUPPLEMENTAL_REQUIRED:
            await self._set_status(
                run,
                RunStatus.SUPPLEMENTAL_REVIEWING,
                "supplemental_review_started",
            )
            opinions = await self._run_panel_round(
                run=run,
                experts=pipeline.panel_profile.reviewers[3:5],
                round_name="supplemental",
                rubric=pipeline.rubric,
                document=pipeline.document,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
                findings=findings,
                discipline_name=pipeline.discipline_name,
                discipline_profile=pipeline.discipline_profile,
                existing=opinions,
                output_path=opinions_path,
            )
            expert_decision = decide_expert_panel(
                initial=[item for item in opinions if item.round == "initial"],
                supplemental=[item for item in opinions if item.round == "supplemental"],
            )
        human_panel_decision = artifacts.load_optional_model(
            "human-panel-decision.json", HumanPanelDecision
        )
        decision = decide_panel(
            initial=[item for item in opinions if item.round == "initial"],
            supplemental=[item for item in opinions if item.round == "supplemental"],
            hard_rules=hard_rules,
            human_decisions=human_decisions,
            human_panel_decision=human_panel_decision,
        )
        artifacts.write_model("expert-panel-decision.json", expert_decision)
        artifacts.write_model("panel-decision.json", decision)
        await self.reviews.save_panel_decision(run.run_id, decision)
        await self._complete_stage(run, "panel", RunStatus.SYNTHESIZING)

    async def _execute_meta_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        assert pipeline.audit is not None
        artifacts = RunArtifactStore(pipeline.run_dir)
        if "meta" in run.completed_stages:
            pipeline.meta = artifacts.load_model("meta-review.json", MetaReview)
            return
        meta_status = RunStatus.SYNTHESIZING if pipeline.dual_advisory else RunStatus.META_REVIEWING
        if run.status != meta_status:
            await self._set_status(run, meta_status, "meta_review_started")
        meta = await run_meta_reviewer(
            run_id=run.run_id,
            model=self.model,
            rubric=pipeline.rubric,
            results=pipeline.results,
            audit=pipeline.audit,
            max_repairs=self.settings.max_output_repairs,
            event_sink=lambda event, payload: self._append_trace(run.run_id, event, payload),
        )
        final_audit = audit_meta_review(
            meta=meta,
            source_results=pipeline.results,
            blocks=pipeline.blocks,
            evidence=pipeline.evidence,
            scoring_enabled=pipeline.rubric.scoring_enabled,
        )
        if not final_audit.passed:
            raise ValueError("meta review audit failed: " + "; ".join(final_audit.errors))
        if pipeline.dual_advisory:
            meta.total_score = None
            meta.verdict = None
        else:
            aggregated = aggregate_scores(pipeline.rubric, pipeline.results)
            meta.total_score = aggregated.total_score
            meta.verdict = aggregated.verdict
        artifacts.write_model("meta-review.json", meta)
        pipeline.meta = meta
        await self._complete_stage(run, "meta", RunStatus.VALIDATING)

    async def _execute_report_stage(self, pipeline: PipelineContext) -> None:
        run = pipeline.run
        assert pipeline.meta is not None
        assert pipeline.audit is not None
        artifacts = RunArtifactStore(pipeline.run_dir)
        evaluation: EvaluationReport | None = None
        if pipeline.dual_advisory:
            diagnostic_path = pipeline.run_dir / "diagnostic-score.json"
            if diagnostic_path.is_file():
                diagnostic = artifacts.load_model("diagnostic-score.json", DiagnosticScore)
            else:
                stored_diagnostic = await self.reviews.get_diagnostic_score(run.run_id)
                if not isinstance(stored_diagnostic, dict):
                    raise ValueError("diagnostic score checkpoint is missing")
                diagnostic = DiagnosticScore.model_validate(stored_diagnostic)
            hard_rules_path = pipeline.run_dir / "hard-rule-assessments.json"
            hard_rules = artifacts.load_model_list(
                "hard-rule-assessments.json", HardRuleAssessment
            )
            if not hard_rules_path.is_file():
                stored_hard_rules = await self.reviews.artifacts.get_json(
                    run.run_id, artifact_type="hard_rule_assessments"
                )
                if isinstance(stored_hard_rules, list):
                    hard_rules = [
                        HardRuleAssessment.model_validate(item) for item in stored_hard_rules
                    ]
            human_decisions = _merge_human_decisions(
                artifacts.load_model_list("human-rule-decisions.json", HumanRuleDecision),
                [
                    _human_decision_from_repository(item)
                    for item in await self.reviews.hard_rules.list_human_rule_decisions(run.run_id)
                ],
            )
            opinions = _merge_expert_opinions(
                artifacts.load_model_list("expert-opinions.json", ExpertOpinion),
                [
                    ExpertOpinion.model_validate(item)
                    for item in await self.reviews.list_expert_opinion_payloads(run.run_id)
                ],
            )
            initial = [item for item in opinions if item.round == "initial"]
            supplemental = [item for item in opinions if item.round == "supplemental"]
            expert_panel_decision = decide_expert_panel(
                initial=initial,
                supplemental=supplemental,
            )
            human_panel_decision = artifacts.load_optional_model(
                "human-panel-decision.json", HumanPanelDecision
            )
            panel_decision = decide_panel(
                initial=initial,
                supplemental=supplemental,
                hard_rules=hard_rules,
                human_decisions=human_decisions,
                human_panel_decision=human_panel_decision,
            )
            human_review_summary = build_human_review_summary(
                hard_rules=hard_rules,
                human_decisions=human_decisions,
                expert_panel_decision=expert_panel_decision,
                human_panel_decision=human_panel_decision,
            )
            policy_value = getattr(pipeline.rubric, "policy_context", None) or getattr(
                pipeline.rubric, "policy", None
            )
            if policy_value is None:
                raise ValueError("dual-advisory rubric is missing policy context")
            evaluation = EvaluationReport(
                run_id=run.run_id,
                policy_context=PolicyContext.model_validate(policy_value),
                diagnostic_score=diagnostic,
                hard_rule_assessments=hard_rules,
                human_rule_decisions=human_decisions,
                expert_opinions=opinions,
                expert_panel_decision=expert_panel_decision,
                human_panel_decision=human_panel_decision,
                human_review_summary=human_review_summary,
                panel_decision=panel_decision,
                meta_review=pipeline.meta,
            )
            findings = [finding for item in pipeline.results for finding in item.findings]
            panel_audit = audit_expert_opinions(
                opinions=opinions,
                findings=findings,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
            )
            resolved_hard_rule_audit = audit_hard_rule_assessments(
                assessments=hard_rules,
                known_rule_ids={item.rule_id for item in pipeline.rubric.hard_rules},
                human_decisions=human_decisions,
                blocks=pipeline.blocks,
                evidence=pipeline.evidence,
            )
            evaluation_audit = audit_evaluation_report(report=evaluation)
            final_evaluation_audit = _combine_audits(
                panel_audit, resolved_hard_rule_audit, evaluation_audit
            )
            if not final_evaluation_audit.passed:
                raise ValueError(
                    "evaluation report audit failed: " + "; ".join(final_evaluation_audit.errors)
                )
            artifacts.write_model("evaluation-report.json", evaluation)
            await self.reviews.save_evaluation_report(run.run_id, evaluation)
        pipeline.evaluation = evaluation
        if "report" in run.completed_stages:
            return
        if run.status != RunStatus.VALIDATING:
            await self._set_status(run, RunStatus.VALIDATING, "report_validation_started")
        projected_run = run.model_copy(deep=True)
        projected_run.completed_stages.append("report")
        target_status = (
            RunStatus.REPORTED_PENDING_HUMAN_REVIEW
            if evaluation is not None and not evaluation.human_review_summary.complete
            else RunStatus.REPORTED
        )
        projected_run.status = transition(projected_run.status, target_status)
        write_report_bundle(
            run_dir=pipeline.run_dir,
            run=projected_run,
            rubric=pipeline.rubric,
            review=evaluation or pipeline.meta,
            audit=pipeline.audit,
            evidence=pipeline.evidence,
        )
        run.completed_stages = projected_run.completed_stages
        run.status = projected_run.status
        await self.runs.save(run, event_type="report_completed", payload={})
        self._append_trace(run.run_id, "report_completed", {})

    async def _run_reviewers(
        self,
        *,
        run: RunRecord,
        plan: ReviewPlan,
        rubric: RubricProfile,
        document: DocumentInfo,
        blocks: list[DocumentBlock],
        evidence: list[EvidenceItem],
        reviewer_ids: set[str] | None = None,
        repair_sources: dict[str, ReviewerResult] | None = None,
        discipline_name: str = "",
        discipline_profile: str | None = None,
        persist_results: bool = False,
    ) -> list[ReviewerResult]:
        dimensions = {dimension.dimension_id: dimension for dimension in rubric.dimensions}
        semaphore = asyncio.Semaphore(self.settings.reviewer_concurrency)
        evidence_lock = asyncio.Lock()
        persistence_lock = asyncio.Lock()
        web_search_tools = (
            WebSearchTools(
                client=self.web_search_client,
                run_id=run.run_id,
                evidence=evidence,
                evidence_lock=evidence_lock,
            )
            if self.web_search_client is not None
            else None
        )

        async def invoke(assignment: object) -> ReviewerResult:
            from paper_reviewer.application.review_planner import ReviewAssignment

            if not isinstance(assignment, ReviewAssignment):
                raise TypeError("invalid review assignment")
            async with semaphore:
                result = await run_reviewer(
                    run_id=run.run_id,
                    model=self.model,
                    reviewer=assignment.reviewer,
                    dimensions=[dimensions[item] for item in assignment.dimension_ids],
                    document=document,
                    blocks=blocks,
                    evidence=evidence,
                    scoring_enabled=rubric.scoring_enabled,
                    hard_rules=(
                        rubric.hard_rules if _reviews_hard_rules(assignment.reviewer) else []
                    ),
                    discipline_name=discipline_name,
                    discipline_profile=discipline_profile,
                    max_repairs=self.settings.max_output_repairs,
                    web_search_tools=web_search_tools,
                    repair_source=(
                        repair_sources.get(assignment.reviewer.reviewer_id)
                        if repair_sources is not None
                        else None
                    ),
                    event_sink=lambda event, payload: self._append_trace(
                        run.run_id, event, payload
                    ),
                )
                if persist_results:
                    async with persistence_lock:
                        async with evidence_lock:
                            evidence_snapshot = list(evidence)
                        await self.evidence.replace(run.run_id, evidence_snapshot)
                        await self.reviews.save_result(run.run_id, result)
                return result

        assignments = [
            item
            for item in plan.assignments
            if reviewer_ids is None or item.reviewer.reviewer_id in reviewer_ids
        ]
        if reviewer_ids is not None:
            available_ids = {item.reviewer.reviewer_id for item in assignments}
            missing_ids = reviewer_ids - available_ids
            if missing_ids:
                raise ValueError(
                    "stored reviewer results are not present in the review profile: "
                    f"{sorted(missing_ids)}"
                )
        outcomes = await asyncio.gather(
            *(invoke(item) for item in assignments), return_exceptions=True
        )
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        if failures:
            raise failures[0]
        return [item for item in outcomes if isinstance(item, ReviewerResult)]

    async def _run_panel_round(
        self,
        *,
        run: RunRecord,
        experts: list[ReviewerProfile],
        round_name: str,
        rubric: RubricProfile,
        document: DocumentInfo,
        blocks: list[DocumentBlock],
        evidence: list[EvidenceItem],
        findings: list[ReviewFinding],
        discipline_name: str,
        discipline_profile: str | None,
        existing: list[ExpertOpinion],
        output_path: Path,
    ) -> list[ExpertOpinion]:
        from typing import Literal, cast

        panel_round = cast(Literal["initial", "supplemental"], round_name)
        opinions = list(existing)
        completed = {item.expert_id for item in opinions if item.round == panel_round}
        for expert in experts:
            if expert.reviewer_id in completed:
                continue
            opinion = await run_panel_reviewer(
                run_id=run.run_id,
                model=self.model,
                expert=expert,
                round=panel_round,
                rubric=rubric,
                document=document,
                blocks=blocks,
                evidence=evidence,
                findings=findings,
                discipline_name=discipline_name,
                discipline_profile=discipline_profile,
                max_repairs=self.settings.max_output_repairs,
                event_sink=lambda event, payload: self._append_trace(run.run_id, event, payload),
            )
            opinions.append(opinion)
            await self.reviews.save_expert_opinion(
                run.run_id,
                opinion,
                role=panel_round,
                expert_id=opinion.expert_id,
            )
            RunArtifactStore(output_path.parent).write_model_list(output_path.name, opinions)
            self._append_trace(
                run.run_id,
                "panel_expert_completed",
                {
                    "expert_id": opinion.expert_id,
                    "round": opinion.round,
                    "verdict": opinion.verdict.value,
                },
            )
        return opinions

    async def _repair_invalid_reviewer_references(
        self,
        *,
        run: RunRecord,
        plan: ReviewPlan,
        rubric: RubricProfile,
        document: DocumentInfo,
        blocks: list[DocumentBlock],
        evidence: list[EvidenceItem],
        results: list[ReviewerResult],
        discipline_name: str = "",
        discipline_profile: str | None = None,
    ) -> list[ReviewerResult]:
        block_ids = {block.block_id for block in blocks}
        block_by_id = {block.block_id: block for block in blocks}
        evidence_ids = {item.evidence_id for item in evidence}
        invalid_results = {
            result.reviewer_id: result
            for result in results
            if reviewer_reference_errors(
                result=result,
                block_ids=block_ids,
                evidence_ids=evidence_ids,
                block_by_id=block_by_id,
            )
        }
        invalid_reviewer_ids = set(invalid_results)
        if not invalid_reviewer_ids:
            return results

        # The legacy evidence-repair contract only rewrites Findings.  A
        # checkpoint with invalid criterion/hard-rule evidence (or a page/quote
        # mismatch) must therefore be rerun as a complete isolated Reviewer;
        # all other valid Reviewer checkpoints remain untouched.
        repair_sources = {
            reviewer_id: result
            for reviewer_id, result in invalid_results.items()
            if _can_use_legacy_finding_repair(
                result=result,
                block_by_id=block_by_id,
                evidence_ids=evidence_ids,
            )
        }

        ordered_ids = sorted(invalid_reviewer_ids)
        self._append_trace(
            run.run_id,
            "review_reference_repair_started",
            {
                "reviewer_ids": ordered_ids,
                "full_rerun_ids": sorted(invalid_reviewer_ids - set(repair_sources)),
            },
        )
        repaired = await self._run_reviewers(
            run=run,
            plan=plan,
            rubric=rubric,
            document=document,
            blocks=blocks,
            evidence=evidence,
            reviewer_ids=invalid_reviewer_ids,
            repair_sources=repair_sources,
            discipline_name=discipline_name,
            discipline_profile=discipline_profile,
        )
        await self.evidence.replace(run.run_id, evidence)
        for result in repaired:
            await self.reviews.save_result(run.run_id, result)
        self._append_trace(
            run.run_id,
            "review_reference_repair_completed",
            {"reviewer_ids": ordered_ids},
        )
        return await self.reviews.list_results(run.run_id)

    async def _set_status(self, run: RunRecord, status: RunStatus, event_type: str) -> None:
        run.status = transition(run.status, status)
        run.error = None
        await self.runs.save(run, event_type=event_type, payload={})
        self._append_trace(run.run_id, event_type, {"status": run.status.value})

    async def _complete_stage(
        self,
        run: RunRecord,
        stage: str,
        status: RunStatus,
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        if stage not in run.completed_stages:
            run.completed_stages.append(stage)
        if run.status != status:
            run.status = transition(run.status, status)
        await self.runs.save(run, event_type=f"{stage}_completed", payload=payload or {})
        self._append_trace(
            run.run_id,
            f"{stage}_completed",
            {"status": run.status.value, **(payload or {})},
        )

    def _append_trace(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        trace_path = self.settings.runs_dir / run_id / "trace.jsonl"
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "payload": payload,
        }
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if self.event_sink is not None:
            self.event_sink(
                project_run_event(
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                )
            )


def load_run_snapshots(run_dir: Path) -> tuple[RubricProfile, ReviewProfile]:
    artifacts = RunArtifactStore(run_dir)
    rubric = artifacts.load_model("rubric.json", RubricProfile)
    profile = artifacts.load_model("review-profile.json", ReviewProfile)
    return rubric, profile


def _load_panel_profile_snapshot(run_dir: Path) -> ReviewProfile | None:
    return RunArtifactStore(run_dir).load_optional_model("panel-profile.json", ReviewProfile)


def load_run_request_context(run_dir: Path) -> dict[str, object]:
    artifacts = RunArtifactStore(run_dir)
    if not artifacts.exists("request-context.json"):
        return {}
    payload = artifacts.read_json("request-context.json")
    return payload if isinstance(payload, dict) else {}


def load_provider_snapshot(run_dir: Path) -> ProviderSnapshot | None:
    """Load the immutable, non-secret provider connection snapshot for a run."""
    return RunArtifactStore(run_dir).load_optional_model("provider.json", ProviderSnapshot)


def _builtin_provider_snapshot(provider_ref: str, model: str) -> ProviderSnapshot | None:
    for connection in builtin_provider_connections():
        if connection.provider_ref == provider_ref:
            return ProviderSnapshot(
                provider_ref=connection.provider_ref,
                display_name=connection.display_name,
                protocol=connection.protocol,
                base_url=connection.base_url,
                endpoint_fingerprint=connection.endpoint_fingerprint,
                model=model,
            )
    return None


def _run_config_hash(
    *,
    rubric: RubricProfile,
    profile: ReviewProfile,
    panel_profile: ReviewProfile | None,
    discipline_name: str,
    discipline_profile: str | None,
    external_search: bool,
    provider_snapshot: ProviderSnapshot | None = None,
) -> str:
    payload = {
        "rubric": rubric.model_dump(mode="json"),
        "review_profile": profile.model_dump(mode="json"),
        "panel_profile": (
            panel_profile.model_dump(mode="json") if panel_profile is not None else None
        ),
        "discipline_name": discipline_name,
        "discipline_profile": discipline_profile,
        "external_search": external_search,
        "provider": (
            {
                "provider_ref": provider_snapshot.provider_ref,
                "protocol": provider_snapshot.protocol.value,
                "base_url_fingerprint": provider_snapshot.endpoint_fingerprint,
                "model": provider_snapshot.model,
            }
            if provider_snapshot is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_dual_advisory(rubric: RubricProfile) -> bool:
    return (
        getattr(rubric, "evaluation_mode", None) == "dual_advisory" or rubric.schema_version == "2"
    )


def _reviews_hard_rules(reviewer: ReviewerProfile) -> bool:
    values = {
        reviewer.reviewer_id.casefold(),
        *(item.casefold() for item in reviewer.dimension_tags),
    }
    keywords = ("compliance", "integrity", "合规", "诚信", "hard_rule")
    return any(keyword in value for value in values for keyword in keywords)


def _collect_hard_rule_assessments(
    results: list[ReviewerResult],
) -> list[HardRuleAssessment]:
    precedence = {
        HardRuleStatus.NOT_DETECTED: 0,
        HardRuleStatus.NOT_ASSESSABLE: 1,
        HardRuleStatus.SUSPECTED: 2,
        HardRuleStatus.DISMISSED: 3,
        HardRuleStatus.CONFIRMED: 4,
    }
    selected: dict[str, HardRuleAssessment] = {}
    for result in results:
        for assessment in result.hard_rule_assessments:
            current = selected.get(assessment.rule_id)
            if current is None or precedence[assessment.status] > precedence[current.status]:
                selected[assessment.rule_id] = assessment
    return [selected[key] for key in sorted(selected)]


def _diagnostic_group_scores(
    rubric: RubricProfile, assessments: list[CriterionAssessment]
) -> dict[str, float]:
    contribution_by_criterion = {
        item.criterion_id: item.weighted_contribution for item in assessments
    }
    scores: dict[str, float] = {}
    for group in getattr(rubric, "groups", []):
        group_id = str(getattr(group, "group_id", "")).strip()
        criterion_ids = getattr(group, "dimensions", None) or getattr(group, "criterion_ids", [])
        if group_id and isinstance(criterion_ids, list):
            scores[group_id] = round(
                sum(
                    contribution_by_criterion.get(str(criterion_id), 0.0)
                    for criterion_id in criterion_ids
                ),
                2,
            )
    return scores


def _combine_audits(*reports: AuditReport) -> AuditReport:
    combined = AuditReport()
    covered: set[str] = set()
    for report in reports:
        combined.errors.extend(report.errors)
        combined.warnings.extend(report.warnings)
        covered.update(report.covered_dimensions)
    combined.covered_dimensions = sorted(covered)
    return combined


def _can_use_legacy_finding_repair(
    *,
    result: ReviewerResult,
    block_by_id: dict[str, DocumentBlock],
    evidence_ids: set[str],
) -> bool:
    """Return whether the old Findings-only repair contract is sufficient."""

    block_ids = set(block_by_id)
    policy_only = result.model_copy(update={"findings": []}, deep=True)
    if reviewer_reference_errors(
        result=policy_only,
        block_ids=block_ids,
        evidence_ids=evidence_ids,
        block_by_id=block_by_id,
    ):
        return False
    for finding in result.findings:
        for reference in finding.paper_evidence:
            if reference.block_id is None:
                continue
            block = block_by_id.get(reference.block_id)
            if block is None:
                continue
            if reference.page is not None and reference.page != block.page:
                return False
            if reference.quote and reference.quote not in block.text:
                return False
    return bool(
        reviewer_reference_errors(
            result=result,
            block_ids=block_ids,
            evidence_ids=evidence_ids,
        )
    )


def _merge_expert_opinions(
    first: list[ExpertOpinion], second: list[ExpertOpinion]
) -> list[ExpertOpinion]:
    merged: dict[tuple[str, str], ExpertOpinion] = {}
    for opinion in (*first, *second):
        merged[(opinion.round, opinion.expert_id)] = opinion
    return list(merged.values())


def _human_decision_from_repository(item: Mapping[str, object]) -> HumanRuleDecision:
    return HumanRuleDecision.model_validate(
        {
            "rule_id": item.get("rule_id"),
            "decision": item.get("decision"),
            "reviewer": item.get("reviewer"),
            "rationale": item.get("reason"),
            "decided_at": item.get("decided_at") or item.get("timestamp"),
        }
    )


def _merge_human_decisions(
    first: list[HumanRuleDecision], second: list[HumanRuleDecision]
) -> list[HumanRuleDecision]:
    merged: dict[str, HumanRuleDecision] = {}
    for decision in (*first, *second):
        merged[decision.rule_id] = decision
    return list(merged.values())


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_database_error(error: BaseException) -> bool:
    """Return whether an exception may contain SQL and bound parameters.

    SQLAlchemy's ``StatementError`` hierarchy includes ``DBAPIError`` and
    ``IntegrityError``.  Direct sqlite errors are included for lightweight
    adapters and tests; both are formatted from their ``orig``/message rather
    than from SQLAlchemy's verbose ``str(error)``.
    """

    return isinstance(error, (StatementError, sqlite3.Error))


def _safe_error_message(error: BaseException) -> str:
    """Format an error for persistence, trace events, and user-facing output.

    SQLAlchemy statement errors include the SQL statement and bound parameter
    representation in ``str(error)``.  Only a short, allow-listed reason from
    the DBAPI exception is retained.  Non-database errors keep their existing
    useful message for compatibility.
    """

    if not _is_database_error(error):
        return _safe_external_reason(error)
    original = getattr(error, "orig", error)
    reason = _safe_database_reason(str(original))
    return f"{type(error).__name__}: {reason}"


def _safe_external_reason(error: BaseException) -> str:
    module = type(error).__module__
    if module == "openai" or module.startswith("openai."):
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            reason = {
                400: "provider rejected the request",
                401: "provider authentication failed",
                403: "provider access was denied",
                404: "provider endpoint or model was not found",
                429: "provider rate limit was reached",
            }.get(status, "provider request failed")
            reason = f"{reason} (HTTP {status})"
        else:
            reason = "provider request failed"
        return f"{type(error).__name__}: {reason}"
    message = " ".join(str(error).split())
    message = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
        r"\1<redacted>",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", message)
    message = re.sub(r"(?i)\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b", "<api-key>", message)
    message = re.sub(r"(?i)https?://[^\s\]\[(){}<>\"']+", "<provider-url>", message)
    if not message:
        message = "external provider request failed"
    return message[:400]


def _safe_database_reason(message: str) -> str:
    """Extract a bounded, identifier-safe database reason.

    The accepted messages cover common SQLite and PostgreSQL constraint
    failures plus a small set of operational messages.  Unknown DBAPI text is
    intentionally collapsed to a generic reason because it may contain values
    from the paper or another secret.
    """

    normalized = " ".join(message.split())
    if not normalized:
        return "database operation failed"

    sqlite_match = re.fullmatch(
        r"(?i)(unique|not null|foreign key|check) constraint failed"
        r"(?:\s*:\s*([A-Za-z_][A-Za-z0-9_.]*))?",
        normalized,
    )
    if sqlite_match:
        kind = sqlite_match.group(1).upper()
        identifier = sqlite_match.group(2)
        return f"{kind} constraint failed" + (f": {identifier}" if identifier else "")

    postgres_match = re.fullmatch(
        r"(?i)duplicate key value violates unique constraint"
        r"(?:\s+[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"'])?",
        normalized,
    )
    if postgres_match:
        identifier = postgres_match.group(1)
        return "duplicate key violates unique constraint" + (
            f": {identifier}" if identifier else ""
        )

    violated_match = re.fullmatch(
        r"(?i)violates (unique|not[- ]null|foreign key|check) constraint"
        r"(?:\s+[\"']([A-Za-z_][A-Za-z0-9_.-]*)[\"'])?",
        normalized,
    )
    if violated_match:
        kind = violated_match.group(1).replace("-", " ").upper()
        identifier = violated_match.group(2)
        return f"violates {kind} constraint" + (f": {identifier}" if identifier else "")

    operational_reasons = (
        "database is locked",
        "database table is locked",
        "unable to open database file",
        "connection refused",
        "connection reset",
        "connection closed",
    )
    lowered = normalized.casefold()
    for reason in operational_reasons:
        if lowered == reason or lowered.startswith(reason + ":"):
            return reason
    return "database operation failed"
