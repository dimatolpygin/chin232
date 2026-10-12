from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Partner(Base):
    """Тот, кто приводит юзеров за процент с их оплат (блогер, преподаватель).

    Юзер закрепляется за партнёром один раз, при первом входе по ссылке
    `?start=ref_<code>`. Процент хранится здесь, а не в платеже: бот только
    считает, сколько причитается, выплачивает клиент сам.
    """

    __tablename__ = "partners"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Код живёт в ссылке, поэтому только то, что Telegram пропускает в
    # параметре старта: латиница, цифры, `_` и `-`. Хранится в нижнем регистре.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default="0")
    # Отключённый партнёр не закрепляет новых, но его история оплат остаётся.
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Partner {self.code} {self.percent}%>"
