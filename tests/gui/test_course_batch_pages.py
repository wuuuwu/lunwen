from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from paper_reviewer.application.app_state import GuiPreferences
from paper_reviewer.application.batch_output import BATCH_SUMMARY_FILENAME
from paper_reviewer.application.models import RubricValidationResult
from paper_reviewer.domain.batch import (
    BatchEvent,
    BatchItem,
    BatchItemStatus,
    BatchRecord,
    BatchReviewRequest,
    BatchSourceSnapshot,
    BatchStatus,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.gui.batch_models import BatchItemsTableModel
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.pages import course_batch_new
from paper_reviewer.gui.pages.course_batch_detail import CourseBatchDetailPage
from paper_reviewer.gui.pages.course_batch_new import CourseBatchNewPage
from paper_reviewer.gui.theme import FluentThemeManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COURSE_RUBRIC = PROJECT_ROOT / "configs/rubrics/course_paper_v1.yaml"


class CourseBatchServiceStub:
    def __init__(self) -> None:
        payload = yaml.safe_load(COURSE_RUBRIC.read_text(encoding="utf-8"))
        self.rubric = RubricProfile.model_validate(payload)

    def validate_rubric(self, _path: Path, *, profile_path: Path) -> RubricValidationResult:
        assert profile_path.name == "course_paper_reviewers_v1.yaml"
        return RubricValidationResult(
            valid=True,
            rubric=self.rubric,
            weight_total=100,
            profile_compatible=True,
        )

    def provider_has_key(self, _provider_ref: str) -> bool:
        return True


def _icons(qapp: QApplication) -> FluentIconService:
    return FluentIconService(FluentThemeManager(qapp))


def _metadata(*, needs_review: bool = False) -> SubmissionMetadata:
    confidence = 0.5 if needs_review else 0.95
    source = (
        SubmissionMetadataSource.FILE_NAME
        if needs_review
        else SubmissionMetadataSource.COVER_LABEL
    )
    return SubmissionMetadata(
        student_name="张三",
        student_id="20260001",
        major="公共管理",
        paper_title="课程治理案例分析",
        field_evidence={
            field: SubmissionFieldEvidence(source=source, confidence=confidence)
            for field in SUBMISSION_METADATA_FIELDS
        },
        warnings=["请核对自动提取信息"] if needs_review else [],
    )


def _item(
    tmp_path: Path,
    *,
    item_id: str = "item-1",
    status: BatchItemStatus = BatchItemStatus.COMPLETED,
    with_metadata: bool = True,
) -> BatchItem:
    source = tmp_path / f"{item_id}.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    report = tmp_path / f"{item_id}-report.pdf"
    return BatchItem(
        item_id=item_id,
        source=BatchSourceSnapshot(
            path=source,
            filename=source.name,
            sha256="a" * 64,
            size_bytes=source.stat().st_size,
            modified_time_ns=source.stat().st_mtime_ns,
        ),
        status=status,
        run_id=f"run-{item_id}",
        metadata=_metadata(needs_review=True) if with_metadata else None,
        total_score=82.5 if status is BatchItemStatus.COMPLETED else None,
        grade="良好" if status is BatchItemStatus.COMPLETED else None,
        conclusion="达到课程论文基本要求"
        if status is BatchItemStatus.COMPLETED
        else None,
        report_path=report if status is BatchItemStatus.COMPLETED else None,
        error="脱敏错误摘要" if status is BatchItemStatus.FAILED else None,
    )


def _batch(
    tmp_path: Path,
    items: list[BatchItem],
    *,
    status: BatchStatus,
) -> BatchRecord:
    request = BatchReviewRequest(
        source_dir=tmp_path,
        output_dir=tmp_path / "reports",
        provider="openai",
        model="gpt-5-mini",
        rubric=COURSE_RUBRIC,
        profile=PROJECT_ROOT
        / "configs/review_profiles/course_paper_reviewers_v1.yaml",
        cloud_processing_authorized=True,
    )
    # Snapshot values are irrelevant to this presentation-only fixture.  The
    # real service always returns a fully validated BatchRecord.
    return BatchRecord.model_construct(
        batch_id="batch-1",
        status=status,
        request=request,
        items=items,
        current_item_id=None,
        error=None,
    )


def test_batch_items_model_exposes_chinese_status_metadata_and_accessibility(
    tmp_path: Path,
) -> None:
    item = _item(tmp_path)
    model = BatchItemsTableModel()
    model.set_items([item])
    model.set_item_stage(item.item_id, "Meta 汇总")

    assert model.rowCount() == 1
    assert model.columnCount() == 9
    assert model.data(model.index(0, 1)) == "张三"
    assert model.data(model.index(0, 5)) == "Meta 汇总"
    assert model.data(model.index(0, 6)) == "已完成"
    assert model.data(model.index(0, 7)) == "82.5"
    assert model.data(model.index(0, 8)) == "已生成"
    assert model.data(model.index(0, 0), model.ItemIdRole) == "item-1"
    assert "需要人工核对" in str(
        model.data(model.index(0, 1), Qt.ItemDataRole.AccessibleDescriptionRole)
    )


def test_course_batch_new_page_scans_top_level_and_emits_valid_request(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "B.pdf").write_bytes(b"%PDF-1.4\n")
    (source / "a.PDF").write_bytes(b"%PDF-1.4\n")
    nested = source / "nested"
    nested.mkdir()
    (nested / "ignored.pdf").write_bytes(b"%PDF-1.4\n")

    page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.source_picker.set_path(source)

    assert not hasattr(page, "discipline_name")
    assert page.preview_model.rowCount() == 2
    assert page.preview_model.data(page.preview_model.index(0, 0)) == "a.PDF"
    assert "10 次模型请求" in page.request_estimate.text()
    assert page.output_picker.path() is not None
    assert page.output_picker.path().parent == source
    assert not page.external_search.isChecked()
    assert not page.start_button.isEnabled()

    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)
    page.pii_output_confirmation.setChecked(True)
    assert page.start_button.isEnabled()

    requests: list[object] = []
    page.start_requested.connect(requests.append)
    page.start_button.click()

    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, BatchReviewRequest)
    assert request.source_dir == source
    assert request.output_dir.parent == source
    assert request.external_search is False
    assert request.cloud_processing_authorized is True
    assert request.contains_classified_material is False


