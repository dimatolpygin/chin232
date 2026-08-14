"""Голосовой круг в очереди.

Круг целиком уходит сюда, а не выполняется в обработчике апдейта: так он
переживает всплески нагрузки и сбои внешних сервисов, а бот в это время
продолжает обслуживать других юзеров.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from aiogram.types import BufferedInputFile

from app.bot.keyboards.answer import answer_keyboard
from app.bot.render import esc
from app.bot.texts import ru
from app.config import get_settings
from app.core.providers.base import ProviderError
from app.core.providers.http import get_client
from app.core.services.dialog import VoiceAnswer, make_greeting, run_voice_round
from app.core.services.recognition import NotRecognized
from app.db.models import User
from app.db.repositories.dialogs import set_audio_file_id
from app.db.session import session_scope
from app.logging import bind_request, get_logger

log = get_logger("worker")


async def _download_voice(bot: Any, file_id: str) -> bytes:
    """Скачать голосовое через общий прогретый клиент.

    Свой клиент, а не сессия aiogram: та живёт отдельным пулом и прогрева не
    видит. Канал до файловых серверов Telegram и так узкий — платить сверху
    ещё и за рукопожатие незачем.
    """
    settings = get_settings()
    file = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{settings.bot_token}/{file.file_path}"
    response = await get_client().get(url, timeout=120.0)
    response.raise_for_status()
    return response.content


async def _send_answer(bot: Any, chat_id: int, answer: VoiceAnswer) -> str | None:
    """Отправить голосовое и текст. Возвращает file_id голосового."""
    message = await bot.send_voice(
        chat_id,
        BufferedInputFile(answer.audio_ogg, filename="reply.ogg"),
        caption=esc(answer.text_zh)[:1000],
        # Кнопки разбора идут вместе с голосовым: отдельным сообщением они
        # отвязались бы от своей реплики в потоке диалога.
        reply_markup=answer_keyboard(answer.dialog_id),
    )
    if answer.correction:
        # Исправление отдельным блоком, чтобы не ломать голосовой поток.
        await bot.send_message(chat_id, ru.CORRECTION.format(text=esc(answer.correction)))
    return message.voice.file_id if message.voice else None


async def _fail(bot: Any, chat_id: int, exc: Exception, этап: str) -> None:
    """Сбой внешнего сервиса не должен ронять бота: юзер видит понятное сообщение."""
    if isinstance(exc, NotRecognized):
        # Это не авария, а просьба повторить: текст юзеру другой.
        log.info("речь не распознана", этап=этап, причина=str(exc))
        try:
            await bot.send_message(chat_id, ru.NOT_RECOGNIZED)
        except Exception:  # noqa: BLE001
            log.warning("не удалось отправить просьбу повторить", chat_id=chat_id)
        return
    if isinstance(exc, ProviderError):
        log.error(
            "круг оборван сбоем внешнего сервиса",
            этап=этап,
            провайдер=exc.provider,
            операция=exc.operation,
            http_код=exc.status_code,
            тело_ответа=(exc.body or "")[:1000],
        )
    else:
        log.exception("круг оборван ошибкой", этап=этап, ошибка=repr(exc))
    try:
        await bot.send_message(chat_id, ru.ERROR_GENERIC)
    except Exception:  # noqa: BLE001  бот мог быть заблокирован юзером
        log.warning("не удалось отправить сообщение об ошибке", chat_id=chat_id)


async def process_voice_round(
    ctx: dict[str, Any],
    user_id: str,
    chat_id: int,
    request_id: str | None = None,
    file_id: str | None = None,
    text: str | None = None,
) -> None:
    """Круг по голосовому или тексту пользователя."""
    bind_request(request_id, user_id=user_id, job=ctx.get("job_id"))
    bot = ctx["bot"]
    started = time.monotonic()

    try:
        audio: bytes | None = None
        if file_id:
            # Индикатор записи и скачивание идут параллельно: последовательно
            # это добавляло бы юзеру ожидание на ровном месте.
            audio, _ = await asyncio.gather(
                _download_voice(bot, file_id),
                bot.send_chat_action(chat_id, "record_voice"),
            )
            мс = round((time.monotonic() - started) * 1000)
            log.info(
                "голосовое скачано",
                байт=len(audio),
                длительность_мс=мс,
                # Скорость в логе не для красоты: узкий канал до Telegram —
                # главная статья расхода в бюджете круга.
                скорость_кбс=round(len(audio) / 1024 / max(мс / 1000, 0.001), 1),
            )

        async with session_scope() as session:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                log.error("пользователь не найден", user_id=user_id)
                return
            if not file_id:
                await bot.send_chat_action(chat_id, "record_voice")
            answer = await run_voice_round(
                session, user, audio=audio, text=text, started_at=started
            )
            voice_file_id = await _send_answer(bot, chat_id, answer)
            if voice_file_id:
                await set_audio_file_id(session, answer.dialog_id, voice_file_id)
        log.info("круг отправлен юзеру", длительность_сек=answer.elapsed_sec, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001  падение круга не должно ронять воркер
        await _fail(bot, chat_id, exc, "круг")


async def greet_user(
    ctx: dict[str, Any],
    user_id: str,
    chat_id: int,
    request_id: str | None = None,
) -> None:
    """Первая фраза сразу после выбора уровня."""
    bind_request(request_id, user_id=user_id, job=ctx.get("job_id"))
    bot = ctx["bot"]
    try:
        async with session_scope() as session:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                log.error("пользователь не найден", user_id=user_id)
                return
            await bot.send_chat_action(chat_id, "record_voice")
            answer = await make_greeting(session, user)
            voice_file_id = await _send_answer(bot, chat_id, answer)
            if voice_file_id:
                await set_audio_file_id(session, answer.dialog_id, voice_file_id)
        log.info("приветствие отправлено", chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        await _fail(bot, chat_id, exc, "приветствие")
