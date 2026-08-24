"""Scope deterministic document and evidence identifiers to a run.

Revision ID: 0002_run_scoped_identifiers
Revises: 0001_initial
"""

from __future__ import annotations

from alembic import op

from paper_reviewer.adapters.persistence.database import (
    upgrade_sqlite_run_scoped_identifiers,
)

revision = "0002_run_scoped_identifiers"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("run-scoped identifier migration currently supports SQLite only")
    upgrade_sqlite_run_scoped_identifiers(bind)


def downgrade() -> None:
    # A downgrade could merge identifiers that legitimately occur in multiple
    # runs and would therefore require deleting history. Keep the safe schema.
    raise RuntimeError("run-scoped identifiers cannot be downgraded without data loss")
