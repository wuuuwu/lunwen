from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from paper_reviewer.application.models import ReportView, RunDetail, RunEvent
from paper_reviewer.config import load_rubric
from paper_reviewer.domain.review import (
    CriterionAssessment,
    DiagnosticScore,
    EvaluationReport,
    ExpertOpinion,
    MetaReview,
    PanelDecision,
    PanelOutcome,
    PolicyContext,
    ReviewFinding,
    Severity,
)
from paper_reviewer.domain.rubric import (
    AggregationPolicy,
    RubricDimension,
    RubricGroup,
    RubricProfile,
)
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.domain.submission import (
    SUBMISSION_METADATA_FIELDS,
    SubmissionFieldEvidence,
    SubmissionMetadata,
    SubmissionMetadataSource,
)
from paper_reviewer.gui.icons import FluentIconService
from paper_reviewer.gui.main_window import MainWindow
from paper_reviewer.gui.pages.run_detail import RunDetailPage
from paper_reviewer.gui.pages.run_detail_presenter import _course_grade
from paper_reviewer.gui.theme import FluentThemeManager
from paper_reviewer.reporting.presentation import ReportPresentationProfile
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


def test_course_progress_uses_metadata_and_reviews_without_thesis_panel(
    qapp: QApplication,
    qtbot: object,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    page.prepare_run("course-run")

    page.append_event(
        RunEvent(
            run_id="course-run",
            event_type="submission_metadata_started",
            status=RunStatus.INGESTED,
            stage="metadata",
            message="正在提取姓名、学号、专业和论文题目",
        )
    )

    labels = [
        page.stage_model.item(row).text()
        for row in range(page.stage_model.rowCount())
    ]
    assert len(labels) == 7
    assert any("进行中 · 提取学生与论文信息" in label for label in labels)
    assert any("课程专项 Reviewer 评阅" in label for label in labels)
    assert all("专业化评分" not in label for label in labels)
    assert all("独立专家面板" not in label for label in labels)

    page.append_event(
        RunEvent(
            run_id="course-run",
            event_type="submission_metadata_completed",
            stage="metadata",
            message="学生与论文信息提取完成",
        )
    )
    page.append_event(
        RunEvent(
            run_id="course-run",
            event_type="reviews_started",
            status=RunStatus.REVIEWING,
            stage="reviews",
            message="多位 Reviewer 正在评测",
        )
    )

    labels = [
        page.stage_model.item(row).text()
        for row in range(page.stage_model.rowCount())
    ]
    assert any("已完成 · 提取学生与论文信息" in label for label in labels)
    assert any("进行中 · 课程专项 Reviewer 评阅" in label for label in labels)
    assert page.stage_progress.maximum() == 7
    assert page.stage_progress.value() == 1


def test_course_progress_is_recovered_from_trace_while_non_course_stays_legacy(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    run = RunRecord(
        run_id="course-run",
        status=RunStatus.AUDITING,
        input_path="paper.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="custom-course-rubric@1",
        provider="fake",
        model="fake",
        completed_stages=["ingest", "evidence", "reviews"],
    )
    detail = RunDetail(
        run=run,
        events=[
            RunEvent(
                run_id=run.run_id,
                event_type="submission_metadata_started",
                status=RunStatus.INGESTED,
                stage="metadata",
                message="正在提取信息",
            ),
            RunEvent(
                run_id=run.run_id,
                event_type="evidence_collection_started",
                status=RunStatus.BUILDING_EVIDENCE,
                stage="evidence",
                message="正在收集证据",
            ),
        ],
    )

    page.show_detail(detail, run_dir=tmp_path)

    course_labels = [
        page.stage_model.item(row).text()
        for row in range(page.stage_model.rowCount())
    ]
    assert any("已完成 · 提取学生与论文信息" in label for label in course_labels)
    assert any("进行中 · 确定性审计" in label for label in course_labels)
    assert all("独立专家面板" not in label for label in course_labels)

    page.prepare_run("legacy-run")
    page.append_event(
        RunEvent(
            run_id="legacy-run",
            event_type="reviews_started",
            status=RunStatus.REVIEWING,
            stage="reviews",
            message="Reviewer 评测开始",
        )
    )
    legacy_labels = [
        page.stage_model.item(row).text()
        for row in range(page.stage_model.rowCount())
    ]
    assert any("进行中 · 专业化评分" in label for label in legacy_labels)
    assert any("独立专家面板" in label for label in legacy_labels)
    assert all("提取学生与论文信息" not in label for label in legacy_labels)


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
        groups=[
            RubricGroup(
                group_id="group",
                title="测试分组",
                description="测试分组说明",
                weight=100,
                dimensions=["criterion"],
            )
        ],
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

    localized = report.model_copy(
        update={"presentation_profile": ReportPresentationProfile.ZH_CN_V1},
        deep=True,
    )
    page.show_report(localized, run_dir=tmp_path)

    assert page.diagnostic_scores_model.item(0, 0).text() == "测试指标"
    assert page.diagnostic_scores_model.item(0, 1).text() == "测试分组"
    assert "三名初评专家均判定合格" in page.decision_path.toPlainText()
    assert "risk_not_triggered" not in page.decision_path.toPlainText()
    assert "初评专家 1" in page.panel_report.toPlainText()
    assert "expert-1" not in page.panel_report.toPlainText()


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, "核心任务明显缺失"),
        (39, "核心任务明显缺失"),
        (40, "完成不足"),
        (59, "完成不足"),
        (60, "达到基本要求"),
        (74, "达到基本要求"),
        (75, "良好"),
        (89, "良好"),
        (90, "优秀"),
        (100, "优秀"),
    ],
)
def test_course_grade_uses_the_five_rubric_anchors(score: int, expected: str) -> None:
    assert _course_grade(score) == expected


