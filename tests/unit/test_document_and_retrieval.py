from __future__ import annotations

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.retrieval.ranking import search_blocks


def test_block_id_is_stable_for_equivalent_whitespace() -> None:
    first = DocumentBlock.create(
        document_id="doc", page=1, text="A method\nwith   spaces", bbox=(1, 2, 3, 4)
    )
    second = DocumentBlock.create(
        document_id="doc", page=1, text="A method with spaces", bbox=(1, 2, 3, 4)
    )
    assert first.block_id == second.block_id
    assert first.content_hash == second.content_hash


def test_bm25_ranking_returns_relevant_block() -> None:
    relevant = DocumentBlock.create(
        document_id="doc",
        page=2,
        text="The experiment uses a transformer baseline and accuracy metric.",
    )
    unrelated = DocumentBlock.create(
        document_id="doc", page=1, text="This paper introduces the background."
    )
    results = search_blocks([unrelated, relevant], "transformer baseline")
    assert results[0].block.block_id == relevant.block_id
