from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from paper_reviewer.application.batch_output import build_report_filename
from paper_reviewer.application.batch_store import (
    BatchExecutionInProgressError,
    BatchStore,
    scan_batch_sources,
)
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.config import Settings, load_review_profile, load_rubric
from paper_reviewer.domain.batch import (
    BatchItem,
    BatchItemStatus,
    BatchRecord,
    BatchReviewRequest,
    BatchStatus,
)
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
)
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COURSE_RUBRIC = PROJECT_ROOT / "configs" / "rubrics" / "course_paper_v1.yaml"
COURSE_PROFILE = PROJECT_ROOT / "configs" / "review_profiles" / "course_paper_reviewers_v1.yaml"


class MemoryBatchStore:
    """Small persistence double that keeps the real service transitions observable."""

    def __init__(self) -> None:
        self.record: BatchRecord | None = None
        self.saves = 0

    def create(self, record: BatchRecord) -> None:
        assert self.record is None
        self.record = record.model_copy(deep=True)

    def save(self, record: BatchRecord) -> None:
        self.saves += 1
        self.record = record.model_copy(deep=True)

    def load(self, batch_id: str) -> BatchRecord:
        assert self.record is not None
        assert self.record.batch_id == batch_id
        return self.record.model_copy(deep=True)

    def list_records(self) -> list[BatchRecord]:
        return [self.record.model_copy(deep=True)] if self.record is not None else []


class FakeProviders:
    def __init__(self, snapshot: ProviderSnapshot, *, api_key: str | None = "test-key") -> None:
        self.snapshot_value = snapshot
        self.api_key = api_key

    def snapshot(self, provider: str, model: str) -> ProviderSnapshot:
        assert provider == self.snapshot_value.provider_ref
        assert model == self.snapshot_value.model
        return self.snapshot_value

    def get_snapshot_api_key(self, snapshot: ProviderSnapshot) -> str | None:
        assert snapshot == self.snapshot_value
        return self.api_key


def _provider(model: str = "gpt-test") -> ProviderSnapshot:
    base_url = "https://api.openai.com/v1"
    return ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(base_url, ModelApiProtocol.CHAT_COMPLETIONS),
        model=model,
    )


def _request(tmp_path: Path, *, output_dir: Path | None = None) -> BatchReviewRequest:
    source_dir = tmp_path / "papers"
    source_dir.mkdir(exist_ok=True)
    return BatchReviewRequest(
        source_dir=source_dir,
        output_dir=output_dir or tmp_path / "reports",
        provider="openai",
        model="gpt-test",
        rubric=COURSE_RUBRIC,
        profile=COURSE_PROFILE,
        cloud_processing_authorized=True,
        pii_output_authorized=True,
        external_search=False,
    )


def _record(
    tmp_path: Path,
    statuses: list[BatchItemStatus],
    *,
    batch_status: BatchStatus = BatchStatus.PAUSED,
    current_item_id: str | None = None,
) -> BatchRecord:
    request = _request(tmp_path)
    for index in range(len(statuses)):
        (request.source_dir / f"paper-{index}.pdf").write_bytes(b"%PDF-1.4\n")
    items = scan_batch_sources(request)
    for item, status in zip(items, statuses, strict=True):
        item.status = status
        if status is BatchItemStatus.FAILED:
            item.error = "previous safe error"
    return BatchRecord(
        batch_id="batch-test",
        status=batch_status,
        request=request,
        rubric_snapshot=load_rubric(COURSE_RUBRIC),
        profile_snapshot=load_review_profile(COURSE_PROFILE),
        provider_snapshot=_provider(),
        items=items,
        current_item_id=current_item_id,
    )


def _service(
    tmp_path: Path,
    store: MemoryBatchStore | BatchStore,
    *,
    api_key: str | None = "test-key",
) -> ReviewApplicationService:
    service = ReviewApplicationService.__new__(ReviewApplicationService)
    service.paths = SimpleNamespace(batches_dir=tmp_path / "batches")
    service.settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    service.providers = FakeProviders(_provider(), api_key=api_key)
    service._batch_store = lambda: store  # type: ignore[method-assign]
    return service


async def _no_output(*_args: Any, **_kwargs: Any) -> None:
    return None


