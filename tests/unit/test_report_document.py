from pathlib import Path

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.provider import ModelApiProtocol, ProviderSnapshot, endpoint_fingerprint
from paper_reviewer.domain.review import MetaReview
from paper_reviewer.reporting.adapters import EvaluationReportAdapter, adapt_report
from paper_reviewer.reporting.document import ReportKind
from paper_reviewer.reporting.presentation import ReportPresentationProfile
from paper_reviewer.validation.audits import AuditReport


def _rubric():
    return load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))


def test_adapt_report_selects_legacy_projection_without_changing_payload() -> None:
    report = MetaReview(run_id="run-1", overall_summary="summary", findings=[])
    document = adapt_report(_rubric(), report, AuditReport())

    assert document.kind is ReportKind.LEGACY
    assert document.report is report
    assert document.presentation_profile is ReportPresentationProfile.LEGACY
    assert document.provider_lines == (
        "- Provider：自定义 Provider",
        "- 接口协议：Chat Completions",
    )


def test_evaluation_adapter_preserves_provider_snapshot_and_safe_lines() -> None:
    class EvaluationShape:
        diagnostic_score = object()

    base_url = "https://example.test/v1"
    snapshot = ProviderSnapshot(
        provider_ref="custom:" + "a" * 32,
        display_name="校内模型服务",
        protocol=ModelApiProtocol.RESPONSES,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(base_url, ModelApiProtocol.RESPONSES),
        model="review-model",
    )
    report = EvaluationShape()
    document = EvaluationReportAdapter.adapt(
        _rubric(),
        report,
        AuditReport(),
        provider_snapshot=snapshot,
        presentation_profile=ReportPresentationProfile.ZH_CN_V1,
    )

    assert document.kind is ReportKind.EVALUATION
    assert document.is_evaluation
    assert document.report is report
    assert document.presentation_profile is ReportPresentationProfile.ZH_CN_V1
    assert document.provider_lines == (
        "- Provider：校内模型服务",
        "- 接口协议：Responses API",
        "- 模型：review-model",
    )
    assert "a" * 32 not in "\n".join(document.provider_lines)
