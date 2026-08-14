"""Разбор ответа модели и выбор провайдера по переменной окружения."""

from __future__ import annotations

import asyncio

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


def _reply_from(content: str):
    """Весь путь ответа модели: сырой текст → словарь → LlmReply.

    Проверять только `_parse` мало: разбор словаря в реплику живёт в базовом
    классе, и ошибка ровно на этой границе не была бы видна ни одному тесту.
    """
    llm = _llm()
    data = llm._parse(content)

    async def fake(_prompt, _history):
        return data

    llm.complete_json = fake  # type: ignore[method-assign]
    return asyncio.run(llm.reply("промпт", []))


def test_чистый_json_разбирается():
    reply = _reply_from(
        '{"reply_zh": "你好", "pinyin": "Nǐ hǎo", "translation": "Привет", "correction": null}'
    )
    assert reply.reply_zh == "你好"
    assert reply.pinyin == "Nǐ hǎo"
    assert reply.correction is None


def test_json_в_markdown_обёртке_разбирается():
    # Модели регулярно оборачивают ответ в ```json вопреки инструкции.
    reply = _reply_from('```json\n{"reply_zh": "你好", "pinyin": "Nǐ hǎo"}\n```')
    assert reply.reply_zh == "你好"
    assert reply.pinyin == "Nǐ hǎo"


def test_сломанный_json_не_теряет_ответ():
    # Иероглифы есть — значит круг можно докрутить, терять реплику нельзя.
    reply = _reply_from("你好，今天怎么样？")
    assert reply.reply_zh == "你好，今天怎么样？"
    assert reply.pinyin is None


def test_ответ_не_словарём_не_роняет_круг():
    # Модель может вернуть валидный JSON-массив вместо объекта.
    assert _llm()._parse("[1, 2, 3]") == {"_raw": "[1, 2, 3]"}


def test_исправленная_фраза_доезжает_до_эталона():
    reply = _reply_from(
        '{"reply_zh": "你有三本书吗？", "correction": "Нужно счётное слово 本.",'
        ' "corrected_zh": "我有三本书"}'
    )
    assert reply.corrected_zh == "我有三本书"


def test_слово_null_в_исправленной_фразе_не_становится_эталоном():
    """Та же болезнь, что и у поправки: модель пишет отсутствие значения строкой.

    Проверено прогоном боевого промпта: на фразе без ошибок приходит
    corrected_zh: "null". Без отсева юзер получил бы эталон «повторите за мной:
    null» — и бот бы это ещё и озвучил.
    """
    reply = _reply_from('{"reply_zh": "你好", "corrected_zh": "null"}')
    assert reply.corrected_zh is None


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