def test_course_report_shows_student_metadata_and_course_only_sections(
    qapp: QApplication,
    qtbot: object,
    tmp_path: Path,
) -> None:
    theme = FluentThemeManager(qapp)
    theme.set_mode("light")
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]
    rubric = load_rubric(Path("configs/rubrics/course_paper_v1.yaml"))
    scores = {
        dimension.dimension_id: score
        for dimension, score in zip(
            rubric.dimensions,
            (86.0, 82.0, 78.0, 80.0, 88.0, 76.0),
            strict=True,
        )
    }
    metadata = SubmissionMetadata(
        student_name="张三",
        student_id="20260001",
        major="公共管理",
        paper_title="数字政务课程论文",
        field_evidence={
            field: SubmissionFieldEvidence(
                source=SubmissionMetadataSource.COVER_LABEL,
                confidence=0.99,
                page=1,
                evidence=f"{field} 封面标签",
            )
            for field in SUBMISSION_METADATA_FIELDS
        },
    )
    finding = ReviewFinding(
        finding_id="course-finding-1",
        reviewer_id="course-reviewer",
        dimension_id="argument_evidence",
        severity=Severity.MINOR,
        confidence=0.8,
        claim="个别论断支撑不足",
        rationale="关键论断需要增加课程材料依据。",
        recommendation="补充课程案例或文献。",
    )
    review = MetaReview(
        run_id="course-report-run",
        overall_summary="论文已较好完成课程任务。",
        findings=[finding],
        total_score=82.0,
        verdict="pass",
    )
    run = RunRecord(
        run_id="course-report-run",
        status=RunStatus.REPORTED,
        input_path="raw_upload_001.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id=f"{rubric.rubric_id}@{rubric.version}",
        provider="fake",
        model="fake",
    )
    report = ReportView(
        run=run,
        rubric=rubric,
        review=review,
        audit=AuditReport(),
        dimension_scores=scores,
        report_markdown=tmp_path / "report.md",
        report_json=tmp_path / "report.json",
        presentation_profile=ReportPresentationProfile.COURSE_ZH_CN_V1,
        submission_metadata=metadata,
    )

    page.show_report(report, run_dir=tmp_path)

    metadata_text = page.report_metadata.text()
    assert "题目：数字政务课程论文" in metadata_text
    assert "姓名：张三" in metadata_text
    assert "学号：20260001" in metadata_text
    assert "专业：公共管理" in metadata_text
    assert "信息核对：自动提取" in metadata_text

    pending_evidence = dict(metadata.field_evidence)
    pending_evidence["student_name"] = SubmissionFieldEvidence(
        source=SubmissionMetadataSource.FILE_NAME,
        confidence=0.5,
    )
    pending_report = report.model_copy(
        update={
            "submission_metadata": metadata.model_copy(
                update={"field_evidence": pending_evidence}
            )
        }
    )
    page.show_report(pending_report, run_dir=tmp_path)
    assert "信息核对：人工核对未完成（待核对：姓名）" in page.report_metadata.text()
    assert page.total_score.text() == "课程总分\n82 分"
    assert "五级等级：良好" in page.dimension_scores.text()
    assert "课程要求结论：达到课程论文基本要求" in page.dimension_scores.text()
    assert "pass" not in page.dimension_scores.text().casefold()
    assert page.diagnostic_title.text() == "六项课程评价维度"
    assert page.diagnostic_scores_model.rowCount() == 6
    assert page.diagnostic_scores_model.columnCount() == 4
    assert page.diagnostic_scores_model.headerData(0, Qt.Orientation.Horizontal) == "课程评价维度"
    assert [
        page.diagnostic_scores_model.item(row, 0).text()
        for row in range(page.diagnostic_scores_model.rowCount())
    ] == [dimension.title for dimension in rubric.dimensions]
    assert page.overall_summary.text() == "论文已较好完成课程任务。"
    assert page.findings_model.rowCount() == 1
    assert not page.findings_frame.isHidden()
    assert page.hard_rule_review_frame.isHidden()
    assert page.hard_rule_report_frame.isHidden()
    assert page.panel_report_frame.isHidden()
    assert page.decision_frame.isHidden()
    assert page.notes_title.text() == "分歧与审计说明"
    visible_course_text = "\n".join(
        (
            page.diagnostic_title.text(),
            page.disclaimers.text(),
            page.notes_title.text(),
        )
    )
    for policy_term in ("浙江", "抽检风险", "否决项", "专家面板", "人工复核"):
        assert policy_term not in visible_course_text
    assert page.diagnostic_scores.objectName() == "diagnosticScores"
    assert page.diagnostic_scores.accessibleName() == "六项课程评价维度得分"


def test_report_mode_restores_policy_sections_after_course_report(
    qapp: QApplication,
    qtbot: object,
) -> None:
    theme = FluentThemeManager(qapp)
    page = RunDetailPage(FluentIconService(theme))
    qtbot.addWidget(page)  # type: ignore[attr-defined]

    page._configure_report_mode(True)
    page._configure_report_mode(False)

    assert page.diagnostic_title.text() == "九项诊断评分（0–4）"
    assert not page.hard_rule_report_frame.isHidden()
    assert not page.panel_report_frame.isHidden()
    assert not page.decision_frame.isHidden()
    assert page.notes_title.text() == "分歧、人工复核与审计说明"


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
