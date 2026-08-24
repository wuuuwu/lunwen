from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    RunRepository,
)
from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceLevel
from paper_reviewer.domain.run import RunRecord


def _run(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        input_path=f"{run_id}.pdf",
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="rubric@1",
        provider="fake",
        model="fake",
    )


def _evidence(run_id: str, *, content: str = "shared evidence") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="deterministic-evidence",
        run_id=run_id,
        kind=EvidenceKind.EXTERNAL,
        title="Shared source",
        content=content,
        source_name="fixture",
        level=EvidenceLevel.METADATA,
    )


@pytest.mark.asyncio
async def test_identifiers_are_run_scoped_and_retries_replace_rows(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'scoped.db').as_posix()}")
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    runs = RunRepository(sessions)
    documents = DocumentRepository(sessions)
    evidence = EvidenceRepository(sessions)
    await runs.create(_run("run-one"))
    await runs.create(_run("run-two"))

    shared_block = DocumentBlock.create(
        document_id="same-document", page=1, text="shared paper content"
    )
    await documents.add_blocks("run-one", [shared_block])
    await documents.add_blocks("run-two", [shared_block])
    assert [row.block_id for row in await documents.list_blocks("run-one")] == [
        shared_block.block_id
    ]
    assert [row.block_id for row in await documents.list_blocks("run-two")] == [
        shared_block.block_id
    ]

    replacement = shared_block.model_copy(update={"text": "replacement unique-to-one"})
    await documents.add_blocks("run-one", [replacement])
    assert [row.text for row in await documents.list_blocks("run-one")] == [
        "replacement unique-to-one"
    ]
    assert [row.text for row in await documents.list_blocks("run-two")] == [
        "shared paper content"
    ]
    assert await documents.search("run-one", "replacement")
    assert not await documents.search("run-two", "replacement")

    await evidence.replace("run-one", [_evidence("run-one")])
    await evidence.replace("run-two", [_evidence("run-two")])
    await evidence.replace("run-one", [_evidence("run-one", content="replacement evidence")])
    assert [row.content for row in await evidence.list("run-one")] == [
        "replacement evidence"
    ]
    assert [row.content for row in await evidence.list("run-two")] == ["shared evidence"]

    duplicate_block = DocumentBlock.create(
        document_id="same-document", page=2, text="duplicate replacement"
    )
    with pytest.raises(IntegrityError):
        await documents.add_blocks("run-one", [duplicate_block, duplicate_block])
    assert [row.text for row in await documents.list_blocks("run-one")] == [
        "replacement unique-to-one"
    ]
    assert await documents.search("run-one", "replacement")

    duplicate_evidence = _evidence("run-one", content="duplicate replacement evidence")
    with pytest.raises(IntegrityError):
        await evidence.replace("run-one", [duplicate_evidence, duplicate_evidence])
    assert [row.content for row in await evidence.list("run-one")] == [
        "replacement evidence"
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_initialize_upgrades_create_all_legacy_database_without_data_loss(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id VARCHAR(64) PRIMARY KEY,
            status VARCHAR(40) NOT NULL,
            input_path TEXT NOT NULL,
            input_hash VARCHAR(64) NOT NULL,
            config_hash VARCHAR(64) NOT NULL,
            rubric_id VARCHAR(160) NOT NULL,
            provider VARCHAR(40) NOT NULL,
            model VARCHAR(160) NOT NULL,
            completed_stages_json TEXT NOT NULL,
            error TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE document_blocks (
            block_id VARCHAR(32) PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL REFERENCES runs(run_id),
            document_id VARCHAR(64) NOT NULL,
            page INTEGER NOT NULL,
            block_type VARCHAR(32) NOT NULL,
            section_path_json TEXT NOT NULL,
            text TEXT NOT NULL,
            bbox_json TEXT,
            content_hash VARCHAR(64) NOT NULL
        );
        CREATE INDEX ix_document_blocks_run_id ON document_blocks (run_id);
        CREATE INDEX ix_document_blocks_document_id ON document_blocks (document_id);
        CREATE TABLE evidence_items (
            evidence_id VARCHAR(64) PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL REFERENCES runs(run_id),
            kind VARCHAR(20) NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_name VARCHAR(80) NOT NULL,
            level VARCHAR(4) NOT NULL,
            doi VARCHAR(300),
            url TEXT,
            metadata_json TEXT NOT NULL
        );
        CREATE INDEX ix_evidence_items_run_id ON evidence_items (run_id);
        CREATE VIRTUAL TABLE document_blocks_fts
            USING fts5(run_id UNINDEXED, block_id UNINDEXED, text);
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-run",
            "retryable_failure",
            "paper.pdf",
            "a" * 64,
            "b" * 64,
            "rubric@1",
            "deepseek",
            "deepseek-chat",
            "[]",
            "UNIQUE constraint failed: document_blocks.block_id\n[SQL: INSERT ...]"
            "\n[parameters: secret expanded payload]",
            "2026-08-23 00:00:00",
            "2026-08-23 00:00:00",
        ),
    )
    block = DocumentBlock.create(document_id="doc", page=1, text="legacy paper content")
    connection.execute(
        "INSERT INTO document_blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            block.block_id,
            "legacy-run",
            block.document_id,
            block.page,
            block.block_type.value,
            "[]",
            block.text,
            None,
            block.content_hash,
        ),
    )
    connection.execute(
        "INSERT INTO document_blocks_fts VALUES (?, ?, ?)",
        ("legacy-run", block.block_id, block.text),
    )
    connection.execute(
        "INSERT INTO evidence_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "deterministic-evidence",
            "legacy-run",
            "external",
            "Legacy source",
            "legacy evidence",
            "fixture",
            "C",
            None,
            None,
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    await initialize_database(engine)
    await initialize_database(engine)
    sessions = create_session_factory(engine)

    async with engine.connect() as async_connection:
        block_pk = await async_connection.run_sync(
            lambda sync: inspect(sync).get_pk_constraint("document_blocks")[
                "constrained_columns"
            ]
        )
        evidence_pk = await async_connection.run_sync(
            lambda sync: inspect(sync).get_pk_constraint("evidence_items")[
                "constrained_columns"
            ]
        )
        has_alembic_version = await async_connection.run_sync(
            lambda sync: inspect(sync).has_table("alembic_version")
        )
        stored_error = (
            await async_connection.execute(
                text("SELECT error FROM runs WHERE run_id = 'legacy-run'")
            )
        ).scalar_one()
    assert block_pk == ["run_id", "block_id"]
    assert evidence_pk == ["run_id", "evidence_id"]
    assert not has_alembic_version
    assert stored_error == "UNIQUE constraint failed: document_blocks.block_id"
    assert [row.text for row in await DocumentRepository(sessions).list_blocks("legacy-run")] == [
        "legacy paper content"
    ]
    assert [row.content for row in await EvidenceRepository(sessions).list("legacy-run")] == [
        "legacy evidence"
    ]

    await RunRepository(sessions).create(_run("new-run"))
    await DocumentRepository(sessions).add_blocks("new-run", [block])
    await EvidenceRepository(sessions).replace("new-run", [_evidence("new-run")])
    assert await DocumentRepository(sessions).search("legacy-run", "legacy")
    assert await DocumentRepository(sessions).search("new-run", "legacy")
    await engine.dispose()
