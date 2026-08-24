from __future__ import annotations

import pytest

from paper_reviewer.adapters.models.factory import create_model_adapter
from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter
from paper_reviewer.adapters.models.openai_responses import OpenAIResponsesAdapter
from paper_reviewer.application.orchestrator import _run_config_hash
from paper_reviewer.application.service import (
    ReviewApplicationService,
    _extract_provider_error_details,
    _provider_response_diagnostics,
    _sanitize_provider_error,
)
from paper_reviewer.config import ReviewerProfile, ReviewProfile
from paper_reviewer.domain.provider import (
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall


def _snapshot(*, protocol: ModelApiProtocol, model: str) -> ProviderSnapshot:
    base_url = "https://api.openai.com/v1"
    return ProviderSnapshot(
        provider_ref="openai_responses" if protocol is ModelApiProtocol.RESPONSES else "openai",
        display_name="OpenAI",
        protocol=protocol,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(base_url, protocol),
        model=model,
    )


def test_factory_selects_adapter_from_snapshot_protocol() -> None:
    chat = create_model_adapter(
        "openai",
        "gpt-test",
        api_key="secret",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url="https://api.openai.com/v1",
    )
    responses = create_model_adapter(
        "openai_responses",
        "gpt-test",
        api_key="secret",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://api.openai.com/v1",
    )

    assert isinstance(chat, OpenAICompatibleAdapter)
    assert isinstance(responses, OpenAIResponsesAdapter)
    assert responses.include_encrypted_reasoning is True


def test_factory_omits_openai_only_include_for_custom_responses_provider() -> None:
    responses = create_model_adapter(
        "custom:" + "a" * 32,
        "compatible-model",
        api_key="secret",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://compatible.example/v1",
    )

    assert isinstance(responses, OpenAIResponsesAdapter)
    assert responses.include_encrypted_reasoning is False


def test_factory_requires_full_custom_snapshot_connection() -> None:
    try:
        create_model_adapter("custom:" + "a" * 32, "model", api_key="secret")
    except ValueError as error:
        assert "protocol and Base URL snapshot" in str(error)
    else:
        raise AssertionError("custom provider without snapshot connection was accepted")


def test_config_hash_covers_provider_protocol_endpoint_and_model() -> None:
    rubric = RubricProfile(
        rubric_id="unscored", version="1", title="Unscored", scoring_enabled=False
    )
    profile = ReviewProfile(
        profile_id="one",
        version="1",
        reviewers=[
            ReviewerProfile(
                reviewer_id="reviewer",
                title="Reviewer",
                description="Review",
                allowed_tools=[],
            )
        ],
    )

    def calculate(snapshot: ProviderSnapshot) -> str:
        return _run_config_hash(
            rubric=rubric,
            profile=profile,
            panel_profile=None,
            discipline_name="test",
            discipline_profile=None,
            external_search=False,
            provider_snapshot=snapshot,
        )

    chat = _snapshot(protocol=ModelApiProtocol.CHAT_COMPLETIONS, model="gpt-test")
    responses = _snapshot(protocol=ModelApiProtocol.RESPONSES, model="gpt-test")
    another_model = _snapshot(protocol=ModelApiProtocol.RESPONSES, model="gpt-other")
    custom_a = ProviderSnapshot(
        provider_ref="custom:" + "a" * 32,
        display_name="Custom",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://one.example/v1",
        endpoint_fingerprint=endpoint_fingerprint(
            "https://one.example/v1", ModelApiProtocol.RESPONSES
        ),
        model="gpt-test",
    )
    custom_b = custom_a.model_copy(
        update={
            "provider_ref": "custom:" + "b" * 32,
            "base_url": "https://two.example/v1",
            "endpoint_fingerprint": endpoint_fingerprint(
                "https://two.example/v1", ModelApiProtocol.RESPONSES
            ),
        }
    )

    assert calculate(chat) != calculate(responses)
    assert calculate(responses) != calculate(another_model)
    assert calculate(custom_a) != calculate(custom_b)


@pytest.mark.asyncio
async def test_transient_compatibility_probe_is_forced_and_called_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdapter:
        calls = 0
        closed = False

        async def complete_once(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            assert request.forced_tool_name == "paper_reviewer_compatibility_probe"
            assert request.max_output_tokens == 1024
            return ModelResponse(
                content="private model output must not enter compatibility result",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="paper_reviewer_compatibility_probe",
                        arguments={"ok": True},
                    )
                ],
                response_status="completed",
                finish_reason="tool_calls",
                output_item_types=["reasoning", "function_call"],
            )

        async def close(self) -> None:
            self.closed = True

    adapter = FakeAdapter()
    monkeypatch.setattr(
        "paper_reviewer.application.service.create_model_adapter",
        lambda *args, **kwargs: adapter,
    )
    service = ReviewApplicationService.__new__(ReviewApplicationService)

    result = await service.test_provider_compatibility(
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://example.test/v1",
        model="gpt-test",
        api_key="never-persist-this-key",
    )

    assert result.compatible
    assert result.response_diagnostics is not None
    assert result.response_diagnostics.response_status == "completed"
    assert result.response_diagnostics.finish_reason == "tool_calls"
    assert result.response_diagnostics.output_item_types == ["reasoning", "function_call"]
    assert "private model output" not in result.model_dump_json()
    assert adapter.calls == 1
    assert adapter.closed


