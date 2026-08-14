"""Отсев выдуманного распознавания.

Whisper на тишине и коротких шумных фрагментах не молчит, а выдаёт заготовку из
обучающих данных — чаще всего японские титры вроде «ご清聴ありがとうございました»
или английское «Thanks for watching». Бот отвечал бы на эту выдумку всерьёз,
и юзер получал бы реплики невпопад. Поэтому такой результат считаем «не расслышал».
"""

from __future__ import annotations

import re

from app.core.providers.base import Transcript
from app.logging import get_logger

log = get_logger("dialog")


class NotRecognized(RuntimeError):
    """Речь распознать не удалось. Это не сбой сервиса, а просьба повторить."""


# Языки, на которых с ботом вообще говорят. Всё остальное — почти наверняка выдумка.
ALLOWED_LANGUAGES = {"chinese", "russian", "english"}

# Вероятность «здесь нет речи» выше — считаем, что речи и не было.
NO_SPEECH_LIMIT = 0.6

# Устойчивые заготовки, которые сервис выдаёт на тишине.
HALLUCINATIONS = [
    re.compile(r"ご (?:清聴|視聴)", re.X),
    re.compile(r"thanks?\s+for\s+watching", re.I),
    re.compile(r"字幕(?:由|制作)"),
    re.compile(r"请不吝(?:点赞|订阅)"),
    re.compile(r"^\W*$"),  # одни знаки препинания
]


def try_recognize(transcript: Transcript) -> str | None:
    """Как ensure_recognized, но без исключения: не прошло — вернёт None."""
    try:
        return ensure_recognized(transcript)
    except NotRecognized:
        return None


VARIANT_PREFIX = re.compile(r"^\s*вариант\s*\d+\s*[:：]\s*", re.IGNORECASE)


def strip_variant_prefix(text: str) -> str:
    """Убрать «вариант N: » из выбора модели.

    Модель иногда возвращает выбранный вариант вместе с меткой, под которой он
    был предложен. В базу и в историю такая метка попасть не должна.
    """
    return VARIANT_PREFIX.sub("", text or "").strip()


def has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def ensure_recognized(transcript: Transcript) -> str:
    text = (transcript.text or "").strip()

    if not text:
        log.info("распознавание пустое", язык=transcript.language)
        raise NotRecognized("сервис вернул пустой текст")

    if transcript.language and transcript.language.lower() not in ALLOWED_LANGUAGES:
        log.info("распознан посторонний язык", язык=transcript.language, текст=text)
        raise NotRecognized(f"язык {transcript.language} в разговоре не ожидается")

    if transcript.no_speech_prob is not None and transcript.no_speech_prob > NO_SPEECH_LIMIT:
        log.info(
            "в записи, похоже, нет речи",
            вероятность_тишины=round(transcript.no_speech_prob, 3),
            текст=text,
        )
        raise NotRecognized("в записи не найдено речи")

    for pattern in HALLUCINATIONS:
        if pattern.search(text):
            log.info("распознана типовая выдумка сервиса", текст=text)
            raise NotRecognized("сервис выдал заготовку вместо речи")

    return text
