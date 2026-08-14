"""Разговор: принимаем голосовое и текст, круг считает воркер."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.hsk import hsk_keyboard
from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.db.models import User
from app.logging import get_logger

router = Router(name="voice")
log = get_logger("bot")


async def _needs_level(message: Message, user: User) -> bool:
    if user.hsk_level:
        return False
    await message.answer(ru.CHOOSE_LEVEL_FIRST, reply_markup=hsk_keyboard())
    return True


@router.message(F.voice)
async def on_voice(
    message: Message,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> None:
    if await _needs_level(message, user):
        return

    settings = get_settings()
    voice = message.voice
    if voice.duration > settings.max_voice_duration_sec:
        await message.answer(ru.VOICE_TOO_LONG.format(limit=settings.max_voice_duration_sec))
        await track(session, "voice_too_long", user_id=user.id, секунд=voice.duration)
        return

    await track(session, "voice_received", user_id=user.id, секунд=voice.duration)
    await queue.enqueue_job(
        "process_voice_round",
        user_id=str(user.id),
        chat_id=message.chat.id,
        request_id=request_id,
        file_id=voice.file_id,
    )
    log.info("голосовое поставлено в очередь", user_id=str(user.id), секунд=voice.duration)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> None:
    if await _needs_level(message, user):
        return

    await track(session, "text_received", user_id=user.id, знаков=len(message.text or ""))
    await queue.enqueue_job(
        "process_voice_round",
        user_id=str(user.id),
        chat_id=message.chat.id,
        request_id=request_id,
        text=message.text,
    )
    log.info("текст поставлен в очередь", user_id=str(user.id))


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(ru.UNSUPPORTED_CONTENT)