def test_response_diagnostics_reject_untrusted_metadata_and_content() -> None:
    response = ModelResponse(
        content="student paper text and secret upstream response",
        response_status="Bearer secret-token",
        incomplete_reason="https://private.example/reason",
        finish_reason="sk-provider-secret-123456",
        output_item_types=["message", "sk-provider-secret-123456", "message"] * 8,
        plain_text_only=True,
    )

    diagnostics = _provider_response_diagnostics(response)

    assert diagnostics.response_status == "unknown"
    assert diagnostics.incomplete_reason == "unknown"
    assert diagnostics.finish_reason == "unknown"
    assert diagnostics.output_item_types == ["message", "unknown"]
    assert diagnostics.plain_text_only is True
    serialized = diagnostics.model_dump_json()
    assert "student paper" not in serialized
    assert "secret" not in serialized
    assert "private.example" not in serialized


def test_provider_error_sanitizer_removes_credentials_and_urls() -> None:
    message = _sanitize_provider_error(
        RuntimeError(
            "Authorization: Bearer sk-secret-value-123456 at "
            "https://private.example/v1/responses"
        )
    )

    assert "sk-secret" not in message
    assert "private.example" not in message
    assert "<redacted>" in message


def test_provider_error_sanitizer_never_includes_sdk_response_body() -> None:
    error_type = type(
        "AuthenticationError",
        (RuntimeError,),
        {"__module__": "openai", "status_code": 401},
    )
    message = _sanitize_provider_error(
        error_type("raw upstream body containing student-data-and-secret")
    )

    assert "认证失败" in message
    assert "student-data" not in message
    assert "secret" not in message


def test_provider_error_details_whitelist_and_redact_safe_fields() -> None:
    error_type = type(
        "BadRequestError",
        (RuntimeError,),
        {
            "__module__": "openai",
            "status_code": 400,
            "body": {
                "error": {
                    "message": (
                        "Unsupported tool_choice for sk-provider-secret-12345 at "
                        "https://private.example/v1/chat/completions"
                    ),
                    "code": "unsupported_parameter",
                    "param": "tool_choice",
                    "request": {"authorization": "Bearer do-not-display"},
                },
                "upstream_raw_body": "must-not-display",
            },
        },
    )

    details = _extract_provider_error_details(
        error_type("raw body must not be stringified"),
        secrets=("sk-provider-secret-12345",),
    )

    assert details is not None
    assert details.message == (
        "Unsupported tool_choice for [API Key 已隐藏] at [Provider URL 已隐藏]"
    )
    assert details.code == "unsupported_parameter"
    assert details.param == "tool_choice"
    serialized = details.model_dump_json()
    assert "authorization" not in serialized
    assert "do-not-display" not in serialized
    assert "must-not-display" not in serialized


def test_provider_error_details_ignore_non_scalar_and_unstructured_body() -> None:
    error_type = type(
        "BadRequestError",
        (RuntimeError,),
        {
            "__module__": "openai",
            "status_code": 400,
            "body": {
                "error": {
                    "message": {"raw": "do-not-display"},
                    "code": ["nested", "value"],
                    "param": None,
                }
            },
        },
    )

    assert _extract_provider_error_details(error_type("unsafe raw body")) is None
