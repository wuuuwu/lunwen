from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from paper_reviewer.application.batch_errors import (
    BatchErrorKind,
    BatchErrorScope,
    BatchFailure,
    classify_batch_error,
    sanitize_batch_error_summary,
)
from paper_reviewer.application.orchestrator import SanitizedReviewError


class AuthenticationError(RuntimeError):
    status_code = 401


class RateLimitError(RuntimeError):
    status_code = 429


class BadModelError(RuntimeError):
    status_code = 404


class NetworkError(RuntimeError):
    pass


class FileDataError(RuntimeError):
    pass


class KeyringError(RuntimeError):
    pass


class ProviderHTTPError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.param = param


def test_shared_provider_failures_pause_the_batch() -> None:
    cases = [
        (AuthenticationError("Bearer sk-secret"), BatchErrorKind.AUTHENTICATION),
        (
            RateLimitError("upstream body with https://provider.example/v1?key=secret"),
            BatchErrorKind.RATE_LIMIT,
        ),
        (BadModelError("raw model response"), BatchErrorKind.INVALID_MODEL),
        (ValueError("custom provider API Key is not configured"), BatchErrorKind.MISSING_API_KEY),
        (ValueError("unsupported protocol"), BatchErrorKind.INVALID_PROTOCOL),
        (ValueError("rubric validation failed"), BatchErrorKind.INVALID_RUBRIC),
        (TimeoutError("paper text and query"), BatchErrorKind.TIMEOUT),
        (PermissionError("C:/secret/report output"), BatchErrorKind.OUTPUT_DIRECTORY),
    ]

    for error, kind in cases:
        context = "output_directory" if kind is BatchErrorKind.OUTPUT_DIRECTORY else None
        result = classify_batch_error(error, context=context)
        assert result.scope is BatchErrorScope.SHARED_PAUSE
        assert result.kind is kind
        assert isinstance(result, BatchFailure)
        assert len(result.summary) <= 500


def test_item_failures_do_not_pause_other_papers() -> None:
    cases = [
        (FileDataError("malformed PDF containing student paper"), "pdf"),
        (ValueError("deterministic review audit failed: secret paper"), "audit"),
        (ValueError("agent did not produce valid output within its budget"), None),
        (RuntimeError("unknown failure with full paper body"), None),
    ]

    for error, context in cases:
        result = classify_batch_error(error, context=context)
        assert result.scope is BatchErrorScope.ITEM_FAILURE
        assert result.kind in {
            BatchErrorKind.PDF_CORRUPT,
            BatchErrorKind.PDF_PARSE,
            BatchErrorKind.AUDIT,
            BatchErrorKind.INVALID_MODEL_OUTPUT,
            BatchErrorKind.UNKNOWN,
        }


def test_summaries_never_include_secrets_urls_queries_or_exception_body() -> None:
    secret = "sk-super-secret-123456789"
    body = (
        f"Authorization: Bearer {secret} https://private.example/v1/responses?api_key={secret} "
        "student paper body must not appear"
    )
    result = classify_batch_error(RuntimeError(body))
    summary = sanitize_batch_error_summary(RuntimeError(body))

    for value in (result.summary, summary):
        assert len(value) <= 500
        assert secret not in value
        assert "Bearer" not in value
        assert "private.example" not in value
        assert "api_key=" not in value
        assert "student paper" not in value


def test_cancelled_error_is_control_flow_and_is_not_classified() -> None:
    with pytest.raises(asyncio.CancelledError):
        classify_batch_error(asyncio.CancelledError())


def test_output_directory_context_disambiguates_generic_os_error(tmp_path: Path) -> None:
    error = OSError("could not create report")
    result = classify_batch_error(error, context="output_directory")
    assert result.scope is BatchErrorScope.SHARED_PAUSE
    assert result.kind is BatchErrorKind.OUTPUT_DIRECTORY


def test_batch_authorization_failure_is_shared_and_safely_classified() -> None:
    result = classify_batch_error(
        ValueError("paper names and private authorization details"),
        context="authorization",
    )

    assert result.scope is BatchErrorScope.SHARED_PAUSE
    assert result.kind is BatchErrorKind.AUTHORIZATION
    assert "paper names" not in result.summary


