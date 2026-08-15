from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    """Пользователь продукта.

    Намеренно не привязан к telegram_id: способ входа вынесен в `identities`,
    иначе вебапп следующим этапом потребует миграции всей базы.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    hsk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    speech_speed: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Почта нужна платёжке: счёт выставляется на неё, туда же уходит чек.
    # Спрашивается один раз перед первой оплатой, для разговора не нужна.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    tz: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Europe/Moscow", server_default="Europe/Moscow"
    )

    identities: Mapped[list[Identity]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.id} hsk={self.hsk_level}>"
