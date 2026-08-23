"""Журнал вызовов внешних сервисов: расход по каждому виден в админке

Revision ID: 0010_service_calls
Revises: 0009_topic_prompt
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_service_calls"
down_revision: str | None = "0009_topic_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("units", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=16), nullable=False, server_default="запросов"),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Единственный запрос по этой таблице — «расход по сервисам за период»:
    # сначала отсечь по дате, потом сгруппировать. Индекс ровно под него.
    op.create_index(
        "ix_service_calls_created_provider", "service_calls", ["created_at", "provider"]
    )


def downgrade() -> None:
    op.drop_index("ix_service_calls_created_provider", table_name="service_calls")
    op.drop_table("service_calls")
