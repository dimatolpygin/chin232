"""Дневные лимиты: daily_usage и настройки лимитов в settings

Revision ID: 0006_limits
Revises: 0005_pronunciation
Create Date: 2026-08-15
"""
from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_limits"
down_revision: str | None = "0005_pronunciation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Числа лимитов лежат в базе, а не в коде: заказчику обещана правка из админки
# без деплоя. Значения взяты из ТЗ (раздел 7.6). В пробном периоде повышается
# только число голосовых — разборы произношения ТЗ не повышает, это самая
# дорогая операция проекта.
LIMITS = {
    "trial_days": 3,
    "trial_messages": 30,
    "trial_checks": 3,
    "messages": 10,
    "checks": 3,
}


def upgrade() -> None:
    op.create_table(
        "daily_usage",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checks", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "date"),
    )
    # Статистика читается по дню целиком (сколько народу практиковалось), а
    # первичный ключ начинается с user_id и такому запросу не помогает.
    op.create_index("ix_daily_usage_date", "daily_usage", ["date"])
    op.execute(
        sa.text(
            "INSERT INTO settings (key, value) VALUES ('limits', CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(sa.bindparam("value", json.dumps(LIMITS), type_=sa.Text()))
    )


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'limits'")
    op.drop_index("ix_daily_usage_date", table_name="daily_usage")
    op.drop_table("daily_usage")
