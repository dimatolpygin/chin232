"""Вход: /start и выбор уровня HSK.

От запуска до первой сказанной фразы не больше двух нажатий.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.hsk import LEVEL_PREFIX, hsk_keyboard
from app.bot.texts import ru
from app.core.services.dialog import DEFAULT_HSK, set_hsk_level
from app.core.services.onboarding import handle_start
from app.db.models import User
from app.logging import get_logger

router = Router(name="start")
log = get_logger("bot")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    user: User,
    is_new_user: bool,
    queue,
    request_id: str,
) -> None:
    result = await handle_start(session, user, is_new_user)
    if result.need_level:
        await message.answer(ru.WELCOME, reply_markup=hsk_keyboard())
    else:
        await message.answer(ru.WELCOME_BACK)
    log.info("ответ отправлен", user_id=str(user.id), нужен_уровень=result.need_level)


@router.callback_query(F.data.startswith(f"{LEVEL_PREFIX}:"))
async def choose_level(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> None:
    code = (callback.data or "").split(":", 1)[1]
    await set_hsk_level(session, user, DEFAULT_HSK if code == "unknown" else code)

    if code == "unknown":
        text = ru.LEVEL_UNKNOWN_FALLBACK
    else:
        text = ru.LEVEL_CHOSEN.format(level=ru.LEVEL_TITLES.get(user.hsk_level, user.hsk_level))

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text)
    await callback.answer()

    # Первая фраза уходит в очередь: тяжёлое не выполняется в обработчике апдейта.
    await queue.enqueue_job(
        "greet_user",
        user_id=str(user.id),
        chat_id=callback.message.chat.id,
        request_id=request_id,
    )
    log.info("приветствие поставлено в очередь", user_id=str(user.id), уровень=user.hsk_level)
