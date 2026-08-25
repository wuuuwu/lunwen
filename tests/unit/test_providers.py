from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_reviewer.application.providers import (
    CustomProviderRegistry,
    ProviderCatalog,
    ProviderCatalogError,
    ProviderStore,
    validate_provider_snapshot_identity,
)
from paper_reviewer.domain.provider import (
    CustomProviderProfile,
    ModelApiProtocol,
    ProviderSnapshot,
    endpoint_fingerprint,
    normalize_base_url,
)


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _key(profile: CustomProviderProfile) -> tuple[str, str, str]:
        return profile.provider_id, profile.protocol.value, profile.endpoint_fingerprint

    def set_custom(self, profile: CustomProviderProfile, secret: str) -> None:
        if not secret.strip():
            raise ValueError("empty")
        self.values[self._key(profile)] = secret.strip()

    def get_custom(self, profile: CustomProviderProfile) -> str | None:
        return self.values.get(self._key(profile))

    def has_custom(self, profile: CustomProviderProfile) -> bool:
        return self.get_custom(profile) is not None

    def get_custom_for_snapshot(self, snapshot: ProviderSnapshot) -> str | None:
        provider_id = snapshot.provider_ref.removeprefix("custom:")
        return self.values.get(
            (provider_id, snapshot.protocol.value, snapshot.endpoint_fingerprint)
        )

    def delete_custom(self, profile: CustomProviderProfile) -> None:
        self.values.pop(self._key(profile), None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM/v1/", "https://example.com/v1"),
        ("http://localhost:9000/v1", "http://localhost:9000/v1"),
        ("http://127.0.0.1/v1", "http://127.0.0.1/v1"),
        ("http://[::1]:8080/v1/", "http://[::1]:8080/v1"),
    ],
)
def test_normalize_base_url_accepts_https_and_strict_loopback(
    raw: str, expected: str
) -> None:
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "http://localhost.example.com/v1",
        "https://user:secret@example.com/v1",
        "https://example.com/v1?token=secret",
        "https://example.com/v1#fragment",
        "https://example.com/v1/responses",
        "https://example.com/v1/chat/completions",
        " https://example.com/v1",
        "ftp://example.com/v1",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_endpoint_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(url)


def test_snapshot_rejects_mismatched_endpoint_fingerprint() -> None:
    with pytest.raises(ValidationError, match="端点指纹"):
        ProviderSnapshot(
            provider_ref="openai_responses",
            display_name="OpenAI",
            protocol=ModelApiProtocol.RESPONSES,
            base_url="https://api.openai.com/v1",
            endpoint_fingerprint="0" * 64,
            model="gpt-5-mini",
        )


def test_snapshot_identity_rejects_provider_or_model_changes() -> None:
    base_url = "https://api.openai.com/v1"
    snapshot = ProviderSnapshot(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url=base_url,
        endpoint_fingerprint=endpoint_fingerprint(
            base_url, ModelApiProtocol.CHAT_COMPLETIONS
        ),
        model="recorded-model",
    )

    validate_provider_snapshot_identity("openai", "recorded-model", snapshot)
    with pytest.raises(ValueError, match="Provider 或模型不一致"):
        validate_provider_snapshot_identity("deepseek", "recorded-model", snapshot)
    with pytest.raises(ValueError, match="Provider 或模型不一致"):
        validate_provider_snapshot_identity("openai", "different-model", snapshot)


