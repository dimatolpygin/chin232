"""Голосовой круг: STT → LLM → TTS → ffmpeg.

Логика целиком здесь, потому что на следующем этапе тот же круг дёргает вебапп
по HTTP, а не только хендлер aiogram.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.events import track
from app.core.providers.base import LlmReply
from app.core.providers.registry import get_llm, get_stt, get_tts
from app.core.services.recognition import ensure_recognized
from app.db.models import User
from app.db.repositories.dialogs import (
    ROLE_ASSISTANT,
    ROLE_USER,
    add_reply,
    get_prompt,
    recent_history,
)
from app.logging import get_logger

log = get_logger("dialog")

# Как уровень HSK объясняется модели. Уровень — параметр в базе, а не отдельная
# версия бота: подставляется в промпт и в скорость озвучки.
HSK_DESCRIPTIONS = {
    "hsk12": "HSK 1-2, начинающий: только самая частотная лексика, короткие простые предложения",
    "hsk34": "HSK 3-4, средний: бытовая лексика, сложносочинённые предложения",
    "hsk56": "HSK 5-6, продвинутый: богатая лексика, идиомы, развёрнутая живая речь",
}
DEFAULT_HSK = "hsk12"

# Медленнее для начинающих: на HSK 1-2 обычный темп неразборчив.
HSK_SPEED = {"hsk12": 0.8, "hsk34": 0.9, "hsk56": 1.0}

TOPIC_FREE = "свободная"


@dataclass(slots=True)
class VoiceAnswer:
    """Готовый ответ круга: голос, текст и разбор."""

    audio_ogg: bytes
    text_zh: str
    pinyin: str | None
    translation: str | None
    correction: str | None
    heard_text: str | None
    elapsed_sec: float


def _describe_level(user: User) -> str:
    return HSK_DESCRIPTIONS.get(user.hsk_level or DEFAULT_HSK, HSK_DESCRIPTIONS[DEFAULT_HSK])


def _speed(user: User) -> float:
    base = HSK_SPEED.get(user.hsk_level or DEFAULT_HSK, 1.0)
    # speech_speed — множитель из настроек пользователя (этап 6).
    return round(base * (user.speech_speed or 1.0), 2)


async def _synthesize(text: str, user: User, settings: Settings) -> bytes:
    from app.core.audio import to_voice_ogg

    speech = await get_tts(settings).synthesize(text, user.voice_id, _speed(user))
    return await to_voice_ogg(speech.audio, source_format=speech.fmt)


async def _ask_llm(
    session: AsyncSession,
    user: User,
    prompt_code: str,
    history: list[dict[str, str]],
    settings: Settings,
) -> LlmReply:
    template = await get_prompt(session, prompt_code)
    system_prompt = template.format(hsk_level=_describe_level(user), topic=user.topic or TOPIC_FREE)
    return await get_llm(settings).reply(system_prompt, history)


async def make_greeting(session: AsyncSession, user: User) -> VoiceAnswer:
    """Первая фраза после выбора уровня. Меню для этого не требуется."""
    settings = get_settings()
    started = time.monotonic()

    reply = await _ask_llm(session, user, "greeting", [], settings)
    audio = await _synthesize(reply.reply_zh, user, settings)

    await add_reply(
        session,
        user_id=user.id,
        role=ROLE_ASSISTANT,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
    )
    elapsed = round(time.monotonic() - started, 2)
    await track(session, "greeting_sent", user_id=user.id, длительность_сек=elapsed)
    log.info("приветствие собрано", user_id=str(user.id), длительность_сек=elapsed)

    return VoiceAnswer(
        audio_ogg=audio,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
        correction=None,
        heard_text=None,
        elapsed_sec=elapsed,
    )


async def run_voice_round(
    session: AsyncSession,
    user: User,
    audio: bytes | None = None,
    audio_filename: str = "voice.ogg",
    text: str | None = None,
) -> VoiceAnswer:
    """Полный круг. На входе либо голосовое, либо текст — принимаем и то, и то."""
    settings = get_settings()
    started = time.monotonic()

    if audio is not None:
        transcript = await get_stt(settings).transcribe(audio, audio_filename)
        # Выдуманный сервисом текст отсеиваем до того, как на него ответит модель.
        heard = ensure_recognized(transcript)
        log.info(
            "распознано",
            user_id=str(user.id),
            язык=transcript.language,
            секунд=transcript.duration_sec,
            текст=heard,
        )
        await track(
            session,
            "speech_recognized",
            user_id=user.id,
            язык=transcript.language,
            секунд=transcript.duration_sec,
        )
    else:
        heard = (text or "").strip()

    if not heard:
        raise ValueError("на входе круга пусто: ни голоса, ни текста")

    await add_reply(session, user_id=user.id, role=ROLE_USER, text_zh=heard)

    history = await recent_history(session, user.id, settings.dialog_history_limit)
    reply = await _ask_llm(session, user, "dialog_system", history, settings)
    audio_ogg = await _synthesize(reply.reply_zh, user, settings)

    await add_reply(
        session,
        user_id=user.id,
        role=ROLE_ASSISTANT,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
        correction=reply.correction,
    )

    elapsed = round(time.monotonic() - started, 2)
    await track(
        session,
        "voice_round_done",
        user_id=user.id,
        длительность_сек=elapsed,
        была_ошибка_юзера=bool(reply.correction),
    )
    log.info(
        "круг завершён",
        user_id=str(user.id),
        длительность_сек=elapsed,
        услышано=heard,
        ответ=reply.reply_zh,
    )

    return VoiceAnswer(
        audio_ogg=audio_ogg,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
        correction=reply.correction,
        heard_text=heard,
        elapsed_sec=elapsed,
    )


async def set_hsk_level(session: AsyncSession, user: User, level: str) -> None:
    user.hsk_level = level if level in HSK_DESCRIPTIONS else DEFAULT_HSK
    await session.flush()
    await track(session, "hsk_level_set", user_id=user.id, уровень=user.hsk_level)
    log.info("уровень HSK выбран", user_id=str(user.id), уровень=user.hsk_level)


def user_uuid(user: User) -> uuid.UUID:
    return user.id
