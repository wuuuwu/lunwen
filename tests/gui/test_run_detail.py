from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from paper_reviewer.application.models import ReportView, RunEvent
from paper_reviewer.domain.review import (
    CriterionAssessment,
    DiagnosticScore,
    EvaluationReport,
    ExpertOpinion,
    MetaReview,
    PanelDecision,
    PanelOutcome,
    PolicyContext,
)
from paper_reviewer.domain.rubric import AggregationPolicy, RubricDimension, RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.main_window import MainWindow
from paper_reviewer.gui.pages.run_detail import RunDetailPage
from paper_reviewer.gui.theme import FluentThemeManager
from paper_reviewer.validation.audits import AuditReport


def test_prepare_run_rebinds_detail_and_cancel_action(qapp: QApplication, qtbot: object) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    cancelled: list[str] = []
    page.cancel_requested.connect(cancelled.append)
    page.run_id = "old-run"
    page.events.setPlainText("旧任务事件")

    page.prepare_run("new-run")
    page.append_event(
        RunEvent(
            run_id="new-run",
            event_type="review_started",
            status=RunStatus.REVIEWING,
            stage="reviews",
            message="新任务正在评测",
        )
    )
    page._cancel()

    assert page.run_id == "new-run"
    assert page.events.toPlainText() == "新任务正在评测"
    assert cancelled == ["new-run"]


