from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PronunciationCheck(Base):
    """Одна попытка «повтори за мной» и её оценка.

    Баллы вынесены в колонки, чтобы этап 7 (прогресс) считал динамику запросом,
    а не разбором JSON. Сам ответ сервиса при этом сохраняется целиком: в нём
    есть посимвольная и пофонемная разбивка, которая сегодня рисуется частично,
    а завтра понадобится вся — переспросить сервис по той же записи уже нельзя.
    """

    __tablename__ = "pronunciation_checks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Реплика, с которой брался эталон. Разговор чистится — оценка остаётся.
    dialog_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("dialogs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    ref_text: Mapped[str] = mapped_column(Text, nullable=False)
    overall: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pronunciation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tone: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fluency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    integrity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PronunciationCheck {self.overall} user={self.user_id}>"
