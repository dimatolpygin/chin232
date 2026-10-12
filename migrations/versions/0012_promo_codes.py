"""Промокоды: код скидки lava.top, привязанный к партнёру

Revision ID: 0012_promo_codes
Revises: 0011_partners
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_promo_codes"
down_revision: str | None = "0011_partners"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("code", sa.String(length=36), nullable=False),
        sa.Column("partner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("code"),
    )
    # Код, который юзер ввёл и который уйдёт в его следующий счёт. Без внешнего
    # ключа: отключённый или удалённый код ловится при выставлении счёта.
    op.add_column("users", sa.Column("promo_code", sa.String(length=36), nullable=True))
    op.add_column("payments", sa.Column("promo_code", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("payments", "promo_code")
    op.drop_column("users", "promo_code")
    op.drop_table("promo_codes")
