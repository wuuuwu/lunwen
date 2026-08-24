from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf

from paper_reviewer.domain.document import BlockType, DocumentBlock, DocumentInfo
from paper_reviewer.ports.document_parser import ParsedDocument

_REFERENCE_HEADING_PATTERN = re.compile(
    r"^(?:references|bibliography|参考文献|参考资料)\s*$",
    flags=re.IGNORECASE,
)
_NUMBERED_REFERENCE_PATTERN = re.compile(
    r"^(?:(?:\[\d+\]|\uff3b\d+\uff3d|\d+[.、])\s*|\[?\d+\]?\s+)\S"
)
_BRACKETED_REFERENCE_PATTERN = re.compile(r"^(?:\[\d+\]|\uff3b\d+\uff3d)\s*\S")
_REFERENCE_SIGNAL_PATTERN = re.compile(
    r"(?<!\d)(?:19|20)\d{2}(?!\d)|\bdoi\b|"
    r"(?:\[|\uff3b)(?:J|M|D|C|N|R|S|P|A|Z|EB/OL|DB/OL|CP/DK)(?:\]|\uff3d)",
    flags=re.IGNORECASE,
)
_MIN_SHORT_BRACKETED_REFERENCE_LENGTH = 24
_MIN_NUMBERED_REFERENCE_LENGTH = 61


class UnsupportedDocumentError(ValueError):
    pass


class PyMuPDFParser:
    def parse(self, path: Path) -> ParsedDocument:
        if path.suffix.lower() != ".pdf":
            raise UnsupportedDocumentError("MVP currently supports searchable PDF files only")
        if not path.is_file():
            raise FileNotFoundError(path)
        file_hash = _sha256(path)
        document_id = file_hash[:24]
        blocks: list[DocumentBlock] = []
        headings: list[str] = []
        with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
            if document.page_count < 1:
                raise UnsupportedDocumentError("PDF has no pages")
            metadata_title = (document.metadata or {}).get("title") or None
            page_character_counts: list[int] = []
            for page_index, page in enumerate(document, start=1):
                raw_blocks = page.get_text("blocks", sort=True)
                page_character_counts.append(sum(len(str(item[4]).strip()) for item in raw_blocks))
                for raw in raw_blocks:
                    x0, y0, x1, y1, text = raw[:5]
                    clean = str(text).strip()
                    if not clean:
                        continue
                    block_type = _classify(clean, y0=float(y0), page_height=float(page.rect.height))
                    if block_type is BlockType.HEADING:
                        headings = [clean]
                    blocks.append(
                        DocumentBlock.create(
                            document_id=document_id,
                            page=page_index,
                            text=clean,
                            bbox=(float(x0), float(y0), float(x1), float(y1)),
                            section_path=headings.copy(),
                            block_type=block_type,
                        )
                    )
            if sum(page_character_counts) / document.page_count < 40:
                raise UnsupportedDocumentError(
                    "PDF appears to be scanned or contains too little searchable text; "
                    "OCR is not enabled"
                )
            title = metadata_title or _guess_title(blocks)
            info = DocumentInfo(
                document_id=document_id,
                source_path=str(path.resolve()),
                sha256=file_hash,
                title=title,
                page_count=document.page_count,
            )
        return ParsedDocument(info=info, blocks=blocks)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _classify(text: str, *, y0: float, page_height: float) -> BlockType:
    stripped = text.strip()
    if _REFERENCE_HEADING_PATTERN.fullmatch(stripped):
        return BlockType.HEADING
    if _is_numbered_reference(stripped):
        return BlockType.REFERENCE
    if y0 < page_height * 0.18 and len(stripped) < 180 and "\n" not in stripped:
        return BlockType.TITLE
    if re.match(r"^(\d+(?:\.\d+)*)?\s*[A-Z][A-Za-z\s:&-]{2,80}$", stripped):
        return BlockType.HEADING
    return BlockType.PARAGRAPH


def _is_numbered_reference(text: str) -> bool:
    if not _NUMBERED_REFERENCE_PATTERN.match(text):
        return False
    if len(text) >= _MIN_NUMBERED_REFERENCE_LENGTH:
        return True
    return (
        len(text) >= _MIN_SHORT_BRACKETED_REFERENCE_LENGTH
        and _BRACKETED_REFERENCE_PATTERN.match(text) is not None
        and _REFERENCE_SIGNAL_PATTERN.search(text) is not None
    )


def _guess_title(blocks: list[DocumentBlock]) -> str | None:
    for block in blocks:
        if block.block_type is BlockType.TITLE and 10 <= len(block.text) <= 300:
            return block.text
    return blocks[0].text[:300] if blocks else None
