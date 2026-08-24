from __future__ import annotations

import pytest

from paper_reviewer.adapters.models.factory import create_model_adapter
from paper_reviewer.adapters.models.openai_compatible import OpenAICompatibleAdapter
from paper_reviewer.adapters.models.openai_responses import OpenAIResponsesAdapter
from paper_reviewer.application.orchestrator import _run_config_hash
from paper_reviewer.application.service import ReviewApplicationService, _sanitize_provider_error
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
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="paper_reviewer_compatibility_probe",
                        arguments={"ok": True},
                    )
                ]
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
    assert adapter.calls == 1
    assert adapter.closed


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
