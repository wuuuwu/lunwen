from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from paper_reviewer.application.batch_store import (
    BatchExecutionInProgressError,
    BatchSourceChangedError,
    BatchStore,
    scan_batch_sources,
    validate_source_snapshot,
)
from paper_reviewer.config import ReviewerProfile, ReviewProfile
from paper_reviewer.domain.batch import BatchRecord, BatchReviewRequest
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
)
from paper_reviewer.domain.rubric import RubricProfile


def _request(source_dir: Path, output_dir: Path) -> BatchReviewRequest:
    return BatchReviewRequest(
        source_dir=source_dir,
        output_dir=output_dir,
        provider="openai",
        model="gpt-test",
        rubric=Path("rubric.yaml"),
        profile=Path("profile.yaml"),
        cloud_processing_authorized=True,
    )


def _record(request: BatchReviewRequest) -> BatchRecord:
    base_url = "https://api.openai.com/v1"
    return BatchRecord(
        batch_id="batch-1",
        request=request,
        rubric_snapshot=RubricProfile(
            rubric_id="course-test",
            version="1",
            title="课程论文",
        ),
        profile_snapshot=ReviewProfile(
            profile_id="course-test",
            version="1",
            reviewers=[
                ReviewerProfile(
                    reviewer_id="reviewer",
                    title="评阅人",
                    description="测试",
                )
            ],
        ),
        provider_snapshot=ProviderSnapshot(
            provider_ref="openai",
            display_name="OpenAI",
            protocol=ModelApiProtocol.CHAT_COMPLETIONS,
            base_url=base_url,
            endpoint_fingerprint=endpoint_fingerprint(
                base_url,
                ModelApiProtocol.CHAT_COMPLETIONS,
            ),
            model=request.model,
        ),
        items=scan_batch_sources(request),
    )


def test_scan_batch_sources_is_top_level_sorted_and_marks_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "子目录").mkdir()
    (source / "子目录" / "ignored.pdf").write_bytes(b"nested")
    (source / "ignored.txt").write_text("not a PDF", encoding="utf-8")
    (source / "\uff22.pdf").write_bytes(b"same")
    (source / "a.PDF").write_bytes(b"same")
    (source / "c.pdf").write_bytes(b"different")

    items = scan_batch_sources(_request(source, tmp_path / "output"))

    assert [item.source.filename for item in items] == ["a.PDF", "\uff22.pdf", "c.pdf"]
    assert [item.source.duplicate_sha256 for item in items] == [True, True, False]
    assert items[0].warnings and items[1].warnings
    assert not items[2].warnings
    assert all(item.source.path.is_absolute() for item in items)


def test_scan_batch_sources_rejects_empty_and_more_than_one_hundred(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no top-level PDF"):
        scan_batch_sources(_request(empty, tmp_path / "output"))

    for index in range(101):
        (empty / f"{index:03}.pdf").write_bytes(str(index).encode())
    with pytest.raises(ValueError, match="at most 100"):
        scan_batch_sources(_request(empty, tmp_path / "output"))


def test_validate_source_snapshot_detects_replacement(tmp_path: Path) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    paper = source / "paper.pdf"
    paper.write_bytes(b"original")
    item = scan_batch_sources(_request(source, tmp_path / "output"))[0]

    validate_source_snapshot(item.source)
    paper.write_bytes(b"replacement")

    with pytest.raises(BatchSourceChangedError, match="changed after"):
        validate_source_snapshot(item.source)


def test_batch_store_round_trip_and_refuses_to_overwrite_corruption(tmp_path: Path) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"paper")
    request = _request(source, tmp_path / "output")
    record = _record(request)
    store = BatchStore(tmp_path / "batches")

    store.create(record)
    assert store.load("batch-1") == record
    assert store.list_records() == [record]

    manifest = store.manifest_path("batch-1")
    manifest.write_text("{damaged", encoding="utf-8")
    with pytest.raises(ValueError):
        store.save(record)
    assert manifest.read_text(encoding="utf-8") == "{damaged"


