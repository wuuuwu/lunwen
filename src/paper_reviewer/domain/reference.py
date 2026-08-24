from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, Field


class ReferenceVerificationStatus(StrEnum):
    VERIFIED = "verified"
    PROBABLE = "probable"
    UNRESOLVED = "unresolved"


class ReferenceEntry(BaseModel):
    reference_id: str
    text: str
    block_id: str
    page: int = Field(ge=1)
    doi: str | None = None
    year: int | None = Field(default=None, ge=1000, le=9999)

    @classmethod
    def create(
        cls,
        *,
        text: str,
        block_id: str,
        page: int,
        doi: str | None = None,
        year: int | None = None,
    ) -> ReferenceEntry:
        normalized_text = _normalize_whitespace(text)
        if not normalized_text:
            raise ValueError("reference text cannot be empty")
        normalized_doi = normalize_doi(doi) if doi else None
        identity = normalized_doi or _canonical_reference_text(normalized_text)
        reference_id = hashlib.sha256(f"reference|{identity}".encode()).hexdigest()[:24]
        return cls(
            reference_id=reference_id,
            text=normalized_text,
            block_id=block_id,
            page=page,
            doi=normalized_doi,
            year=year,
        )

    @property
    def original_text(self) -> str:
        """Compatibility-friendly name for the retained bibliography text."""
        return self.text


class ReferenceCheck(BaseModel):
    entry: ReferenceEntry
    status: ReferenceVerificationStatus
    matched_evidence_ids: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0)
    message: str


class ReferenceCheckReport(BaseModel):
    checks: list[ReferenceCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(
            check.status is ReferenceVerificationStatus.VERIFIED for check in self.checks
        )

    @property
    def probable_count(self) -> int:
        return sum(
            check.status is ReferenceVerificationStatus.PROBABLE for check in self.checks
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            check.status is ReferenceVerificationStatus.UNRESOLVED for check in self.checks
        )


def normalize_doi(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = normalized.rstrip(" \t\r\n.,;:，。；：")
    while normalized.endswith(")") and normalized.count("(") < normalized.count(")"):
        normalized = normalized[:-1]
    while normalized.endswith("]") and normalized.count("[") < normalized.count("]"):
        normalized = normalized[:-1]
    return normalized.casefold()


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _canonical_reference_text(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
