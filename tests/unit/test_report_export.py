from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pymupdf
import pytest
from PySide6.QtCore import QUrl

from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import RunRepository
from paper_reviewer.application.app_state import AppPaths
from paper_reviewer.application.models import ReportExportFormat
from paper_reviewer.application.service import ReviewApplicationService
from paper_reviewer.config import Settings
from paper_reviewer.domain.review import (
    CriterionAssessment,
    DiagnosticScore,
    EvaluationReport,
    MetaReview,
    PanelDecision,
    PanelOutcome,
    PolicyContext,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.reporting.exporter import (
    ReportPdfExportError,
    _normalize_pdf_text,
    _ResourceBlockingDocument,
    render_pdf,
    validate_pdf,
)
from paper_reviewer.reporting.renderer import DISCLAIMER_LINES
from paper_reviewer.validation.audits import AuditReport


def _run(run_id: str = "run-1", *, status: RunStatus = RunStatus.REPORTED) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        status=status,
        input_path="paper.pdf",
        input_hash="input",
        config_hash="config",
        rubric_id="rubric",
        provider="test",
        model="test",
    )


def _service(tmp_path: Path, run: RunRecord) -> tuple[ReviewApplicationService, Path]:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / run.run_id
    run_dir.mkdir(parents=True)
    service = ReviewApplicationService.__new__(ReviewApplicationService)
    service.settings = Settings(runs_dir=runs_dir)
    service._read_run_for_export = AsyncMock(return_value=run)  # type: ignore[method-assign]
    return service, run_dir


def _write_rebuild_snapshots(run_dir: Path, kind: str) -> object:
    rubric = RubricProfile(rubric_id="snapshot", version="1", title="Snapshot")
    audit = AuditReport()
    (run_dir / "rubric.json").write_text(rubric.model_dump_json(), encoding="utf-8")
    (run_dir / "audit.json").write_text(audit.model_dump_json(), encoding="utf-8")
    if kind == "v1":
        report: object = MetaReview(
            run_id="run-1",
            overall_summary="旧任务快照",
            findings=[],
        )
        filename = "report.json"
    else:
        report = EvaluationReport(
            run_id="run-1",
            policy_context=PolicyContext(
                source="test",
                document_number="test-1",
                effective_date=date(2026, 1, 1),
                source_sha256="a" * 64,
            ),
            diagnostic_score=DiagnosticScore(
                assessments=[
                    CriterionAssessment(
                        criterion_id="criterion",
                        reviewer_id="reviewer",
                        rating=2,
                        weight=100,
                        rationale="达到基本要求",
                        confidence=0.5,
                    )
                ],
                total_score=50,
            ),
            panel_decision=PanelDecision(
                outcome=PanelOutcome.RISK_NOT_TRIGGERED,
                reason="未触发风险",
            ),
            meta_review=MetaReview(
                run_id="run-1",
                overall_summary="新版任务快照",
                findings=[],
            ),
        )
        filename = "evaluation-report.json" if kind == "v2_evaluation" else "report.json"
    assert hasattr(report, "model_dump_json")
    (run_dir / filename).write_text(report.model_dump_json(), encoding="utf-8")
    return report


@pytest.mark.asyncio
async def test_markdown_export_is_exact_atomic_copy(tmp_path: Path) -> None:
    service, run_dir = _service(tmp_path, _run())
    canonical = b"# Report\r\n\r\nexact bytes: \xe4\xb8\xad\xe6\x96\x87\r\n"
    (run_dir / "report.md").write_bytes(canonical)
    destination = tmp_path / "exports" / "report.md"
    destination.parent.mkdir()

    result = await service.export_report("run-1", ReportExportFormat.MARKDOWN, destination)

    assert destination.read_bytes() == canonical
    assert result.path == destination.resolve()
    assert result.format is ReportExportFormat.MARKDOWN
    assert result.size_bytes == len(canonical)
    assert not result.reconstructed_from_snapshot
    assert not list(destination.parent.glob(".report.md.*.tmp"))


