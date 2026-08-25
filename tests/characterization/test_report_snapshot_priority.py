from __future__ import annotations

import json
from pathlib import Path

from paper_reviewer.application.service import _load_export_report_snapshot
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.validation.audits import AuditReport


def _write_legacy_snapshot(run_dir: Path) -> None:
    (run_dir / "rubric.json").write_text(
        RubricProfile(
            rubric_id="legacy-characterization",
            version="1",
            title="Legacy characterization rubric",
        ).model_dump_json(),
        encoding="utf-8",
    )
    (run_dir / "audit.json").write_text(AuditReport().model_dump_json(), encoding="utf-8")


def _meta(run_id: str, summary: str) -> dict[str, object]:
    return {"run_id": run_id, "overall_summary": summary, "findings": []}


def test_report_snapshot_prefers_evaluation_then_report_then_meta_review(
    tmp_path: Path,
) -> None:
    """The file candidates are an on-disk compatibility chain for old tasks."""

    _write_legacy_snapshot(tmp_path)
    (tmp_path / "meta-review.json").write_text(
        json.dumps(_meta("run", "meta fallback")), encoding="utf-8"
    )
    (tmp_path / "report.json").write_text(
        json.dumps(_meta("run", "legacy report")), encoding="utf-8"
    )

    rubric, selected, audit = _load_export_report_snapshot(tmp_path)
    assert rubric.rubric_id == "legacy-characterization"
    assert selected.overall_summary == "legacy report"
    assert audit.passed

    (tmp_path / "evaluation-report.json").write_text(
        json.dumps(_meta("run", "evaluation-shaped legacy payload")), encoding="utf-8"
    )
    # A legacy MetaReview-shaped payload in the newest slot is still readable,
    # and the slot itself has precedence over older report files.
    _rubric, selected, _audit = _load_export_report_snapshot(tmp_path)
    assert selected.overall_summary == "evaluation-shaped legacy payload"


def test_report_snapshot_rejects_missing_required_inputs(tmp_path: Path) -> None:
    try:
        _load_export_report_snapshot(tmp_path)
    except ValueError as error:
        assert "rubric" in str(error)
    else:  # pragma: no cover - defensive assertion for a broken compatibility gate
        raise AssertionError("missing rubric/audit snapshots must be rejected")