def test_cancel_button_busy_state_blocks_duplicate_requests(
    qapp: QApplication, qtbot: object
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.prepare_run("active-run")
    cancelled: list[str] = []
    page.cancel_requested.connect(cancelled.append)

    page.set_cancel_pending("active-run", True)
    page._cancel()

    assert cancelled == []
    assert not page.cancel_button.isEnabled()
    assert page.cancel_button.text() == "正在取消…"
    assert page.cancel_button.property("fluentBusy") is True
    assert page.cancel_button.accessibleName() == "正在取消当前评测"

    page.set_cancel_pending("active-run", False)
    page._cancel()

    assert cancelled == ["active-run"]
    assert page.cancel_button.isEnabled()
    assert page.cancel_button.text() == "取消评测"


def test_cancel_confirmation_accepts_qt_integer_button_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Worker:
        def __init__(self) -> None:
            self.cancel_calls = 0

        def isRunning(self) -> bool:
            return True

        def cancel_task(self) -> None:
            self.cancel_calls += 1

    class DetailPage:
        def __init__(self) -> None:
            self.pending: list[tuple[str, bool]] = []

        def set_cancel_pending(self, run_id: str, pending: bool) -> None:
            self.pending.append((run_id, pending))

    worker = Worker()
    page = DetailPage()
    window = SimpleNamespace(
        _review_worker=worker,
        _active_run_id="active-run",
        run_detail_page=page,
        global_status=SimpleNamespace(setText=lambda _value: None),
        _persist_cancelled_run=lambda _run_id: None,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes.value,
    )

    MainWindow.cancel_review(window, "active-run")  # type: ignore[arg-type]

    assert worker.cancel_calls == 1
    assert page.pending == [("active-run", True)]


def test_report_export_controls_emit_sanitized_default_and_block_duplicates(
    qapp: QApplication,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.run_id = "report-run"
    page.run_dir = tmp_path
    page._report_available = True
    page._report_input_path = r"C:\papers\期末:论文?.pdf"
    page._set_export_buttons_enabled(True)
    emitted: list[tuple[str, str, str, bool]] = []
    page.report_export_requested.connect(lambda *value: emitted.append(value))
    selected_defaults: list[str] = []
    dialog_state: dict[str, object] = {}

    monkeypatch.setattr(
        QFileDialog,
        "selectFile",
        lambda _dialog, value: selected_defaults.append(value),
    )
    monkeypatch.setattr(
        QFileDialog,
        "selectedFiles",
        lambda _dialog: [str(tmp_path / "期末_AI辅助评测报告.md")],
    )

    def accept_dialog(dialog: QFileDialog) -> int:
        dialog_state["suffix"] = dialog.defaultSuffix()
        dialog_state["filters"] = dialog.nameFilters()
        return QDialog.DialogCode.Accepted.value

    monkeypatch.setattr(QFileDialog, "exec", accept_dialog)
    page.export_markdown_button.click()
    page.export_markdown_button.click()

    assert selected_defaults == ["期末_论文__AI辅助评测报告.md"]
    assert dialog_state == {
        "suffix": "md",
        "filters": ["Markdown 文件 (*.md)"],
    }
    assert emitted == [
        (
            "report-run",
            "markdown",
            str(tmp_path / "期末_AI辅助评测报告.md"),
            False,
        )
    ]
    assert not page.export_markdown_button.isEnabled()
    assert not page.export_pdf_button.isEnabled()
    assert page.export_markdown_button.property("fluentBusy") is True
    assert page.export_pdf_button.property("fluentBusy") is False
    assert page.export_markdown_button.objectName() == "exportMarkdownButton"
    assert page.export_pdf_button.objectName() == "exportPdfButton"
    assert page.open_report_folder_button.objectName() == "openReportFolderButton"
    assert page.export_markdown_button.accessibleName()
    assert page.export_pdf_button.accessibleName()


def test_report_export_confirms_the_final_suffixed_overwrite_target(
    qapp: QApplication,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.run_id = "report-run"
    page.run_dir = tmp_path
    page._report_available = True
    page._report_input_path = "paper.pdf"
    page._set_export_buttons_enabled(True)
    final_path = tmp_path / "existing.md"
    final_path.write_text("old", encoding="utf-8")
    emitted: list[tuple[str, str, str, bool]] = []
    page.report_export_requested.connect(lambda *value: emitted.append(value))
    monkeypatch.setattr(
        QFileDialog,
        "selectedFiles",
        lambda _dialog: [str(tmp_path / "existing")],
    )
    monkeypatch.setattr(
        QFileDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted.value,
    )
    confirmations: list[str] = []

    def confirm(
        _parent: object,
        _title: str,
        message: str,
        *_args: object,
    ) -> QMessageBox.StandardButton:
        confirmations.append(message)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)

    page.export_markdown_button.click()

    assert confirmations and "existing.md" in confirmations[0]
    assert emitted == [("report-run", "markdown", str(final_path), True)]


def test_report_export_cancel_does_not_emit_or_enter_busy_state(
    qapp: QApplication,
    qtbot: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.run_id = "report-run"
    page.run_dir = tmp_path
    page._report_available = True
    page._report_input_path = "paper.pdf"
    page._set_export_buttons_enabled(True)
    emitted: list[tuple[object, ...]] = []
    page.report_export_requested.connect(lambda *value: emitted.append(value))
    monkeypatch.setattr(
        QFileDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Rejected.value,
    )

    page.export_pdf_button.click()

    assert emitted == []
    assert not page._export_busy
    assert page.export_markdown_button.isEnabled()
    assert page.export_pdf_button.isEnabled()


def test_report_export_result_message_and_stale_callback_are_isolated(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.run_id = "report-run"
    page.run_dir = tmp_path
    page._report_available = True
    page._set_export_buttons_enabled(True)
    page._set_export_busy(True, "pdf")
    output = tmp_path / "报告.pdf"

    assert page.report_export_succeeded("report-run", SimpleNamespace(output_path=output))
    assert not page.message.isHidden()
    assert page.message.property("fluentSeverity") == "success"
    assert page.message.action_button.text() == "打开文件"
    assert not page._export_busy

    page._set_export_busy(True, "markdown")
    page.reset_report_export_state()
    assert not page.report_export_failed("report-run", "旧任务错误")
    assert not page._export_busy
    assert not page.export_markdown_button.isEnabled()


def test_main_window_controller_blocks_duplicate_programmatic_export() -> None:
    page = SimpleNamespace(
        run_id="report-run",
        report_export_generation=lambda: 4,
        report_export_failed=lambda *_args: True,
    )
    started: list[object] = []
    controller = SimpleNamespace(
        run_detail_page=page,
        _detail_request_generation=9,
        _report_export_inflight=set(),
        _report_export_format=MainWindow._report_export_format,
        _report_export_completed=lambda *_args: None,
        _report_export_failed=lambda *_args: None,
        _run_async=lambda operation, _completed, _failed: started.append(operation),
    )

    for _ in range(2):
        MainWindow._export_report_requested(
            controller,
            "report-run",
            "pdf",
            "C:/exports/report.pdf",
            False,
        )

    assert len(started) == 1
    assert controller._report_export_inflight == {("report-run", 4)}


def test_hard_rule_review_requires_reviewer_and_reason_and_emits_resolve_then_resume(
    qapp: QApplication, qtbot: object
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    resolved: list[tuple[str, dict[str, object]]] = []
    resumed: list[str] = []
    page.hard_rule_resolution_requested.connect(
        lambda run_id, decision: resolved.append((run_id, decision))
    )
    page.resume_after_human_review_requested.connect(resumed.append)

    page.prepare_run("rule-run")
    page._show_hard_rule_review(
        [
            {
                "rule_id": "integrity-1",
                "description": "疑似学术不端",
                "status": "suspected",
                "ai_judgment": "存在相似表述",
                "paper_evidence": [{"page": 3, "block_id": "b3", "quote": "引文"}],
                "external_evidence": [{"title": "来源论文", "url": "https://example.test"}],
            }
        ]
    )
    assert not page.hard_rule_review_frame.isHidden()
    assert not page.confirm_rule_button.isEnabled()

    page.hard_rule_reviewer_input.setText("教师甲")
    page.hard_rule_reason_input.setPlainText("已线下核对原文，确认不成立")
    assert page.dismiss_rule_button.isEnabled()
    page.dismiss_rule_button.click()

    assert resolved[0][0] == "rule-run"
    assert resolved[0][1]["rule_id"] == "integrity-1"
    assert resolved[0][1]["confirmed"] is False
    assert resumed == []
    assert page._review_busy
    assert "第 3 页" in page.hard_rule_detail.toPlainText()


def test_report_page_shows_post_review_panel_task(
    qapp: QApplication, qtbot: object
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    submitted: list[tuple[str, object]] = []
    page.panel_review_resolution_requested.connect(
        lambda run_id, decision: submitted.append((run_id, decision))
    )
    page.prepare_run("panel-run")
    page.stack.setCurrentWidget(page.report_page)
    page._show_hard_rule_review(
        [],
        panel_review_required=True,
        panel_review_detail="专家一无法判断，需要人工面板复核。",
    )
    page.hard_rule_reviewer_input.setText("教师甲")
    page.hard_rule_reason_input.setPlainText("已结合完整报告进行人工面板评议。")
    page.confirm_rule_button.click()

    assert submitted[0][0] == "panel-run"
    assert submitted[0][1]["outcome"] == "risk_triggered"
    assert page.hard_rule_review_frame.parent() is page.report_content


def test_hard_rule_busy_state_blocks_duplicate_submission(
    qapp: QApplication,
    qtbot: object,
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    emitted: list[tuple[str, object]] = []
    page.hard_rule_resolution_requested.connect(
        lambda run_id, decision: emitted.append((run_id, decision))
    )
    page.prepare_run("busy-run")
    page._show_hard_rule_review(
        [{"rule_id": "integrity-1", "description": "疑似学术不端", "status": "suspected"}]
    )
    page.hard_rule_reviewer_input.setText("教师甲")
    page.hard_rule_reason_input.setPlainText("正在核查")

    page._set_review_busy(True)

    assert not page.confirm_rule_button.isEnabled()
    assert not page.dismiss_rule_button.isEnabled()
    assert page.confirm_rule_button.property("fluentBusy") is True
    assert "正在保存" in page.confirm_rule_button.accessibleDescription()
    page.confirm_rule_button.click()
    assert emitted == []

    page._set_review_busy(False)
    assert page.confirm_rule_button.isEnabled()
    assert page.dismiss_rule_button.isEnabled()


def test_v2_report_uses_experimental_score_without_legacy_verdict_card(
    qapp: QApplication, qtbot: object, tmp_path: Path
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    rubric = RubricProfile(
        rubric_id="v2-view",
        version="1",
        title="动态报告测试",
        scoring_enabled=True,
        aggregation=AggregationPolicy(method="weighted_mean"),
        dimensions=[
            RubricDimension(
                dimension_id="criterion",
                title="测试指标",
                description="测试",
                weight=100,
                minimum_score=0,
                maximum_score=4,
                checks=["check"],
                anchors=[
                    {
                        "label": "range",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "range",
                    }
                ],
                group_id="group",
            )
        ],
    )
    meta = MetaReview(run_id="report-run", overall_summary="总体评价", findings=[])
    opinion = ExpertOpinion(
        expert_id="expert-1",
        round="initial",
        verdict="qualified",
        rationale="合格",
    )
    evaluation = EvaluationReport(
        run_id="report-run",
        policy_context=PolicyContext(
            source="政策",
            document_number="文号",
            effective_date="2023-04-01",
            source_sha256="a" * 64,
        ),
        diagnostic_score=DiagnosticScore(
            assessments=[
                CriterionAssessment(
                    criterion_id="criterion",
                    reviewer_id="specialist",
                    rating=2,
                    weight=100,
                    rationale="基本达到",
                    confidence=0.5,
                )
            ],
            group_scores={"group": 50},
            total_score=50,
        ),
        expert_opinions=[opinion],
        panel_decision=PanelDecision(
            outcome=PanelOutcome.RISK_NOT_TRIGGERED,
            reason="测试结论",
            decision_path=["initial_unqualified_zero", "risk_not_triggered"],
        ),
        meta_review=meta,
    )
    run = RunRecord(
        run_id="report-run",
        status=RunStatus.REPORTED,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="v2-view@1",
        provider="fake",
        model="fake",
    )
    report = ReportView(
        run=run,
        rubric=rubric,
        review=meta,
        audit=AuditReport(),
        report_markdown=tmp_path / "report.md",
        report_json=tmp_path / "report.json",
        evaluation=evaluation,
    )

    page.show_report(report, run_dir=tmp_path)

    assert page.score_frame.isHidden()
    assert page.diagnostic_scores_model.rowCount() == 1
    assert "50" in page.experimental_score.text()
    assert "risk_not_triggered" in page.decision_path.toPlainText()
    assert "不是浙江省教育厅正式抽检结论" in page.disclaimers.text()


@pytest.mark.parametrize(("width", "height"), [(900, 600), (1180, 760)])
def test_report_outputs_keep_readable_height_inside_vertical_scroll_page(
    qapp: QApplication,
    qtbot: object,
    width: int,
    height: int,
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.resize(width, height)
    page.stack.setCurrentWidget(page.report_page)
    page.report_metadata.setText("超长论文标题和模型元数据" * 300)
    page.show()
    qapp.processEvents()

    assert page.report_scroll.verticalScrollBar().maximum() > 0
    assert page.report_scroll.horizontalScrollBar().maximum() == 0
    assert page.report_content.width() <= page.report_scroll.viewport().width()
    for output in (
        page.diagnostic_scores,
        page.hard_rule_report,
        page.panel_report,
        page.decision_path,
        page.findings,
        page.finding_detail,
        page.notes,
    ):
        assert output.minimumHeight() > output.fontMetrics().lineSpacing() * 4
        assert output.height() >= output.minimumHeight()
    expected_diagnostic_height = (
        page.diagnostic_scores.horizontalHeader().sizeHint().height()
        + page.diagnostic_scores.verticalHeader().defaultSectionSize() * 9
    )
    assert page.diagnostic_scores.minimumHeight() >= expected_diagnostic_height
    assert not page.findings_splitter.childrenCollapsible()
    assert all(size > 0 for size in page.findings_splitter.sizes())
    page.findings_splitter.setSizes([0, 10_000])
    qapp.processEvents()
    assert page.findings.width() >= page.findings.minimumWidth()
    assert page.finding_detail.width() >= page.finding_detail.minimumWidth()
    assert page.report_scroll.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert page.report_scroll.accessibleName() == "评测报告可滚动内容"


def test_progress_outputs_scroll_without_compressing_hard_rule_review(
    qapp: QApplication,
    qtbot: object,
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page._show_hard_rule_review(
        [
            {
                "rule_id": "integrity-1",
                "description": "疑似学术不端",
                "status": "suspected",
            }
        ]
    )
    page.resize(900, 600)
    page.stack.setCurrentWidget(page.progress_page)
    page.run_metadata.setText("超长任务元数据" * 300)
    page.show()
    qapp.processEvents()

    assert page.progress_scroll.verticalScrollBar().maximum() > 0
    assert page.progress_scroll.horizontalScrollBar().maximum() == 0
    assert page.progress_content.width() <= page.progress_scroll.viewport().width()
    for output in (
        page.stage_list,
        page.hard_rule_list,
        page.hard_rule_detail,
        page.hard_rule_reason_input,
        page.events,
    ):
        assert output.height() >= output.minimumHeight()
    assert page.hard_rule_detail.minimumHeight() > page.fontMetrics().lineSpacing() * 8
    assert page.events.minimumHeight() > page.fontMetrics().lineSpacing() * 9
