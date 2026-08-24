from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import HttpUrl
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from paper_reviewer.adapters.persistence.database import (
    ArtifactRow,
    DocumentBlockRow,
    EvidenceRow,
    HardRuleDecisionRow,
    ReviewResultRow,
    RunEventRow,
    RunRow,
)
from paper_reviewer.domain.document import BlockType, DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceLevel
from paper_reviewer.domain.review import ReviewerResult
from paper_reviewer.domain.run import RunRecord, RunStatus

# A detection report is intentionally not part of the first-version storage
# contract.  Keep a defensive guard at this boundary so a future UI field
# cannot accidentally put its path into the database or JSON artifacts.
_FORBIDDEN_DETECTION_KEYS = {
    "integrity_report",
    "integrity_report_path",
    "plagiarism_report",
    "plagiarism_report_path",
    "similarity_report",
    "similarity_report_path",
    "academic_integrity_report",
    "academic_integrity_report_path",
    "check_report_path",
    "duplicate_check_report",
}


class ArtifactRepository:
    """Persist run-scoped files and small structured JSON artifacts.

    The original ``artifacts`` table represented files using ``path`` and
    ``sha256``.  Structured evaluation results use the additive
    ``payload_json`` column and leave ``path`` empty, so old databases and old
    artifact readers continue to work.  ``artifact_type`` is the stable
    lookup key; callers can encode a reviewer role/id in it for independently
    persisted expert results.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def register(
        self,
        run_id: str,
        artifact_type: str,
        path: str,
        sha256: str,
    ) -> int:
        """Register an existing file artifact without reading its contents."""
        _reject_detection_artifact(artifact_type, path)
        async with self.sessions() as session:
            row = ArtifactRow(
                run_id=run_id,
                artifact_type=artifact_type,
                path=path,
                sha256=sha256,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.artifact_id

    async def save_json(
        self,
        run_id: str,
        artifact_type: str,
        payload: Any,
        replace: bool = True,
    ) -> int:
        """Save a JSON-serializable artifact and return its row id.

        ``replace`` is useful for deterministic checkpoints (diagnostic score,
        audit, or final report).  Independent reviewer/panel results pass a
        role-specific ``artifact_type`` and therefore never overwrite one
        another.
        """
        serialized = _serialize_artifact_payload(payload)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        async with self.sessions() as session:
            existing = None
            if replace:
                existing = (
                    await session.execute(
                        select(ArtifactRow)
                        .where(
                            ArtifactRow.run_id == run_id,
                            ArtifactRow.artifact_type == artifact_type,
                            ArtifactRow.payload_json.is_not(None),
                        )
                        .order_by(ArtifactRow.artifact_id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if existing is None:
                row = ArtifactRow(
                    run_id=run_id,
                    artifact_type=artifact_type,
                    path="",
                    sha256=digest,
                    payload_json=serialized,
                )
                session.add(row)
            else:
                existing.path = ""
                existing.sha256 = digest
                existing.payload_json = serialized
                row = existing
            await session.commit()
            await session.refresh(row)
            return row.artifact_id

    async def get_json(
        self, run_id: str, artifact_type: str
    ) -> dict[str, Any] | list[Any] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.run_id == run_id,
                        ArtifactRow.artifact_type == artifact_type,
                        ArtifactRow.payload_json.is_not(None),
                    )
                    .order_by(ArtifactRow.artifact_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None or row.payload_json is None:
                return None
            return cast(dict[str, Any] | list[Any], json.loads(row.payload_json))

    async def list_json(
        self, run_id: str, *, artifact_type_prefix: str = ""
    ) -> list[dict[str, Any] | list[Any]]:
        async with self.sessions() as session:
            statement = (
                select(ArtifactRow)
                .where(
                    ArtifactRow.run_id == run_id,
                    ArtifactRow.payload_json.is_not(None),
                )
                .order_by(ArtifactRow.artifact_id)
            )
            if artifact_type_prefix:
                statement = statement.where(
                    ArtifactRow.artifact_type.like(f"{artifact_type_prefix}%")
                )
            rows = (await session.execute(statement)).scalars().all()
            return [
                cast(dict[str, Any] | list[Any], json.loads(row.payload_json))
                for row in rows
                if row.payload_json is not None
            ]

    async def list(self, run_id: str) -> list[dict[str, Any]]:
        """Return metadata for all artifacts, including legacy file rows."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ArtifactRow)
                    .where(ArtifactRow.run_id == run_id)
                    .order_by(ArtifactRow.artifact_id)
                )
            ).scalars().all()
            return [
                {
                    "artifact_id": row.artifact_id,
                    "run_id": row.run_id,
                    "artifact_type": row.artifact_type,
                    "path": row.path,
                    "sha256": row.sha256,
                    "has_payload": row.payload_json is not None,
                }
                for row in rows
            ]


class RunRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(self, run: RunRecord) -> None:
        async with self.sessions() as session:
            session.add(
                RunRow(
                    run_id=run.run_id,
                    status=run.status.value,
                    input_path=run.input_path,
                    input_hash=run.input_hash,
                    config_hash=run.config_hash,
                    rubric_id=run.rubric_id,
                    provider=run.provider,
                    model=run.model,
                    completed_stages_json=json.dumps(run.completed_stages),
                    error=run.error,
                )
            )
            await session.commit()

    async def get(self, run_id: str) -> RunRecord | None:
        async with self.sessions() as session:
            row = await session.get(RunRow, run_id)
            if row is None:
                return None
            return RunRecord(
                run_id=row.run_id,
                status=RunStatus(row.status),
                input_path=row.input_path,
                input_hash=row.input_hash,
                config_hash=row.config_hash,
                rubric_id=row.rubric_id,
                provider=row.provider,
                model=row.model,
                completed_stages=json.loads(row.completed_stages_json),
                error=row.error,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def list(self, *, status: RunStatus | None = None) -> list[RunRecord]:
        async with self.sessions() as session:
            statement = select(RunRow).order_by(RunRow.updated_at.desc())
            if status is not None:
                statement = statement.where(RunRow.status == status.value)
            rows = (await session.execute(statement)).scalars().all()
            return [
                RunRecord(
                    run_id=row.run_id,
                    status=RunStatus(row.status),
                    input_path=row.input_path,
                    input_hash=row.input_hash,
                    config_hash=row.config_hash,
                    rubric_id=row.rubric_id,
                    provider=row.provider,
                    model=row.model,
                    completed_stages=json.loads(row.completed_stages_json),
                    error=row.error,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ]

    async def save(self, run: RunRecord, *, event_type: str, payload: dict[str, object]) -> None:
        async with self.sessions() as session:
            row = await session.get(RunRow, run.run_id)
            if row is None:
                raise KeyError(f"unknown run: {run.run_id}")
            row.status = run.status.value
            row.completed_stages_json = json.dumps(run.completed_stages)
            row.error = run.error
            session.add(
                RunEventRow(
                    run_id=run.run_id,
                    event_type=event_type,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
            )
            await session.commit()
            await session.refresh(row)
            run.updated_at = row.updated_at


class HardRuleDecisionRepository:
    """Append-only persistence for the human hard-rule review gate."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_decision(
        self,
        run_id: str,
        decision: Any,
        *,
        rule_id: str | None = None,
        confirmed: bool | None = None,
        dismissed: bool | None = None,
        reviewer: str | None = None,
        reason: str | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one human decision and return its normalized representation.

        A Pydantic ``HumanRuleDecision`` or a plain mapping is accepted so
        this adapter remains independent of the evolving domain package.
        ``confirmed`` and ``dismissed`` are stored explicitly in addition to
        the compact decision string for straightforward SQL/report queries.
        """
        values = _mapping_from_value(decision)
        resolved_rule_id = rule_id or _first_text(values, "rule_id", "id")
        resolved_reviewer = reviewer or _first_text(
            values, "reviewer", "reviewer_id", "reviewer_name"
        )
        resolved_reason = (
            reason
            if reason is not None
            else _first_text(values, "reason", "rationale", "review_reason")
        )
        if not resolved_rule_id:
            raise ValueError("human rule decision requires rule_id")
        if not resolved_reviewer:
            raise ValueError("human rule decision requires reviewer")
        if resolved_reason is None or not str(resolved_reason).strip():
            raise ValueError("human rule decision requires reason")

        value = _first_text(values, "decision", "status")
        resolved_confirmed = (
            confirmed if confirmed is not None else _optional_bool(values.get("confirmed"))
        )
        resolved_dismissed = (
            dismissed if dismissed is not None else _optional_bool(values.get("dismissed"))
        )
        if value is not None:
            normalized_value = value.casefold()
            if normalized_value in {"confirmed", "confirm", "成立", "true"}:
                resolved_confirmed, resolved_dismissed = True, False
                value = "confirmed"
            elif normalized_value in {"dismissed", "dismiss", "rejected", "驳回", "false"}:
                resolved_confirmed, resolved_dismissed = False, True
                value = "dismissed"
        if resolved_confirmed is None and resolved_dismissed is None:
            raise ValueError("human rule decision must be confirmed or dismissed")
        resolved_confirmed = bool(resolved_confirmed)
        resolved_dismissed = bool(resolved_dismissed)
        if resolved_confirmed == resolved_dismissed:
            raise ValueError("human rule decision must choose exactly one outcome")
        resolved_decision = "confirmed" if resolved_confirmed else "dismissed"
        resolved_timestamp = timestamp or _timestamp_from_values(values) or datetime.now(UTC)
        if resolved_timestamp.tzinfo is None:
            resolved_timestamp = resolved_timestamp.replace(tzinfo=UTC)

        async with self.sessions() as session:
            row = HardRuleDecisionRow(
                run_id=run_id,
                rule_id=str(resolved_rule_id),
                decision=resolved_decision,
                confirmed=resolved_confirmed,
                dismissed=resolved_dismissed,
                reviewer=str(resolved_reviewer),
                reason=str(resolved_reason),
                decided_at=resolved_timestamp,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _hard_rule_decision_dict(row)

    async def list_decisions(
        self, run_id: str, *, rule_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            statement = (
                select(HardRuleDecisionRow)
                .where(HardRuleDecisionRow.run_id == run_id)
                .order_by(HardRuleDecisionRow.decided_at, HardRuleDecisionRow.decision_id)
            )
            if rule_id is not None:
                statement = statement.where(HardRuleDecisionRow.rule_id == rule_id)
            rows = (await session.execute(statement)).scalars().all()
            return [_hard_rule_decision_dict(row) for row in rows]

    async def latest_decisions(self, run_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for decision in await self.list_decisions(run_id):
            latest[str(decision["rule_id"])] = decision
        return latest

    # The names below match the application service language and make the
    # repository convenient to inject without an adapter-specific wrapper.
    async def save_human_rule_decision(
        self, run_id: str, decision: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.save_decision(run_id, decision, **kwargs)

    async def list_human_rule_decisions(
        self, run_id: str, *, rule_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.list_decisions(run_id, rule_id=rule_id)


class DocumentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def add_blocks(self, run_id: str, blocks: list[DocumentBlock]) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(DocumentBlockRow).where(DocumentBlockRow.run_id == run_id)
            )
            await session.execute(
                text("DELETE FROM document_blocks_fts WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            for block in blocks:
                session.add(
                    DocumentBlockRow(
                        block_id=block.block_id,
                        run_id=run_id,
                        document_id=block.document_id,
                        page=block.page,
                        block_type=block.block_type.value,
                        section_path_json=json.dumps(block.section_path, ensure_ascii=False),
                        text=block.text,
                        bbox_json=json.dumps(block.bbox) if block.bbox else None,
                        content_hash=block.content_hash,
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO document_blocks_fts(run_id, block_id, text) "
                        "VALUES (:run_id, :block_id, :text)"
                    ),
                    {"run_id": run_id, "block_id": block.block_id, "text": block.text},
                )
            await session.commit()

    async def list_blocks(self, run_id: str) -> list[DocumentBlock]:
        async with self.sessions() as session:
            result = await session.execute(
                select(DocumentBlockRow)
                .where(DocumentBlockRow.run_id == run_id)
                .order_by(DocumentBlockRow.page, DocumentBlockRow.block_id)
            )
            rows = result.scalars().all()
            return [
                DocumentBlock(
                    block_id=row.block_id,
                    document_id=row.document_id,
                    page=row.page,
                    block_type=BlockType(row.block_type),
                    section_path=json.loads(row.section_path_json),
                    text=row.text,
                    bbox=tuple(json.loads(row.bbox_json)) if row.bbox_json else None,
                    content_hash=row.content_hash,
                )
                for row in rows
            ]

    async def search(self, run_id: str, query: str, *, limit: int = 8) -> list[DocumentBlock]:
        terms = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]", query.lower())
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        async with self.sessions() as session:
            matches = await session.execute(
                text(
                    "SELECT block_id FROM document_blocks_fts "
                    "WHERE run_id = :run_id AND document_blocks_fts MATCH :query "
                    "ORDER BY bm25(document_blocks_fts) LIMIT :limit"
                ),
                {"run_id": run_id, "query": fts_query, "limit": min(max(limit, 1), 20)},
            )
            block_ids = [row[0] for row in matches]
            if not block_ids:
                return []
            result = await session.execute(
                select(DocumentBlockRow).where(
                    DocumentBlockRow.run_id == run_id,
                    DocumentBlockRow.block_id.in_(block_ids),
                )
            )
            by_id = {row.block_id: row for row in result.scalars()}
            return [
                DocumentBlock(
                    block_id=row.block_id,
                    document_id=row.document_id,
                    page=row.page,
                    block_type=BlockType(row.block_type),
                    section_path=json.loads(row.section_path_json),
                    text=row.text,
                    bbox=tuple(json.loads(row.bbox_json)) if row.bbox_json else None,
                    content_hash=row.content_hash,
                )
                for block_id in block_ids
                if (row := by_id.get(block_id)) is not None
            ]


class ReviewRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.artifacts = ArtifactRepository(sessions)
        self.hard_rules = HardRuleDecisionRepository(sessions)

    async def save_result(self, run_id: str, result: ReviewerResult) -> None:
        async with self.sessions() as session:
            existing = (
                await session.execute(
                    select(ReviewResultRow).where(
                        ReviewResultRow.run_id == run_id,
                        ReviewResultRow.reviewer_id == result.reviewer_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ReviewResultRow(
                        run_id=run_id,
                        reviewer_id=result.reviewer_id,
                        payload_json=result.model_dump_json(),
                    )
                )
            else:
                existing.payload_json = result.model_dump_json()
            await session.commit()

    async def list_results(self, run_id: str) -> list[ReviewerResult]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ReviewResultRow).where(ReviewResultRow.run_id == run_id)
                )
            ).scalars()
            return [ReviewerResult.model_validate_json(row.payload_json) for row in rows]

    async def save_expert_opinion(
        self,
        run_id: str,
        opinion: Any,
        *,
        role: str | None = None,
        expert_id: str | None = None,
    ) -> int:
        """Persist one independent panel/reviewer opinion as a JSON artifact.

        Each role/id gets a distinct artifact key, so a completed expert can
        be saved immediately and a resumed run only needs to invoke missing
        experts.  The adapter intentionally returns a plain JSON-compatible
        object from its read API; callers validate it against their current
        ``ExpertOpinion`` schema.
        """
        opinion_payload = _mapping_from_value(opinion)
        resolved_role = role or _first_text(
            opinion_payload, "role", "reviewer_role", "expert_role"
        ) or "expert"
        resolved_id = expert_id or _first_text(
            opinion_payload, "expert_id", "reviewer_id", "opinion_id", "id"
        ) or resolved_role
        artifact_type = _expert_artifact_type(resolved_role, resolved_id)
        envelope = {
            "kind": "expert_opinion",
            "role": resolved_role,
            "expert_id": resolved_id,
            "opinion": opinion_payload,
        }
        return await self.artifacts.save_json(
            run_id, artifact_type=artifact_type, payload=envelope, replace=True
        )

    async def list_expert_opinions(
        self, run_id: str, *, role: str | None = None
    ) -> list[dict[str, Any]]:
        prefix = "expert_opinion:"
        if role:
            prefix = f"{prefix}{_safe_component(role)}:"
        payloads = await self.artifacts.list_json(run_id, artifact_type_prefix=prefix)
        return [payload for payload in payloads if isinstance(payload, dict)]

    async def list_expert_opinion_payloads(
        self, run_id: str, *, role: str | None = None
    ) -> list[dict[str, Any]]:
        """Return only the original opinion bodies for model validation."""
        return [
            opinion
            for envelope in await self.list_expert_opinions(run_id, role=role)
            if isinstance(opinion := envelope.get("opinion"), dict)
        ]

    async def save_panel_expert_result(
        self,
        run_id: str,
        opinion: Any,
        *,
        role: str | None = None,
        expert_id: str | None = None,
    ) -> int:
        return await self.save_expert_opinion(
            run_id, opinion, role=role, expert_id=expert_id
        )

    async def list_panel_expert_results(
        self, run_id: str, *, role: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.list_expert_opinions(run_id, role=role)

    async def save_diagnostic_score(self, run_id: str, score: Any) -> int:
        return await self.artifacts.save_json(
            run_id, artifact_type="diagnostic_score", payload=score, replace=True
        )

    async def get_diagnostic_score(self, run_id: str) -> dict[str, Any] | list[Any] | None:
        return await self.artifacts.get_json(run_id, artifact_type="diagnostic_score")

    async def save_panel_decision(self, run_id: str, decision: Any) -> int:
        return await self.artifacts.save_json(
            run_id, artifact_type="panel_decision", payload=decision, replace=True
        )

    async def get_panel_decision(self, run_id: str) -> dict[str, Any] | list[Any] | None:
        return await self.artifacts.get_json(run_id, artifact_type="panel_decision")

    async def save_evaluation_report(self, run_id: str, report: Any) -> int:
        return await self.artifacts.save_json(
            run_id, artifact_type="evaluation_report", payload=report, replace=True
        )

    async def get_evaluation_report(self, run_id: str) -> dict[str, Any] | list[Any] | None:
        return await self.artifacts.get_json(run_id, artifact_type="evaluation_report")

    async def save_human_rule_decision(
        self, run_id: str, decision: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return await self.hard_rules.save_decision(run_id, decision, **kwargs)

    async def list_human_rule_decisions(
        self, run_id: str, *, rule_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await self.hard_rules.list_decisions(run_id, rule_id=rule_id)


class EvidenceRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def replace(self, run_id: str, items: list[EvidenceItem]) -> None:
        async with self.sessions() as session:
            await session.execute(delete(EvidenceRow).where(EvidenceRow.run_id == run_id))
            for item in items:
                session.add(
                    EvidenceRow(
                        evidence_id=item.evidence_id,
                        run_id=run_id,
                        kind=item.kind.value,
                        title=item.title,
                        content=item.content,
                        source_name=item.source_name,
                        level=item.level.value,
                        doi=item.doi,
                        url=str(item.url) if item.url else None,
                        metadata_json=json.dumps(item.metadata, ensure_ascii=False),
                    )
                )
            await session.commit()

    async def list(self, run_id: str) -> list[EvidenceItem]:
        async with self.sessions() as session:
            rows = (
                await session.execute(select(EvidenceRow).where(EvidenceRow.run_id == run_id))
            ).scalars()
            return [
                EvidenceItem(
                    evidence_id=row.evidence_id,
                    run_id=row.run_id,
                    kind=EvidenceKind(row.kind),
                    title=row.title,
                    content=row.content,
                    source_name=row.source_name,
                    level=EvidenceLevel(row.level),
                    doi=row.doi,
                    url=HttpUrl(row.url) if row.url else None,
                    metadata=json.loads(row.metadata_json),
                )
                for row in rows
            ]


def _mapping_from_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif hasattr(value, "__dict__"):
        payload = dict(vars(value))
    else:
        raise TypeError("persistence payload must be a mapping or Pydantic model")
    _reject_detection_payload(payload)
    return payload


def _serialize_artifact_payload(value: Any) -> str:
    payload = _json_compatible(value)
    _reject_detection_payload(payload)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_compatible(value: Any) -> Any:
    """Convert nested Pydantic/list payloads without losing the security scan."""

    if hasattr(value, "model_dump"):
        return _json_compatible(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(child) for child in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _reject_detection_payload(value: Any, *, parent_key: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
            compact = normalized.replace("_", "")
            if (
                normalized in _FORBIDDEN_DETECTION_KEYS
                or "integrityreport" in compact
                or "plagiarismreport" in compact
                or "similarityreport" in compact
            ):
                raise ValueError(
                    "academic-integrity detection report data is not persisted in the first version"
                )
            _reject_detection_payload(child, parent_key=normalized)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_detection_payload(child, parent_key=parent_key)


def _reject_detection_artifact(artifact_type: str, path: str) -> None:
    normalized = artifact_type.casefold().replace("-", "_").replace(" ", "_")
    if normalized in _FORBIDDEN_DETECTION_KEYS or "integrity_report" in normalized:
        raise ValueError(
            "academic-integrity detection report data is not persisted in the first version"
        )
    if path and normalized in {"plagiarism", "similarity", "duplicate_check"}:
        raise ValueError(
            "academic-integrity detection report data is not persisted in the first version"
        )


def _safe_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._-")
    return component[:48] or "unnamed"


def _expert_artifact_type(role: str, expert_id: str) -> str:
    # Keep the original identity in the JSON envelope while bounding the DB
    # key to the schema's 80-character column.
    return f"expert_opinion:{_safe_component(role)}:{_safe_component(expert_id)}"[:80]


def _first_text(values: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.casefold().strip()
        if normalized in {"true", "1", "yes", "confirmed", "成立"}:
            return True
        if normalized in {"false", "0", "no", "dismissed", "驳回"}:
            return False
    return bool(value)


def _timestamp_from_values(values: Mapping[str, Any]) -> datetime | None:
    value = values.get("timestamp", values.get("decided_at", values.get("created_at")))
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid human rule decision timestamp") from error
    raise ValueError("invalid human rule decision timestamp")


def _hard_rule_decision_dict(row: HardRuleDecisionRow) -> dict[str, Any]:
    timestamp = row.decided_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return {
        "decision_id": row.decision_id,
        "run_id": row.run_id,
        "rule_id": row.rule_id,
        "decision": row.decision,
        "confirmed": bool(row.confirmed),
        "dismissed": bool(row.dismissed),
        "reviewer": row.reviewer,
        "reason": row.reason,
        "timestamp": timestamp,
        "decided_at": timestamp,
    }
