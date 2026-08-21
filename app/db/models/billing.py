"""Тариф, подписка и платёж.

Подписка — сущность, а не флажок в `users` (ТЗ 7.7). Годовой тариф, промокод,
подарок и рефералка добавляются строкой в `plans` и записью в `subscriptions`,
не переделывая биллинг.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Статусы подписки.
SUB_ACTIVE = "active"
SUB_EXPIRED = "expired"
SUB_CANCELLED = "cancelled"

# Статусы платежа.
PAY_PENDING = "pending"
PAY_COMPLETED = "completed"
PAY_FAILED = "failed"
PAY_CANCELLED = "cancelled"
PAY_REFUNDED = "refunded"


class Plan(Base):
    """Тариф: что покупают и на сколько."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="RUB")
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    periodicity: Mapped[str] = mapped_column(String(32), nullable=False, server_default="MONTHLY")
    # Идентификатор цены у платёжки. Пусто — оплату включить нельзя.
    offer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Провайдер и способ оплаты у платёжки. Пусто — на усмотрение платёжки:
    # для рублей она сама подставит карту через SMART_GLOCAL.
    payment_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Списывается ли следующий платёж сам. У СБП автосписаний не бывает, такой
    # тариф продаётся разово, и человеку нужно напоминание перед окончанием.
    autorenew: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Чем тариф отличается по лимитам. Пусто — безлимит.
    limits: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Plan {self.code} {self.price} {self.currency}>"


class Subscription(Base):
    """Право пользоваться ботом без дневного лимита до определённой даты."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("plans.code", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=SUB_ACTIVE)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="lavatop")
    # Контракт первой покупки у платёжки: по нему приходят продления.
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Когда напомнили об окончании. Нужно только тарифам без автопродления,
    # чтобы напоминание ушло один раз, а не каждый час до самого конца.
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Subscription {self.user_id} {self.status} до {self.expires_at}>"


class Payment(Base):
    """Движение денег. Пишем и удачные, и сорвавшиеся.

    Пара «провайдер + внешний идентификатор» уникальна: на ней стоит
    идемпотентность вебхука, иначе повторная доставка продлит подписку дважды.
    """

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_payments_provider_external"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, server_default="lavatop")
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=PAY_PENDING)
    raw: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Payment {self.provider}:{self.external_id} {self.status}>"