def test_course_batch_new_page_enforces_hundred_pdf_limit(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "too-many"
    source.mkdir()
    for number in range(101):
        (source / f"paper-{number:03}.pdf").write_bytes(b"%PDF-1.4\n")
    page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]

    page.source_picker.set_path(source)
    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)
    page.pii_output_confirmation.setChecked(True)

    assert page.preview_model.rowCount() == 101
    assert "最多支持 100 篇" in page.scan_summary.text()
    assert not page.start_button.isEnabled()
    assert page.source_picker.edit.property("fluentInvalid") is True


def test_course_batch_new_page_rotates_automatic_output_for_consecutive_starts(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FrozenDateTime:
            del tz
            return cls(2026, 8, 26, 12, 34, 56)

    monkeypatch.setattr(course_batch_new, "datetime", FrozenDateTime)
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    occupied = source / "课程论文评测报告_20260826_123456"
    occupied.mkdir()

    page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.source_picker.set_path(source)
    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)
    page.pii_output_confirmation.setChecked(True)

    requests: list[BatchReviewRequest] = []
    page.start_requested.connect(requests.append)
    first_output = source / "课程论文评测报告_20260826_123456_2"
    assert page.output_picker.path() == first_output

    page.start_button.click()
    second_output = source / "课程论文评测报告_20260826_123456_3"
    assert requests[0].output_dir == first_output
    assert page.output_picker.path() == second_output
    assert page.start_button.isEnabled()

    page.start_button.click()
    assert [request.output_dir for request in requests] == [first_output, second_output]
    assert page.output_picker.path() == (
        source / "课程论文评测报告_20260826_123456_4"
    )