def test_composite_preflight_context_uses_the_actual_failure_signal() -> None:
    context = "credential output directory rubric"
    cases = [
        (
            ValueError("batch rubric snapshot is not a course assessment rubric"),
            BatchErrorKind.INVALID_RUBRIC,
        ),
        (
            ValueError("批次 Provider 的 API Key 不存在。"),
            BatchErrorKind.MISSING_API_KEY,
        ),
        (
            PermissionError("C:/private/output/report.pdf"),
            BatchErrorKind.OUTPUT_DIRECTORY,
        ),
        (
            KeyringError("credential backend returned secret account data"),
            BatchErrorKind.CREDENTIAL_ACCESS,
        ),
    ]

    for error, expected_kind in cases:
        result = classify_batch_error(error, context=context)
        assert result.scope is BatchErrorScope.SHARED_PAUSE
        assert result.kind is expected_kind
        assert str(error) not in result.summary


def test_database_and_sqlite_failures_pause_the_batch_with_static_text() -> None:
    nested = sqlite3.OperationalError(
        "unable to open database file C:/private/student-paper.db"
    )
    outer = RuntimeError("repository write failed with a private paper title")
    outer.__cause__ = nested

    for error in (nested, outer, OSError("private path")):
        context = "database_write" if isinstance(error, OSError) else None
        result = classify_batch_error(error, context=context)
        assert result.scope is BatchErrorScope.SHARED_PAUSE
        assert result.kind is BatchErrorKind.DATABASE
        assert result.summary == "本地数据库连接或写入失败；批次已暂停。"
        assert "private" not in result.summary


def test_sanitized_database_wrapper_is_a_shared_failure() -> None:
    SanitizedDatabaseError = type("SanitizedDatabaseError", (RuntimeError,), {})
    result = classify_batch_error(SanitizedDatabaseError("private SQL parameters"))

    assert result.scope is BatchErrorScope.SHARED_PAUSE
    assert result.kind is BatchErrorKind.DATABASE
    assert "private SQL" not in result.summary


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (
            ProviderHTTPError(400, "opaque provider failure", param="tools"),
            BatchErrorKind.INVALID_PROTOCOL,
        ),
        (
            ProviderHTTPError(400, "function calling is not supported"),
            BatchErrorKind.INVALID_PROTOCOL,
        ),
        (
            ProviderHTTPError(404, "private route", code="unsupported_endpoint"),
            BatchErrorKind.INVALID_PROTOCOL,
        ),
        (
            ProviderHTTPError(400, "opaque provider failure", param="model"),
            BatchErrorKind.INVALID_MODEL,
        ),
        (
            ProviderHTTPError(404, "private model", code="model_not_found"),
            BatchErrorKind.INVALID_MODEL,
        ),
    ],
)
def test_http_model_and_tool_incompatibilities_pause_the_batch(
    error: ProviderHTTPError,
    expected_kind: BatchErrorKind,
) -> None:
    result = classify_batch_error(error)
    assert result.scope is BatchErrorScope.SHARED_PAUSE
    assert result.kind is expected_kind
    assert str(error) not in result.summary


def test_generic_http_400_remains_paper_local_without_a_shared_failure_hint() -> None:
    result = classify_batch_error(
        ProviderHTTPError(400, "paper-specific context length exceeded")
    )
    assert result.scope is BatchErrorScope.ITEM_FAILURE
    assert result.kind is BatchErrorKind.UNKNOWN


@pytest.mark.parametrize(
    ("original", "expected_kind"),
    [
        (ProviderHTTPError(401, "Bearer sk-private"), BatchErrorKind.AUTHENTICATION),
        (ProviderHTTPError(429, "private response body"), BatchErrorKind.RATE_LIMIT),
        (
            ProviderHTTPError(400, "private response body", param="tools"),
            BatchErrorKind.INVALID_PROTOCOL,
        ),
        (TimeoutError("private request payload"), BatchErrorKind.TIMEOUT),
    ],
)
def test_sanitized_orchestrator_errors_preserve_safe_shared_classification(
    original: BaseException,
    expected_kind: BatchErrorKind,
) -> None:
    wrapped = SanitizedReviewError(
        "provider request failed",
        original_error=original,
    )

    result = classify_batch_error(wrapped)

    assert result.scope is BatchErrorScope.SHARED_PAUSE
    assert result.kind is expected_kind
    assert "private" not in result.summary
    assert "Bearer" not in result.summary


def test_unreadable_source_pdf_does_not_pause_other_papers() -> None:
    result = classify_batch_error(PermissionError("C:/private/student-paper.pdf"))
    assert result.scope is BatchErrorScope.ITEM_FAILURE
    assert result.kind is BatchErrorKind.PDF_PARSE
    assert "private" not in result.summary
