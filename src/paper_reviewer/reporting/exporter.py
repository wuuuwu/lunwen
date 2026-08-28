"""Local, deterministic Markdown-to-PDF report export."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from importlib import resources
from pathlib import Path
from typing import Any

import pymupdf
from PySide6.QtCore import QByteArray, QMarginsF, QUrl
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QGuiApplication,
    QPageLayout,
    QPageSize,
    QPdfWriter,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import QApplication

from paper_reviewer.reporting.renderer import DISCLAIMER_LINES

_FONT_CANDIDATES = (
    "PingFang SC",
    "Hiragino Sans GB",
    "Songti SC",
    "Heiti SC",
    "Noto Sans CJK SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimSun",
)
_WINDOWS_FONT_FILES = ("msyh.ttc", "simsun.ttc")
_V2_REPORT_MARKERS = ("## 九项诊断评分", "## 独立专家面板", "## 确定性决策路径")
_A4_WIDTH_POINTS = 595.28
_A4_HEIGHT_POINTS = 841.89
_OWNED_APPLICATION: QApplication | None = None


class ReportPdfExportError(ValueError):
    """Raised when a report cannot be rendered or verified safely."""


class _ResourceBlockingDocument(QTextDocument):
    """A text document that never resolves Markdown image resources."""

    def loadResource(self, resource_type: int, name: QUrl | str) -> Any:
        del resource_type, name
        return QByteArray()


def render_pdf(
    markdown: str,
    destination: Path,
    *,
    title: str,
    author: str = "Paper Reviewer",
) -> None:
    """Render Markdown as a white A4 PDF without resolving external resources."""

    _application = _ensure_gui_application()
    font_family = _preferred_chinese_font()
    stylesheet = (
        resources.files("paper_reviewer.reporting.resources")
        .joinpath("report_print.css")
        .read_text(encoding="utf-8")
    )

    document = _ResourceBlockingDocument()
    document.setDefaultFont(QFont(font_family, 10))
    document.setDefaultStyleSheet(stylesheet)
    features = (
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub
        | QTextDocument.MarkdownFeature.MarkdownNoHTML
    )
    printable_markdown = re.sub(r"[\u2010-\u2015]", "-", markdown)
    document.setMarkdown(printable_markdown, features)
    _start_disclaimer_section_on_new_page(document)

    writer = QPdfWriter(str(destination))
    writer.setResolution(96)
    writer.setPageLayout(
        QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            QPageLayout.Orientation.Portrait,
            QMarginsF(0.0, 0.0, 0.0, 0.0),
            QPageLayout.Unit.Millimeter,
        )
    )
    writer.setTitle(title)
    writer.setCreator("Paper Reviewer report exporter")
    writer.setAuthor(author)

    # An unpaginated QTextDocument uses Qt's native print layout: roughly 2 cm
    # content margins, automatic page breaks, and a current-page footer.
    document.print_(writer)


def _start_disclaimer_section_on_new_page(document: QTextDocument) -> None:
    """Keep the short mandatory disclaimer list away from page boundaries."""

    block = document.begin()
    while block.isValid():
        if block.text().strip() == "重要说明":
            block_format = block.blockFormat()
            block_format.setPageBreakPolicy(
                QTextFormat.PageBreakFlag.PageBreak_AlwaysBefore
            )
            QTextCursor(block).setBlockFormat(block_format)
            return
        block = block.next()


def validate_pdf(path: Path, markdown: str) -> None:
    """Reopen a generated PDF and validate its structure and searchable text."""

    if path.read_bytes()[:5] != b"%PDF-":
        raise ReportPdfExportError("生成文件不是有效的 PDF。")
    try:
        with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
            if document.page_count < 1:
                raise ReportPdfExportError("生成的 PDF 不包含页面。")
            for page in document:
                width, height = float(page.rect.width), float(page.rect.height)
                if abs(width - _A4_WIDTH_POINTS) > 3 or abs(height - _A4_HEIGHT_POINTS) > 3:
                    raise ReportPdfExportError("生成的 PDF 不是 A4 纵向页面。")
            extracted = "\n".join(page.get_text("text") for page in document)
    except ReportPdfExportError:
        raise
    except Exception as error:
        raise ReportPdfExportError("无法重新打开生成的 PDF。") from error

    # CoreText-backed fonts may expose visually equivalent CJK compatibility
    # characters (for example, ⾃ instead of 自) in the PDF text layer.
    normalized_text = _normalize_pdf_text(extracted)
    if not normalized_text:
        raise ReportPdfExportError("生成的 PDF 不包含可抽取文本。")
    expected_disclaimers = (
        list(DISCLAIMER_LINES)
        if any(marker in markdown for marker in _V2_REPORT_MARKERS)
        else [line for line in DISCLAIMER_LINES if line in markdown]
    )
    missing = [
        line
        for line in expected_disclaimers
        if _normalize_pdf_text(line) not in normalized_text
    ]
    if missing:
        raise ReportPdfExportError(f"生成的 PDF 缺少关键免责声明：{missing[0]}")


def _normalize_pdf_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _ensure_gui_application() -> QGuiApplication:
    global _OWNED_APPLICATION

    instance = QGuiApplication.instance()
    if isinstance(instance, QGuiApplication):
        return instance
    # macOS GUI sessions normally have no DISPLAY variable; forcing the
    # offscreen plugin there would make the packaged app invisible.
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Keep the internally created application alive for the process lifetime.
    # Destroying and recreating Qt's application singleton in one process can
    # crash later GUI tests and embedding hosts.
    _OWNED_APPLICATION = QApplication([])
    return _OWNED_APPLICATION


def _preferred_chinese_font() -> str:
    available = set(QFontDatabase.families())
    for family in _FONT_CANDIDATES:
        if family in available:
            return family
    # Qt's offscreen Windows platform plugin does not enumerate the system
    # font collection.  Register the same operating-system fonts explicitly
    # so packaged/self-test and headless verification still exercise the real
    # Chinese typeface without bundling a separate font file.
    if sys.platform == "win32":
        fonts_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
        for filename in _WINDOWS_FONT_FILES:
            font_path = fonts_dir / filename
            if not font_path.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id < 0:
                continue
            registered = set(QFontDatabase.applicationFontFamilies(font_id))
            for family in _FONT_CANDIDATES:
                if family in registered:
                    return family
    raise ReportPdfExportError(
        "未找到可用中文字体（macOS 可使用苹方/冬青黑体/宋体，Windows 可使用微软雅黑/宋体），"
        "已取消 PDF 导出以避免生成乱码。"
    )
