from __future__ import annotations

from pytest import MonkeyPatch

from paper_reviewer.adapters.security import keyring_store
from paper_reviewer.domain.provider import (
    CustomProviderProfile,
    ModelApiProtocol,
    ProviderSnapshot,
)


def test_credential_self_test_writes_reads_and_deletes(monkeypatch: MonkeyPatch) -> None:
    stored: dict[tuple[str, str], str] = {}

    def set_password(service: str, account: str, secret: str) -> None:
        stored[(service, account)] = secret

    def get_password(service: str, account: str) -> str | None:
        return stored.get((service, account))

    def delete_password(service: str, account: str) -> None:
        del stored[(service, account)]

    monkeypatch.setattr(keyring_store.keyring, "set_password", set_password)
    monkeypatch.setattr(keyring_store.keyring, "get_password", get_password)
    monkeypatch.setattr(keyring_store.keyring, "delete_password", delete_password)

    keyring_store.SystemCredentialStore.self_test()

    assert stored == {}


def test_openai_responses_shares_openai_credential(monkeypatch: MonkeyPatch) -> None:
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring_store.keyring,
        "set_password",
        lambda service, account, secret: stored.__setitem__((service, account), secret),
    )
    monkeypatch.setattr(
        keyring_store.keyring,
        "get_password",
        lambda service, account: stored.get((service, account)),
    )
    credentials = keyring_store.SystemCredentialStore()

    credentials.set("openai_responses", "shared-secret")

    assert credentials.get("openai") == "shared-secret"
    assert credentials.get("openai_responses") == "shared-secret"


def test_custom_credentials_are_isolated_by_protocol_and_endpoint(
    monkeypatch: MonkeyPatch,
) -> None:
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring_store.keyring,
        "set_password",
        lambda service, account, secret: stored.__setitem__((service, account), secret),
    )
    monkeypatch.setattr(
        keyring_store.keyring,
        "get_password",
        lambda service, account: stored.get((service, account)),
    )
    credentials = keyring_store.SystemCredentialStore()
    first = CustomProviderProfile(
        provider_id="a" * 32,
        display_name="first",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url="https://one.example/v1",
        default_model="model",
    )
    changed_protocol = CustomProviderProfile(
        provider_id=first.provider_id,
        display_name="second",
        protocol=ModelApiProtocol.RESPONSES,
        base_url=first.base_url,
        default_model="model",
    )
    changed_endpoint = CustomProviderProfile(
        provider_id=first.provider_id,
        display_name="third",
        protocol=first.protocol,
        base_url="https://two.example/v1",
        default_model="model",
    )

    credentials.set_custom(first, "only-first")

    assert credentials.get_custom(first) == "only-first"
    assert credentials.get_custom(changed_protocol) is None
    assert credentials.get_custom(changed_endpoint) is None
    snapshot = ProviderSnapshot(
        provider_ref=first.provider_ref,
        display_name=first.display_name,
        protocol=first.protocol,
        base_url=first.base_url,
        endpoint_fingerprint=first.endpoint_fingerprint,
        model=first.default_model,
    )
    assert credentials.get_custom_for_snapshot(snapshot) == "only-first"