def test_store_rejects_corruption_without_overwriting_it(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    original = "{broken"
    path.write_text(original, encoding="utf-8")
    store = ProviderStore(path)

    with pytest.raises(ProviderCatalogError):
        store.load()

    assert path.read_text(encoding="utf-8") == original


def test_registry_lifecycle_and_endpoint_replacement(tmp_path: Path) -> None:
    credentials = MemoryCredentials()
    referenced: set[str] = set()
    registry = CustomProviderRegistry(  # type: ignore[arg-type]
        ProviderStore(tmp_path / "providers.json"),
        credentials,
        is_provider_referenced=lambda provider_ref: provider_ref in referenced,
    )
    original = registry.create(
        display_name="校内模型",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url="https://model.example.edu/v1",
        default_model="reviewer-v1",
        api_key="secret-one",
    )
    assert registry.has_key(original.provider_ref)
    assert registry.resolve(original.provider_ref).custom
    snapshot = registry.snapshot(original.provider_ref, "reviewer-special")
    assert snapshot.model == "reviewer-special"
    assert snapshot.endpoint_fingerprint == endpoint_fingerprint(
        original.base_url, original.protocol
    )
    assert registry.resolve_snapshot(snapshot).default_model == "reviewer-special"
    assert registry.get_snapshot_api_key(snapshot) == "secret-one"

    updated = registry.update(
        original.provider_ref, display_name="校内评测", default_model="reviewer-v2"
    )
    assert updated.provider_id == original.provider_id
    registry.rotate_key(updated.provider_ref, "secret-two")
    assert credentials.get_custom(updated) == "secret-two"

    replacement = registry.replace_endpoint(
        updated.provider_ref,
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://responses.example.edu/v1",
        api_key="secret-three",
    )
    assert replacement.provider_id != original.provider_id
    assert registry.get(original.provider_ref).is_archived
    assert credentials.get_custom(replacement) == "secret-three"
    assert credentials.get_custom(original) == "secret-two"
    assert registry.get_snapshot_api_key(snapshot) == "secret-two"

    with pytest.raises(ValueError, match="名称不能重复"):
        registry.restore(original.provider_ref)

    archived = registry.archive(replacement.provider_ref)
    referenced.add(archived.provider_ref)
    with pytest.raises(ValueError, match="历史任务"):
        registry.delete(archived.provider_ref)
    referenced.clear()
    registry.delete(archived.provider_ref)
    assert credentials.get_custom(archived) is None


def test_active_provider_names_are_case_insensitively_unique(tmp_path: Path) -> None:
    registry = CustomProviderRegistry(  # type: ignore[arg-type]
        ProviderStore(tmp_path / "providers.json"), MemoryCredentials()
    )
    registry.create(
        display_name="Local Model",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="http://localhost:8080/v1",
        default_model="local",
        api_key="one",
    )
    with pytest.raises(ValueError, match="名称不能重复"):
        registry.create(
            display_name="local model",
            protocol=ModelApiProtocol.RESPONSES,
            base_url="http://localhost:8081/v1",
            default_model="local",
            api_key="two",
        )


def test_builtin_responses_connection_uses_distinct_protocol(tmp_path: Path) -> None:
    registry = CustomProviderRegistry(  # type: ignore[arg-type]
        ProviderStore(tmp_path / "providers.json"), MemoryCredentials()
    )
    chat = registry.resolve("openai")
    responses = registry.resolve("openai_responses")

    assert chat.protocol is ModelApiProtocol.CHAT_COMPLETIONS
    assert responses.protocol is ModelApiProtocol.RESPONSES
    assert chat.display_name == "OpenAI"
    assert responses.display_name == "OpenAI"
    assert registry.resolve("deepseek").display_name == "DeepSeek"
    assert chat.endpoint_fingerprint != responses.endpoint_fingerprint


def test_snapshot_can_recover_key_without_current_catalog_profile(tmp_path: Path) -> None:
    store = ProviderStore(tmp_path / "providers.json")
    credentials = MemoryCredentials()
    registry = CustomProviderRegistry(store, credentials)  # type: ignore[arg-type]
    profile = registry.create(
        display_name="Snapshot only",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://snapshot.example/v1",
        default_model="model",
        api_key="snapshot-secret",
    )
    snapshot = registry.snapshot(profile.provider_ref, "model")
    store.save(ProviderCatalog())

    assert registry.get_snapshot_api_key(snapshot) == "snapshot-secret"
