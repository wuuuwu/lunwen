from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from paper_reviewer.domain.document import DocumentBlock, DocumentInfo


class ParsedDocument(BaseModel):
    info: DocumentInfo
    blocks: list[DocumentBlock]


class DocumentParserPort(Protocol):
    def parse(self, path: Path) -> ParsedDocument: ...