@pytest.mark.asyncio
async def test_pending_human_review_report_can_be_exported(tmp_path: Path) -> None:
    service, run_dir = _service(
        tmp_path,
        _run(status=RunStatus.REPORTED_PENDING_HUMAN_REVIEW),
    )
    canonical = "# Report\n\n人工复核尚未完成，当前风险结论待定。\n".encode()
    (run_dir / "report.md").write_bytes(canonical)
    destination = tmp_path / "pending-report.md"

    await service.export_report("run-1", ReportExportFormat.MARKDOWN, destination)

    assert destination.read_bytes() == canonical


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["v1", "v2_evaluation", "v2_report"])
async def test_missing_markdown_is_reconstructed_from_report_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    service, run_dir = _service(tmp_path, _run())
    expected_report = _write_rebuild_snapshots(run_dir, kind)
    selected: list[object] = []

    def rebuilt_markdown(
        rubric: object, review: object, audit: object, **context: object
    ) -> str:
        del rubric, audit, context
        selected.append(review)
        return "# Rebuilt\n\n快照报告\n"

    monkeypatch.setattr(
        "paper_reviewer.application.service.render_markdown",
        rebuilt_markdown,
    )
    destination = tmp_path / "rebuilt.md"

    result = await service.export_report("run-1", ReportExportFormat.MARKDOWN, destination)

    assert destination.read_text(encoding="utf-8") == "# Rebuilt\n\n快照报告\n"
    assert result.reconstructed_from_snapshot
    assert len(selected) == 1
    assert type(selected[0]) is type(expected_report)


@pytest.mark.asyncio
async def test_export_rejects_unreported_run_and_invalid_targets(tmp_path: Path) -> None:
    service, run_dir = _service(tmp_path, _run(status=RunStatus.REVIEWING))
    (run_dir / "report.md").write_text("# Report", encoding="utf-8")

    with pytest.raises(ValueError, match="仅已生成报告"):
        await service.export_report("run-1", ReportExportFormat.MARKDOWN, tmp_path / "report.md")

    service._read_run_for_export = AsyncMock(return_value=_run())  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="扩展名"):
        await service.export_report("run-1", ReportExportFormat.MARKDOWN, tmp_path / "report.pdf")
    with pytest.raises(ValueError, match="快照目录"):
        await service.export_report("run-1", ReportExportFormat.MARKDOWN, run_dir / "copy.md")
    sibling_run = service.settings.runs_dir / "run-2"
    sibling_run.mkdir()
    sibling_report = sibling_run / "report.md"
    sibling_report.write_text("untouched", encoding="utf-8")
    with pytest.raises(ValueError, match="快照目录"):
        await service.export_report(
            "run-1",
            ReportExportFormat.MARKDOWN,
            sibling_report,
            overwrite=True,
        )
    assert sibling_report.read_text(encoding="utf-8") == "untouched"
    with pytest.raises(ValueError, match="导出目录不存在"):
        await service.export_report(
            "run-1", ReportExportFormat.MARKDOWN, tmp_path / "missing" / "report.md"
        )


@pytest.mark.asyncio
async def test_export_requires_overwrite_and_preserves_existing_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run_dir = _service(tmp_path, _run())
    (run_dir / "report.md").write_text("# Report", encoding="utf-8")
    destination = tmp_path / "report.pdf"
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        await service.export_report("run-1", ReportExportFormat.PDF, destination)

    def fail_render(markdown: str, path: Path, *, title: str) -> None:
        del markdown, title
        path.write_bytes(b"partial")
        raise RuntimeError("render failed")

    monkeypatch.setattr("paper_reviewer.application.service.render_pdf", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        await service.export_report("run-1", ReportExportFormat.PDF, destination, overwrite=True)

    assert destination.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".report.pdf.*.tmp"))  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_export_rejects_run_id_path_traversal(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _run())

    with pytest.raises(ValueError, match="任务目录"):
        await service.export_report(
            "../outside",
            ReportExportFormat.MARKDOWN,
            tmp_path / "report.md",
        )


