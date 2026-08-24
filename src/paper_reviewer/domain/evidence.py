from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, model_validator


class EvidenceKind(StrEnum):
    PAPER = "paper"
    EXTERNAL = "external"


class EvidenceLevel(StrEnum):
    FULL_TEXT = "A"
    ABSTRACT = "B"
    METADATA = "C"


class EvidenceRef(BaseModel):
    evidence_id: str
    kind: EvidenceKind
    quote: str | None = None
    block_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    title: str | None = None
    doi: str | None = None
    url: HttpUrl | None = None
    level: EvidenceLevel = EvidenceLevel.FULL_TEXT

    @model_validator(mode="after")
    def validate_location(self) -> EvidenceRef:
        if self.kind is EvidenceKind.PAPER and not self.block_id:
            raise ValueError("paper evidence requires block_id")
        if self.kind is EvidenceKind.EXTERNAL and not (self.doi or self.url or self.title):
            raise ValueError("external evidence requires a DOI, URL, or title")
        return self


class EvidenceItem(BaseModel):
    evidence_id: str
    run_id: str
    kind: EvidenceKind
    title: str
    content: str
    source_name: str
    level: EvidenceLevel
    doi: str | None = None
    url: HttpUrl | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
