from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

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
_COVER_SIGNAL_PATTERN = re.compile(
    r"(?:课程试题|任课教师|班\s*级|学\s*号|姓\s*名|得\s*分|"
    r"考核方法|教师评语)"
)
_ABSTRACT_PATTERN = re.compile(r"^(?:摘\s*要|abstract)\s*[:：]?", re.IGNORECASE)
_EXCLUDED_TITLE_PATTERN = re.compile(
    r"^(?:"
    r"课程试题(?:卷)?|.*(?:课程)?考试(?:试题|试卷)|.*评审表|"
    r"摘\s*要(?:\s*[:：].*)?|abstract(?:\s*[:：].*)?|"
    r"关\s*键\s*词(?:\s*[:：].*)?|keywords?(?:\s*[:：].*)?|"
    r"目\s*录|contents|"
    r"(?:第?[一二三四五六七八九十百\d]+[章节篇]|"
    r"\d+(?:\.\d+)+(?:[.、])?|\d+[.、])\s*\S*"
    r")\s*$",
    re.IGNORECASE,
)
_IDENTITY_OR_BYLINE_PATTERN = re.compile(
    r"(?:学生姓名|姓名|作者|学生学号|学号|学生编号|专业名称|所学专业|专业|"
    r"班\s*级|任课教师|指导教师)\s*[:：]",
    re.IGNORECASE,
)
_EXPLICIT_ENGLISH_BYLINE_PATTERN = re.compile(
    r"^\s*authors?\s*[:：]\s*\S(?:.{0,120}\S)?\s*$",
    re.IGNORECASE,
)
_ENGLISH_NAME_TOKEN = r"(?:[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?|[A-Z]\.)"
_UNLABELLED_ENGLISH_BYLINE_PATTERN = re.compile(
    rf"^\s*(?i:authors?|by)\s+{_ENGLISH_NAME_TOKEN}"
    rf"(?:\s+{_ENGLISH_NAME_TOKEN}){{1,4}}\s*$"
)
_GENERIC_PAPER_HEADING_PATTERN = re.compile(
    r"^(?:课程论文|课程作业|结课论文|期末论文|课程报告)$",
    re.IGNORECASE,
)
_BARE_PERSON_NAME_PATTERN = re.compile(
    r"^[赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    r"戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉"
    r"岑薛雷贺倪汤滕殷罗毕郝邬安常乐于傅皮卞齐康伍余元顾孟平黄和穆萧尹姚"
    r"邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季"
    r"麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支柯管卢莫"
    r"经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程嵇邢滑裴陆荣翁荀羊"
    r"甄曲封芮储靳汲邴糜松井段富巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊"
    r"宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司韶黎蓟薄印宿白怀蒲台从鄂索"
    r"咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍郤璩"
    r"桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎"
    r"戈廖庾终暨居衡步都耿满弘匡国文寇广禄阙东欧欧阳][\u3400-\u9fff]{1,3}$"
)
_INSTITUTION_NAME_PATTERN = re.compile(
    r"^[\u4e00-\u9fff]{2,16}(?:学院|大学|学校|系|研究院)$"
)
_TITLE_SEMANTIC_PATTERN = re.compile(r"(?:研究|分析|探讨|路径|机制|影响|创新|课程)")
_HAN_END_PATTERN = re.compile(r"[\u3400-\u9fff]$")
_HAN_START_PATTERN = re.compile(r"^[\u3400-\u9fff]")
_ATTACHED_PUNCTUATION_START_PATTERN = re.compile(
    r"^[，。？\uff01；：、）》】」』—–-]"
)


@dataclass(frozen=True)
class _LayoutBlock:
    block: DocumentBlock
    raw_text: str
    page_width: float
    page_height: float
    font_size: float
    bold: bool
    line_height: float

    @property
    def center_x(self) -> float:
        assert self.block.bbox is not None
        return (self.block.bbox[0] + self.block.bbox[2]) / 2


