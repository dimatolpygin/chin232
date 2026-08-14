"""Сервис входа пользователя. Логика живёт здесь, не в хендлере.

На этапе 1 сюда же приедет выбор уровня HSK и первая фраза бота.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.texts import ru
from app.core.events import track
from app.db.models import User


@dataclass(slots=True)
class StartResult:
    text: str
    is_new_user: bool


async def handle_start(session: AsyncSession, user: User, is_new_user: bool) -> StartResult:
    await track(session, "start", user_id=user.id, is_new_user=is_new_user)
    text = ru.START_STUB if is_new_user else ru.START_BACK
    return StartResult(text=text, is_new_user=is_new_user)
