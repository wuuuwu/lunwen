from __future__ import annotations

from pathlib import Path

import pymupdf

from paper_reviewer.adapters.documents.pymupdf_parser import PyMuPDFParser


def test_searchable_pdf_is_parsed_with_stable_blocks(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "A Test Paper About Reliable Evaluation")
    page.insert_text(
        (72, 120),
        "This paper presents a method and evaluates it with a controlled experiment. " * 4,
    )
    document.save(path)
    document.close()

    parsed = PyMuPDFParser().parse(path)
    assert parsed.info.page_count == 1
    assert parsed.blocks
    assert all(block.page == 1 for block in parsed.blocks)