def _metadata(name: str = "张三") -> SubmissionMetadata:
    return SubmissionMetadata(
        student_name=name,
        student_id="20260001",
        major="公共管理",
        paper_title="课程论文",
        field_evidence={
            field: SubmissionFieldEvidence(
                source=SubmissionMetadataSource.COVER_LABEL,
                confidence=0.95,
            )
            for field in SUBMISSION_METADATA_FIELDS
        },
    )


@pytest.mark.asyncio
async def test_create_batch_freezes_rubric_profile_provider_and_persists_manifest(
    tmp_path: Path,
) -> None:
    store = MemoryBatchStore()
    service = _service(tmp_path, store)
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4\n")

    record = await service.create_batch(_request(tmp_path))

    assert record.status is BatchStatus.CREATED
    persisted = store.load(record.batch_id)
    assert persisted.rubric_snapshot == load_rubric(COURSE_RUBRIC)
    assert persisted.profile_snapshot == load_review_profile(COURSE_PROFILE)
    assert persisted.provider_snapshot == _provider()
    assert persisted.request.provider == "openai"
    assert persisted.provider_snapshot.model == "gpt-test"
    assert persisted.summary_path is not None
    assert persisted.summary_path.name == "课程论文评测汇总.csv"


@pytest.mark.asyncio
async def test_create_and_run_batch_expose_initial_csv_failure_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    service = _service(tmp_path, store)
    (tmp_path / "papers").mkdir()
    (tmp_path / "papers" / "paper.pdf").write_bytes(b"%PDF-1.4\n")

    def fail_csv(_record: BatchRecord) -> None:
        raise PermissionError("output locked")

    monkeypatch.setattr(
        "paper_reviewer.application.service._write_batch_csv",
        fail_csv,
    )

    created = await service.create_batch(_request(tmp_path))

    assert created.status is BatchStatus.PAUSED
    assert created.error

    monkeypatch.setattr(
        service,
        "_run_batch_item",
        lambda *_args, **_kwargs: pytest.fail("CSV preflight must run before the model"),
    )
    resumed = await service.run_batch(created.batch_id)

    assert resumed.status is BatchStatus.PAUSED
    assert resumed.items[0].status is BatchItemStatus.QUEUED


@pytest.mark.asyncio
async def test_run_batch_is_sequential_and_single_item_failure_does_not_stop_later_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED] * 3)
    store.create(record)
    service = _service(tmp_path, store)
    order: list[str] = []

    async def run_item(
        _record_arg: BatchRecord,
        item: BatchItem,
        *,
        store: MemoryBatchStore,
        event_sink: Any,
    ) -> None:
        del store, event_sink
        order.append(item.source.filename)
        if item.source.filename == "paper-1.pdf":
            raise RuntimeError("paper-specific parse failure")
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", run_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    result = await service.run_batch(record.batch_id)

    assert order == ["paper-0.pdf", "paper-1.pdf", "paper-2.pdf"]
    assert [item.status for item in result.items] == [
        BatchItemStatus.COMPLETED,
        BatchItemStatus.FAILED,
        BatchItemStatus.COMPLETED,
    ]
    assert result.status is BatchStatus.COMPLETED_WITH_ERRORS
    assert result.error is None


@pytest.mark.asyncio
async def test_run_batch_pauses_on_shared_provider_error_and_leaves_later_items_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED, BatchItemStatus.QUEUED])
    store.create(record)
    service = _service(tmp_path, store)

    class AuthenticationError(RuntimeError):
        status_code = 401

    async def run_item(*_args: Any, **_kwargs: Any) -> None:
        raise AuthenticationError("Bearer secret must never be persisted")

    monkeypatch.setattr(service, "_run_batch_item", run_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    result = await service.run_batch(record.batch_id)

    assert result.status is BatchStatus.PAUSED
    assert result.current_item_id == result.items[0].item_id
    assert result.items[0].status is BatchItemStatus.RUNNING
    assert result.items[0].error == "Provider 认证失败；请检查 API Key 和账号权限。"
    assert result.items[1].status is BatchItemStatus.QUEUED
    assert "Bearer" not in (result.error or "")


@pytest.mark.asyncio
async def test_pause_batch_marks_current_item_cancelled_and_preserves_checkpoint(
    tmp_path: Path,
) -> None:
    current_id = "not-used-until-overwritten"
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.RUNNING, BatchItemStatus.QUEUED],
        batch_status=BatchStatus.RUNNING,
    )
    current_id = record.items[0].item_id
    record.current_item_id = current_id
    store.create(record)
    service = _service(tmp_path, store)

    result = await service.pause_batch(record.batch_id)

    assert result.status is BatchStatus.PAUSED
    assert result.current_item_id == current_id
    assert result.items[0].status is BatchItemStatus.CANCELLED
    assert store.load(record.batch_id).items[0].status is BatchItemStatus.CANCELLED


