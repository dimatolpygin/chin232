"""Выбор провайдеров по переменным окружения.

Смена провайдера — настройка, а не правка кода: добавить реализацию в таблицу
ниже и переключить переменную.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings, get_settings
from app.core.providers.base import (
    LLMProvider,
    PaymentProvider,
    PronunciationProvider,
    ProviderError,
    STTProvider,
    TTSProvider,
    Voice,
)
from app.core.providers.llm.openrouter import OpenRouterLLM
from app.core.providers.payments.lavatop import LavaTopPayments
from app.core.providers.pronunciation.speechsuper import SpeechSuperPronunciation
from app.core.providers.stt.openai_whisper import OpenAIWhisperSTT
from app.core.providers.tts.fish import FishTTS
from app.core.providers.tts.openai_tts import OpenAITTS
from app.logging import get_logger

log = get_logger("providers")

STT_PROVIDERS: dict[str, Callable[[Settings], STTProvider]] = {
    "openai_whisper": OpenAIWhisperSTT,
}
LLM_PROVIDERS: dict[str, Callable[[Settings], LLMProvider]] = {
    "openrouter": OpenRouterLLM,
}
TTS_PROVIDERS: dict[str, Callable[[Settings], TTSProvider]] = {
    "fish": FishTTS,
    "openai": OpenAITTS,
}
PRONUNCIATION_PROVIDERS: dict[str, Callable[[Settings], PronunciationProvider]] = {
    "speechsuper": SpeechSuperPronunciation,
}
PAYMENT_PROVIDERS: dict[str, Callable[[Settings], PaymentProvider]] = {
    "lavatop": LavaTopPayments,
}


# Экземпляры провайдеров живут на процесс: они держат keep-alive соединение,
# а пересоздание на каждый круг — лишнее TLS-рукопожатие в бюджете 12 секунд.
_instances: dict[tuple[str, str], object] = {}
_TABLES = {
    "stt": STT_PROVIDERS,
    "llm": LLM_PROVIDERS,
    "tts": TTS_PROVIDERS,
    "pronunciation": PRONUNCIATION_PROVIDERS,
    "payments": PAYMENT_PROVIDERS,
}


def _process_settings() -> Settings | None:
    try:
        return get_settings()
    except SystemExit:  # конфигурация неполна — кэшировать нечего
        return None


def _cached(kind: str, name: str, settings: Settings):
    # Кэшируем только процессные настройки. Со своим объектом Settings (тесты,
    # разовые проверки) провайдер собирается заново, иначе кэш отдал бы чужой.
    if settings is not _process_settings():
        return _build(kind, _TABLES[kind], name, settings)
    key = (kind, name)
    if key not in _instances:
        _instances[key] = _build(kind, _TABLES[kind], name, settings)
    return _instances[key]


def _build(kind: str, table: dict, name: str, settings: Settings):
    factory = table.get(name)
    if factory is None:
        raise ProviderError(
            name, "init", f"неизвестный провайдер {kind}: {name}. Доступны: {', '.join(table)}"
        )
    provider = factory(settings)
    log.info("провайдер выбран", вид=kind, провайдер=name)
    return provider


def get_stt(settings: Settings | None = None) -> STTProvider:
    settings = settings or get_settings()
    return _cached("stt", settings.stt_provider, settings)


def get_llm(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    return _cached("llm", settings.llm_provider, settings)


def get_tts(settings: Settings | None = None) -> TTSProvider:
    settings = settings or get_settings()
    return _cached("tts", settings.tts_provider, settings)


def get_pronunciation(settings: Settings | None = None) -> PronunciationProvider:
    settings = settings or get_settings()
    return _cached("pronunciation", settings.pronunciation_provider, settings)


def get_payments(settings: Settings | None = None) -> PaymentProvider:
    settings = settings or get_settings()
    return _cached("payments", settings.payment_provider, settings)


def get_tts_by_name(name: str, settings: Settings | None = None) -> TTSProvider:
    """Озвучка по имени — для фолбэка, когда основная сбойнула."""
    settings = settings or get_settings()
    return _cached("tts", name, settings)


def tts_voices(settings: Settings | None = None) -> tuple[Voice, ...]:
    """Голоса действующего сервиса озвучки.

    Берём у класса, а не у экземпляра: список нужен, чтобы нарисовать раздел
    настроек, и требовать ради этого живой ключ провайдера незачем.
    """
    settings = settings or get_settings()
    factory = TTS_PROVIDERS.get(settings.tts_provider)
    return getattr(factory, "VOICES", ()) if factory is not None else ()
