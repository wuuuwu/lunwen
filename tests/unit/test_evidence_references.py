from __future__ import annotations

from paper_reviewer.agents.reviewer_context import build_reviewer_read_context
from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.evidence import EvidenceKind, EvidenceRef
from paper_reviewer.validation.evidence_references import (
    EvidenceIndex,
    evidence_reference_errors,
)


def test_audit_reference_errors_preserve_order_and_wording() -> None:
    block = DocumentBlock.create(document_id="doc", page=2, text="Exact paper quote.")
    index = EvidenceIndex.build(blocks=[block], evidence=[])

    errors = evidence_reference_errors(
        owner="finding finding-1",
        paper_evidence=[
            EvidenceRef(
                evidence_id="external-in-paper-list",
                kind=EvidenceKind.EXTERNAL,
                title="External source",
            ),
            EvidenceRef(
                evidence_id="paper:unknown",
                kind=EvidenceKind.PAPER,
                block_id="unknown",
            ),
            EvidenceRef(
                evidence_id=f"paper:{block.block_id}",
                kind=EvidenceKind.PAPER,
                block_id=block.block_id,
                page=1,
                quote="Invented quote",
            ),
        ],
        external_evidence=[
            EvidenceRef(
                evidence_id=f"paper:{block.block_id}",
                kind=EvidenceKind.PAPER,
                block_id=block.block_id,
            )
        ],
        index=index,
    )

    assert errors == [
        "finding finding-1: paper evidence contains a non-paper reference",
        "finding finding-1: paper evidence references unknown block unknown",
        "finding finding-1: paper evidence page 1 does not match block page 2",
        "finding finding-1: paper evidence quote does not match its block",
        "finding finding-1: external evidence contains a non-external reference",
        (
            "finding finding-1: external evidence references unknown item "
            f"paper:{block.block_id}"
        ),
    ]


def test_reviewer_read_context_preserves_tool_registry_and_overview_shape() -> None:
    blocks = [
        DocumentBlock.create(document_id="doc", page=page, text="x" * 1300)
        for page in range(1, 14)
    ]

    context = build_reviewer_read_context(blocks=blocks, evidence=[])

    assert [item.name for item in context.registry.specs(
        ["search_paper", "read_blocks", "search_evidence", "read_evidence"]
    )] == ["search_paper", "read_blocks", "search_evidence", "read_evidence"]
    assert len(context.paper_overview) == 12
    assert context.paper_overview[0] == {
        "block_id": blocks[0].block_id,
        "page": 1,
        "type": blocks[0].block_type.value,
        "text": "x" * 1200,
    }