@pytest.mark.asyncio
async def test_resume_batch_skips_completed_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.COMPLETED, BatchItemStatus.QUEUED])
    store.create(record)
    service = _service(tmp_path, store)
    called: list[str] = []

    async def run_item(_record_arg: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        called.append(item.source.filename)
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", run_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    result = await service.resume_batch(record.batch_id)

    assert called == ["paper-1.pdf"]
    assert result.items[0].status is BatchItemStatus.COMPLETED
    assert result.items[1].status is BatchItemStatus.COMPLETED


@pytest.mark.asyncio
async def test_resume_revalidates_persisted_cloud_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED])
    record.request.cloud_processing_authorized = False
    store.create(record)
    service = _service(tmp_path, store)
    monkeypatch.setattr(
        service,
        "_run_batch_item",
        lambda *_args, **_kwargs: pytest.fail("unauthorized batch must not start a run"),
    )

    result = await service.resume_batch(record.batch_id)

    assert result.status is BatchStatus.PAUSED
    assert result.items[0].status is BatchItemStatus.QUEUED


@pytest.mark.asyncio
async def test_retry_failed_items_does_not_retry_or_run_queued_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.COMPLETED, BatchItemStatus.FAILED, BatchItemStatus.QUEUED],
        batch_status=BatchStatus.COMPLETED_WITH_ERRORS,
    )
    store.create(record)
    service = _service(tmp_path, store)
    called: list[str] = []

    async def run_item(_record_arg: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        called.append(item.source.filename)
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", run_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    result = await service.retry_failed_items(record.batch_id)

    assert called == ["paper-1.pdf"]
    assert result.items[1].status is BatchItemStatus.COMPLETED
    assert result.items[2].status is BatchItemStatus.QUEUED
    assert result.retry_item_ids is None
    assert result.status is BatchStatus.PAUSED


@pytest.mark.asyncio
async def test_retry_scope_survives_cancellation_and_resume_runs_only_selected_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.COMPLETED, BatchItemStatus.FAILED, BatchItemStatus.QUEUED],
        batch_status=BatchStatus.COMPLETED_WITH_ERRORS,
    )
    store.create(record)
    service = _service(tmp_path, store)

    async def cancel_item(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_run_batch_item", cancel_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    with pytest.raises(asyncio.CancelledError):
        await service.retry_failed_items(record.batch_id)

    persisted = store.load(record.batch_id)
    selected_id = persisted.items[1].item_id
    assert persisted.retry_item_ids == [selected_id]
    assert persisted.items[1].status is BatchItemStatus.CANCELLED
    assert persisted.items[2].status is BatchItemStatus.QUEUED

    called: list[str] = []

    async def complete_item(_record: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        called.append(item.source.filename)
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", complete_item)
    resumed = await service.resume_batch(record.batch_id)

    assert called == ["paper-1.pdf"]
    assert resumed.retry_item_ids is None
    assert resumed.items[2].status is BatchItemStatus.QUEUED
    assert resumed.status is BatchStatus.PAUSED


@pytest.mark.asyncio
async def test_retry_scope_survives_shared_error_and_resume_stays_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.COMPLETED, BatchItemStatus.FAILED, BatchItemStatus.QUEUED],
        batch_status=BatchStatus.COMPLETED_WITH_ERRORS,
    )
    store.create(record)
    service = _service(tmp_path, store)

    class AuthenticationError(RuntimeError):
        status_code = 401

    async def shared_failure(*_args: Any, **_kwargs: Any) -> None:
        raise AuthenticationError("Bearer secret")

    monkeypatch.setattr(service, "_run_batch_item", shared_failure)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    paused = await service.retry_failed_items(record.batch_id)

    selected_id = paused.items[1].item_id
    assert paused.retry_item_ids == [selected_id]
    assert paused.status is BatchStatus.PAUSED
    called: list[str] = []

    async def complete_item(_record: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        called.append(item.source.filename)
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", complete_item)
    resumed = await service.resume_batch(record.batch_id)

    assert called == ["paper-1.pdf"]
    assert resumed.retry_item_ids is None
    assert resumed.items[2].status is BatchItemStatus.QUEUED
    assert resumed.status is BatchStatus.PAUSED


@pytest.mark.asyncio
async def test_persisted_retry_scope_survives_process_restart_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.COMPLETED, BatchItemStatus.RUNNING, BatchItemStatus.QUEUED],
        batch_status=BatchStatus.RUNNING,
    )
    record.retry_item_ids = [record.items[1].item_id]
    record.current_item_id = record.items[1].item_id
    store.create(record)
    service = _service(tmp_path, store)
    called: list[str] = []

    async def complete_item(_record: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        called.append(item.source.filename)
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", complete_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    resumed = await service.resume_batch(record.batch_id)

    assert called == ["paper-1.pdf"]
    assert resumed.retry_item_ids is None
    assert resumed.items[2].status is BatchItemStatus.QUEUED
    assert resumed.status is BatchStatus.PAUSED


@pytest.mark.asyncio
async def test_final_status_is_saved_only_after_csv_and_resume_retries_csv_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED])
    store.create(record)
    service = _service(tmp_path, store)
    model_calls = 0

    async def complete_item(_record: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        nonlocal model_calls
        model_calls += 1
        item.status = BatchItemStatus.COMPLETED

    csv_calls = 0
    csv_persisted_statuses: list[BatchStatus] = []

    def fail_final_csv(_record: BatchRecord) -> None:
        nonlocal csv_calls
        csv_calls += 1
        csv_persisted_statuses.append(store.load(record.batch_id).status)
        if csv_calls == 3:
            raise PermissionError("output is locked")

    monkeypatch.setattr(service, "_run_batch_item", complete_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", fail_final_csv)

    paused = await service.run_batch(record.batch_id)

    assert paused.status is BatchStatus.PAUSED
    assert store.load(record.batch_id).status is BatchStatus.PAUSED
    assert csv_persisted_statuses == [
        BatchStatus.PAUSED,
        BatchStatus.RUNNING,
        BatchStatus.RUNNING,
    ]
    assert model_calls == 1

    repaired_csv_statuses: list[BatchStatus] = []

    def repair_csv(_record: BatchRecord) -> None:
        repaired_csv_statuses.append(store.load(record.batch_id).status)

    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", repair_csv)
    completed = await service.resume_batch(record.batch_id)

    assert repaired_csv_statuses == [BatchStatus.PAUSED, BatchStatus.RUNNING]
    assert completed.status is BatchStatus.COMPLETED
    assert store.load(record.batch_id).status is BatchStatus.COMPLETED
    assert model_calls == 1


@pytest.mark.asyncio
async def test_duplicate_batch_execution_is_rejected_while_first_call_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED])
    store.create(record)
    service = _service(tmp_path, store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_item(_record: BatchRecord, item: BatchItem, **_kwargs: Any) -> None:
        started.set()
        await release.wait()
        item.status = BatchItemStatus.COMPLETED

    monkeypatch.setattr(service, "_run_batch_item", blocked_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )
    monkeypatch.setattr("paper_reviewer.application.service._write_batch_csv", lambda _record: None)

    first = asyncio.create_task(service.run_batch(record.batch_id))
    await started.wait()
    with pytest.raises(BatchExecutionInProgressError, match="正在执行"):
        await service.resume_batch(record.batch_id)
    release.set()

    assert (await first).status is BatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_pause_cannot_overwrite_a_batch_owned_by_an_active_writer(tmp_path: Path) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.RUNNING], batch_status=BatchStatus.RUNNING)
    record.current_item_id = record.items[0].item_id
    store.create(record)
    service = _service(tmp_path, store)
    lock_store = BatchStore(service.paths.batches_dir)

    with lock_store.execution_lock(record.batch_id):
        with pytest.raises(BatchExecutionInProgressError, match="正在执行"):
            await service.pause_batch(record.batch_id)

    persisted = store.load(record.batch_id)
    assert persisted.status is BatchStatus.RUNNING
    assert persisted.items[0].status is BatchItemStatus.RUNNING


