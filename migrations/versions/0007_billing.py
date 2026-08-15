"""Биллинг: plans, subscriptions, payments и почта покупателя

Revision ID: 0007_billing
Revises: 0006_limits
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_billing"
down_revision: str | None = "0006_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Тариф — строка в базе, а не константа в коде: годовой тариф, промокоды и
# подарки (ТЗ 7.7) добавляются записью, а не правкой биллинга. Цена здесь
# показывается юзеру, а списывает деньги lava по своему офферу, поэтому обе
# цифры обязаны совпадать — расхождение попадёт в лог при создании счёта.
PLAN = {
    "code": "monthly",
    "title": "Подписка на месяц",
    "price": 590,
    "currency": "RUB",
    "duration_days": 30,
    "periodicity": "MONTHLY",
}


def upgrade() -> None:
    # Почта нужна платёжке: счёт в lava.top выставляется на email покупателя,
    # туда же уходит чек. Спрашиваем один раз перед первой оплатой.
    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))

    op.create_table(
        "plans",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="RUB"),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        # Периодичность в терминах lava: MONTHLY, PERIOD_YEAR и так далее.
        sa.Column("periodicity", sa.String(32), nullable=False, server_default="MONTHLY"),
        # Идентификатор цены на стороне платёжки. Пусто — оплату включить
        # нельзя, и бот честно скажет об этом вместо битой ссылки.
        sa.Column("offer_id", sa.String(64), nullable=True),
        # Что тариф делает с лимитами. Пусто — безлимит.
        sa.Column("limits", postgresql.JSONB(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.String(32), nullable=False),
        # active | expired | cancelled
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Откуда взялась: оплата, подарок, ручная выдача админом.
        sa.Column("source", sa.String(32), nullable=False, server_default="lavatop"),
        # Родительский контракт платёжки: по нему приходят продления.
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
    )
    # Главный запрос проекта: «есть ли у этого юзера действующая подписка».
    # Он выполняется на каждом сообщении, рядом со счётчиком лимита.
    op.create_index("ix_subscriptions_user_status", "subscriptions", ["user_id", "status"])
    op.create_index("ix_subscriptions_external", "subscriptions", ["external_id"])

    op.create_table(
        "payments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="lavatop"),
        # Идентификатор контракта у платёжки. Уникален вместе с провайдером —
        # на этом стоит идемпотентность: повторный вебхук не продлит подписку.
        sa.Column("external_id", sa.String(128), nullable=False),
        # Контракт первой покупки, если это продление.
        sa.Column("parent_external_id", sa.String(128), nullable=True),
        sa.Column("plan_code", sa.String(32), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        # pending | completed | failed | cancelled
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        # Тело вебхука целиком: при разборе спорной оплаты пересказ не поможет.
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider", "external_id", name="uq_payments_provider_external"),
    )
    op.create_index("ix_payments_user", "payments", ["user_id"])
    op.create_index("ix_payments_parent", "payments", ["parent_external_id"])

    op.execute(
        sa.text(
            "INSERT INTO plans (code, title, price, currency, duration_days, periodicity) "
            "VALUES (:code, :title, :price, :currency, :duration_days, :periodicity) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(**PLAN)
    )


def downgrade() -> None:
    op.drop_index("ix_payments_parent", table_name="payments")
    op.drop_index("ix_payments_user", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_subscriptions_external", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_status", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
    op.drop_column("users", "email")
