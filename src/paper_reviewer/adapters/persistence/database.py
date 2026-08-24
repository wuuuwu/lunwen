from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    input_path: Mapped[str] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    rubric_id: Mapped[str] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(160))
    completed_stages_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunEventRow(Base):
    __tablename__ = "run_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    artifact_type: Mapped[str] = mapped_column(String(80))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    # Newer evaluation stages persist small, structured JSON results directly
    # in the database.  ``path`` and ``sha256`` remain for the original file
    # artifact contract; nullable keeps databases created by older releases
    # readable while they are upgraded in place by ``initialize_database``.
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class HardRuleDecisionRow(Base):
    """An append-only human decision for a policy or integrity hard rule.

    Decisions are deliberately kept separate from JSON artifacts.  This makes
    the audit trail queryable and preserves every correction/review instead of
    silently replacing the previous decision.  The application chooses the
    latest decision for a rule when it resumes a run.
    """

    __tablename__ = "hard_rule_decisions"

    decision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(160), index=True)
    decision: Mapped[str] = mapped_column(String(24))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewer: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class DocumentBlockRow(Base):
    __tablename__ = "document_blocks"
    __table_args__ = (PrimaryKeyConstraint("run_id", "block_id"),)

    block_id: Mapped[str] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    page: Mapped[int] = mapped_column(Integer)
    block_type: Mapped[str] = mapped_column(String(32))
    section_path_json: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str] = mapped_column(Text)
    bbox_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))


class ReviewResultRow(Base):
    __tablename__ = "review_results"
    __table_args__ = (UniqueConstraint("run_id", "reviewer_id"),)

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class EvidenceRow(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (PrimaryKeyConstraint("run_id", "evidence_id"),)

    evidence_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(80))
    level: Mapped[str] = mapped_column(String(4))
    doi: Mapped[str | None] = mapped_column(String(300), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


def create_engine(database_url: str) -> AsyncEngine:
    # Bound values can contain paper text. Keep them out of any unhandled
    # SQLAlchemy exception or diagnostic log as a defense in depth measure.
    return create_async_engine(database_url, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            await connection.run_sync(upgrade_sqlite_run_scoped_identifiers)
            await connection.run_sync(upgrade_sqlite_evaluation_artifacts)
        await connection.run_sync(Base.metadata.create_all)
        if connection.dialect.name == "sqlite":
            await connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS document_blocks_fts "
                    "USING fts5(run_id UNINDEXED, block_id UNINDEXED, text)"
                )
            )


def upgrade_sqlite_evaluation_artifacts(connection: Connection) -> None:
    """Add JSON artifact storage to databases created before the v2 flow.

    ``metadata.create_all`` intentionally does not alter an existing table.
    The desktop application has historically initialized databases directly
    (without an Alembic version table), so the one additive column needed by
    structured evaluation artifacts is applied here as an idempotent SQLite
    upgrade.  The hard-rule decision table is created by ``create_all`` below.
    """
    if connection.dialect.name != "sqlite":
        return
    inspector = inspect(connection)
    if not inspector.has_table("artifacts"):
        return
    columns = {str(column["name"]) for column in inspector.get_columns("artifacts")}
    if "payload_json" not in columns:
        connection.exec_driver_sql("ALTER TABLE artifacts ADD COLUMN payload_json TEXT")


def upgrade_sqlite_run_scoped_identifiers(connection: Connection) -> None:
    """Upgrade legacy global identifiers without discarding persisted run data.

    Early application builds used ``block_id`` and ``evidence_id`` as global
    primary keys. Both identifiers are deterministic from their content, so the
    same paper/evidence legitimately appears in more than one run. Existing app
    databases were created directly with ``metadata.create_all`` and therefore
    commonly have no Alembic version table; initialization must upgrade them in
    place.
    """
    if connection.dialect.name != "sqlite":
        return
    if inspect(connection).has_table("runs"):
        connection.exec_driver_sql(
            "UPDATE runs SET error = rtrim("
            "substr(error, 1, instr(error, char(10) || '[SQL:') - 1), char(13) || char(10)) "
            "WHERE error IS NOT NULL AND instr(error, char(10) || '[SQL:') > 0"
        )
    _rebuild_legacy_primary_key(
        connection,
        table_name="document_blocks",
        legacy_primary_key=["block_id"],
        table=cast(Table, DocumentBlockRow.__table__),
    )
    _rebuild_legacy_primary_key(
        connection,
        table_name="evidence_items",
        legacy_primary_key=["evidence_id"],
        table=cast(Table, EvidenceRow.__table__),
    )


def _rebuild_legacy_primary_key(
    connection: Connection,
    *,
    table_name: str,
    legacy_primary_key: list[str],
    table: Table,
) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return
    primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
    if primary_key != legacy_primary_key:
        return

    # SQLite cannot alter a primary key. Rebuild the table inside the caller's
    # transaction, retaining every row and recreating declared indexes/FKs.
    legacy_name = f"_{table_name}_global_id_legacy"
    if inspector.has_table(legacy_name):
        raise RuntimeError(f"incomplete database migration: {legacy_name} already exists")
    indexes = inspector.get_indexes(table_name)
    connection.exec_driver_sql(f'ALTER TABLE "{table_name}" RENAME TO "{legacy_name}"')
    for index in indexes:
        name = index.get("name")
        if name:
            escaped_name = str(name).replace('"', '""')
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{escaped_name}"')

    table.create(connection, checkfirst=False)
    column_names = [column.name for column in table.columns]
    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    connection.exec_driver_sql(
        f'INSERT INTO "{table_name}" ({quoted_columns}) '
        f'SELECT {quoted_columns} FROM "{legacy_name}"'
    )
    connection.exec_driver_sql(f'DROP TABLE "{legacy_name}"')
