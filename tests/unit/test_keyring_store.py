from __future__ import annotations

from pytest import MonkeyPatch

from paper_reviewer.adapters.security import keyring_store


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
