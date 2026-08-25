from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.tools.evidence_reader import EvidenceReaderTools, register_evidence_tools
from paper_reviewer.tools.paper_reader import PaperReaderTools, register_paper_tools
from paper_reviewer.tools.registry import ToolRegistry
from paper_reviewer.validation.evidence_references import EvidenceIndex


@dataclass(frozen=True, slots=True)
class ReviewerReadContext:
    """Shared paper/evidence read context for reviewer-style agents."""

    registry: ToolRegistry
    evidence_index: EvidenceIndex
    paper_overview: list[dict[str, object]]


def build_reviewer_read_context(
    *,
    blocks: list[DocumentBlock],
    evidence: list[EvidenceItem],
) -> ReviewerReadContext:
    registry = ToolRegistry()
    register_paper_tools(registry, PaperReaderTools(blocks))
    register_evidence_tools(registry, EvidenceReaderTools(evidence))
    return ReviewerReadContext(
        registry=registry,
        evidence_index=EvidenceIndex.build(blocks=blocks, evidence=evidence),
        paper_overview=[
            {
                "block_id": block.block_id,
                "page": block.page,
                "type": block.block_type.value,
                "text": block.text[:1200],
            }
            for block in blocks[:12]
        ],
    )


def finding_evidence_blocks(
    *,
    finding_block_ids: Collection[str | None],
    index: EvidenceIndex,
) -> list[dict[str, object]]:
    return [
        {
            "block_id": block.block_id,
            "page": block.page,
            "section_path": block.section_path,
            "text": block.text,
        }
        for block_id in sorted(item for item in finding_block_ids if item is not None)
        if (block := index.block_by_id.get(block_id)) is not None
    ]
