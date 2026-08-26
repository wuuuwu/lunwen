from __future__ import annotations

import csv
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from paper_reviewer.domain.batch import BatchItem, BatchRecord
from paper_reviewer.domain.submission import SubmissionMetadata

_INVALID_WINDOWS_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_REPORT_SUFFIX = "_课程论文评测报告.pdf"
_MAX_FILENAME_UTF16_UNITS = 240
_MAX_WINDOWS_PATH_UTF16_UNITS = 259


def sanitize_filename_component(
    value: str,
    *,
    fallback: str,
    max_utf16_units: int = 60,
) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    normalized = _INVALID_WINDOWS_FILENAME.sub("_", normalized).rstrip(" .")
    if not normalized:
        normalized = fallback
    if normalized.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        normalized = f"_{normalized}"
    normalized = _truncate_utf16(normalized, max_utf16_units).rstrip(" .")
    return normalized or fallback


def build_report_filename(metadata: SubmissionMetadata, run_id: str) -> str:
    components = (
        sanitize_filename_component(metadata.student_name, fallback="未识别姓名"),
        sanitize_filename_component(metadata.student_id, fallback="未识别学号"),
        sanitize_filename_component(metadata.major, fallback="未识别专业"),
        sanitize_filename_component(
            metadata.paper_title,
            fallback="未识别题目",
            max_utf16_units=90,
        ),
    )
    base = "_".join(components)
    available = _MAX_FILENAME_UTF16_UNITS - _utf16_units(_REPORT_SUFFIX)
    base = _truncate_utf16(base, available).rstrip(" ._") or "未识别论文"
    return f"{base}{_REPORT_SUFFIX}"


def allocate_report_path(output_dir: Path, metadata: SubmissionMetadata, run_id: str) -> Path:
    """Choose a deterministic, non-overwriting report path."""

    output_dir = output_dir.resolve(strict=False)
    initial = build_report_filename(metadata, run_id)
    filename = _fit_report_filename(
        output_dir,
        initial.removesuffix(_REPORT_SUFFIX),
        unique_suffix="",
        fallback="未识别论文",
    )
    existing = (
        {entry.name.casefold() for entry in output_dir.iterdir()}
        if output_dir.is_dir()
        else set()
    )
    if filename.casefold() not in existing:
        return output_dir / filename

    run_suffix = f"__{sanitize_filename_component(run_id[:8], fallback='run', max_utf16_units=8)}"
    suffix_units = _utf16_units(run_suffix + _REPORT_SUFFIX)
    stem = filename.removesuffix(_REPORT_SUFFIX)
    stem = _truncate_utf16(stem, _MAX_FILENAME_UTF16_UNITS - suffix_units).rstrip(" ._")
    candidate = _fit_report_filename(
        output_dir,
        stem,
        unique_suffix=run_suffix,
        fallback="报告",
    )
    counter = 2
    while candidate.casefold() in existing:
        numbered_suffix = f"{run_suffix}_{counter}"
        suffix_units = _utf16_units(numbered_suffix + _REPORT_SUFFIX)
        shortened = _truncate_utf16(stem, _MAX_FILENAME_UTF16_UNITS - suffix_units).rstrip(" ._")
        candidate = _fit_report_filename(
            output_dir,
            shortened,
            unique_suffix=numbered_suffix,
            fallback="报告",
        )
        counter += 1
    return output_dir / candidate