@pytest.mark.asyncio
async def test_run_batch_cancelled_error_preserves_running_item_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.QUEUED, BatchItemStatus.QUEUED])
    store.create(record)
    service = _service(tmp_path, store)

    async def run_item(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_run_batch_item", run_item)
    monkeypatch.setattr(
        "paper_reviewer.application.service.validate_source_snapshot", lambda _source: None
    )

    with pytest.raises(asyncio.CancelledError):
        await service.run_batch(record.batch_id)

    persisted = store.load(record.batch_id)
    assert persisted.status is BatchStatus.PAUSED
    assert persisted.current_item_id == persisted.items[0].item_id
    assert persisted.items[0].status is BatchItemStatus.CANCELLED
    assert persisted.items[1].status is BatchItemStatus.QUEUED


@pytest.mark.asyncio
async def test_new_batch_run_passes_frozen_source_hash_to_orchestrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    record = _record(tmp_path, [BatchItemStatus.RUNNING], batch_status=BatchStatus.RUNNING)
    item = record.items[0]
    item.report_path = record.request.output_dir / "existing.pdf"
    item.report_path.parent.mkdir(parents=True)
    item.report_path.write_bytes(b"pdf")
    store.create(record)
    service = _service(tmp_path, store)
    captured_hashes: list[str | None] = []

    async def start_from_snapshots(*_args: Any, **kwargs: Any) -> RunRecord:
        captured_hashes.append(kwargs.get("expected_input_hash"))
        return RunRecord(
            run_id="run-hash",
            status=RunStatus.REPORTED,
            input_path=str(item.source.path),
            input_hash=item.source.sha256,
            config_hash="b" * 64,
            rubric_id="course-paper-general-assessment@0.1.0-experimental",
            provider="openai",
            model="gpt-test",
        )

    async def load_report(_run_id: str) -> Any:
        return SimpleNamespace(
            submission_metadata=_metadata(),
            dimension_scores={},
            review=SimpleNamespace(total_score=80.0),
        )

    monkeypatch.setattr(service, "_start_review_from_snapshots", start_from_snapshots)
    monkeypatch.setattr(service, "load_report", load_report)

    await service._run_batch_item(record, item, store=store, event_sink=None)  # type: ignore[arg-type]

    assert captured_hashes == [item.source.sha256]
    assert item.status is BatchItemStatus.COMPLETED


@pytest.mark.asyncio
async def test_report_destination_is_persisted_before_publish_and_reused_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryBatchStore()
    record = _record(
        tmp_path,
        [BatchItemStatus.RUNNING],
        batch_status=BatchStatus.RUNNING,
    )
    item = record.items[0]
    item.run_id = "run-crash"
    store.create(record)
    service = _service(tmp_path, store)
    run = RunRecord(
        run_id=item.run_id,
        status=RunStatus.REPORTED,
        input_path=str(item.source.path),
        input_hash=item.source.sha256,
        config_hash="b" * 64,
        rubric_id="course-paper-general-assessment@0.1.0-experimental",
        provider="openai",
        model="gpt-test",
    )

    async def get_run(_run_id: str) -> Any:
        return SimpleNamespace(run=run)

    async def load_report(_run_id: str) -> Any:
        return SimpleNamespace(
            submission_metadata=_metadata(),
            dimension_scores={},
            review=SimpleNamespace(total_score=80.0),
        )

    async def publish_then_crash(
        _run_id: str,
        _format: Any,
        destination: Path,
        *,
        overwrite: bool,
    ) -> Any:
        assert overwrite is False
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            destination.write_bytes,
            b"%PDF-1.4\napplication-owned",
        )
        raise RuntimeError("simulated process interruption after PDF publish")

    monkeypatch.setattr(service, "get_run", get_run)
    monkeypatch.setattr(service, "load_report", load_report)
    monkeypatch.setattr(service, "export_report", publish_then_crash)

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        await service._run_batch_item(record, item, store=store, event_sink=None)  # type: ignore[arg-type]

    interrupted = store.load(record.batch_id)
    reserved_path = interrupted.items[0].report_path
    assert reserved_path is not None
    assert reserved_path.is_file()
    assert "__run-crash" not in reserved_path.name

    async def must_not_export_again(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("recovery must reuse the already published reserved PDF")

    monkeypatch.setattr(service, "export_report", must_not_export_again)
    recovered_item = interrupted.items[0]
    await service._run_batch_item(  # type: ignore[arg-type]
        interrupted,
        recovered_item,
        store=store,
        event_sink=None,
    )

    persisted = store.load(record.batch_id)
    assert persisted.items[0].status is BatchItemStatus.COMPLETED
    assert persisted.items[0].report_path == reserved_path
    assert list(reserved_path.parent.glob("*课程论文评测报告.pdf")) == [reserved_path]


@pytest.mark.asyncio
async def test_metadata_correction_refreshes_local_artifacts_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryBatchStore()
    service, record, run_dir, old_report = _metadata_update_case(tmp_path, store)
    calls: list[str] = []
    _install_metadata_update_fakes(monkeypatch, service, record, calls=calls)

    corrected = await service.update_submission_metadata(
        record.batch_id,
        record.items[0].item_id,
        _metadata("新姓名"),
    )

    assert corrected.items[0].metadata is not None
    assert corrected.items[0].metadata.student_name == "新姓名"
    assert calls == ["report_bundle", "pdf", "validate_pdf", "csv"]
    assert corrected.items[0].report_path is not None
    assert "新姓名" in corrected.items[0].report_path.name
    assert corrected.items[0].report_path.read_bytes() == b"new local pdf"
    assert not old_report.exists()
    assert "新姓名" in (run_dir / "submission-metadata.json").read_text(encoding="utf-8")
    assert (run_dir / "report.md").read_bytes() == b"new report.md"
    assert corrected.summary_path is not None
    assert corrected.summary_path.read_bytes() == b"new csv"


@pytest.mark.asyncio
async def test_metadata_correction_does_not_delete_unverified_output_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryBatchStore()
    service, record, _run_dir, old_report = _metadata_update_case(tmp_path, store)
    unrelated = old_report.with_name("教师已有文件.pdf")
    unrelated.write_bytes(b"user-owned")
    persisted = store.load(record.batch_id)
    persisted.items[0].report_path = unrelated
    store.save(persisted)
    _install_metadata_update_fakes(monkeypatch, service, persisted)

    corrected = await service.update_submission_metadata(
        persisted.batch_id,
        persisted.items[0].item_id,
        _metadata("新姓名"),
    )

    assert unrelated.read_bytes() == b"user-owned"
    assert old_report.read_bytes() == b"old local pdf"
    assert corrected.items[0].report_path not in {unrelated, old_report}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["metadata", "report_bundle", "pdf", "csv", "manifest"])
