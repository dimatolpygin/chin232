"""Разговор: принимаем голосовое и текст, круг считает воркер."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.limits import spend
from app.bot.keyboards.hsk import hsk_keyboard
from app.bot.texts import ru
from app.config import get_settings
from app.core.events import track
from app.core.services.limits import KIND_CHECK, KIND_MESSAGE
from app.core.services.pronunciation import load_practice, stop_practice
from app.core.services.turn import finish_round, start_round
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

    if not await _hold(message, session, user, queue):
        return

    поставлено = False
    try:
        поставлено = await _route_voice(message, session, user, queue, request_id)
    finally:
        # Замок снимает воркер, когда круг закончился. Если задача не уехала —
        # упёрлась в лимит или сорвалась — снимаем прямо здесь, иначе юзер
        # прождал бы до конца TTL непонятно чего.
        if not поставлено:
            await finish_round(queue, str(user.id))


async def _hold(message: Message, session: AsyncSession, user: User, queue) -> bool:
    """Занять круг. False — предыдущая реплика ещё считается, юзеру сказано."""
    if await start_round(queue, str(user.id)):
        return True
    await message.answer(ru.ROUND_IN_PROGRESS)
    await track(session, "round_busy", user_id=user.id)
    log.info("реплика отбита замком круга", user_id=str(user.id))
    return False


async def _route_voice(
    message: Message,
    session: AsyncSession,
    user: User,
    queue,
    request_id: str,
) -> bool:
    """Отправить запись в оценку или в разговор. True — задача уехала в очередь."""
    voice = message.voice

    # Пока включён режим «повторите за мной», запись — это попытка произнести
    # эталон, а не реплика в разговоре. Проверяем до постановки в круг: иначе
    # бот ответит на попытку встречным вопросом, а оценки юзер не увидит вовсе.
    target = await load_practice(queue, user)
    if target is not None and target.ref_text:
        # Разбор произношения списывается со своего счётчика и именно здесь, в
        # момент записи: нажатие кнопки «Оценка» ещё ничего не разбирает.
        if await spend(message, session, user, queue, KIND_CHECK) is None:
            return False
        await queue.enqueue_job(
            "process_pronunciation",
            user_id=str(user.id),
            chat_id=message.chat.id,
            request_id=request_id,
            file_id=voice.file_id,
            dialog_id=target.dialog_id,
            ref_text=target.ref_text,
            pinyin=target.pinyin,
            from_correction=target.from_correction,
            audio_file_id=target.audio_file_id,
        )
        log.info(
            "запись отправлена на оценку",
            user_id=str(user.id),
            эталон=target.ref_text,
            секунд=voice.duration,
        )
        return True

    # Списываем до постановки в очередь: за стеной лимита не должно уйти ни
    # одного платного вызова, и проверить это можно прямо по логам воркера.
    if await spend(message, session, user, queue, KIND_MESSAGE) is None:
        return False

    await queue.enqueue_job(
        "process_voice_round",
        user_id=str(user.id),
        chat_id=message.chat.id,
        request_id=request_id,
        file_id=voice.file_id,
    )
    log.info("голосовое поставлено в очередь", user_id=str(user.id), секунд=voice.duration)
    return True


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

    # Текстом в режиме тренировки юзер возвращается к разговору: произнести
    # фразу текстом нельзя, и держать его в режиме против воли незачем.
    if await load_practice(queue, user) is not None:
        await stop_practice(queue, user)
        await message.answer(ru.PRACTICE_CANCELLED)

    await track(session, "text_received", user_id=user.id, знаков=len(message.text or ""))

    if not await _hold(message, session, user, queue):
        return

    поставлено = False
    try:
        # Текстовая реплика запускает тот же круг с теми же четырьмя сервисами,
        # значит и стоит столько же — считаем её тем же счётчиком.
        if await spend(message, session, user, queue, KIND_MESSAGE) is None:
            return
        await queue.enqueue_job(
            "process_voice_round",
            user_id=str(user.id),
            chat_id=message.chat.id,
            request_id=request_id,
            text=message.text,
        )
        поставлено = True
        log.info("текст поставлен в очередь", user_id=str(user.id))
    finally:
        if not поставлено:
            await finish_round(queue, str(user.id))


@router.message()
async def on_other(message: Message) -> None:
    await message.answer(ru.UNSUPPORTED_CONTENT)
