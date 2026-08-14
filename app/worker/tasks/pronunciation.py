"""Оценка произношения в очереди.

Круг оценки тяжёлый ровно так же, как разговорный: скачивание из Telegram,
ffmpeg и вызов внешнего сервиса. В обработчике апдейта ему делать нечего.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.bot.keyboards.answer import result_keyboard
from app.bot.render import render_result
from app.bot.texts import ru
from app.core.providers.base import ProviderError, SpeechUnclear
from app.core.services.pronunciation import PracticeTarget, assess_attempt, stop_practice
from app.db.models import User
from app.db.session import session_scope
from app.logging import bind_request, get_logger
from app.worker.tasks.voice import download_voice

log = get_logger("worker")


async def process_pronunciation(
    ctx: dict[str, Any],
    user_id: str,
    chat_id: int,
    file_id: str,
    dialog_id: int,
    ref_text: str,
    pinyin: str = "",
    from_correction: bool = False,
    audio_file_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """Оценить запись юзера по эталону и показать разбор по иероглифам.

    Эталон приезжает аргументами, а не читается из redis заново: пока задача
    ждала очереди, юзер мог нажать «Ещё раз» и сменить фразу — оценивать надо
    ту, под которую он записывался.
    """
    bind_request(request_id, user_id=user_id, job=ctx.get("job_id"))
    bot = ctx["bot"]
    queue = ctx.get("redis")
    started = time.monotonic()
    target = PracticeTarget(
        dialog_id=dialog_id,
        ref_text=ref_text,
        pinyin=pinyin,
        translation=None,
        from_correction=from_correction,
        audio_file_id=audio_file_id,
    )

    try:
        audio = await download_voice(bot, file_id)
        async with session_scope() as session:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                log.error("пользователь не найден", user_id=user_id)
                return
            result = await assess_attempt(session, user, audio, target)
            # Режим гасим только после удачной оценки: после «не расслышал» юзер
            # должен просто нажать запись ещё раз, а не искать кнопку заново.
            if queue is not None:
                await stop_practice(queue, user)
        await bot.send_message(
            chat_id, render_result(result), reply_markup=result_keyboard(dialog_id)
        )
        log.info(
            "оценка отправлена юзеру",
            chat_id=chat_id,
            балл=result.overall,
            длительность_сек=round(time.monotonic() - started, 2),
        )
    except SpeechUnclear as exc:
        # Не авария: сервис услышал шум или тишину. Режим остаётся включённым,
        # следующее голосовое снова уйдёт на оценку той же фразы.
        log.info("запись не разобрана сервисом оценки", причина=str(exc), эталон=ref_text)
        await _say(bot, chat_id, ru.PRON_UNCLEAR, dialog_id)
    except ProviderError as exc:
        log.error(
            "сервис оценки не ответил",
            провайдер=exc.provider,
            операция=exc.operation,
            http_код=exc.status_code,
            тело_ответа=(exc.body or "")[:1000],
        )
        await _say(bot, chat_id, ru.PRON_FAILED, dialog_id)
    except Exception as exc:  # noqa: BLE001  падение оценки не должно ронять воркер
        log.exception("оценка произношения оборвалась", ошибка=repr(exc))
        await _say(bot, chat_id, ru.ERROR_GENERIC, dialog_id)


async def _say(bot: Any, chat_id: int, text: str, dialog_id: int) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=result_keyboard(dialog_id))
    except Exception:  # noqa: BLE001  бот мог быть заблокирован юзером
        log.warning("не удалось отправить ответ об ошибке оценки", chat_id=chat_id)
