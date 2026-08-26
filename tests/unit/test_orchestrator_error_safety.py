from __future__ import annotations

import json
import sqlite3
import traceback
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from paper_reviewer.application.orchestrator import (
    ReviewOrchestrator,
    SanitizedDatabaseError,
    SanitizedReviewError,
    _safe_error_message,
)
from paper_reviewer.config import ReviewerProfile, ReviewProfile, Settings
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.run import RunRecord
from paper_reviewer.ports.document_parser import ParsedDocument


class _Parser:
    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(
            info=DocumentInfo(
                document_id="document",
                source_path=str(path),
                sha256="a" * 64,
                title="Paper",
                page_count=1,
            ),
            blocks=[
                DocumentBlock.create(
                    document_id="document",
                    page=1,
                    text="paper body that must never appear in a database error",
                )
            ],
        )


class _RunRepository:
    def __init__(self) -> None:
        self.saves: list[tuple[RunRecord, dict[str, object]]] = []

    async def save(
        self, run: RunRecord, *, event_type: str, payload: dict[str, object]
    ) -> None:
        self.saves.append((run.model_copy(deep=True), {"event_type": event_type, **payload}))


class _FailingDocumentRepository:
    def __init__(self, error: IntegrityError) -> None:
        self.error = error

    async def add_blocks(self, run_id: str, blocks: list[DocumentBlock]) -> None:
        raise self.error


class _UnusedRepository:
    pass


def _run(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="rubric@1",
        provider="test",
        model="test-model",
    )


def _profile() -> tuple[object, ReviewProfile]:
    from paper_reviewer.domain.rubric import RubricProfile

    rubric = RubricProfile(
        rubric_id="rubric",
        version="1",
        title="Unscored test rubric",
        scoring_enabled=False,
    )
    profile = ReviewProfile(
        profile_id="profile",
        version="1",
        reviewers=[
            ReviewerProfile(
                reviewer_id="reviewer",
                title="Reviewer",
                description="Test reviewer",
            )
        ],
    )
    return rubric, profile


@pytest.mark.asyncio
async def test_database_failure_is_sanitized_in_run_trace_and_public_error(
    tmp_path: Path,
) -> None:
    secret = "PAPER_BODY_SECRET_123"
    statement = (
        "INSERT INTO document_blocks (block_id, text) VALUES (?, ?)"
    )
    original = IntegrityError(
        statement,
        ("duplicate-block", secret),
        sqlite3.IntegrityError("UNIQUE constraint failed: document_blocks.block_id"),
    )
    runs_dir = tmp_path / "runs"
    run_id = "run-with-db-error"
    (runs_dir / run_id).mkdir(parents=True)
    run = _run(run_id)
    run_repository = _RunRepository()
    orchestrator = ReviewOrchestrator(
        settings=Settings(runs_dir=runs_dir),
        model=_UnusedRepository(),  # type: ignore[arg-type]
        parser=_Parser(),
        run_repository=run_repository,  # type: ignore[arg-type]
        document_repository=_FailingDocumentRepository(original),  # type: ignore[arg-type]
        evidence_repository=_UnusedRepository(),  # type: ignore[arg-type]
        review_repository=_UnusedRepository(),  # type: ignore[arg-type]
    )
    rubric, profile = _profile()

    with pytest.raises(SanitizedDatabaseError) as caught:
        await orchestrator.execute(run, rubric=rubric, profile=profile)  # type: ignore[arg-type]

    public_error = caught.value
    assert public_error.original_error is original
    assert public_error.__cause__ is None
    assert str(public_error) == "IntegrityError: UNIQUE constraint failed: document_blocks.block_id"
    assert secret not in str(public_error)
    assert statement not in str(public_error)
    rendered_traceback = "".join(traceback.format_exception(public_error))
    assert secret not in rendered_traceback
    assert statement not in rendered_traceback

    assert run.error == str(public_error)
    trace = (runs_dir / run_id / "trace.jsonl").read_text(encoding="utf-8")
    assert "stage_failed" in trace
    assert str(public_error) in trace
    assert secret not in trace
    assert statement not in trace
    persisted_failure = [
        payload for _, payload in run_repository.saves if payload["event_type"] == "stage_failed"
    ]
    assert persisted_failure == [
        {
            "event_type": "stage_failed",
            "error_type": "IntegrityError",
            "message": str(public_error),
            "status": "retryable_failure",
        }
    ]
    assert secret not in json.dumps(persisted_failure)


def test_non_database_error_keeps_useful_message() -> None:
    assert _safe_error_message(ValueError("configuration is incomplete")) == (
        "configuration is incomplete"
    )


def test_unknown_database_reason_is_collapsed() -> None:
    original = IntegrityError(
        "INSERT INTO secrets VALUES (?)",
        ("paper secret",),
        sqlite3.IntegrityError("constraint detail contains paper secret"),
    )
    message = _safe_error_message(original)
    assert message == "IntegrityError: database operation failed"
    assert "paper secret" not in message
    assert "INSERT INTO" not in message


@pytest.mark.asyncio
async def test_provider_failure_never_crosses_worker_boundary_with_raw_response(
    tmp_path: Path,
) -> None:
    class ProviderFailure(RuntimeError):
        status_code = 400

    secret = "Bearer sk-provider-secret raw response body"
    runs_dir = tmp_path / "runs"
    run_id = "run-with-provider-error"
    (runs_dir / run_id).mkdir(parents=True)
    run = _run(run_id)

    class FailingParser:
        def parse(self, _path: Path) -> ParsedDocument:
            raise ProviderFailure(secret)

    orchestrator = ReviewOrchestrator(
        settings=Settings(runs_dir=runs_dir),
        model=_UnusedRepository(),  # type: ignore[arg-type]
        parser=FailingParser(),  # type: ignore[arg-type]
        run_repository=_RunRepository(),  # type: ignore[arg-type]
        document_repository=_UnusedRepository(),  # type: ignore[arg-type]
        evidence_repository=_UnusedRepository(),  # type: ignore[arg-type]
        review_repository=_UnusedRepository(),  # type: ignore[arg-type]
    )
    rubric, profile = _profile()

    with pytest.raises(SanitizedReviewError) as caught:
        await orchestrator.execute(run, rubric=rubric, profile=profile)  # type: ignore[arg-type]

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "ProviderFailure: provider rejected the request (HTTP 400)"
    assert secret not in rendered
    assert caught.value.__cause__ is None