async def test_metadata_correction_rolls_back_every_published_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    store = (
        _FailAfterSaveBatchStore(tmp_path / "batches")
        if failure_stage == "manifest"
        else BatchStore(tmp_path / "batches")
    )
    service, record, run_dir, old_report = _metadata_update_case(tmp_path, store)
    old_run_files = {
        path.name: path.read_bytes()
        for path in run_dir.iterdir()
        if path.is_file()
    }
    old_pdf = old_report.read_bytes()
    assert record.summary_path is not None
    csv_path = record.summary_path
    old_csv = csv_path.read_bytes()
    old_manifest = store.load(record.batch_id)
    old_manifest_bytes = store.manifest_path(record.batch_id).read_bytes()
    _install_metadata_update_fakes(
        monkeypatch,
        service,
        record,
        failure_stage=failure_stage,
    )
    if failure_stage == "manifest":
        assert isinstance(store, _FailAfterSaveBatchStore)
        store.fail_after_next_save = True

    with pytest.raises((OSError, RuntimeError), match="injected"):
        await service.update_submission_metadata(
            record.batch_id,
            record.items[0].item_id,
            _metadata("新姓名"),
        )

    assert {
        name: (run_dir / name).read_bytes()
        for name in old_run_files
    } == old_run_files
    assert old_report.read_bytes() == old_pdf
    assert csv_path.read_bytes() == old_csv
    assert store.load(record.batch_id) == old_manifest
    assert store.manifest_path(record.batch_id).read_bytes() == old_manifest_bytes
    temporary_paths = await asyncio.to_thread(
        lambda: [
            path
            for path in tmp_path.rglob("*")
            if path.name.endswith((".new", ".bak"))
            or path.name.startswith(".metadata-update-build-")
        ]
    )
    assert not temporary_paths


