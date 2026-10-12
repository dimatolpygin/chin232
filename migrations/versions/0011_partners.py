"""Партнёры: блогер приводит юзеров по ссылке, оплаты засчитываются ему

Revision ID: 0011_partners
Revises: 0010_service_calls
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_partners"
down_revision: str | None = "0010_service_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partners",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_partners_code"),
    )

    # Обе колонки пустые у всех, кто был до партнёров: старые юзеры и старые
    # оплаты остаются ничьими, задним числом никого не закрепляем.
    op.add_column("users", sa.Column("partner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_users_partner", "users", "partners", ["partner_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_users_partner_id", "users", ["partner_id"])

    op.add_column(
        "payments", sa.Column("partner_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_payments_partner", "payments", "partners", ["partner_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_payments_partner_id", "payments", ["partner_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_partner_id", table_name="payments")
    op.drop_constraint("fk_payments_partner", "payments", type_="foreignkey")
    op.drop_column("payments", "partner_id")

    op.drop_index("ix_users_partner_id", table_name="users")
    op.drop_constraint("fk_users_partner", "users", type_="foreignkey")
    op.drop_column("users", "partner_id")

    op.drop_table("partners")
