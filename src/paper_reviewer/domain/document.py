from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field


class BlockType(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    EQUATION = "equation"
    TABLE = "table"
    FIGURE = "figure"
    REFERENCE = "reference"


class DocumentInfo(BaseModel):
    document_id: str
    source_path: str
    sha256: str
    title: str | None = None
    page_count: int = Field(ge=1)


class DocumentBlock(BaseModel):
    block_id: str
    document_id: str
    page: int = Field(ge=1)
    section_path: list[str] = Field(default_factory=list)
    block_type: BlockType = BlockType.PARAGRAPH
    text: str
    bbox: tuple[float, float, float, float] | None = None
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        page: int,
        text: str,
        bbox: tuple[float, float, float, float] | None = None,
        section_path: list[str] | None = None,
        block_type: BlockType = BlockType.PARAGRAPH,
    ) -> DocumentBlock:
        normalized = normalize_text(text)
        content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        location = ",".join(f"{value:.2f}" for value in bbox) if bbox else ""
        seed = f"{document_id}|{page}|{location}|{content_hash}"
        block_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        return cls(
            block_id=block_id,
            document_id=document_id,
            page=page,
            section_path=section_path or [],
            block_type=block_type,
            text=normalized,
            bbox=bbox,
            content_hash=content_hash,
        )


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
