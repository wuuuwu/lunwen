from __future__ import annotations

from dataclasses import dataclass

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceRef


@dataclass(frozen=True, slots=True)
class EvidenceIndex:
    """Stable lookup data used by agent validators and checkpoint audits."""

    block_by_id: dict[str, DocumentBlock]
    block_ids: frozenset[str]
    block_pages: dict[str, int]
    evidence_ids: frozenset[str]

    @classmethod
    def build(
        cls,
        *,
        blocks: list[DocumentBlock],
        evidence: list[EvidenceItem],
    ) -> EvidenceIndex:
        block_by_id = {block.block_id: block for block in blocks}
        return cls(
            block_by_id=block_by_id,
            block_ids=frozenset(block_by_id),
            block_pages={block_id: block.page for block_id, block in block_by_id.items()},
            evidence_ids=frozenset(item.evidence_id for item in evidence),
        )


def evidence_reference_errors(
    *,
    owner: str,
    paper_evidence: list[EvidenceRef],
    external_evidence: list[EvidenceRef],
    index: EvidenceIndex,
) -> list[str]:
    """Return audit-grade reference errors in their established order and wording."""

    errors: list[str] = []
    for reference in paper_evidence:
        if reference.kind is not EvidenceKind.PAPER:
            errors.append(f"{owner}: paper evidence contains a non-paper reference")
            continue
        if reference.block_id is None:
            errors.append(f"{owner}: paper evidence is missing a block id")
            continue
        block = index.block_by_id.get(reference.block_id)
        if block is None:
            errors.append(
                f"{owner}: paper evidence references unknown block {reference.block_id}"
            )
            continue
        if reference.page is not None and reference.page != block.page:
            errors.append(
                f"{owner}: paper evidence page {reference.page} does not match "
                f"block page {block.page}"
            )
        if reference.quote and reference.quote not in block.text:
            errors.append(f"{owner}: paper evidence quote does not match its block")
    for reference in external_evidence:
        if reference.kind is not EvidenceKind.EXTERNAL:
            errors.append(f"{owner}: external evidence contains a non-external reference")
        if reference.evidence_id not in index.evidence_ids:
            errors.append(
                f"{owner}: external evidence references unknown item {reference.evidence_id}"
            )
    return errors