@dataclass(frozen=True)
class _TitleCandidate:
    text: str
    page: int
    block_ids: tuple[str, ...]
    score: int
    positions: frozenset[int]
    continuation_quality: int


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
        layout_pages: list[list[_LayoutBlock]] = []
        headings: list[str] = []
        with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
            if document.page_count < 1:
                raise UnsupportedDocumentError("PDF has no pages")
            metadata_title = (document.metadata or {}).get("title") or None
            page_character_counts: list[int] = []
            for page_index, page in enumerate(document, start=1):
                raw_blocks = page.get_text("blocks", sort=True)
                page_character_counts.append(sum(len(str(item[4]).strip()) for item in raw_blocks))
                spans = _page_spans(page) if page_index <= 3 else []
                page_layout: list[_LayoutBlock] = []
                for raw in raw_blocks:
                    x0, y0, x1, y1, text = raw[:5]
                    clean = str(text).strip()
                    if not clean:
                        continue
                    block_type = _classify(clean, y0=float(y0), page_height=float(page.rect.height))
                    if block_type is BlockType.HEADING:
                        headings = [clean]
                    bbox = (float(x0), float(y0), float(x1), float(y1))
                    block = DocumentBlock.create(
                        document_id=document_id,
                        page=page_index,
                        text=clean,
                        bbox=bbox,
                        section_path=headings.copy(),
                        block_type=block_type,
                    )
                    blocks.append(block)
                    if page_index <= 3:
                        font_size, bold, line_height = _block_typography(bbox, spans)
                        page_layout.append(
                            _LayoutBlock(
                                block=block,
                                raw_text=str(text).strip(),
                                page_width=float(page.rect.width),
                                page_height=float(page.rect.height),
                                font_size=font_size,
                                bold=bold,
                                line_height=line_height,
                            )
                        )
                if page_index <= 3:
                    layout_pages.append(page_layout)
            if sum(page_character_counts) / document.page_count < 40:
                raise UnsupportedDocumentError(
                    "PDF appears to be scanned or contains too little searchable text; "
                    "OCR is not enabled"
                )
            visible_candidate = _find_visible_title(layout_pages)
            visible_title = visible_candidate.text if visible_candidate else None
            # Embedded PDF metadata is authoring-tool state, not paper text.  Keep it
            # separately for cross-checking and retain a visible-text fallback for the
            # generic (non-course) parser title.
            title = metadata_title or visible_title or _guess_title(blocks)
            info = DocumentInfo(
                document_id=document_id,
                source_path=str(path.resolve()),
                sha256=file_hash,
                title=title,
                embedded_title=metadata_title,
                visible_title=visible_title,
                visible_title_page=visible_candidate.page if visible_candidate else None,
                visible_title_block_ids=(
                    list(visible_candidate.block_ids) if visible_candidate else None
                ),
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


def _page_spans(page: Any) -> list[tuple[tuple[float, float, float, float], float, bool, int]]:
    spans: list[tuple[tuple[float, float, float, float], float, bool, int]] = []
    payload = page.get_text("dict", sort=True)
    for raw_block in payload.get("blocks", []):
        for line in raw_block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text", ""))
                if not text.strip():
                    continue
                bbox = tuple(float(value) for value in span["bbox"])
                spans.append(
                    (
                        (bbox[0], bbox[1], bbox[2], bbox[3]),
                        float(span.get("size", 0.0)),
                        bool(int(span.get("flags", 0)) & 16),
                        len(text.strip()),
                    )
                )
    return spans


def _block_typography(
    bbox: tuple[float, float, float, float],
    spans: list[tuple[tuple[float, float, float, float], float, bool, int]],
) -> tuple[float, bool, float]:
    contained = [item for item in spans if _bbox_overlap_ratio(item[0], bbox) >= 0.5]
    if not contained:
        height = max(bbox[3] - bbox[1], 1.0)
        return height, False, height
    sizes = [(size, weight) for _span_bbox, size, _bold, weight in contained]
    font_size = _weighted_median(sizes)
    total_weight = sum(weight for _span_bbox, _size, _bold, weight in contained)
    bold_weight = sum(weight for _span_bbox, _size, bold, weight in contained if bold)
    line_heights = [span_bbox[3] - span_bbox[1] for span_bbox, *_rest in contained]
    return font_size, bold_weight >= total_weight * 0.6, median(line_heights)


def _bbox_overlap_ratio(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> float:
    width = max(inner[2] - inner[0], 0.0)
    height = max(inner[3] - inner[1], 0.0)
    area = width * height
    if area == 0:
        return 0.0
    overlap_width = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    overlap_height = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    return overlap_width * overlap_height / area


def _weighted_median(values: list[tuple[float, int]]) -> float:
    ordered = sorted(values)
    threshold = sum(weight for _value, weight in ordered) / 2
    cumulative = 0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]


def _find_visible_title(layout_pages: list[list[_LayoutBlock]]) -> _TitleCandidate | None:
    all_sizes = [
        (item.font_size, max(len(item.block.text), 1))
        for page in layout_pages
        for item in page
        if item.font_size > 0
    ]
    if not all_sizes:
        return None
    body_median = _weighted_median(all_sizes)
    cover_pages = {
        page_number
        for page_number, page in enumerate(layout_pages, start=1)
        if _cover_signal_count(page) >= 2
    }
    candidates: list[_TitleCandidate] = []
    for page_number, page in enumerate(layout_pages, start=1):
        for start in range(len(page)):
            group: list[_LayoutBlock] = []
            for end in range(start, min(start + 3, len(page))):
                group.append(page[end])
                if len(group) > 1 and not _can_merge_title_blocks(group[-2], group[-1]):
                    break
                candidate = _score_title_candidate(
                    group,
                    page=page,
                    start=start,
                    end=end,
                    body_median=body_median,
                    follows_cover=page_number - 1 in cover_pages,
                )
                if candidate is not None:
                    candidates.append(candidate)
    if not candidates:
        return None
    # A two-line title naturally also produces its one-line substrings.  They are one
    # piece of evidence, not competing candidates, so retain only the best overlapping
    # interpretation before applying the required runner-up margin.
    collapsed: list[_TitleCandidate] = []
    for candidate in sorted(
        candidates,
        # Prefer a multi-block interpretation only when its continuation looks
        # title-like.  Block count by itself must not pull a short byline or
        # identity fragment into an otherwise valid title.
        key=lambda item: (
            item.score,
            item.continuation_quality,
            -len(item.block_ids),
            len(item.text),
        ),
        reverse=True,
    ):
        if any(
            item.page == candidate.page and item.positions & candidate.positions
            for item in collapsed
        ):
            continue
        collapsed.append(candidate)
    best = collapsed[0]
    runner_up_score = max(
        (
            candidate.score
            for candidate in collapsed[1:]
            if not _is_probable_byline_after(best, candidate)
        ),
        default=-1,
    )
    if best.score < 7 or best.score - runner_up_score < 2:
        return None
    return best


def _cover_signal_count(page: list[_LayoutBlock]) -> int:
    signals: set[str] = set()
    for item in page:
        signals.update(
            match.group(0).replace(" ", "")
            for match in _COVER_SIGNAL_PATTERN.finditer(item.block.text)
        )
    return len(signals)


def _can_merge_title_blocks(previous: _LayoutBlock, current: _LayoutBlock) -> bool:
    assert previous.block.bbox is not None
    assert current.block.bbox is not None
    # A byline may use exactly the same font, centring and line spacing as the
    # title.  It is identity metadata, never a continuation of the title.
    if _is_identity_or_byline(current.raw_text) or _is_unlabelled_english_byline(
        current.raw_text
    ):
        return False
    largest_size = max(previous.font_size, current.font_size)
    if largest_size <= 0 or abs(previous.font_size - current.font_size) / largest_size > 0.15:
        return False
    if abs(previous.center_x - current.center_x) > previous.page_width * 0.15:
        return False
    gap = current.block.bbox[1] - previous.block.bbox[3]
    return gap <= max(previous.line_height, current.line_height) * 1.8


def _score_title_candidate(
    group: list[_LayoutBlock],
    *,
    page: list[_LayoutBlock],
    start: int,
    end: int,
    body_median: float,
    follows_cover: bool,
) -> _TitleCandidate | None:
    if any(item.block.bbox is None for item in group):
        return None
    if any(item.block.bbox[1] > item.page_height * 0.35 for item in group):  # type: ignore[index]
        return None
    text = _merge_title_parts([item.raw_text for item in group])
    if (
        not text
        or len(text) > 300
        or _is_excluded_title(text)
        or any(_is_excluded_title(_merge_title_parts([item.raw_text])) for item in group)
    ):
        return None
    # A real title often uses a slightly smaller subtitle.  Block grouping has
    # already enforced a maximum 15% size difference, so requiring the
    # smallest line to clear the 1.2x threshold would discard otherwise valid
    # title/subtitle pairs (for example 16pt + 14pt over 12pt body text).
    large = max(item.font_size for item in group) >= body_median * 1.2
    centered = all(
        abs(item.center_x - item.page_width / 2) <= item.page_width * 0.15 for item in group
    )
    bold = all(item.bold for item in group)
    if not large and not (bold and centered):
        return None
    score = 0
    if any(_ABSTRACT_PATTERN.match(item.block.text) for item in page[end + 1 : end + 4]):
        score += 4
    if follows_cover:
        score += 3
    if large:
        score += 2
    if bold:
        score += 1
    if centered:
        score += 1
    score += 1  # page upper 35%, enforced above
    return _TitleCandidate(
        text=text,
        page=group[0].block.page,
        block_ids=tuple(item.block.block_id for item in group),
        score=score,
        positions=frozenset(range(start, end + 1)),
        continuation_quality=sum(
            _title_continuation_quality(item.raw_text) for item in group[1:]
        ),
    )


def _is_excluded_title(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    bare_institution = bool(_INSTITUTION_NAME_PATTERN.fullmatch(compact)) and not bool(
        _TITLE_SEMANTIC_PATTERN.search(compact)
    )
    return bool(
        _EXCLUDED_TITLE_PATTERN.fullmatch(text.strip())
        or _GENERIC_PAPER_HEADING_PATTERN.fullmatch(compact)
        or _is_identity_or_byline(text)
        or bare_institution
    )


def _is_identity_or_byline(text: str) -> bool:
    return bool(
        _IDENTITY_OR_BYLINE_PATTERN.search(text)
        or _EXPLICIT_ENGLISH_BYLINE_PATTERN.fullmatch(text)
    )


def _is_unlabelled_english_byline(text: str) -> bool:
    return _UNLABELLED_ENGLISH_BYLINE_PATTERN.fullmatch(text) is not None


def _is_probable_byline_after(
    title: _TitleCandidate,
    candidate: _TitleCandidate,
) -> bool:
    """Use a bare-name shape only to resolve an adjacent candidate tie.

    A short Chinese phrase can be a legitimate paper title, so this heuristic
    must never exclude it globally.  It applies only when a one-block phrase
    immediately follows an already stronger title candidate on the same page.
    """

    return bool(
        title.page == candidate.page
        and len(candidate.positions) == 1
        and min(candidate.positions) == max(title.positions) + 1
        and (
            _BARE_PERSON_NAME_PATTERN.fullmatch(re.sub(r"\s+", "", candidate.text))
            or _is_unlabelled_english_byline(candidate.text)
        )
    )


def _title_continuation_quality(text: str) -> int:
    compact = re.sub(r"\s+", "", text)
    return int(
        _BARE_PERSON_NAME_PATTERN.fullmatch(compact) is None
        and (
            len(compact) >= 2
            or _ATTACHED_PUNCTUATION_START_PATTERN.match(compact) is not None
        )
    )


def _merge_title_parts(parts: list[str]) -> str:
    lines = [line.strip() for part in parts for line in part.splitlines() if line.strip()]
    if not lines:
        return ""
    merged = lines[0]
    for line in lines[1:]:
        separator = ""
        if not (
            (_HAN_END_PATTERN.search(merged) and _HAN_START_PATTERN.match(line))
            or _ATTACHED_PUNCTUATION_START_PATTERN.match(line)
        ):
            separator = " "
        merged += separator + line
    return merged.strip()
