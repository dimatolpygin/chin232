from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ServiceCall(Base):
    """Вызов внешнего сервиса: сколько занял, сколько израсходовал, чей был.

    Одна строка на каждое обращение к OpenRouter, Whisper, Fish, SpeechSuper и
    платёжке. Из неё админка считает расход по каждому сервису — по логам это
    посчитать нельзя, логи ротируются.
    """

    __tablename__ = "service_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    # Юзер известен не всегда: прогрев соединений и вебхуки ничьи.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Токены, секунды звука, знаки — что именно, написано в `unit`.
    units: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, server_default="0")
    unit: Mapped[str] = mapped_column(String(16), nullable=False, server_default="запросов")
    # Доллары. Оценка по прайсу провайдера, а не выставленный счёт.
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ServiceCall {self.provider}/{self.operation} {self.cost}$>"
