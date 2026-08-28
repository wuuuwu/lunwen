"""Classification of failures while processing a course-paper batch.

The batch coordinator needs to make one important distinction: a failure that
is specific to the current PDF should be recorded and processing may continue,
whereas a failure shared by the provider or the batch configuration should
pause the batch.  This module deliberately does not expose exception text.
Provider exceptions can contain request URLs, credentials, response bodies or
even text extracted from a paper, so summaries are selected from a small,
static set of safe messages.

``asyncio.CancelledError`` is intentionally re-raised.  Cancellation is a
control-flow signal, not a failed paper, and swallowing it makes stopping a
batch unreliable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from paper_reviewer.application.batch_output import (
    BATCH_OUTPUT_OWNED_MESSAGE,
    BATCH_OUTPUT_OWNERSHIP_UNVERIFIABLE_MESSAGE,
    BATCH_OUTPUT_SUMMARY_EXISTS_MESSAGE,
    BatchOutputOwnedByAnotherBatchError,
    BatchOutputOwnershipUnverifiableError,
    BatchOutputSummaryExistsError,
)


class BatchErrorScope(StrEnum):
    """What the batch coordinator should do after a failure."""

    SHARED_PAUSE = "shared_pause"
    ITEM_FAILURE = "item_failure"


class BatchErrorKind(StrEnum):
    """Stable, non-sensitive reason for a classified batch failure."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    MISSING_API_KEY = "missing_api_key"
    CREDENTIAL_ACCESS = "credential_access"
    INVALID_MODEL = "invalid_model"
    INVALID_PROTOCOL = "invalid_protocol"
    INVALID_RUBRIC = "invalid_rubric"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    OUTPUT_DIRECTORY = "output_directory"
    OUTPUT_DIRECTORY_OWNED = "output_directory_owned"
    OUTPUT_OWNERSHIP_UNVERIFIABLE = "output_ownership_unverifiable"
    OUTPUT_SUMMARY_EXISTS = "output_summary_exists"
    DATABASE = "database"
    PDF_PARSE = "pdf_parse"
    PDF_CORRUPT = "pdf_corrupt"
    AUDIT = "audit"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    UNKNOWN = "unknown"


