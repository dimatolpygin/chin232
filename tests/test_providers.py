"""Разбор ответа модели и выбор провайдера по переменной окружения."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.providers.base import ProviderError
from app.core.providers.llm.openrouter import OpenRouterLLM
from app.core.providers.registry import get_tts

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}


def _llm() -> OpenRouterLLM:
    return OpenRouterLLM(Settings(openrouter_api_key="sk-or-v1-test", **BASE))  # type: ignore[arg-type]


def test_чистый_json_разбирается():
    reply = _llm()._parse(
        '{"reply_zh": "你好", "pinyin": "Nǐ hǎo", "translation": "Привет", "correction": null}'
    )
    assert reply.reply_zh == "你好"
    assert reply.pinyin == "Nǐ hǎo"
    assert reply.correction is None


def test_json_в_markdown_обёртке_разбирается():
    # Модели регулярно оборачивают ответ в ```json вопреки инструкции.
    reply = _llm()._parse('```json\n{"reply_zh": "你好", "pinyin": "Nǐ hǎo"}\n```')
    assert reply.reply_zh == "你好"
    assert reply.pinyin == "Nǐ hǎo"


def test_сломанный_json_не_теряет_ответ():
    # Иероглифы есть — значит круг можно докрутить, терять реплику нельзя.
    reply = _llm()._parse("你好，今天怎么样？")
    assert reply.reply_zh == "你好，今天怎么样？"
    assert reply.pinyin is None


def test_неизвестный_провайдер_падает_с_понятным_текстом():
    settings = Settings(tts_provider="неведомый", fish_api_key="x", **BASE)  # type: ignore[arg-type]
    with pytest.raises(ProviderError) as exc:
        get_tts(settings)
    assert "неизвестный провайдер" in str(exc.value)
    # В сообщении перечислены доступные — иначе непонятно, что писать в .env.
    assert "fish" in str(exc.value)


def test_провайдер_без_ключа_не_создаётся():
    # Ключ гасим явно: иначе Settings подхватит его из окружения контейнера.
    settings = Settings(tts_provider="fish", fish_api_key=None, **BASE)  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        get_tts(settings)
