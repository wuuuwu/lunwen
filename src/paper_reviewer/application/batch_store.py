from __future__ import annotations

import hashlib
import importlib
import os
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from uuid import uuid4

from paper_reviewer.domain.batch import (
    BatchItem,
    BatchRecord,
    BatchReviewRequest,
    BatchSourceSnapshot,
)

_MANIFEST_NAME = "batch.json"
_EXECUTION_LOCK_NAME = ".execution.lock"
_HASH_CHUNK_SIZE = 1024 * 1024
_PROCESS_LOCKS: dict[str, Lock] = {}
_PROCESS_LOCKS_GUARD = Lock()


class BatchSourceChangedError(ValueError):
    """Raised when a source no longer matches its immutable batch snapshot."""


class BatchExecutionInProgressError(RuntimeError):
    """Raised when another caller already owns a batch execution lease."""


@dataclass(frozen=True, slots=True)
class BatchLoadError:
    """Safe diagnostic for one manifest skipped during batch listing."""

    batch_id: str
    message: str


class BatchStore:
    """Validated, atomic access to versioned batch manifests."""

    def __init__(self, batches_root: Path) -> None:
        self.batches_root = batches_root

    def batch_dir(self, batch_id: str) -> Path:
        candidate = Path(batch_id)
        if candidate.name != batch_id or batch_id in {"", ".", ".."}:
            raise ValueError(f"invalid batch id: {batch_id}")
        return self.batches_root / batch_id

    def manifest_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / _MANIFEST_NAME

    def execution_lock_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / _EXECUTION_LOCK_NAME

    @contextmanager
    def execution_lock(self, batch_id: str) -> Iterator[None]:
        """Prevent concurrent execution of one batch in this or another process."""

        with _batch_execution_lock(self.execution_lock_path(batch_id)):
            yield

    def exists(self, batch_id: str) -> bool:
        return self.manifest_path(batch_id).is_file()

    def create(self, record: BatchRecord) -> None:
        destination = self.manifest_path(record.batch_id)
        if destination.exists():
            raise FileExistsError(f"batch already exists: {record.batch_id}")
        self._write_atomic(destination, record)

    def save(self, record: BatchRecord) -> None:
        destination = self.manifest_path(record.batch_id)
        if destination.exists():
            # Refuse to replace a manifest that is no longer valid.  A damaged
            # recovery source must remain available for diagnosis.
            self.load(record.batch_id)
        self._write_atomic(destination, record)

    def load(self, batch_id: str) -> BatchRecord:
        destination = self.manifest_path(batch_id)
        record = BatchRecord.model_validate_json(destination.read_text(encoding="utf-8"))
        if record.batch_id != batch_id:
            raise ValueError("batch manifest id does not match its directory")
        return record

    def list_records(self) -> list[BatchRecord]:
        records, _ = self.list_records_with_errors()
        return records

    def list_load_errors(self) -> list[BatchLoadError]:
        """Return safe diagnostics without exposing manifest contents or paths."""

        _, errors = self.list_records_with_errors()
        return errors

    def list_records_with_errors(self) -> tuple[list[BatchRecord], list[BatchLoadError]]:
        if not self.batches_root.is_dir():
            return [], []
        records: list[BatchRecord] = []
        errors: list[BatchLoadError] = []
        for entry in sorted(self.batches_root.iterdir(), key=lambda path: path.name.casefold()):
            try:
                if not entry.is_dir():
                    continue
                manifest = self.manifest_path(entry.name)
                if manifest.is_file():
                    records.append(self.load(entry.name))
            except (OSError, UnicodeError, ValueError):
                errors.append(
                    BatchLoadError(
                        batch_id=_safe_batch_id(entry.name),
                        message="批次清单损坏、版本不受支持或无法读取。",
                    )
                )
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records, errors

    @staticmethod
    def _write_atomic(destination: Path, record: BatchRecord) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _batch_execution_lock(lock_path: Path) -> Iterator[None]:
    key = os.path.normcase(str(lock_path.resolve(strict=False)))
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, Lock())
    if not process_lock.acquire(blocking=False):
        raise BatchExecutionInProgressError("该批次正在执行，请勿重复启动。")

    handle: BinaryIO | None = None
    file_locked = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _lock_file_nonblocking(handle)
        file_locked = True
        yield
    finally:
        if handle is not None:
            if file_locked:
                _unlock_file(handle)
            handle.close()
        process_lock.release()


def _lock_file_nonblocking(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        raise BatchExecutionInProgressError("该批次正在其他进程中执行。") from error


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # Closing the descriptor releases the OS lease even if an explicit
        # unlock races with process shutdown.
        pass


def _safe_batch_id(value: str) -> str:
    normalized = "".join(character for character in value if character.isprintable()).strip()
    return normalized[:64] or "<unknown>"


def scan_batch_sources(request: BatchReviewRequest) -> list[BatchItem]:
    """Snapshot top-level, non-symlink PDF files in deterministic order."""

    source_dir = request.source_dir
    if not source_dir.is_dir():
        raise ValueError("source directory does not exist")
    candidates = [
        path
        for path in source_dir.iterdir()
        if not path.is_symlink() and path.is_file() and path.suffix.casefold() == ".pdf"
    ]
    candidates.sort(
        key=lambda path: (unicodedata.normalize("NFKC", path.name).casefold(), path.name)
    )
    if not candidates:
        raise ValueError("source directory contains no top-level PDF files")
    if len(candidates) > 100:
        raise ValueError("a batch may contain at most 100 PDF files")

    snapshots = [_snapshot_source(path) for path in candidates]
    counts: dict[str, int] = {}
    for snapshot in snapshots:
        counts[snapshot.sha256] = counts.get(snapshot.sha256, 0) + 1

    items: list[BatchItem] = []
    for snapshot in snapshots:
        duplicate = counts[snapshot.sha256] > 1
        snapshot.duplicate_sha256 = duplicate
        warnings = ["检测到批次内内容相同的 PDF。"] if duplicate else []
        items.append(
            BatchItem(
                item_id=uuid4().hex,
                source=snapshot,
                warnings=warnings,
            )
        )
    return items


def validate_source_snapshot(snapshot: BatchSourceSnapshot) -> None:
    """Verify a source immediately before processing, including its content hash."""

    path = snapshot.path
    if path.is_symlink() or not path.is_file():
        raise BatchSourceChangedError(
            f"source PDF is missing or no longer a regular file: {snapshot.filename}"
        )
    current = _snapshot_source(path)
    if (
        current.size_bytes != snapshot.size_bytes
        or current.modified_time_ns != snapshot.modified_time_ns
        or current.sha256 != snapshot.sha256
    ):
        raise BatchSourceChangedError(
            f"source PDF changed after the batch was created: {snapshot.filename}"
        )


def _snapshot_source(path: Path) -> BatchSourceSnapshot:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise BatchSourceChangedError(f"source PDF changed while it was being scanned: {path.name}")
    return BatchSourceSnapshot(
        path=path.resolve(strict=True),
        filename=path.name,
        sha256=digest.hexdigest(),
        size_bytes=after.st_size,
        modified_time_ns=after.st_mtime_ns,
    )
