from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from paper_reviewer.adapters.documents.pymupdf_parser import PyMuPDFParser


def _insert_centered(
    page: pymupdf.Page,
    text: str,
    *,
    y: float,
    size: float = 20,
    font: str = "hebo",
) -> None:
    result = page.insert_textbox(
        pymupdf.Rect(40, y, page.rect.width - 40, y + size * 1.8),
        text,
        fontsize=size,
        fontname=font,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    assert result >= 0


def _insert_body(page: pymupdf.Page, *, y: float = 220) -> None:
    result = page.insert_textbox(
        pymupdf.Rect(60, y, page.rect.width - 60, page.rect.height - 40),
        "The body explains the research design, evidence, analysis, and conclusions. " * 12,
        fontsize=11,
        fontname="helv",
    )
    assert result >= 0


def _add_cover(document: pymupdf.Document) -> None:
    page = document.new_page()
    _insert_centered(page, "School of Health Management", y=50, size=18)
    page.insert_text((80, 150), "姓名：张三", fontsize=12, fontname="china-s")
    page.insert_text((80, 185), "学号：20260001", fontsize=12, fontname="china-s")
    page.insert_text((80, 220), "任课教师：李老师", fontsize=12, fontname="china-s")
    _insert_body(page, y=280)


def _save(document: pymupdf.Document, path: Path, *, title: str | None = None) -> None:
    if title is not None:
        document.set_metadata({"title": title})
    document.save(path)
    document.close()


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


def test_visible_title_wins_when_embedded_title_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "conflict.pdf"
    document = pymupdf.open()
    _add_cover(document)
    page = document.new_page()
    _insert_centered(page, "A Reliable Method for Course Paper Evaluation", y=65)
    page.insert_text((70, 145), "Abstract: This study evaluates a reliable method.", fontsize=11)
    _insert_body(page, y=185)
    _save(document, path, title="School of Health Management")

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.embedded_title == "School of Health Management"
    assert parsed.info.visible_title == "A Reliable Method for Course Paper Evaluation"
    # ``title`` retains its generic-parser compatibility contract.  Course metadata
    # extraction uses ``visible_title`` and treats the embedded value only as a cross-check.
    assert parsed.info.title == parsed.info.embedded_title
    assert parsed.info.visible_title_page == 2
    assert parsed.info.visible_title_block_ids
    assert set(parsed.info.visible_title_block_ids) <= {block.block_id for block in parsed.blocks}


def test_matching_embedded_title_is_kept_only_as_cross_check(tmp_path: Path) -> None:
    path = tmp_path / "matching.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "Visible Research Title"
    _insert_centered(page, title, y=65)
    page.insert_text((70, 140), "Abstract: A concise summary of this research.", fontsize=11)
    _insert_body(page, y=180)
    _save(document, path, title=title)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.embedded_title == title
    assert parsed.info.visible_title == title
    assert parsed.info.title == title


@pytest.mark.parametrize("embedded", [None, "Course Examination Paper"])
def test_embedded_title_is_captured_separately_without_visible_evidence(
    tmp_path: Path,
    embedded: str | None,
) -> None:
    path = tmp_path / f"embedded-{embedded is not None}.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((70, 80), "Introduction", fontsize=11)
    _insert_body(page, y=130)
    _save(document, path, title=embedded)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.embedded_title == embedded
    assert parsed.info.visible_title is None


def test_multiline_chinese_visible_title_joins_only_line_break_space(tmp_path: Path) -> None:
    path = tmp_path / "multiline.pdf"
    document = pymupdf.open()
    _add_cover(document)
    page = document.new_page()
    _insert_centered(page, "课程论文元数据提取与", y=55, size=19, font="china-s")
    _insert_centered(page, "历史修复方法研究", y=87, size=19, font="china-s")
    page.insert_text((70, 155), "摘要：本文研究元数据提取方法。", fontsize=11, fontname="china-s")
    _insert_body(page, y=195)
    _save(document, path, title="示例学院")

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == "课程论文元数据提取与历史修复方法研究"
    assert parsed.info.visible_title_page == 2
    assert parsed.info.visible_title_block_ids


def test_title_and_slightly_smaller_subtitle_are_kept_together(tmp_path: Path) -> None:
    path = tmp_path / "title-subtitle.pdf"
    document = pymupdf.open()
    _add_cover(document)
    page = document.new_page()
    _insert_centered(page, "面向可信应用", y=55, size=16, font="china-s")
    _insert_centered(
        page,
        "——人工智能伦理治理路径研究",
        y=86,
        size=14.2,
        font="china-s",
    )
    page.insert_text((70, 145), "摘要：本文研究人工智能伦理。", fontsize=11, fontname="china-s")
    _insert_body(page, y=185)
    _save(document, path, title="示例学院")

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == "面向可信应用——人工智能伦理治理路径研究"
    assert parsed.info.visible_title_block_ids is not None
    assert len(parsed.info.visible_title_block_ids) == 2


def test_cover_followed_by_title_without_abstract_reaches_threshold(tmp_path: Path) -> None:
    path = tmp_path / "without-abstract.pdf"
    document = pymupdf.open()
    _add_cover(document)
    page = document.new_page()
    _insert_centered(page, "Research Without an Abstract", y=65, font="helv")
    _insert_body(page, y=150)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == "Research Without an Abstract"


def test_single_page_paper_has_strong_visible_title(tmp_path: Path) -> None:
    path = tmp_path / "single-page.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_centered(page, "Single Page Empirical Study", y=65)
    page.insert_text((70, 140), "Abstract: This is the paper summary.", fontsize=11)
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == "Single Page Empirical Study"
    assert parsed.info.visible_title_page == 1


def test_repeated_page_header_is_not_forced_as_title(tmp_path: Path) -> None:
    path = tmp_path / "repeated-header.pdf"
    document = pymupdf.open()
    for _index in range(2):
        page = document.new_page()
        _insert_centered(page, "Repeated Journal Header", y=45)
        page.insert_text((70, 115), "Abstract: Repeated front matter.", fontsize=11)
        _insert_body(page, y=155)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title is None


def test_legal_title_containing_college_and_course_is_not_excluded(tmp_path: Path) -> None:
    path = tmp_path / "legal-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "学院课程治理创新研究"
    _insert_centered(page, title, y=65, font="china-s")
    page.insert_text((70, 140), "摘要：本文研究课程治理。", fontsize=11, fontname="china-s")
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title


def test_research_title_ending_in_college_is_not_mistaken_for_institution(tmp_path: Path) -> None:
    path = tmp_path / "college-ending-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "课程治理创新研究基于某学院"
    _insert_centered(page, title, y=65, font="china-s")
    page.insert_text((70, 140), "摘要：本文研究课程治理。", fontsize=11, fontname="china-s")
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title


def test_institution_heading_is_not_merged_into_visible_title(tmp_path: Path) -> None:
    path = tmp_path / "institution-before-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_centered(page, "示例学院", y=30, size=18, font="china-s")
    title = "健康课程评价机制研究"
    _insert_centered(page, title, y=62, size=18, font="china-s")
    page.insert_text((70, 130), "摘要：本文研究课程评价机制。", fontsize=11, fontname="china-s")
    _insert_body(page, y=170)
    _save(document, path, title="示例学院")

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title
    assert "学院" not in parsed.info.visible_title


def test_author_byline_is_not_merged_into_visible_title(tmp_path: Path) -> None:
    path = tmp_path / "author-after-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "智能时代高校学生数字素养重构"
    _insert_centered(page, title, y=55, size=18, font="china-s")
    _insert_centered(page, "作者：张三", y=88, size=18, font="china-s")
    page.insert_text((70, 150), "摘要：本文研究大学生核心能力。", fontsize=11, fontname="china-s")
    _insert_body(page, y=190)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title
    assert "张三" not in parsed.info.visible_title
    assert parsed.info.visible_title_block_ids is not None
    assert len(parsed.info.visible_title_block_ids) == 1


@pytest.mark.parametrize("byline", ["Author: Jane Doe", "Author Jane Doe", "By Jane Doe"])
def test_english_byline_is_not_merged_into_visible_title(
    tmp_path: Path,
    byline: str,
) -> None:
    path = tmp_path / f"english-byline-{byline.split()[0].lower()}.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "Reliable Course Evaluation with Artificial Intelligence"
    _insert_centered(page, title, y=55, size=18)
    _insert_centered(page, byline, y=88, size=18)
    page.insert_text((70, 150), "Abstract: This study evaluates a reliable method.", fontsize=11)
    _insert_body(page, y=190)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title
    assert "Jane Doe" not in parsed.info.visible_title


@pytest.mark.parametrize(
    "title",
    [
        "Author Identification in Digital Scholarship",
        "By Design: Reliable Course Evaluation",
    ],
)
def test_legal_english_title_with_byline_word_is_not_excluded(
    tmp_path: Path,
    title: str,
) -> None:
    path = tmp_path / "legal-english-byline-word.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_centered(page, title, y=65, size=18)
    page.insert_text((70, 140), "Abstract: This study evaluates a reliable method.", fontsize=11)
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title


@pytest.mark.parametrize(
    "title",
    [
        "2025年数字经济背景下企业创新机制研究",
        "5G技术驱动下的课程教学模式创新研究",
    ],
)
def test_legal_title_starting_with_digits_is_not_treated_as_section(
    tmp_path: Path,
    title: str,
) -> None:
    path = tmp_path / f"digit-title-{title[0]}.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_centered(page, title, y=65, size=18, font="china-s")
    page.insert_text((70, 140), "摘要：本文研究相关问题。", fontsize=11, fontname="china-s")
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title


@pytest.mark.parametrize("heading", ["课程论文", "课程作业", "结课论文"])
def test_short_generic_paper_heading_is_not_selected_as_title(
    tmp_path: Path,
    heading: str,
) -> None:
    path = tmp_path / f"generic-{heading}.pdf"
    document = pymupdf.open()
    page = document.new_page()
    _insert_centered(page, heading, y=65, size=18, font="china-s")
    page.insert_text((70, 140), "摘要：本文为课程论文摘要。", fontsize=11, fontname="china-s")
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title is None


def test_short_unlabelled_name_does_not_win_tie_as_title_continuation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plain-name-after-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "智能时代高校学生数字素养重构"
    _insert_centered(page, title, y=55, size=18, font="china-s")
    _insert_centered(page, "张三", y=88, size=18, font="china-s")
    page.insert_text((70, 150), "摘要：本文研究大学生核心能力。", fontsize=11, fontname="china-s")
    _insert_body(page, y=190)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title


def test_short_chinese_title_that_looks_like_a_name_is_still_allowed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "short-chinese-title.pdf"
    document = pymupdf.open()
    page = document.new_page()
    title = "成长"
    _insert_centered(page, title, y=65, size=18, font="china-s")
    page.insert_text((70, 140), "摘要：本文讨论成长的教育机制。", fontsize=11, fontname="china-s")
    _insert_body(page, y=180)
    _save(document, path)

    parsed = PyMuPDFParser().parse(path)

    assert parsed.info.visible_title == title