class BatchFailure(BaseModel):
    """Safe result returned by :func:`classify_batch_error`.

    ``summary`` is intentionally bounded and contains no original exception
    text.  Consumers should persist this object, rather than ``str(error)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: BatchErrorScope
    safe_message: str = Field(min_length=1, max_length=500)
    kind: BatchErrorKind = BatchErrorKind.UNKNOWN

    @property
    def summary(self) -> str:
        """Compatibility alias used by report and GUI callers."""

        return self.safe_message

    @property
    def is_shared(self) -> bool:
        return self.scope is BatchErrorScope.SHARED_PAUSE

    @property
    def is_item_failure(self) -> bool:
        return self.scope is BatchErrorScope.ITEM_FAILURE


# These aliases make the intention easy to discover for callers that use
# "failure" rather than "error" terminology.  The enum values remain the
# public wire contract used in batch manifests and events.
BatchFailureScope = BatchErrorScope
BatchErrorClassification = BatchFailure
BatchFailureClassification = BatchFailure


_SUMMARY: Mapping[BatchErrorKind, str] = {
    BatchErrorKind.AUTHENTICATION: "Provider 认证失败；请检查 API Key 和账号权限。",
    BatchErrorKind.AUTHORIZATION: "批次处理授权或非涉密确认无效；请重新创建批次。",
    BatchErrorKind.MISSING_API_KEY: "Provider 尚未配置 API Key；请先完成配置。",
    BatchErrorKind.CREDENTIAL_ACCESS: (
        "无法读取 Provider 凭据；请检查系统凭据存储（Windows 凭据管理器或 macOS 钥匙串）。"
    ),
    BatchErrorKind.INVALID_MODEL: "Provider 不支持所选模型；请检查模型名称和权限。",
    BatchErrorKind.INVALID_PROTOCOL: "Provider 不支持当前接口协议或工具调用格式。",
    BatchErrorKind.INVALID_RUBRIC: "课程 Rubric 配置无效；请修正后重新创建批次。",
    BatchErrorKind.RATE_LIMIT: "Provider 达到速率或额度限制；批次已暂停。",
    BatchErrorKind.NETWORK: "无法连接 Provider 或外部服务；批次已暂停。",
    BatchErrorKind.TIMEOUT: "Provider 或外部服务请求超时；批次已暂停。",
    BatchErrorKind.OUTPUT_DIRECTORY: "报告输出目录不可写或不可用；批次已暂停。",
    BatchErrorKind.OUTPUT_DIRECTORY_OWNED: BATCH_OUTPUT_OWNED_MESSAGE,
    BatchErrorKind.OUTPUT_OWNERSHIP_UNVERIFIABLE: (
        BATCH_OUTPUT_OWNERSHIP_UNVERIFIABLE_MESSAGE
    ),
    BatchErrorKind.OUTPUT_SUMMARY_EXISTS: BATCH_OUTPUT_SUMMARY_EXISTS_MESSAGE,
    BatchErrorKind.DATABASE: "本地数据库连接或写入失败；批次已暂停。",
    BatchErrorKind.PDF_PARSE: "该论文 PDF 无法解析；已记录为单篇失败。",
    BatchErrorKind.PDF_CORRUPT: "该论文 PDF 可能已损坏或不受支持；已记录为单篇失败。",
    BatchErrorKind.AUDIT: "该论文的确定性审计未通过；已记录为单篇失败。",
    BatchErrorKind.INVALID_MODEL_OUTPUT: "该论文未得到可验证的模型输出；已记录为单篇失败。",
    BatchErrorKind.UNKNOWN: "该论文评测失败；已记录为单篇失败。",
}

_AUTHENTICATION_NAMES = {
    "authenticationerror",
    "authorizationerror",
    "credentialserror",
}
_RATE_LIMIT_NAMES = {"ratelimiterror", "tooManyRequestsError".casefold()}
_NETWORK_NAMES = {
    "apiconnectionerror",
    "connectionerror",
    "connecterror",
    "networkerror",
    "connecttimeout",
    "readerror",
    "writeerror",
}
_TIMEOUT_NAMES = {
    "apitimeouterror",
    "timeouterror",
    "readtimeout",
    "writetimeout",
    "connecttimeout",
}
_CREDENTIAL_ACCESS_NAMES = {
    "credentialerror",
    "keyringerror",
    "keyringlocked",
    "nokeyringerror",
    "passwordgeterror",
}
_DATABASE_NAMES = {
    "databaseerror",
    "dbapierror",
    "disconnectionerror",
    "integrityerror",
    "operationalerror",
    "pendingrollbackerror",
    "sqlalchemyerror",
    "statementerror",
    "sanitizeddatabaseerror",
}
_PDF_NAMES = {
    "filedataerror",
    "unsupporteddocumenterror",
    "pdferror",
    "pdfparseerror",
    "documentparseerror",
}


def classify_batch_error(
    error: BaseException,
    *,
    context: str | None = None,
) -> BatchFailure:
    """Classify an exception without retaining its potentially unsafe text.

    ``context`` is a short caller-controlled operation label such as
    ``"pdf"``, ``"audit"``, ``"model_output"`` or ``"output_directory"``.
    It is used only to disambiguate generic exceptions; it is never included
    in the returned summary.

    Unknown exceptions default to ``item_failure``.  A paper-specific error is
    therefore isolated by default, while callers can explicitly label a
    shared setup operation before any paper is run.
    """

    if isinstance(error, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)):
        raise error

    operation = _normalized_context(context)
    error_chain = tuple(_error_chain(error))
    status = _status_code(error_chain)
    names = _names(error_chain)
    safe_codes = _safe_codes(error_chain)
    safe_params = _safe_params(error_chain)

    if any(isinstance(item, BatchOutputOwnedByAnotherBatchError) for item in error_chain):
        return _result(BatchErrorKind.OUTPUT_DIRECTORY_OWNED)
    if any(isinstance(item, BatchOutputOwnershipUnverifiableError) for item in error_chain):
        return _result(BatchErrorKind.OUTPUT_OWNERSHIP_UNVERIFIABLE)
    if any(isinstance(item, BatchOutputSummaryExistsError) for item in error_chain):
        return _result(BatchErrorKind.OUTPUT_SUMMARY_EXISTS)

    # HTTP status is more reliable than a provider's free-form body.  Do not
    # inspect or stringify response bodies: only this integer is retained.
    if status in {401, 403} or names & _AUTHENTICATION_NAMES:
        return _result(BatchErrorKind.AUTHENTICATION)
    if _context_has(operation, "authorization", "cloud authorization", "处理授权"):
        return _result(BatchErrorKind.AUTHORIZATION)
    if status == 429 or names & _RATE_LIMIT_NAMES:
        return _result(BatchErrorKind.RATE_LIMIT, suffix="（HTTP 429）" if status == 429 else "")
    if _is_database_failure(error_chain, operation):
        return _result(BatchErrorKind.DATABASE)
    if status in {400, 404, 422}:
        if _has_protocol_hint(error_chain, operation, safe_codes, safe_params):
            return _result(BatchErrorKind.INVALID_PROTOCOL)
        if _has_model_hint(error_chain, operation, safe_codes, safe_params):
            return _result(BatchErrorKind.INVALID_MODEL)
        # Historically a provider 404/422 meant either a missing model or a
        # provider-specific model route.  Preserve that safe shared-failure
        # fallback, but do not treat every generic HTTP 400 as shared: a 400
        # can also be caused by paper-specific context length or content.
        if status in {404, 422}:
            return _result(BatchErrorKind.INVALID_MODEL)
    if status is not None and status >= 500:
        return _result(BatchErrorKind.NETWORK)

    if names & _TIMEOUT_NAMES or isinstance(error, TimeoutError):
        return _result(BatchErrorKind.TIMEOUT)
    if names & _NETWORK_NAMES:
        return _result(BatchErrorKind.NETWORK)

    if _is_rubric_failure(error_chain, operation, safe_codes):
        return _result(BatchErrorKind.INVALID_RUBRIC)
    if _is_missing_api_key(error_chain, operation, safe_codes):
        return _result(BatchErrorKind.MISSING_API_KEY)
    if _is_credential_access_failure(error_chain, operation):
        return _result(BatchErrorKind.CREDENTIAL_ACCESS)
    if _is_output_directory(error_chain, operation):
        return _result(BatchErrorKind.OUTPUT_DIRECTORY)

    # These are deliberately checked before generic ValueError/ValidationError
    # handling.  Invalid output and an audit failure are scoped to one paper.
    if _is_pdf_failure(error_chain, operation):
        kind = (
            BatchErrorKind.PDF_CORRUPT
            if _looks_corrupt(error_chain)
            else BatchErrorKind.PDF_PARSE
        )
        return _result(kind)
    if _is_audit_failure(error_chain, operation):
        return _result(BatchErrorKind.AUDIT)
    if _is_model_output_failure(error_chain, operation):
        return _result(BatchErrorKind.INVALID_MODEL_OUTPUT)

    if _has_protocol_hint(error_chain, operation, safe_codes, safe_params):
        return _result(BatchErrorKind.INVALID_PROTOCOL)
    if _has_model_hint(error_chain, operation, safe_codes, safe_params):
        return _result(BatchErrorKind.INVALID_MODEL)

    return _result(BatchErrorKind.UNKNOWN)


def classify_error(error: BaseException, *, context: str | None = None) -> BatchFailure:
    """Backward-friendly short alias for :func:`classify_batch_error`."""

    return classify_batch_error(error, context=context)


def sanitize_batch_error_summary(
    error: BaseException,
    *,
    context: str | None = None,
) -> str:
    """Return only the safe, bounded summary for an exception."""

    return classify_batch_error(error, context=context).summary


def _result(kind: BatchErrorKind, *, suffix: str = "") -> BatchFailure:
    scope = (
        BatchErrorScope.SHARED_PAUSE
        if kind
        in {
            BatchErrorKind.AUTHENTICATION,
            BatchErrorKind.AUTHORIZATION,
            BatchErrorKind.MISSING_API_KEY,
            BatchErrorKind.CREDENTIAL_ACCESS,
            BatchErrorKind.INVALID_MODEL,
            BatchErrorKind.INVALID_PROTOCOL,
            BatchErrorKind.INVALID_RUBRIC,
            BatchErrorKind.RATE_LIMIT,
            BatchErrorKind.NETWORK,
            BatchErrorKind.TIMEOUT,
            BatchErrorKind.OUTPUT_DIRECTORY,
            BatchErrorKind.OUTPUT_DIRECTORY_OWNED,
            BatchErrorKind.OUTPUT_OWNERSHIP_UNVERIFIABLE,
            BatchErrorKind.OUTPUT_SUMMARY_EXISTS,
            BatchErrorKind.DATABASE,
        }
        else BatchErrorScope.ITEM_FAILURE
    )
    return BatchFailure(scope=scope, kind=kind, safe_message=_SUMMARY[kind] + suffix)


def _error_chain(error: BaseException, *, limit: int = 4) -> Iterable[BaseException]:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(limit):
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        cause = current.__cause__ or current.__context__
        original = getattr(current, "original_error", None)
        if isinstance(cause, BaseException):
            current = cause
        elif isinstance(original, BaseException):
            # Public orchestrator errors deliberately suppress exception
            # chaining so GUI tracebacks cannot expose provider responses or
            # SQL parameters.  The in-memory original is still safe to inspect
            # here for type/status/code/param classification; none of its text
            # is copied into the persisted BatchFailure.
            current = original
        else:
            current = None


def _names(errors: tuple[BaseException, ...]) -> set[str]:
    return {
        name
        for error in errors
        for name in (type(error).__name__.casefold(),)
    }


def _status_code(errors: tuple[BaseException, ...]) -> int | None:
    for error in errors:
        value = getattr(error, "status_code", None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def _safe_codes(errors: tuple[BaseException, ...]) -> set[str]:
    """Read only short scalar provider codes; never read response bodies."""

    values: set[str] = set()
    for error in errors:
        code = getattr(error, "code", None)
        if isinstance(code, str) and len(code) <= 80:
            values.add(code.casefold())
        body = getattr(error, "body", None)
        if isinstance(body, Mapping):
            nested = body.get("error")
            if isinstance(nested, Mapping):
                code = nested.get("code")
                if isinstance(code, str) and len(code) <= 80:
                    values.add(code.casefold())
    return values


def _safe_params(errors: tuple[BaseException, ...]) -> set[str]:
    """Read bounded scalar parameter names solely for classification.

    Provider SDKs commonly expose a safe field name such as ``model``,
    ``tools`` or ``tool_choice`` even when the response body itself must not
    be retained.  Values are never included in :class:`BatchFailure`.
    """

    values: set[str] = set()
    for error in errors:
        param = getattr(error, "param", None)
        if isinstance(param, str) and len(param) <= 80:
            values.add(param.casefold())
        body = getattr(error, "body", None)
        if isinstance(body, Mapping):
            nested = body.get("error")
            if isinstance(nested, Mapping):
                param = nested.get("param")
                if isinstance(param, str) and len(param) <= 80:
                    values.add(param.casefold())
    return values


def _normalized_context(context: str | None) -> str:
    if not context:
        return ""
    # Context is an internal label, not user content.  Keep matching bounded
    # and avoid ever returning it in an error summary.
    return " ".join(context.casefold().replace("_", " ").split())[:80]


def _is_missing_api_key(
    errors: tuple[BaseException, ...], context: str, codes: set[str]
) -> bool:
    if _context_is(context, "api key", "credential", "credentials", "凭据"):
        return True
    if codes & {"invalid_api_key", "missing_api_key", "authentication_required"}:
        return True
    return any(
        _message_has(
            error,
            (
                "api key is missing",
                "api key is not configured",
                "api key missing",
                "api key not found",
                "missing api key",
                "not configured api key",
                "api key 不存在",
                "未配置 api key",
                "缺少 api key",
                "未设置 api key",
            ),
        )
        for error in errors
    )


def _is_credential_access_failure(
    errors: tuple[BaseException, ...], context: str
) -> bool:
    if _names(errors) & _CREDENTIAL_ACCESS_NAMES:
        return True
    if any(
        type(error).__module__.casefold().startswith(("keyring", "win32cred"))
        for error in errors
    ):
        return True
    if any(
        _message_has(
            error,
            (
                "credential backend",
                "credential manager",
                "keyring backend",
                "keyring is locked",
                "凭据管理器",
            ),
        )
        for error in errors
    ):
        return True
    return _context_is(context, "credential", "credentials", "凭据")


def _is_output_directory(errors: tuple[BaseException, ...], context: str) -> bool:
    output_context = _context_has(
        context, "output directory", "output dir", "输出目录", "报告目录"
    )
    if _context_is(
        context, "output directory", "output dir", "输出目录", "报告目录"
    ):
        return True
    if output_context and any(isinstance(error, OSError) for error in errors):
        return True
    return any(
        _message_has(
            error,
            (
                "output directory",
                "output path is not a directory",
                "report destination",
                "destination directory",
                "输出目录",
                "报告目录",
            ),
        )
        for error in errors
    )


def _is_pdf_failure(errors: tuple[BaseException, ...], context: str) -> bool:
    if _context_has(context, "pdf", "document", "论文"):
        return True
    if _names(errors) & _PDF_NAMES:
        return True
    # The batch source snapshot is read immediately before an item starts and
    # currently has no caller context.  A source that becomes unreadable is a
    # paper-local failure, not evidence that the shared report directory is
    # broken.  Shared output-directory checks always supply explicit context.
    return not context and any(
        isinstance(error, (FileNotFoundError, PermissionError)) for error in errors
    )


def _looks_corrupt(errors: tuple[BaseException, ...]) -> bool:
    return any(
        _message_has(error, ("corrupt", "damaged", "损坏", "malformed", "invalid pdf"))
        for error in errors
    )


def _is_audit_failure(errors: tuple[BaseException, ...], context: str) -> bool:
    if "audit" in context or "审计" in context:
        return True
    return any(
        _message_has(error, ("audit failed", "审计失败", "deterministic review audit"))
        for error in errors
    )


def _is_model_output_failure(errors: tuple[BaseException, ...], context: str) -> bool:
    if any(
        token in context
        for token in ("model output", "output validation", "agent output", "模型输出")
    ):
        return True
    return any(
        _message_has(
            error,
            (
                "invalid tool argument",
                "invalid model output",
                "agent did not produce valid output",
                "structured output",
                "tool arguments",
                "output validation",
                "模型输出",
            ),
        )
        for error in errors
    )


def _is_rubric_failure(errors: tuple[BaseException, ...], context: str, codes: set[str]) -> bool:
    if _context_is(context, "rubric", "评分规则", "评分配置"):
        return True
    if codes & {"invalid_rubric", "rubric_validation_error"}:
        return True
    if any(_message_has(error, ("rubric", "评分规则", "评分配置")) for error in errors):
        return True
    validation_names = {
        "markedyamlerror",
        "parsererror",
        "scannererror",
        "validationerror",
    }
    return _context_has(context, "rubric", "评分规则", "评分配置") and bool(
        _names(errors) & validation_names
    )


def _has_protocol_hint(
    errors: tuple[BaseException, ...],
    context: str,
    codes: set[str],
    params: set[str],
) -> bool:
    return bool(
        codes
        & {
            "function_calling_not_supported",
            "invalid_endpoint",
            "unsupported_protocol",
            "unsupported_endpoint",
            "unsupported_parameter",
            "unsupported_tool",
            "unsupported_tool_choice",
            "invalid_protocol",
        }
    ) or bool(
        params
        & {
            "function_call",
            "function_calling",
            "functions",
            "tool_choice",
            "tools",
        }
    ) or _context_has(
        context,
        "protocol",
        "endpoint",
        "tool calling",
        "function calling",
        "协议",
        "端点",
        "工具调用",
        "函数调用",
    ) or any(
        _message_has(
            error,
            (
                "unsupported protocol",
                "invalid protocol",
                "unsupported endpoint",
                "endpoint not found",
                "does not support tool calling",
                "does not support tools",
                "function calling is not supported",
                "function calling not supported",
                "tool calling is not supported",
                "tool calling not supported",
                "tools are not supported",
                "unsupported tool_choice",
                "unsupported tool choice",
                "provider snapshot must match",
                "不支持工具调用",
                "不支持函数调用",
            ),
        )
        for error in errors
    )


def _has_model_hint(
    errors: tuple[BaseException, ...],
    context: str,
    codes: set[str],
    params: set[str],
) -> bool:
    return bool(codes & {"model_not_found", "invalid_model", "model_not_supported"}) or bool(
        params & {"model"}
    ) or _context_has(context, "model", "模型") or any(
        _message_has(error, ("model not found", "invalid model", "model not supported"))
        for error in errors
    )


def _is_database_failure(errors: tuple[BaseException, ...], context: str) -> bool:
    if _context_has(
        context,
        "database",
        "sqlite",
        "sqlalchemy",
        "repository",
        "persistence",
        "db connection",
        "db write",
        "数据库",
    ):
        return True
    if _names(errors) & _DATABASE_NAMES:
        return True
    return any(
        type(error).__module__.casefold().startswith(("sqlite3", "aiosqlite", "sqlalchemy"))
        for error in errors
    )


def _context_is(context: str, *labels: str) -> bool:
    return context in {label.casefold().replace("_", " ") for label in labels}


def _context_has(context: str, *labels: str) -> bool:
    return any(label.casefold().replace("_", " ") in context for label in labels)


def _message_has(error: BaseException, needles: tuple[str, ...]) -> bool:
    """Use exception text only for classification; never return it."""

    try:
        message = str(error).casefold()
    except Exception:
        return False
    return any(needle.casefold() in message for needle in needles)


__all__ = [
    "BatchErrorClassification",
    "BatchErrorKind",
    "BatchErrorScope",
    "BatchFailure",
    "BatchFailureClassification",
    "BatchFailureScope",
    "classify_batch_error",
    "classify_error",
    "sanitize_batch_error_summary",
]
