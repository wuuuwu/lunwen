"""One-time, read-only import from the thesis desktop edition.

The course edition owns a separate data directory and Credential Manager
service.  On its first start it may copy the user's existing provider catalog
and credentials so setup is convenient without ever making the two products
share mutable configuration afterwards.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError
from platformdirs import user_data_path

from paper_reviewer.adapters.security.keyring_store import SystemCredentialStore
from paper_reviewer.application.app_state import (
    COURSE_CREDENTIAL_SERVICE_NAME,
    AppPaths,
)
from paper_reviewer.application.providers import ProviderCatalog, ProviderStore

THESIS_APP_ID = "PaperReviewer"
THESIS_ORGANIZATION_NAME = "PaperReviewer"
THESIS_CREDENTIAL_SERVICE_NAME = "PaperReviewer"
COURSE_PROVIDER_IMPORT_MARKER = "thesis-provider-import-v1.json"


@dataclass(frozen=True, slots=True)
class CourseProviderImportResult:
    attempted: bool
    catalog_imported: bool = False
    credential_count: int = 0
    skipped_existing_course_configuration: bool = False


def import_thesis_provider_settings(
    course_paths: AppPaths,
    *,
    thesis_root: Path | None = None,
) -> CourseProviderImportResult:
    """Copy provider setup once, without modifying the thesis edition.

    Existing course-edition configuration always wins.  Secrets move directly
    between Credential Manager entries and are never returned, logged, written
    to JSON, or placed in environment variables.
    """

    course_paths.ensure()
    marker = course_paths.config_dir / COURSE_PROVIDER_IMPORT_MARKER
    if marker.is_file():
        return CourseProviderImportResult(attempted=False)

    if course_paths.providers_path.exists() or _course_builtin_key_exists():
        _write_marker(
            marker,
            {
                "schema_version": 1,
                "result": "skipped_existing_course_configuration",
            },
        )
        return CourseProviderImportResult(
            attempted=True,
            skipped_existing_course_configuration=True,
        )

    source_root = thesis_root or user_data_path(
        THESIS_APP_ID,
        THESIS_ORGANIZATION_NAME,
        roaming=False,
    )
    source_path = source_root / "config" / "providers.json"
    source_catalog = (
        ProviderStore(source_path).load() if source_path.is_file() else ProviderCatalog()
    )
    catalog_imported = source_path.is_file()
    if catalog_imported:
        ProviderStore(course_paths.providers_path).save(source_catalog)

    accounts = list(SystemCredentialStore.ACCOUNTS.values())
    accounts.extend(
        SystemCredentialStore._custom_account(
            profile.provider_id,
            profile.protocol,
            profile.endpoint_fingerprint,
        )
        for profile in source_catalog.providers
    )
    copied = sum(_copy_credential(account) for account in accounts)
    _write_marker(
        marker,
        {
            "schema_version": 1,
            "result": "completed",
            "catalog_imported": catalog_imported,
            "credential_count": copied,
        },
    )
    return CourseProviderImportResult(
        attempted=True,
        catalog_imported=catalog_imported,
        credential_count=copied,
    )


def _course_builtin_key_exists() -> bool:
    for account in SystemCredentialStore.ACCOUNTS.values():
        try:
            if keyring.get_password(COURSE_CREDENTIAL_SERVICE_NAME, account):
                return True
        except KeyringError:
            raise
    return False


def _copy_credential(account: str) -> int:
    existing = keyring.get_password(COURSE_CREDENTIAL_SERVICE_NAME, account)
    if existing:
        return 0
    source = keyring.get_password(THESIS_CREDENTIAL_SERVICE_NAME, account)
    if not source:
        return 0
    keyring.set_password(COURSE_CREDENTIAL_SERVICE_NAME, account, source)
    return 1


def _write_marker(destination: Path, payload: dict[str, object]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
