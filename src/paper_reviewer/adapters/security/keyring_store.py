from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Mapping
from hmac import compare_digest
from typing import ClassVar

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from paper_reviewer.domain.provider import (
    CustomProviderProfile,
    ModelApiProtocol,
    ProviderSnapshot,
    custom_provider_ref,
)


class SystemCredentialStore:
    SERVICE_NAME = "PaperReviewer"
    ACCOUNTS: ClassVar[dict[str, str]] = {
        "openai": "openai_api_key",
        "deepseek": "deepseek_api_key",
    }
    ENVIRONMENTS: ClassVar[dict[str, str]] = {
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }

    def __init__(
        self,
        *,
        service_name: str | None = None,
        environments: Mapping[str, str] | None = None,
    ) -> None:
        self.service_name = service_name or self.SERVICE_NAME
        self.environments = dict(
            self.ENVIRONMENTS if environments is None else environments
        )

    def get(self, provider: str) -> str | None:
        normalized = provider.lower()
        if normalized == "openai_responses":
            normalized = "openai"
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            return None
        try:
            stored = keyring.get_password(self.service_name, account)
        except KeyringError:
            stored = None
        environment_name = self.environments.get(normalized)
        return stored or (os.environ.get(environment_name) if environment_name else None)

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def set(self, provider: str, secret: str) -> None:
        normalized = provider.lower()
        if normalized == "openai_responses":
            normalized = "openai"
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            raise ValueError(f"unsupported credential provider: {provider}")
        if not secret.strip():
            raise ValueError("API Key 不能为空")
        keyring.set_password(self.service_name, account, secret.strip())

    def delete(self, provider: str) -> None:
        normalized = provider.lower()
        if normalized == "openai_responses":
            normalized = "openai"
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            raise ValueError(f"unsupported credential provider: {provider}")
        try:
            keyring.delete_password(self.service_name, account)
        except PasswordDeleteError:
            return

    def get_custom(self, profile: CustomProviderProfile) -> str | None:
        return self._get_custom(
            profile.provider_id, profile.protocol, profile.endpoint_fingerprint
        )

    def has_custom(self, profile: CustomProviderProfile) -> bool:
        return bool(self.get_custom(profile))

    def get_custom_for_snapshot(self, snapshot: ProviderSnapshot) -> str | None:
        if not snapshot.provider_ref.startswith("custom:"):
            raise ValueError("快照不是自定义 Provider")
        provider_id = snapshot.provider_ref.removeprefix("custom:")
        custom_provider_ref(provider_id)
        return self._get_custom(
            provider_id, snapshot.protocol, snapshot.endpoint_fingerprint
        )

    def has_custom_for_snapshot(self, snapshot: ProviderSnapshot) -> bool:
        return bool(self.get_custom_for_snapshot(snapshot))

    def set_custom(self, profile: CustomProviderProfile, secret: str) -> None:
        if not secret.strip():
            raise ValueError("API Key 不能为空")
        keyring.set_password(
            self.service_name,
            self._custom_account(
                profile.provider_id, profile.protocol, profile.endpoint_fingerprint
            ),
            secret.strip(),
        )

    def delete_custom(self, profile: CustomProviderProfile) -> None:
        account = self._custom_account(
            profile.provider_id, profile.protocol, profile.endpoint_fingerprint
        )
        try:
            keyring.delete_password(self.service_name, account)
        except PasswordDeleteError:
            return

    def _get_custom(
        self,
        provider_id: str,
        protocol: ModelApiProtocol,
        endpoint_fingerprint: str,
    ) -> str | None:
        account = self._custom_account(provider_id, protocol, endpoint_fingerprint)
        try:
            return keyring.get_password(self.service_name, account)
        except KeyringError:
            return None

    @staticmethod
    def _custom_account(
        provider_id: str,
        protocol: ModelApiProtocol,
        endpoint_fingerprint: str,
    ) -> str:
        # Revalidate all identity components before they can select a credential.
        profile = CustomProviderProfile(
            provider_id=provider_id,
            display_name="credential",
            protocol=protocol,
            base_url="https://credential.invalid",
            default_model="credential",
        )
        if len(endpoint_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in endpoint_fingerprint
        ):
            raise ValueError("端点指纹格式无效")
        return f"custom:{profile.provider_id}:{protocol.value}:{endpoint_fingerprint}"

    @classmethod
    def self_test(cls, *, service_name: str | None = None) -> None:
        """Verify the active keyring with a short-lived, randomly named credential."""
        service = f"{service_name or cls.SERVICE_NAME}.SelfTest.{uuid.uuid4().hex}"
        account = "temporary_probe"
        secret = secrets.token_urlsafe(32)
        credential_exists = False
        try:
            keyring.set_password(service, account, secret)
            credential_exists = True
            stored = keyring.get_password(service, account)
            if stored is None or not compare_digest(stored, secret):
                raise KeyringError("系统凭据库读取校验失败")
            keyring.delete_password(service, account)
            credential_exists = False
            if keyring.get_password(service, account) is not None:
                raise KeyringError("系统凭据库删除校验失败")
        finally:
            if credential_exists:
                try:
                    keyring.delete_password(service, account)
                except KeyringError:
                    pass
