from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from paper_reviewer.domain.batch import (
    BatchItem,
    BatchItemStatus,
    BatchRecord,
    BatchReviewRequest,
    BatchSourceSnapshot,
    BatchStatus,
)
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.pages.course_batches import (
    CourseBatchesPage,
    CourseBatchesTableModel,
)
from paper_reviewer.gui.theme import FluentThemeManager


def _icons(qapp: QApplication) -> FluentIconService:
    return FluentIconService(FluentThemeManager(qapp))


def _record(tmp_path: Path, *, status: BatchStatus = BatchStatus.COMPLETED) -> BatchRecord:
    source_dir = tmp_path / "papers"
    output_dir = tmp_path / "reports"
    source_dir.mkdir()
    output_dir.mkdir()
    request = BatchReviewRequest(
        source_dir=source_dir,
        output_dir=output_dir,
        provider="openai",
        model="gpt-5-mini",
        rubric=tmp_path / "rubric.yaml",
        profile=tmp_path / "profile.yaml",
    )
    items: list[BatchItem] = []
    for number, item_status in enumerate(
        (BatchItemStatus.COMPLETED, BatchItemStatus.QUEUED),
        start=1,
    ):
        paper = source_dir / f"paper-{number}.pdf"
        paper.write_bytes(b"%PDF-1.4\n")
        items.append(
            BatchItem(
                item_id=f"item-{number}",
                source=BatchSourceSnapshot(
                    path=paper,
                    filename=paper.name,
                    sha256="a" * 64,
                    size_bytes=paper.stat().st_size,
                    modified_time_ns=paper.stat().st_mtime_ns,
                ),
                status=item_status,
            )
        )
    return BatchRecord.model_construct(
        batch_id="batch-1",
        status=status,
        request=request,
        items=items,
        created_at=datetime(2026, 8, 26, 9, 30, tzinfo=UTC),
    )


def test_course_batches_model_displays_summary_and_status_icon(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, status=BatchStatus.COMPLETED_WITH_ERRORS)
    model = CourseBatchesTableModel(_icons(qapp))
    model.set_items([record])

    assert model.rowCount() == 1
    assert model.columnCount() == 6
    assert model.headerData(0, Qt.Orientation.Horizontal) == "创建时间"
    created = model.data(model.index(0, 0))
    assert isinstance(created, str)
    assert created.endswith(":30")
    assert model.data(model.index(0, 1)) == str(record.request.source_dir)
    assert model.data(model.index(0, 2)) == "2"
    assert model.data(model.index(0, 3)) == "1"
    assert model.data(model.index(0, 4)) == "已完成，部分失败"
    assert model.data(model.index(0, 5)) == str(record.request.output_dir)
    assert model.data(model.index(0, 4), model.BatchIdRole) == "batch-1"
    assert model.data(model.index(0, 4), model.StatusRole) == "completed_with_errors"
    assert not model.data(model.index(0, 4), Qt.ItemDataRole.DecorationRole).isNull()
    assert "已结束" in str(
        model.data(model.index(0, 4), Qt.ItemDataRole.AccessibleDescriptionRole)
    )


def test_course_batches_page_refresh_search_and_keyboard_open(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    page = CourseBatchesPage(_icons(qapp))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.show()
    record = _record(tmp_path)
    page.set_items([record])

    refreshed: list[bool] = []
    opened: list[str] = []
    page.refresh_requested.connect(lambda: refreshed.append(True))
    page.batch_open_requested.connect(opened.append)

    page.refresh_button.click()
    assert refreshed == [True]
    assert page.table.objectName() == "courseBatchesTable"
    assert page.search.accessibleName() == "搜索批次记录"

    page.search.setText("does-not-match")
    assert page.proxy.rowCount() == 0
    page.search.clear()
    assert page.proxy.rowCount() == 1

    page.table.setFocus()
    page.table.selectRow(0)
    qtbot.keyClick(page.table, Qt.Key.Key_Return)  # type: ignore[attr-defined]
    assert opened == ["batch-1"]


def test_course_batches_page_loading_and_error_states(
    qapp: QApplication,
    qtbot: object,
) -> None:
    page = CourseBatchesPage(_icons(qapp))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.show()

    page.set_loading(True)
    assert not page.refresh_button.isEnabled()
    assert page.refresh_button.property("fluentBusy") is True
    page.show_error("批次记录读取失败")
    assert page.refresh_button.isEnabled()
    assert page.refresh_button.property("fluentBusy") is False
    assert page.message.isVisible()
    assert page.message.message_label.text() == "批次记录读取失败"