class _FailAfterSaveBatchStore(BatchStore):
    def __init__(self, batches_root: Path) -> None:
        super().__init__(batches_root)
        self.fail_after_next_save = False

    def save(self, record: BatchRecord) -> None:
        super().save(record)
        if self.fail_after_next_save:
            self.fail_after_next_save = False
            raise OSError("injected manifest failure")


def _metadata_update_case(
    tmp_path: Path,
    store: MemoryBatchStore | BatchStore,
) -> tuple[ReviewApplicationService, BatchRecord, Path, Path]:
    record = _record(tmp_path, [BatchItemStatus.COMPLETED])
    item = record.items[0]
    item.run_id = "run-1"
    item.metadata = _metadata("旧姓名")
    output_dir = tmp_path / "reports"
    output_dir.mkdir()
    old_report = output_dir / build_report_filename(item.metadata, item.run_id)
    old_report.write_bytes(b"old local pdf")
    item.report_path = old_report
    record.summary_path = output_dir / "课程论文评测汇总_batch-test.csv"
    store.create(record)

    run_dir = tmp_path / "runs" / item.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "submission-metadata.json").write_text(
        item.metadata.model_dump_json(indent=2),
        encoding="utf-8",
    )
    for name in (
        "report.json",
        "report.md",
        "evidence.json",
        "run-summary.json",
        "report-presentation.json",
    ):
        (run_dir / name).write_bytes(f"old {name}".encode())
    record.summary_path.write_bytes(b"old csv")
    return _service(tmp_path, store), record, run_dir, old_report


