from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.application import course_import
from paper_reviewer.application.app_state import AppPaths
from paper_reviewer.application.course_import import import_thesis_provider_settings
from paper_reviewer.application.providers import (
    ProviderCatalog,
    ProviderCatalogError,
    ProviderStore,
)
from paper_reviewer.domain.provider import CustomProviderProfile, ModelApiProtocol


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        root=root,
        data_dir=root / "data",
        runs_dir=root / "runs",
        logs_dir=root / "logs",
        config_dir=root / "config",
        batches_dir=root / "batches",
    )


def _install_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        course_import.keyring,
        "get_password",
        lambda service, account: values.get((service, account)),
    )
    monkeypatch.setattr(
        course_import.keyring,
        "set_password",
        lambda service, account, secret: values.__setitem__((service, account), secret),
    )
    return values


def test_first_start_copies_validated_catalog_and_credentials_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course_paths = _paths(tmp_path / "course")
    thesis_root = tmp_path / "thesis"
    profile = CustomProviderProfile(
        provider_id="a" * 32,
        display_name="课堂兼容端点",
        protocol=ModelApiProtocol.RESPONSES,
        base_url="https://provider.example/v1",
        default_model="course-model",
    )
    source_store = ProviderStore(thesis_root / "config" / "providers.json")
    source_store.save(ProviderCatalog(providers=[profile]))
    secrets = _install_memory_keyring(monkeypatch)
    openai_account = SystemCredentialStore.ACCOUNTS["openai"]
    custom_account = SystemCredentialStore._custom_account(
        profile.provider_id,
        profile.protocol,
        profile.endpoint_fingerprint,
    )
    secrets[("PaperReviewer", openai_account)] = "thesis-openai-secret"
    secrets[("PaperReviewer", custom_account)] = "thesis-custom-secret"

    result = import_thesis_provider_settings(
        course_paths,
        thesis_root=thesis_root,
    )

    assert result.attempted is True
    assert result.catalog_imported is True
    assert result.credential_count == 2
    assert ProviderStore(course_paths.providers_path).load().providers == [profile]
    assert secrets[("CoursePaperReviewer", openai_account)] == "thesis-openai-secret"
    assert secrets[("CoursePaperReviewer", custom_account)] == "thesis-custom-secret"
    marker = course_paths.config_dir / course_import.COURSE_PROVIDER_IMPORT_MARKER
    marker_text = marker.read_text(encoding="utf-8")
    assert "thesis-openai-secret" not in marker_text
    assert "thesis-custom-secret" not in marker_text

    source_store.save(ProviderCatalog())
    secrets[("PaperReviewer", openai_account)] = "changed-secret"
    repeated = import_thesis_provider_settings(
        course_paths,
        thesis_root=thesis_root,
    )

    assert repeated.attempted is False
    assert ProviderStore(course_paths.providers_path).load().providers == [profile]
    assert secrets[("CoursePaperReviewer", openai_account)] == "thesis-openai-secret"


def test_existing_course_configuration_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course_paths = _paths(tmp_path / "course")
    course_paths.ensure()
    ProviderStore(course_paths.providers_path).save(ProviderCatalog())
    thesis_root = tmp_path / "thesis"
    source_path = thesis_root / "config" / "providers.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text('{"schema_version": 1, "providers": "invalid"}', encoding="utf-8")
    _install_memory_keyring(monkeypatch)

    result = import_thesis_provider_settings(course_paths, thesis_root=thesis_root)

    assert result.skipped_existing_course_configuration is True
    assert ProviderStore(course_paths.providers_path).load() == ProviderCatalog()


def test_invalid_source_catalog_is_not_used_or_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    course_paths = _paths(tmp_path / "course")
    thesis_root = tmp_path / "thesis"
    source_path = thesis_root / "config" / "providers.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("not-json", encoding="utf-8")
    _install_memory_keyring(monkeypatch)

    with pytest.raises(ProviderCatalogError):
        import_thesis_provider_settings(course_paths, thesis_root=thesis_root)

    assert not course_paths.providers_path.exists()
    assert not (
        course_paths.config_dir / course_import.COURSE_PROVIDER_IMPORT_MARKER
    ).exists()
