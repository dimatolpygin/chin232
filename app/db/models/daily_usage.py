from __future__ import annotations

import uuid
from datetime import date as date_type

from sqlalchemy import Date, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DailyUsage(Base):
    """Расход дневного лимита по дням.

    Оперативный счётчик живёт в redis и умирает в полночь, а история нужна
    надолго: без неё этап 7 (прогресс) и статистика админки посчитают пустоту.
    Дата — местная дата пользователя, а не UTC: лимит сбрасывается по его
    полуночи, и строка должна совпадать с тем, что он видел под ответом.
    """

    __tablename__ = "daily_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DailyUsage {self.user_id} {self.date} m={self.messages} c={self.checks}>"