@pytest.mark.parametrize(
    "marker_name",
    [BATCH_SUMMARY_FILENAME, f".{BATCH_SUMMARY_FILENAME}.owner"],
)
def test_course_batch_new_page_rejects_manual_previous_batch_output_accessibly(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
    marker_name: str,
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    previous_output = tmp_path / "previous-output"
    previous_output.mkdir()
    (previous_output / marker_name).write_text(
        "batch-existing" if marker_name.endswith(".owner") else "summary",
        encoding="utf-8",
    )

    page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.source_picker.set_path(source)
    page.cloud_processing_authorized.setChecked(True)
    page.non_classified_confirmation.setChecked(True)
    page.pii_output_confirmation.setChecked(True)
    page.output_picker.set_path(previous_output)

    assert page.output_picker.edit.property("fluentInvalid") is True
    assert "批次记录" in page.output_picker.edit.accessibleDescription()
    assert "批次记录" in page.output_picker.edit.toolTip()
    assert "批次记录" in page.message.message_label.text()
    assert "批次记录" in page.start_button.accessibleDescription()
    assert not page.start_button.isEnabled()

    page.show()
    page.activateWindow()
    page.source_picker.button.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    assert page.source_picker.button.hasFocus()
    qtbot.keyClick(page.source_picker.button, Qt.Key.Key_Tab)  # type: ignore[attr-defined]
    assert page.output_picker.edit.hasFocus()


def test_clearing_manual_output_restores_a_fresh_automatic_directory(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    source = tmp_path / "papers"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"%PDF-1.4\n")
    page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.source_picker.set_path(source)
    automatic = page.output_picker.path()
    page.output_picker.set_path(tmp_path / "manual-output")

    page.output_picker.edit.clear()

    assert page.output_picker.path() == automatic
    assert page.output_picker.edit.property("fluentInvalid") is False


def test_course_batch_detail_actions_events_and_keyboard_open(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    completed = _item(tmp_path)
    failed = _item(tmp_path, item_id="item-2", status=BatchItemStatus.FAILED)
    page = CourseBatchDetailPage(_icons(qapp))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.set_batch(_batch(tmp_path, [completed, failed], status=BatchStatus.RUNNING))
    page.show()
    page.table.selectRow(0)

    assert page.stop_button.isEnabled()
    assert not page.resume_button.isEnabled()
    assert page.open_run_button.isEnabled()
    assert page.edit_metadata_button.isEnabled()
    assert "已处理 2 / 2" in page.batch_progress.text()

    stopped: list[str] = []
    opened: list[str] = []
    edited: list[tuple[str, str]] = []
    output_paths: list[str] = []
    page.stop_requested.connect(stopped.append)
    page.run_open_requested.connect(opened.append)
    page.metadata_edit_requested.connect(
        lambda batch_id, item_id: edited.append((batch_id, item_id))
    )
    page.open_output_requested.connect(output_paths.append)
    page.stop_button.click()
    page.edit_metadata_button.click()
    page.open_output_button.click()
    page.table.setFocus()
    qtbot.keyClick(page.table, Qt.Key.Key_Return)  # type: ignore[attr-defined]

    assert stopped == ["batch-1"]
    assert edited == [("batch-1", "item-1")]
    assert output_paths == [str(tmp_path / "reports")]
    assert opened == ["run-item-1"]

    event = BatchEvent(
        batch_id="batch-1",
        event_type="stage",
        item_id="item-2",
        message="正在生成报告",
        payload={"stage": "报告生成"},
    )
    page.apply_event(event)
    assert page.model.data(page.model.index(1, 5)) == "报告生成"
    assert "正在生成报告" in page.message.message_label.text()

    page.set_batch(
        _batch(tmp_path, [completed, failed], status=BatchStatus.COMPLETED_WITH_ERRORS)
    )
    assert page.retry_button.isEnabled()
    page.set_busy(True, action="retry")
    assert page.retry_button.property("fluentBusy") is True
    assert not page.retry_button.isEnabled()


def test_course_batch_pages_have_stable_accessible_controls(
    qapp: QApplication,
    qtbot: object,
) -> None:
    new_page = CourseBatchNewPage(
        CourseBatchServiceStub(),  # type: ignore[arg-type]
        GuiPreferences(),
        _icons(qapp),
    )
    detail_page = CourseBatchDetailPage(_icons(qapp))
    qtbot.addWidget(new_page)  # type: ignore[attr-defined]
    qtbot.addWidget(detail_page)  # type: ignore[attr-defined]

    assert new_page.start_button.objectName() == "startCourseBatchButton"
    assert new_page.source_picker.edit.objectName() == "batchSourceDirectory"
    assert new_page.pii_output_confirmation.accessibleName()
    assert detail_page.table.objectName() == "courseBatchItemsTable"
    assert detail_page.stop_button.accessibleName()
    assert detail_page.edit_metadata_button.accessibleName()