def test_batch_store_listing_isolates_corrupt_manifest_and_reports_safe_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"paper")
    record = _record(_request(source, tmp_path / "output"))
    store = BatchStore(tmp_path / "batches")
    store.create(record)
    damaged = store.manifest_path("batch-damaged")
    damaged.parent.mkdir(parents=True)
    damaged.write_text('{"secret": "paper body", "schema_version": "99"}', encoding="utf-8")

    assert store.list_records() == [record]
    errors = store.list_load_errors()
    assert len(errors) == 1
    assert errors[0].batch_id == "batch-damaged"
    assert errors[0].message == "批次清单损坏、版本不受支持或无法读取。"
    assert "paper body" not in errors[0].message


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda payload: payload.update(schema_version="2"), "literal_error"),
        (lambda payload: payload.update(future_field=True), "extra_forbidden"),
        (lambda payload: payload["request"].update(future_field=True), "extra_forbidden"),
    ],
)
def test_batch_schema_v1_rejects_future_versions_and_unknown_fields(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"paper")
    record = _record(_request(source, tmp_path / "output"))
    payload = json.loads(record.model_dump_json())
    mutation(payload)

    with pytest.raises(ValueError, match=expected):
        BatchRecord.model_validate(payload)


def test_batch_execution_lock_rejects_duplicate_caller(tmp_path: Path) -> None:
    store = BatchStore(tmp_path / "batches")

    with store.execution_lock("batch-1"):
        with pytest.raises(BatchExecutionInProgressError, match="正在执行"):
            with store.execution_lock("batch-1"):
                pytest.fail("duplicate batch execution lock must not be acquired")


def test_batch_record_binds_frozen_provider_and_top_level_sources(tmp_path: Path) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"paper")
    request = _request(source, tmp_path / "output")
    record = _record(request)

    mismatched_provider = record.model_copy(deep=True)
    mismatched_provider.request.provider = "deepseek"
    with pytest.raises(ValueError, match="provider snapshot must match"):
        BatchRecord.model_validate(mismatched_provider.model_dump())

    outside_source = record.model_copy(deep=True)
    outside_source.items[0].source.path = tmp_path / "outside.pdf"
    with pytest.raises(ValueError, match="top-level files"):
        BatchRecord.model_validate(outside_source.model_dump())

    escaped_report = record.model_copy(deep=True)
    escaped_report.items[0].report_path = tmp_path / "outside-report.pdf"
    with pytest.raises(ValueError, match="report paths must remain inside"):
        BatchRecord.model_validate(escaped_report.model_dump())

    nested_report = record.model_copy(deep=True)
    nested_report.items[0].report_path = request.output_dir / "reports" / "paper.pdf"
    assert BatchRecord.model_validate(nested_report.model_dump()).items[0].report_path is not None

    historical_payload = record.model_dump()
    historical_payload.pop("workbook_path")
    historical_payload.pop("workbook_export_error")
    historical = BatchRecord.model_validate(historical_payload)
    assert historical.workbook_path is None
    assert historical.workbook_export_error is None

    escaped_workbook = record.model_copy(deep=True)
    escaped_workbook.workbook_path = tmp_path / "outside.xlsx"
    with pytest.raises(ValueError, match="workbook must be an XLSX inside output_dir"):
        BatchRecord.model_validate(escaped_workbook.model_dump())

    wrong_workbook_suffix = record.model_copy(deep=True)
    wrong_workbook_suffix.workbook_path = request.output_dir / "summary.xls"
    with pytest.raises(ValueError, match="workbook must be an XLSX inside output_dir"):
        BatchRecord.model_validate(wrong_workbook_suffix.model_dump())


def test_batch_store_failed_replace_keeps_old_manifest_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"paper")
    request = _request(source, tmp_path / "output")
    record = _record(request)
    store = BatchStore(tmp_path / "batches")
    store.create(record)
    before = store.manifest_path(record.batch_id).read_bytes()

    def fail_replace(source_path: Path, destination_path: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        store.save(record)

    assert store.manifest_path(record.batch_id).read_bytes() == before
    assert not store.manifest_path(record.batch_id).with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("batch_id", ["", ".", "..", "nested/id", "../escape"])
def test_batch_store_rejects_unsafe_batch_ids(tmp_path: Path, batch_id: str) -> None:
    with pytest.raises(ValueError, match="invalid batch id"):
        BatchStore(tmp_path).batch_dir(batch_id)