def write_batch_summary_csv(
    destination: Path,
    batch: BatchRecord,
    dimensions: Sequence[tuple[str, str]],
) -> None:
    """Atomically write a spreadsheet-safe, dynamically-columned batch summary."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    _claim_output_directory(destination, batch.batch_id)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    dimension_columns = _dimension_columns(dimensions)
    headers = [
        "原文件名",
        "姓名",
        "学号",
        "专业",
        "题目",
        "元数据置信度",
        "元数据待核对",
        "重复PDF内容",
        *(column for _, column in dimension_columns),
        "总分",
        "等级",
        "结论",
        "任务状态",
        "PDF文件名",
        "错误摘要",
    ]
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for item in batch.items:
                writer.writerow(_csv_row(item, dimension_columns))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _csv_row(item: BatchItem, dimensions: Sequence[tuple[str, str]]) -> dict[str, object]:
    metadata = item.metadata
    row: dict[str, object] = {
        "原文件名": _spreadsheet_safe(item.source.filename),
        "姓名": _spreadsheet_safe(metadata.student_name if metadata else "未识别姓名"),
        "学号": _spreadsheet_safe(metadata.student_id if metadata else "未识别学号"),
        "专业": _spreadsheet_safe(metadata.major if metadata else "未识别专业"),
        "题目": _spreadsheet_safe(metadata.paper_title if metadata else "未识别题目"),
        "元数据置信度": _metadata_confidence(metadata),
        "元数据待核对": "是" if metadata is None or metadata.needs_review else "否",
        "重复PDF内容": "是" if item.source.duplicate_sha256 else "否",
        "总分": "" if item.total_score is None else item.total_score,
        "等级": _spreadsheet_safe(item.grade or ""),
        "结论": _spreadsheet_safe(item.conclusion or ""),
        "任务状态": item.status.value,
        "PDF文件名": _spreadsheet_safe(item.report_path.name if item.report_path else ""),
        "错误摘要": _spreadsheet_safe(item.error or ""),
    }
    for dimension_id, title in dimensions:
        value = item.dimension_scores.get(dimension_id)
        row[title] = "" if value is None else value
    return row


def _metadata_confidence(metadata: SubmissionMetadata | None) -> str:
    if metadata is None:
        return ""
    values = [detail.confidence for detail in metadata.field_evidence.values()]
    return "" if not values else f"{sum(values) / len(values):.2f}"


def _dimension_columns(dimensions: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    title_counts: dict[str, int] = {}
    for _, title in dimensions:
        title_counts[title] = title_counts.get(title, 0) + 1
    return [
        (
            dimension_id,
            _spreadsheet_safe(
                title if title_counts[title] == 1 else f"{title}（{dimension_id}）"
            ),
        )
        for dimension_id, title in dimensions
    ]


def _claim_output_directory(destination: Path, batch_id: str) -> None:
    """Claim one summary path without monopolizing the entire output folder."""

    owner_path = destination.with_name(f".{destination.name}.owner")
    try:
        descriptor = os.open(
            owner_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        try:
            owner = owner_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise PermissionError("无法验证批次输出目录归属。") from error
        if owner != batch_id:
            raise FileExistsError("该汇总文件路径已由另一个课程论文批次使用。") from None
        return
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="") as handle:
            handle.write(batch_id)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        owner_path.unlink(missing_ok=True)
        raise
    if destination.exists():
        owner_path.unlink(missing_ok=True)
        raise FileExistsError("目标位置已存在未归属的课程论文评测汇总 CSV。")


def _spreadsheet_safe(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _truncate_utf16(value: str, maximum_units: int) -> str:
    if maximum_units <= 0:
        return ""
    result: list[str] = []
    used = 0
    for character in value:
        units = _utf16_units(character)
        if used + units > maximum_units:
            break
        result.append(character)
        used += units
    return "".join(result)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _fit_report_filename(
    output_dir: Path,
    stem: str,
    *,
    unique_suffix: str,
    fallback: str,
) -> str:
    directory_units = _utf16_units(str(output_dir)) + 1
    available = min(_MAX_FILENAME_UTF16_UNITS, _MAX_WINDOWS_PATH_UTF16_UNITS - directory_units)
    tail = unique_suffix + _REPORT_SUFFIX
    if available <= _utf16_units(tail):
        raise OSError("batch output directory path is too long")
    fitted = _truncate_utf16(stem, available - _utf16_units(tail)).rstrip(" ._")
    if not fitted:
        fitted = _truncate_utf16(fallback, available - _utf16_units(tail))
    return f"{fitted}{tail}"
