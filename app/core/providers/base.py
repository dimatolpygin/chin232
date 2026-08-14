"""Интерфейсы внешних сервисов и общее логирование их вызовов.

За проект уже дважды отваливались сервисы по антифроду (Azure, потом Alibaba),
поэтому провайдер выбирается переменной окружения, а код круга не знает, кто
именно за интерфейсом.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.logging import get_logger

log = get_logger("providers")


class ProviderError(RuntimeError):
    """Сбой внешнего сервиса. Тело ответа сохраняем: без него отладка слепая."""

    def __init__(
        self,
        provider: str,
        operation: str,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(f"{provider}/{operation}: {message}")
        self.provider = provider
        self.operation = operation
        self.status_code = status_code
        self.body = body


@asynccontextmanager
async def call_logged(provider: str, operation: str, **extra: object):
    """Логирует вызов внешнего сервиса: длительность, объём, http-код.

    Внутрь передаётся словарь, куда реализация складывает подробности —
    иначе о размере ответа и http-коде знал бы только сам провайдер.
    """
    details: dict[str, object] = {}
    started = time.monotonic()
    log.info("вызов внешнего сервиса", провайдер=provider, операция=operation, **extra)
    try:
        yield details
    except ProviderError as exc:
        # Ключи из details подставляются через словарь, а не как **kwargs рядом
        # с явными: реализация уже могла положить туда http_код, и коллизия
        # роняла сам логгер — то есть при сбое провайдера в лог не попадало
        # ничего, включая тело ответа, ради которого всё и затевалось.
        fields = {
            "провайдер": provider,
            "операция": operation,
            "длительность_мс": round((time.monotonic() - started) * 1000),
            **details,
            "http_код": exc.status_code,
            "тело_ответа": (exc.body or "")[:1000],
        }
        log.error("внешний сервис ответил ошибкой", **fields)
        raise
    except Exception as exc:
        fields = {
            "провайдер": provider,
            "операция": operation,
            "длительность_мс": round((time.monotonic() - started) * 1000),
            **details,
            "ошибка": repr(exc),
        }
        log.exception("вызов внешнего сервиса упал", **fields)
        raise
    else:
        log.info(
            "внешний сервис ответил",
            провайдер=provider,
            операция=operation,
            длительность_мс=round((time.monotonic() - started) * 1000),
            **details,
        )


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None = None
    duration_sec: float | None = None
    # Признаки того, что сервис распознал тишину и придумал текст.
    no_speech_prob: float | None = None
    avg_logprob: float | None = None


@dataclass(slots=True)
class LlmReply:
    reply_zh: str
    pinyin: str | None = None
    translation: str | None = None
    correction: str | None = None
    # Что модель приняла за реплику юзера. Заполняется, когда распознавание
    # дало два варианта и выбор делала она.
    heard: str | None = None


@dataclass(slots=True)
class Speech:
    audio: bytes
    fmt: str


class STTProvider(ABC):
    """Голос пользователя в текст."""

    name: str

    @abstractmethod
    async def transcribe(
        self, audio: bytes, filename: str, language: str | None = None
    ) -> Transcript: ...


class LLMProvider(ABC):
    """Смысл, диалог, грамматика, перевод. Звук не слышит вообще."""

    name: str

    @abstractmethod
    async def complete_json(
        self, system_prompt: str, history: list[dict[str, str]]
    ) -> dict[str, object]:
        """Сырой JSON-ответ модели.

        Общий низ для всех промптов: круг, подсказки, дальше разбор произношения.
        Без него каждая новая кнопка добавляла бы метод в интерфейс, а его
        пришлось бы реализовывать во всех провайдерах — при том что отличается
        только промпт. Если модель не выдержала формат, содержимое приходит
        целиком в ключе `_raw`.
        """

    async def reply(self, system_prompt: str, history: list[dict[str, str]]) -> LlmReply:
        data = await self.complete_json(system_prompt, history)
        raw = data.get("_raw")
        if raw is not None:
            # Формат сломан, но иероглифы в ответе есть — круг докручиваем.
            return LlmReply(reply_zh=str(raw))
        return LlmReply(
            reply_zh=str(data.get("reply_zh") or "").strip(),
            pinyin=_text_or_none(data.get("pinyin")),
            translation=_text_or_none(data.get("translation")),
            correction=_text_or_none(data.get("correction")),
            heard=_text_or_none(data.get("heard")),
        )


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class TTSProvider(ABC):
    """Текст ответа в голос."""

    name: str

    @abstractmethod
    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech: ...
