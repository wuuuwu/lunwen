from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PROVIDER_ID = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ModelApiProtocol(StrEnum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


def normalize_base_url(value: str) -> str:
    """Validate and canonicalize an OpenAI-compatible API root URL."""

    candidate = value.strip()
    if not candidate or candidate != value or _CONTROL_CHARACTER.search(candidate):
        raise ValueError("Base URL 不能为空，也不能包含空白或控制字符")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Base URL 包含非法主机或端口") from error
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Base URL 必须使用 HTTP 或 HTTPS")
    if not parsed.hostname:
        raise ValueError("Base URL 必须包含主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Base URL 不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不得包含查询参数或片段")
    hostname = parsed.hostname.lower()
    if parsed.scheme == "http" and hostname not in _LOOPBACK_HOSTS:
        raise ValueError("远程 Base URL 必须使用 HTTPS；HTTP 仅允许本机回环地址")
    path = parsed.path.rstrip("/")
    lowered_path = path.lower()
    if lowered_path.endswith("/responses") or lowered_path.endswith("/chat/completions"):
        raise ValueError("Base URL 必须是 API 根路径，不能包含具体接口路径")
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def endpoint_fingerprint(base_url: str, protocol: ModelApiProtocol) -> str:
    normalized = normalize_base_url(base_url)
    material = f"{protocol.value}\n{normalized}".encode()
    return hashlib.sha256(material).hexdigest()


def custom_provider_ref(provider_id: str) -> str:
    if not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError("provider_id 必须是 32 位小写十六进制 UUID")
    return f"custom:{provider_id}"


class CustomProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    display_name: str = Field(min_length=1, max_length=80)
    protocol: ModelApiProtocol
    base_url: str
    default_model: str = Field(min_length=1, max_length=160)
    archived_at: datetime | None = None

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if not _PROVIDER_ID.fullmatch(value):
            raise ValueError("provider_id 必须是 32 位小写十六进制 UUID")
        return value

    @field_validator("display_name", "default_model")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or _CONTROL_CHARACTER.search(stripped):
            raise ValueError("字段不能为空或包含控制字符")
        return stripped

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @property
    def provider_ref(self) -> str:
        return custom_provider_ref(self.provider_id)

    @property
    def endpoint_fingerprint(self) -> str:
        return endpoint_fingerprint(self.base_url, self.protocol)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class ProviderSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_ref: str
    display_name: str = Field(min_length=1, max_length=80)
    protocol: ModelApiProtocol
    base_url: str
    endpoint_fingerprint: str
    model: str = Field(min_length=1, max_length=160)

    @field_validator("provider_ref")
    @classmethod
    def validate_provider_ref(cls, value: str) -> str:
        if value in {"openai", "openai_responses", "deepseek"}:
            return value
        if value.startswith("custom:"):
            custom_provider_ref(value.removeprefix("custom:"))
            return value
        raise ValueError("Provider 引用格式无效")

    @field_validator("display_name", "model")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or _CONTROL_CHARACTER.search(stripped):
            raise ValueError("字段不能为空或包含控制字符")
        return stripped

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_base_url(value)

    @field_validator("endpoint_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if not _FINGERPRINT.fullmatch(value):
            raise ValueError("端点指纹格式无效")
        return value

    @model_validator(mode="after")
    def fingerprint_matches_endpoint(self) -> ProviderSnapshot:
        expected = endpoint_fingerprint(self.base_url, self.protocol)
        if self.endpoint_fingerprint != expected:
            raise ValueError("端点指纹与 Base URL 或协议不匹配")
        return self


class ProviderConnection(BaseModel):
    """Resolved, non-secret connection settings for an adapter factory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_ref: str
    display_name: str
    protocol: ModelApiProtocol
    base_url: str
    default_model: str
    endpoint_fingerprint: str
    custom: bool = False
