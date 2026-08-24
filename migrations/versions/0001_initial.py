"""Initial paper reviewer persistence schema."""

from __future__ import annotations

from alembic import op

from paper_reviewer.adapters.persistence.database import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS document_blocks_fts "
            "USING fts5(run_id UNINDEXED, block_id UNINDEXED, text)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS document_blocks_fts")
    Base.metadata.drop_all(bind=bind)
