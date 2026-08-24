from __future__ import annotations

import os
import secrets
import uuid
from hmac import compare_digest
from typing import ClassVar

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


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

    def get(self, provider: str) -> str | None:
        normalized = provider.lower()
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            return None
        try:
            stored = keyring.get_password(self.SERVICE_NAME, account)
        except KeyringError:
            stored = None
        return stored or os.environ.get(self.ENVIRONMENTS[normalized])

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def set(self, provider: str, secret: str) -> None:
        normalized = provider.lower()
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            raise ValueError(f"unsupported credential provider: {provider}")
        if not secret.strip():
            raise ValueError("API Key 不能为空")
        keyring.set_password(self.SERVICE_NAME, account, secret.strip())

    def delete(self, provider: str) -> None:
        normalized = provider.lower()
        account = self.ACCOUNTS.get(normalized)
        if account is None:
            raise ValueError(f"unsupported credential provider: {provider}")
        try:
            keyring.delete_password(self.SERVICE_NAME, account)
        except PasswordDeleteError:
            return

    @classmethod
    def self_test(cls) -> None:
        """Verify the active keyring with a short-lived, randomly named credential."""
        service = f"{cls.SERVICE_NAME}.SelfTest.{uuid.uuid4().hex}"
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
