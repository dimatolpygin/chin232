"""Настройки юзера: уровень, голос, скорость речи, тема разговора.

Логика здесь, а не в хендлерах: тот же набор настроек потом откроет вебапп,
и второй раз описывать, что такое «медленно», никто не будет.

Всё, что тут меняется, лежит в `users` — обычными полями, не в redis. Настройка
должна пережить перезапуск бота, а не жить до вечера.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.events import track
from app.core.providers.base import Voice
from app.core.providers.registry import tts_voices
from app.core.services.dialog import DEFAULT_HSK, HSK_DESCRIPTIONS, TOPIC_FREE
from app.db.models import User
from app.logging import get_logger

log = get_logger("settings")


@dataclass(slots=True, frozen=True)
class Speed:
    """Темп речи. `value` — множитель поверх темпа, заданного уровнем HSK."""

    code: str
    value: float
    title: str


@dataclass(slots=True, frozen=True)
class Topic:
    """Тема разговора.

    `prompt` уходит в системный промпт как есть, поэтому это фраза для модели
    («еда, кафе, заказ блюд»), а не короткая надпись на кнопке.
    """

    code: str
    title: str
    prompt: str


# Шаг ±20%: на живой проверке 10% на слух не отличаются от обычного темпа, а
# половинная скорость превращает речь в растянутую кашу, по которой не понять
# ни тонов, ни границ слов.
SPEEDS: tuple[Speed, ...] = (
    Speed("slow", 0.8, "Медленно"),
    Speed("normal", 1.0, "Обычно"),
    Speed("fast", 1.2, "Быстро"),
)
DEFAULT_SPEED = SPEEDS[1]

TOPIC_FREE_CODE = "free"

TOPICS: tuple[Topic, ...] = (
    Topic("free", "Свободная", TOPIC_FREE),
    Topic("meet", "Знакомство", "знакомство: имя, страна, возраст, работа, увлечения"),
    Topic("food", "Еда", "еда и кафе: заказ блюд, вкусы, счёт"),
    Topic("road", "Дорога", "дорога и транспорт: метро, такси, как пройти, билеты"),
    Topic("work", "Работа", "работа: должность, коллеги, планы, встречи"),
    Topic("shop", "Магазин", "магазин и покупки: размер, цвет, цена, торг"),
    Topic("study", "Учёба", "учёба: университет, предметы, экзамены, языки"),
)


def current_level(user: User) -> str:
    return user.hsk_level if user.hsk_level in HSK_DESCRIPTIONS else DEFAULT_HSK


def voices(settings: Settings | None = None) -> tuple[Voice, ...]:
    return tts_voices(settings)


def current_voice(user: User, settings: Settings | None = None) -> Voice | None:
    """Выбранный голос. None — стоит голос сервиса по умолчанию.

    Сохранённый идентификатор принадлежит конкретному сервису озвучки. После
    смены сервиса он ничего не значит, и показывать его как выбранный нельзя:
    юзер увидел бы галочку у голоса, которым бот не говорит.
    """
    if not user.voice_id:
        return None
    for voice in voices(settings):
        if voice.id == user.voice_id:
            return voice
    return None


async def set_voice(
    session: AsyncSession, user: User, voice_id: str, settings: Settings | None = None
) -> Voice | None:
    """Выбрать голос. Чужой идентификатор игнорируем: подставлять его в сервис
    озвучки — это гарантированная ошибка на следующем же ответе."""
    chosen = next((v for v in voices(settings) if v.id == voice_id), None)
    if chosen is None:
        log.warning("голос не из списка", user_id=str(user.id), голос=voice_id)
        return None
    user.voice_id = chosen.id
    await session.flush()
    await track(session, "voice_set", user_id=user.id, голос=chosen.title)
    log.info("голос выбран", user_id=str(user.id), голос=chosen.title, id=chosen.id)
    return chosen


def current_speed(user: User) -> Speed:
    """Ближайший шаг к сохранённому множителю.

    В базе хранится число, а не код: скорость участвует в арифметике озвучки,
    и лишний перевод кода в множитель на каждом круге ничего не даёт. Обратно
    ищем ближайший — так настройка не потеряется, если шаги когда-то сдвинутся.
    """
    value = user.speech_speed or DEFAULT_SPEED.value
    return min(SPEEDS, key=lambda s: abs(s.value - value))


async def set_speed(session: AsyncSession, user: User, code: str) -> Speed:
    chosen = next((s for s in SPEEDS if s.code == code), DEFAULT_SPEED)
    user.speech_speed = chosen.value
    await session.flush()
    await track(session, "speed_set", user_id=user.id, темп=chosen.title)
    log.info(
        "скорость речи выбрана",
        user_id=str(user.id),
        темп=chosen.title,
        множитель=chosen.value,
    )
    return chosen


def current_topic(user: User) -> Topic:
    """Тема по сохранённой в базе фразе. Незнакомая — считаем свободной."""
    saved = (user.topic or "").strip()
    if not saved:
        return TOPICS[0]
    return next((t for t in TOPICS if t.prompt == saved), TOPICS[0])


async def set_topic(session: AsyncSession, user: User, code: str) -> Topic:
    """Сменить тему. Свободная стирает поле: промпт сам подставит своё слово."""
    chosen = next((t for t in TOPICS if t.code == code), TOPICS[0])
    user.topic = None if chosen.code == TOPIC_FREE_CODE else chosen.prompt
    await session.flush()
    await track(session, "topic_set", user_id=user.id, тема=chosen.title)
    log.info("тема разговора выбрана", user_id=str(user.id), тема=chosen.title)
    return chosen
