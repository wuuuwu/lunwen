from __future__ import annotations

import pytest

from paper_reviewer.adapters.documents.pymupdf_parser import _classify
from paper_reviewer.domain.document import BlockType


@pytest.mark.parametrize(
    "text",
    ["References", "references", "Bibliography", "参考文献", "参考资料"],
)
def test_reference_headings_are_recognized_at_the_top_of_a_page(text: str) -> None:
    assert _classify(text, y0=20, page_height=800) is BlockType.HEADING


@pytest.mark.parametrize(
    "marker",
    ["[1] ", "\uff3b1\uff3d", "1. ", "1、"],
)
def test_numbered_references_are_recognized_at_the_top_of_a_page(marker: str) -> None:
    reference = marker + (
        "Smith J, Zhang W. Reliable agent evaluation with grounded web evidence. "
        "Journal of Evaluation, 2025."
    )

    assert _classify(reference, y0=20, page_height=800) is BlockType.REFERENCE


def test_existing_unbracketed_english_reference_format_is_preserved() -> None:
    reference = (
        "1 Smith J, Zhang W. Reliable agent evaluation with grounded web evidence. "
        "Journal of Evaluation, 2025."
    )

    assert _classify(reference, y0=400, page_height=800) is BlockType.REFERENCE


@pytest.mark.parametrize(
    "reference",
    [
        "\uff3b1\uff3d张三. 智能评测方法[J]. 计算机学报, 47(1): 1-9.",
        "[2] Smith J. Grounded evaluation method. 2024.",
        "[3] Smith J. Agent evaluation. DOI: 10.1000/test.",
    ],
)
def test_short_bracketed_references_use_any_bibliographic_signal(reference: str) -> None:
    assert 24 <= len(reference) < 61
    assert _classify(reference, y0=20, page_height=800) is BlockType.REFERENCE


@pytest.mark.parametrize(
    "text",
    [
        "[1] Short item",
        "\uff3b1\uff3d这是一个普通任务清单项目，仅用于安排本周工作并跟踪完成情况",
        "1. Short item",
        "1、简短列表项",
        "1. Zhang. Evaluation [J]. 2024.",
        "1、张三. 智能评测方法[J]. 2024.",
    ],
)
def test_short_numbered_list_items_are_not_references(text: str) -> None:
    assert _classify(text, y0=400, page_height=800) is BlockType.PARAGRAPH


def test_existing_english_section_heading_behavior_is_preserved() -> None:
    assert _classify("2.1 Related Work", y0=400, page_height=800) is BlockType.HEADING
