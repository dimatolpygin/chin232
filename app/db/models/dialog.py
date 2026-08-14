from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Dialog(Base):
    """Реплика диалога — и пользователя, и бота.

    Пиньинь и перевод считаются на шаге LLM и складываются сюда сразу: кнопка
    «Текст» на этапе 2 не должна делать новых платных вызовов.
    """

    __tablename__ = "dialogs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    text_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    pinyin: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Исправленная фраза собеседника иероглифами — эталон для «повтори за мной».
    # `correction` объясняет ошибку по-русски и произнести его нельзя, поэтому
    # правильный вариант модель отдаёт отдельным полем.
    corrected_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Готовый текст подсказки «Помощь». Кэш платного вызова: повторное нажатие
    # кнопки берёт его отсюда и не платит второй раз.
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Dialog {self.role} user={self.user_id}>"
