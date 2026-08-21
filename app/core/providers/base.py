"""Интерфейсы внешних сервисов и общее логирование их вызовов.

За проект уже дважды отваливались сервисы по антифроду (Azure, потом Alibaba),
поэтому провайдер выбирается переменной окружения, а код круга не знает, кто
именно за интерфейсом.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

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
    # Исправленная фраза самого юзера иероглифами: эталон для «повтори за мной».
    corrected_zh: str | None = None
    # Что модель приняла за реплику юзера. Заполняется, когда распознавание
    # дало два варианта и выбор делала она.
    heard: str | None = None


@dataclass(slots=True)
class Speech:
    audio: bytes
    fmt: str


# Ниже этого балла тон считается сбитым. Сервис ставит верному тону около сотни,
# а промаху — единицы, так что порог посередине ничего не режет по живому.
TONE_OK_SCORE = 60


@dataclass(slots=True)
class CharScore:
    """Оценка одного иероглифа. Тона — главное, ради чего всё затевалось."""

    char: str
    overall: int | None = None
    tone: int | None = None
    # Пиньинь со знаками тонов от самого сервиса: он считает сандхи по фразе.
    pinyin: str | None = None
    # Какой тон полагался по эталону и какой сервис услышал. Второе поле есть
    # не во всех тарифах: на обычном приходит только балл тона.
    tone_expected: int | None = None
    tone_actual: int | None = None

    @property
    def tone_ok(self) -> bool | None:
        if self.tone_expected is not None and self.tone_actual is not None:
            return self.tone_expected == self.tone_actual
        # Услышанного тона нет — судим по баллу: он и падает, когда тон сбит.
        if self.tone is not None:
            return self.tone >= TONE_OK_SCORE
        return None


@dataclass(slots=True)
class Pronunciation:
    """Разбор произношения одной фразы."""

    overall: int | None = None
    pronunciation: int | None = None
    tone: int | None = None
    fluency: int | None = None
    integrity: int | None = None
    chars: list[CharScore] = field(default_factory=list)
    # Полный ответ сервиса. Ложится в pronunciation_checks.detail: переспросить
    # по той же записи потом уже не выйдет.
    raw: dict[str, object] = field(default_factory=dict)


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
            # Через тот же отсев, что и остальные поля: модель пишет отсутствие
            # ответа словом «null», и на живой проверке бот озвучил его вслух.
            reply_zh=_text_or_none(data.get("reply_zh")) or "",
            pinyin=_text_or_none(data.get("pinyin")),
            translation=_text_or_none(data.get("translation")),
            correction=_text_or_none(data.get("correction")),
            corrected_zh=_text_or_none(data.get("corrected_zh")),
            heard=_text_or_none(data.get("heard")),
        )


# Модель регулярно пишет отсутствие значения словом, а не JSON-значением null.
# На живой проверке это дошло до юзера сообщением «Небольшая поправка: null».
EMPTY_WORDS = {"null", "none", "nil", "n/a", "-", "—", "нет", "нет ошибок"}


def _text_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    if text.lower().strip(".") in EMPTY_WORDS:
        return None
    return text or None


class TTSProvider(ABC):
    """Текст ответа в голос."""

    name: str

    @abstractmethod
    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech: ...


class SpeechUnclear(RuntimeError):
    """Сервис принял запись, но разобрать её не смог: тишина, шум, не та речь.

    Это не авария, а просьба перезаписать: юзеру нужен другой текст, чем при
    сбое сервиса, и в логах это не должно выглядеть падением.
    """


class PronunciationProvider(ABC):
    """Единственный в цепочке, кто слышит ЗВУК, а не текст."""

    name: str

    @abstractmethod
    async def assess(self, audio_wav: bytes, ref_text: str, user_id: str) -> Pronunciation:
        """Оценить запись по эталонной фразе. Звук — WAV 16 кГц моно 16 бит."""


# --- платежи ------------------------------------------------------------------


@dataclass(slots=True)
class Invoice:
    """Выставленный счёт: по этой ссылке юзер платит."""

    external_id: str
    payment_url: str | None
    amount: float | None = None
    currency: str | None = None
    status: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


# Что случилось с деньгами. Названия свои, а не как у конкретной платёжки:
# провайдер меняется настройкой, и биллинг не должен знать его словарь.
EVENT_PAID = "paid"
EVENT_FAILED = "failed"
EVENT_CANCELLED = "cancelled"
# Деньги ушли обратно: возврат по просьбе покупателя или спор с банком.
# Доступ по такому платежу закрывается сразу, иначе человек пользуется
# бесплатно, а заказчик ещё и платит комиссию за спор.
EVENT_REFUNDED = "refunded"
EVENT_UNKNOWN = "unknown"


@dataclass(slots=True)
class PaymentEvent:
    """Разобранный вебхук платёжки."""

    kind: str
    external_id: str
    # Контракт первой покупки, если это продление.
    parent_external_id: str | None = None
    email: str | None = None
    amount: float | None = None
    currency: str | None = None
    recurring: bool = False
    # Докуда продлена подписка по версии самой платёжки (событие отмены).
    expires_at: str | None = None
    error: str | None = None
    raw: dict[str, object] = field(default_factory=dict)


class PaymentProvider(ABC):
    """Приём денег. Провайдер меняется настройкой: за проект уже дважды
    отваливались внешние сервисы, и платёжка — последнее, что можно чинить
    переписыванием."""

    name: str

    @abstractmethod
    async def create_invoice(
        self,
        offer_id: str,
        email: str,
        currency: str,
        periodicity: str | None = None,
        **extra: object,
    ) -> Invoice: ...

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Наш ли это вебхук. Чужой запрос не должен доехать до биллинга."""

    @abstractmethod
    def parse_webhook(self, payload: dict[str, object]) -> PaymentEvent: ...
