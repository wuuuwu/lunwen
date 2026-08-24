from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.domain.provider import (
    CustomProviderProfile,
    ModelApiProtocol,
    ProviderConnection,
    ProviderSnapshot,
    custom_provider_ref,
    endpoint_fingerprint,
)


class ProviderCatalogError(ValueError):
    pass


class ProviderCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    providers: list[CustomProviderProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_entries(self) -> ProviderCatalog:
        ids = [item.provider_id for item in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("自定义 Provider ID 重复")
        active_names = [
            item.display_name.casefold() for item in self.providers if not item.is_archived
        ]
        if len(active_names) != len(set(active_names)):
            raise ValueError("活动的自定义 Provider 名称重复")
        return self


class ProviderStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> ProviderCatalog:
        if not self.path.exists():
            return ProviderCatalog()
        try:
            return ProviderCatalog.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise ProviderCatalogError(f"无法读取 Provider 配置：{error}") from error

    def save(self, catalog: ProviderCatalog) -> None:
        validated = ProviderCatalog.model_validate(catalog.model_dump(mode="python"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(validated.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


_BUILTIN_CONNECTIONS: dict[str, ProviderConnection] = {
    "openai": ProviderConnection(
        provider_ref="openai",
        display_name="OpenAI",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url="https://api.openai.com/v1",
        endpoint_fingerprint=endpoint_fingerprint(
            "https://api.openai.com/v1", ModelApiProtocol.CHAT_COMPLETIONS
        ),
        default_model="gpt-5-mini",
    ),
    "openai_responses": ProviderConnection(
        provider_ref="openai_responses",
        display_name="OpenAI",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://api.openai.com/v1",
        endpoint_fingerprint=endpoint_fingerprint(
            "https://api.openai.com/v1", ModelApiProtocol.RESPONSES
        ),
        default_model="gpt-5-mini",
    ),
    "deepseek": ProviderConnection(
        provider_ref="deepseek",
        display_name="DeepSeek",
        protocol=ModelApiProtocol.CHAT_COMPLETIONS,
        base_url="https://api.deepseek.com",
        endpoint_fingerprint=endpoint_fingerprint(
            "https://api.deepseek.com", ModelApiProtocol.CHAT_COMPLETIONS
        ),
        default_model="deepseek-chat",
    ),
}


class CustomProviderRegistry:
    def __init__(
        self,
        store: ProviderStore,
        credentials: SystemCredentialStore,
        *,
        is_provider_referenced: Callable[[str], bool] | None = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self.is_provider_referenced = is_provider_referenced or (lambda _provider_ref: False)

    def list(self, *, include_archived: bool = False) -> list[CustomProviderProfile]:
        providers = self.store.load().providers
        if not include_archived:
            providers = [item for item in providers if not item.is_archived]
        return sorted(providers, key=lambda item: item.display_name.casefold())

    def get(self, provider_ref: str) -> CustomProviderProfile:
        provider_id = self._parse_ref(provider_ref)
        for profile in self.store.load().providers:
            if profile.provider_id == provider_id:
                return profile
        raise KeyError(f"找不到自定义 Provider：{provider_ref}")

    def create(
        self,
        *,
        display_name: str,
        protocol: ModelApiProtocol,
        base_url: str,
        default_model: str,
        api_key: str,
    ) -> CustomProviderProfile:
        catalog = self.store.load()
        profile = CustomProviderProfile(
            provider_id=uuid.uuid4().hex,
            display_name=display_name,
            protocol=protocol,
            base_url=base_url,
            default_model=default_model,
        )
        self._ensure_active_name_available(catalog, profile.display_name)
        self.credentials.set_custom(profile, api_key)
        try:
            catalog.providers.append(profile)
            self.store.save(catalog)
        except Exception:
            with suppress(Exception):
                self.credentials.delete_custom(profile)
            raise
        return profile

    def update(
        self,
        provider_ref: str,
        *,
        display_name: str | None = None,
        default_model: str | None = None,
    ) -> CustomProviderProfile:
        catalog, index, current = self._locate(provider_ref)
        updated = current.model_copy(
            update={
                **({"display_name": display_name} if display_name is not None else {}),
                **({"default_model": default_model} if default_model is not None else {}),
            }
        )
        updated = CustomProviderProfile.model_validate(updated.model_dump())
        if not updated.is_archived:
            self._ensure_active_name_available(
                catalog, updated.display_name, excluding_id=updated.provider_id
            )
        catalog.providers[index] = updated
        self.store.save(catalog)
        return updated

    def archive(self, provider_ref: str) -> CustomProviderProfile:
        catalog, index, current = self._locate(provider_ref)
        if current.is_archived:
            return current
        updated = current.model_copy(update={"archived_at": datetime.now(UTC)})
        catalog.providers[index] = updated
        self.store.save(catalog)
        return updated

    def restore(self, provider_ref: str) -> CustomProviderProfile:
        catalog, index, current = self._locate(provider_ref)
        if not current.is_archived:
            return current
        self._ensure_active_name_available(
            catalog, current.display_name, excluding_id=current.provider_id
        )
        updated = current.model_copy(update={"archived_at": None})
        catalog.providers[index] = updated
        self.store.save(catalog)
        return updated

    def replace_endpoint(
        self,
        provider_ref: str,
        *,
        protocol: ModelApiProtocol,
        base_url: str,
        api_key: str,
        display_name: str | None = None,
        default_model: str | None = None,
    ) -> CustomProviderProfile:
        catalog, index, current = self._locate(provider_ref)
        if current.is_archived:
            raise ValueError("归档的 Provider 不能更换端点")
        replacement = CustomProviderProfile(
            provider_id=uuid.uuid4().hex,
            display_name=display_name or current.display_name,
            protocol=protocol,
            base_url=base_url,
            default_model=default_model or current.default_model,
        )
        self._ensure_active_name_available(
            catalog, replacement.display_name, excluding_id=current.provider_id
        )
        self.credentials.set_custom(replacement, api_key)
        try:
            catalog.providers[index] = current.model_copy(
                update={"archived_at": datetime.now(UTC)}
            )
            catalog.providers.append(replacement)
            self.store.save(catalog)
        except Exception:
            with suppress(Exception):
                self.credentials.delete_custom(replacement)
            raise
        return replacement

    def delete(self, provider_ref: str) -> None:
        catalog, index, current = self._locate(provider_ref)
        if not current.is_archived:
            raise ValueError("仅允许永久删除已归档的 Provider")
        if self.is_provider_referenced(provider_ref):
            raise ValueError("该 Provider 仍被历史任务引用，不能永久删除")
        del catalog.providers[index]
        self.store.save(catalog)
        # A failed keyring deletion may leave an unreachable orphan secret, but
        # must never remove the profile while leaving it selectable in-app.
        self.credentials.delete_custom(current)

    def rotate_key(self, provider_ref: str, api_key: str) -> None:
        self.credentials.set_custom(self.get(provider_ref), api_key)

    def delete_key(self, provider_ref: str) -> None:
        self.credentials.delete_custom(self.get(provider_ref))

    def has_key(self, provider_ref: str) -> bool:
        if provider_ref.lower() in _BUILTIN_CONNECTIONS:
            return self.credentials.has(provider_ref)
        return self.credentials.has_custom(self.get(provider_ref))

    def get_api_key(self, provider_ref: str) -> str | None:
        if provider_ref.lower() in _BUILTIN_CONNECTIONS:
            return self.credentials.get(provider_ref)
        return self.credentials.get_custom(self.get(provider_ref))

    def snapshot(self, provider_ref: str, model: str) -> ProviderSnapshot:
        connection = self.resolve(provider_ref)
        return ProviderSnapshot(
            provider_ref=connection.provider_ref,
            display_name=connection.display_name,
            protocol=connection.protocol,
            base_url=connection.base_url,
            endpoint_fingerprint=connection.endpoint_fingerprint,
            model=model,
        )

    def resolve(self, provider_ref: str) -> ProviderConnection:
        normalized = provider_ref.lower()
        builtin = _BUILTIN_CONNECTIONS.get(normalized)
        if builtin is not None:
            return builtin
        profile = self.get(provider_ref)
        return ProviderConnection(
            provider_ref=profile.provider_ref,
            display_name=profile.display_name,
            protocol=profile.protocol,
            base_url=profile.base_url,
            default_model=profile.default_model,
            endpoint_fingerprint=profile.endpoint_fingerprint,
            custom=True,
        )

    def resolve_snapshot(self, snapshot: ProviderSnapshot) -> ProviderConnection:
        """Resolve immutable task settings without consulting mutable display config."""

        return ProviderConnection(
            provider_ref=snapshot.provider_ref,
            display_name=snapshot.display_name,
            protocol=snapshot.protocol,
            base_url=snapshot.base_url,
            default_model=snapshot.model,
            endpoint_fingerprint=snapshot.endpoint_fingerprint,
            custom=snapshot.provider_ref.startswith("custom:"),
        )

    def get_snapshot_api_key(self, snapshot: ProviderSnapshot) -> str | None:
        if snapshot.provider_ref in _BUILTIN_CONNECTIONS:
            expected = _BUILTIN_CONNECTIONS[snapshot.provider_ref]
            if (
                snapshot.protocol is not expected.protocol
                or snapshot.endpoint_fingerprint != expected.endpoint_fingerprint
            ):
                raise ValueError("内置 Provider 快照与当前固定端点不匹配")
            return self.credentials.get(snapshot.provider_ref)
        return self.credentials.get_custom_for_snapshot(snapshot)

    def _locate(
        self, provider_ref: str
    ) -> tuple[ProviderCatalog, int, CustomProviderProfile]:
        provider_id = self._parse_ref(provider_ref)
        catalog = self.store.load()
        for index, profile in enumerate(catalog.providers):
            if profile.provider_id == provider_id:
                return catalog, index, profile
        raise KeyError(f"找不到自定义 Provider：{provider_ref}")

    @staticmethod
    def _parse_ref(provider_ref: str) -> str:
        if not provider_ref.startswith("custom:"):
            raise ValueError("自定义 Provider 引用格式无效")
        provider_id = provider_ref.removeprefix("custom:")
        custom_provider_ref(provider_id)
        return provider_id

    @staticmethod
    def _ensure_active_name_available(
        catalog: ProviderCatalog, display_name: str, *, excluding_id: str | None = None
    ) -> None:
        normalized = display_name.strip().casefold()
        if any(
            not item.is_archived
            and item.provider_id != excluding_id
            and item.display_name.casefold() == normalized
            for item in catalog.providers
        ):
            raise ValueError("活动的自定义 Provider 名称不能重复")


def builtin_provider_connections() -> tuple[ProviderConnection, ...]:
    return tuple(_BUILTIN_CONNECTIONS.values())
