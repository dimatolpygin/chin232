"""Голосовой круг: STT → LLM → TTS → ffmpeg.

Логика целиком здесь, потому что на следующем этапе тот же круг дёргает вебапп
по HTTP, а не только хендлер aiogram.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.events import track
from app.core.providers.base import LlmReply, ProviderError, Transcript
from app.core.providers.registry import get_llm, get_stt, get_tts, get_tts_by_name
from app.core.services.recognition import (
    NotRecognized,
    ensure_recognized,
    has_han,
    strip_variant_prefix,
    try_recognize,
)
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
    # id реплики в `dialogs`. Через него кнопки «Текст» и «Помощь» находят
    # сохранённый разбор, поэтому клавиатура строится только после записи.
    dialog_id: int


def describe_level(user: User) -> str:
    return HSK_DESCRIPTIONS.get(user.hsk_level or DEFAULT_HSK, HSK_DESCRIPTIONS[DEFAULT_HSK])


def _speed(user: User) -> float:
    base = HSK_SPEED.get(user.hsk_level or DEFAULT_HSK, 1.0)
    # speech_speed — множитель из настроек пользователя (этап 6).
    return round(base * (user.speech_speed or 1.0), 2)


async def synthesize_voice(text: str, user: User, settings: Settings) -> bytes:
    """Озвучить ответ. При сбое основного сервиса пробуем запасной.

    На живой проверке Fish ответил 500 «Inference backend returned empty audio»
    и круг пропал — юзеру пришлось перезаписывать голосовое. Ради этого случая
    и заводился второй провайдер: чужая пятисотка не должна стоить пользователю
    целой реплики.
    """
    from app.core.audio import to_voice_ogg

    speed = _speed(user)
    try:
        speech = await get_tts(settings).synthesize(text, user.voice_id, speed)
    except ProviderError as exc:
        fallback = settings.tts_fallback_provider
        if not fallback or fallback == settings.tts_provider:
            raise
        log.warning(
            "основная озвучка сбойнула, идём на запасную",
            основной=settings.tts_provider,
            запасной=fallback,
            http_код=exc.status_code,
        )
        provider = get_tts_by_name(fallback, settings)
        # Голос у запасного сервиса свой, пользовательский id ему не подойдёт.
        speech = await provider.synthesize(text, None, speed)

    return await to_voice_ogg(speech.audio, source_format=speech.fmt)


async def _ask_llm(
    session: AsyncSession,
    user: User,
    prompt_code: str,
    history: list[dict[str, str]],
    settings: Settings,
) -> LlmReply:
    template = await get_prompt(session, prompt_code)
    system_prompt = template.format(hsk_level=describe_level(user), topic=user.topic or TOPIC_FREE)
    return await get_llm(settings).reply(system_prompt, history)


async def make_greeting(session: AsyncSession, user: User) -> VoiceAnswer:
    """Первая фраза после выбора уровня. Меню для этого не требуется."""
    settings = get_settings()
    started = time.monotonic()

    reply = await _ask_llm(session, user, "greeting", [], settings)
    audio = await synthesize_voice(reply.reply_zh, user, settings)

    row = await add_reply(
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
        dialog_id=row.id,
    )


async def _recognize(
    audio: bytes, filename: str, settings: Settings
) -> tuple[str, bool, Transcript]:
    """Распознать речь. Возвращает текст, признак двух вариантов и авто-разбор.

    Односекундная китайская фраза от человека с русским акцентом уверенно
    определяется как русская («你好» приходит как «Мил!»). Подсказка это не
    лечит, поэтому распознаём дважды — авто и с принудительным китайским — и,
    если результаты разошлись, отдаём оба модели: она понимает смысл и выберет.
    Оба запроса идут параллельно, так что время круга не растёт.
    """
    stt = get_stt(settings)
    auto, forced = await asyncio.gather(
        stt.transcribe(audio, filename),
        stt.transcribe(audio, filename, language="zh"),
        return_exceptions=True,
    )
    if isinstance(auto, BaseException) and isinstance(forced, BaseException):
        raise auto
    auto_t = None if isinstance(auto, BaseException) else auto
    forced_t = None if isinstance(forced, BaseException) else forced

    auto_text = try_recognize(auto_t) if auto_t else None
    forced_text = try_recognize(forced_t) if forced_t else None

    # Авто уверенно услышало китайский — разбирать нечего.
    if auto_text and (auto_t.language or "").lower() == "chinese":
        return auto_text, False, auto_t

    # Оба варианта живые и разные: пусть выбирает модель.
    if auto_text and forced_text and forced_text != auto_text and has_han(forced_text):
        return f"вариант 1: {auto_text} | вариант 2: {forced_text}", True, auto_t

    if auto_text:
        return auto_text, False, auto_t
    if forced_text:
        return forced_text, False, forced_t

    # Ни один проход не дал живой речи — это просьба повторить, а не сбой.
    if auto_t is not None:
        ensure_recognized(auto_t)
    raise NotRecognized("ни один проход распознавания не дал речи")


async def run_voice_round(
    session: AsyncSession,
    user: User,
    audio: bytes | None = None,
    audio_filename: str = "voice.ogg",
    text: str | None = None,
    started_at: float | None = None,
) -> VoiceAnswer:
    """Полный круг. На входе либо голосовое, либо текст — принимаем и то, и то.

    `started_at` — момент, с которого юзер ждёт ответ. Скачивание голосового из
    Telegram он ждёт наравне с остальным, поэтому отсчёт ведём оттуда, а не с
    распознавания: иначе метрика показывала бы число, которого никто не видит.
    """
    settings = get_settings()
    started = started_at if started_at is not None else time.monotonic()

    ambiguous = False
    if audio is not None:
        heard, ambiguous, transcript = await _recognize(audio, audio_filename, settings)
        log.info(
            "распознано",
            user_id=str(user.id),
            язык=transcript.language,
            секунд=transcript.duration_sec,
            текст=heard,
            два_варианта=ambiguous,
        )
        await track(
            session,
            "speech_recognized",
            user_id=user.id,
            язык=transcript.language,
            секунд=transcript.duration_sec,
            два_варианта=ambiguous,
        )
    else:
        heard = (text or "").strip()

    if not heard:
        raise ValueError("на входе круга пусто: ни голоса, ни текста")

    history = await recent_history(session, user.id, settings.dialog_history_limit)
    history.append({"role": ROLE_USER, "content": heard})
    reply = await _ask_llm(session, user, "dialog_system", history, settings)

    # Если вариантов было два, в базу и в историю идёт тот, что выбрала модель:
    # иначе следующая реплика потянет за собой мусор вроде «Мил!».
    сказано = strip_variant_prefix(reply.heard) if (ambiguous and reply.heard) else heard
    await add_reply(session, user_id=user.id, role=ROLE_USER, text_zh=сказано)

    audio_ogg = await synthesize_voice(reply.reply_zh, user, settings)

    row = await add_reply(
        session,
        user_id=user.id,
        role=ROLE_ASSISTANT,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
        correction=reply.correction,
        corrected_zh=reply.corrected_zh,
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
        услышано=сказано,
        ответ=reply.reply_zh,
    )

    return VoiceAnswer(
        audio_ogg=audio_ogg,
        text_zh=reply.reply_zh,
        pinyin=reply.pinyin,
        translation=reply.translation,
        correction=reply.correction,
        heard_text=сказано,
        elapsed_sec=elapsed,
        dialog_id=row.id,
    )


async def set_hsk_level(session: AsyncSession, user: User, level: str) -> None:
    user.hsk_level = level if level in HSK_DESCRIPTIONS else DEFAULT_HSK
    await session.flush()
    await track(session, "hsk_level_set", user_id=user.id, уровень=user.hsk_level)
    log.info("уровень HSK выбран", user_id=str(user.id), уровень=user.hsk_level)


def user_uuid(user: User) -> uuid.UUID:
    return user.id
