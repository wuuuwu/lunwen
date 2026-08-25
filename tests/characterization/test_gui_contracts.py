from __future__ import annotations

from paper_reviewer.gui.main_window import MainWindow
from paper_reviewer.gui.pages.new_review import NewReviewPage
from paper_reviewer.gui.pages.run_detail import RunDetailPage


def test_navigation_ids_and_status_labels_are_stable() -> None:
    navigation_ids = [item[0] for item in MainWindow.NAVIGATION]

    assert navigation_ids == ["new_review", "runs", "rubrics", "settings"]
    assert MainWindow.STATUS_TEXT["awaiting_hard_rule_confirmation"] == "等待人工复核"
    assert MainWindow.STATUS_TEXT["reported_pending_human_review"] == "评测完成 · 待人工复核"
    assert MainWindow.STATUS_TEXT["reported"] == "评测已完成"


def test_page_signals_keep_the_desktop_controller_contract() -> None:
    # PySide Signal descriptors are intentionally checked by name/arity only;
    # implementations may freely move widgets behind these page boundaries.
    assert str(NewReviewPage.start_requested) == "start_requested(PyObject)"
    assert str(RunDetailPage.back_requested) == "back_requested()"
    assert str(RunDetailPage.cancel_requested) == "cancel_requested(QString)"
    assert str(RunDetailPage.resume_requested) == "resume_requested(QString)"
    assert str(RunDetailPage.report_export_requested) == (
        "report_export_requested(QString,QString,QString,bool)"
    )
