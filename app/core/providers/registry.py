"""Выбор провайдеров по переменным окружения.

Смена провайдера — настройка, а не правка кода: добавить реализацию в таблицу
ниже и переключить переменную.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings, get_settings
from app.core.providers.base import LLMProvider, ProviderError, STTProvider, TTSProvider
from app.core.providers.llm.openrouter import OpenRouterLLM
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
    return _build("stt", STT_PROVIDERS, settings.stt_provider, settings)


def get_llm(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    return _build("llm", LLM_PROVIDERS, settings.llm_provider, settings)


def get_tts(settings: Settings | None = None) -> TTSProvider:
    settings = settings or get_settings()
    return _build("tts", TTS_PROVIDERS, settings.tts_provider, settings)
