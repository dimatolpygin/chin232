"""Раздел «Прогресс»: серия дней, календарь месяца, счётчики и произношение.

Хендлер только показывает: считает всё `app/core/services/progress.py`, и тот
же расчёт потом откроет вебапп со словарём.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.render import render_progress
from app.bot.texts import ru
from app.core.events import track
from app.core.services.progress import load_progress
from app.db.models import User
from app.logging import get_logger

router = Router(name="progress")
log = get_logger("bot")


@router.message(Command("progress"))
@router.message(F.text == ru.MENU_PROGRESS)
async def cmd_progress(message: Message, session: AsyncSession, user: User) -> None:
    progress = await load_progress(session, user)
    # Без клавиатуры: нижнее меню уже на экране, а инлайн-кнопкам здесь некуда
    # вести — раздел показывает, а не настраивает.
    await message.answer(render_progress(progress))
    await track(
        session,
        "progress_opened",
        user_id=user.id,
        стрик=progress.streak,
        дней=progress.days_total,
    )
    log.info(
        "открыт раздел прогресса",
        user_id=str(user.id),
        стрик=progress.streak,
        пусто=progress.empty,
    )
