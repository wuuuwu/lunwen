"""Persist structured evaluation artifacts and human hard-rule decisions.

Revision ID: 0003_evaluation_persistence
Revises: 0002_run_scoped_identifiers
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    inspect,
)

revision = "0003_evaluation_persistence"
down_revision = "0002_run_scoped_identifiers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("artifacts"):
        columns = {str(column["name"]) for column in inspector.get_columns("artifacts")}
        if "payload_json" not in columns:
            op.add_column("artifacts", Column("payload_json", Text(), nullable=True))

    if not inspector.has_table("hard_rule_decisions"):
        op.create_table(
            "hard_rule_decisions",
            Column("decision_id", Integer(), primary_key=True, autoincrement=True),
            Column("run_id", String(64), nullable=False),
            Column("rule_id", String(160), nullable=False),
            Column("decision", String(24), nullable=False),
            Column("confirmed", Boolean(), nullable=False, server_default="0"),
            Column("dismissed", Boolean(), nullable=False, server_default="0"),
            Column("reviewer", String(200), nullable=False),
            Column("reason", Text(), nullable=False),
            Column("decided_at", DateTime(timezone=True), nullable=False),
            ForeignKeyConstraint(["run_id"], ["runs.run_id"]),
        )
        op.create_index(
            "ix_hard_rule_decisions_run_id", "hard_rule_decisions", ["run_id"]
        )
        op.create_index(
            "ix_hard_rule_decisions_rule_id", "hard_rule_decisions", ["rule_id"]
        )
        op.create_index(
            "ix_hard_rule_decisions_decided_at", "hard_rule_decisions", ["decided_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("hard_rule_decisions"):
        op.drop_index("ix_hard_rule_decisions_decided_at", table_name="hard_rule_decisions")
        op.drop_index("ix_hard_rule_decisions_rule_id", table_name="hard_rule_decisions")
        op.drop_index("ix_hard_rule_decisions_run_id", table_name="hard_rule_decisions")
        op.drop_table("hard_rule_decisions")
    if inspector.has_table("artifacts"):
        columns = {str(column["name"]) for column in inspector.get_columns("artifacts")}
        if "payload_json" in columns:
            # SQLite does not support DROP COLUMN on all versions used by the
            # application.  Retain the additive column on downgrade rather
            # than risking loss of legacy artifact rows.
            if bind.dialect.name != "sqlite":
                op.drop_column("artifacts", "payload_json")