def _install_metadata_update_fakes(
    monkeypatch: pytest.MonkeyPatch,
    service: ReviewApplicationService,
    record: BatchRecord,
    *,
    failure_stage: str | None = None,
    calls: list[str] | None = None,
) -> None:
    observed = calls if calls is not None else []
    run = RunRecord(
        run_id="run-1",
        status=RunStatus.REPORTED,
        input_path=str(record.items[0].source.path),
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="course-paper-general-assessment@0.1.0-experimental",
        provider="openai",
        model="gpt-test",
    )
    report = SimpleNamespace(
        evaluation=None,
        review=object(),
        run=run,
        rubric=record.rubric_snapshot,
        audit=object(),
        evidence=[],
        presentation_profile=None,
        dimension_scores={},
    )

    async def load_report(_run_id: str) -> Any:
        return report

    def write_bundle(*, run_dir: Path, **_kwargs: Any) -> None:
        observed.append("report_bundle")
        if failure_stage == "report_bundle":
            raise OSError("injected report_bundle failure")
        for name in ("report.json", "report.md", "evidence.json", "run-summary.json"):
            (run_dir / name).write_bytes(f"new {name}".encode())
        (run_dir / "report-presentation.json").write_bytes(b"new presentation")

    def render(markdown: str, destination: Path, *, title: str, author: str = "") -> None:
        del markdown, title, author
        observed.append("pdf")
        if failure_stage == "pdf":
            raise OSError("injected pdf failure")
        destination.write_bytes(b"new local pdf")

    def validate(destination: Path, markdown: str) -> None:
        del destination, markdown
        observed.append("validate_pdf")

    def write_csv(destination: Path, _batch: BatchRecord, _dimensions: Any) -> None:
        observed.append("csv")
        if failure_stage == "csv":
            raise OSError("injected csv failure")
        destination.write_bytes(b"new csv")

    monkeypatch.setattr(service, "load_report", load_report)
    monkeypatch.setattr("paper_reviewer.application.service.write_report_bundle", write_bundle)
    monkeypatch.setattr("paper_reviewer.application.service.render_pdf", render)
    monkeypatch.setattr("paper_reviewer.application.service.validate_pdf", validate)
    monkeypatch.setattr("paper_reviewer.application.service.write_batch_summary_csv", write_csv)
    monkeypatch.setattr(
        service,
        "_start_review_from_snapshots",
        lambda *_args, **_kwargs: pytest.fail("metadata correction must not call the model"),
    )
    if failure_stage == "metadata":
        monkeypatch.setattr(
            "paper_reviewer.application.artifacts.RunArtifactStore.write_model",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("injected metadata failure")
            ),
        )