@pytest.mark.asyncio
async def test_export_does_not_mutate_database_trace_or_canonical_report(
    tmp_path: Path,
) -> None:
    paths = AppPaths(
        root=tmp_path / "app",
        data_dir=tmp_path / "app" / "data",
        runs_dir=tmp_path / "app" / "runs",
        logs_dir=tmp_path / "app" / "logs",
        config_dir=tmp_path / "app" / "config",
    )
    service = ReviewApplicationService(paths=paths)
    run = _run(run_id="immutable-run")
    engine = create_engine(paths.database_url)
    await initialize_database(engine)
    try:
        await RunRepository(create_session_factory(engine)).create(run)
    finally:
        await engine.dispose()
    run_dir = paths.runs_dir / run.run_id
    run_dir.mkdir()
    report_path = run_dir / "report.md"
    trace_path = run_dir / "trace.jsonl"
    report_path.write_bytes(b"# Immutable report\n")
    trace_path.write_bytes(b'{"event_type":"reported"}\n')
    watched = (paths.database_path, report_path, trace_path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}

    destination = tmp_path / "exported.md"
    await service.export_report(
        run.run_id,
        ReportExportFormat.MARKDOWN,
        destination,
    )

    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in watched}
    assert after == before
    assert destination.read_bytes() == report_path.read_bytes()


@pytest.mark.asyncio
async def test_pdf_export_is_a4_searchable_and_contains_metadata(tmp_path: Path) -> None:
    service, run_dir = _service(tmp_path, _run())
    markdown = "\n".join(
        [
            "# 中文评测报告",
            "",
            "| 维度 | 评分 |",
            "| --- | --- |",
            "| 选题意义 | 90 |",
            "",
            *("这是一段用于验证自动分页和中文字体嵌入的正文。" for _ in range(220)),
            "",
            "## 重要说明",
            "",
            *(f"- {line}" for line in DISCLAIMER_LINES),
        ]
    )
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    destination = tmp_path / "report.pdf"

    result = await service.export_report("run-1", ReportExportFormat.PDF, destination)

    assert result.size_bytes == destination.stat().st_size
    assert destination.read_bytes().startswith(b"%PDF-")
    with pymupdf.open(destination) as document:  # type: ignore[no-untyped-call]
        assert document.page_count > 1
        assert document.metadata["title"] == "中文评测报告"
        assert document.metadata["creator"] == "Paper Reviewer report exporter"
        assert document.metadata["author"] == "Paper Reviewer"
        extracted = "\n".join(page.get_text("text") for page in document)
        normalized_extracted = _normalize_pdf_text(extracted)
        assert _normalize_pdf_text("中文评测报告") in normalized_extracted
        assert _normalize_pdf_text("选题意义") in normalized_extracted
        assert all(
            _normalize_pdf_text(line) in normalized_extracted
            for line in DISCLAIMER_LINES
        )
        assert all(
            abs(float(page.rect.width) - 595.28) < 3 and abs(float(page.rect.height) - 841.89) < 3
            for page in document
        )


def test_pdf_renderer_blocks_all_markdown_image_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    original = _ResourceBlockingDocument.loadResource

    def record_blocked_resource(
        document: _ResourceBlockingDocument,
        resource_type: int,
        name: object,
    ) -> object:
        requested.append(name.toString() if isinstance(name, QUrl) else str(name))
        return original(document, resource_type, name)  # type: ignore[arg-type]

    monkeypatch.setattr(_ResourceBlockingDocument, "loadResource", record_blocked_resource)
    local_image = (tmp_path / "private.png").as_uri()
    markdown = (
        "# Resource test\n\n"
        f"![local]({local_image})\n\n"
        "![remote](https://example.invalid/tracker.png)\n\n"
        '<img src="https://example.invalid/html.png" alt="html">\n'
    )
    destination = tmp_path / "resources.pdf"

    render_pdf(markdown, destination, title="Resource test")
    validate_pdf(destination, markdown)

    assert any(value.startswith("file:") for value in requested)
    assert any(value.startswith("https:") for value in requested)


def test_v2_pdf_validation_rejects_missing_mandatory_disclaimers(tmp_path: Path) -> None:
    markdown = "# 报告\n\n## 九项诊断评分\n\n正文内容。\n"
    destination = tmp_path / "missing-disclaimer.pdf"
    render_pdf(markdown, destination, title="报告")

    with pytest.raises(ReportPdfExportError, match="缺少关键免责声明"):
        validate_pdf(destination, markdown)
