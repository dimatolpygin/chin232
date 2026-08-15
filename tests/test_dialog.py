"""Подстановка уровня HSK в промпт и темп озвучки."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.providers.base import LlmReply
from app.core.services import dialog
from app.core.services.dialog import HSK_DESCRIPTIONS, _speed, describe_level
from app.db.models import User


def _user(level: str | None, speed: float = 1.0) -> User:
    user = User()
    user.hsk_level = level
    user.speech_speed = speed
    return user


def test_уровень_подставляется_в_промпт():
    assert "HSK 1-2" in describe_level(_user("hsk12"))
    assert "HSK 5-6" in describe_level(_user("hsk56"))


def test_неизвестный_уровень_падает_на_начальный():
    assert describe_level(_user(None)) == HSK_DESCRIPTIONS["hsk12"]
    assert describe_level(_user("чепуха")) == HSK_DESCRIPTIONS["hsk12"]


def test_начинающим_говорим_медленнее():
    # На HSK 1-2 обычный темп неразборчив.
    assert _speed(_user("hsk12")) < _speed(_user("hsk56"))


def test_настройка_скорости_пользователя_умножается():
    assert _speed(_user("hsk56", speed=0.5)) == 0.5


def test_голос_fish_задан_по_умолчанию():
    """Регрессия: без reference_id Fish даёт новый случайный голос на каждый вызов.

    На живой проверке это выглядело как три разных собеседника подряд.
    """
    from app.config import Settings

    settings = Settings(
        bot_token="123:AAtest",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/5",
    )  # type: ignore[call-arg]
    assert settings.fish_voice_id, "голос Fish не задан — собеседник будет меняться каждую реплику"


@pytest.mark.asyncio
async def test_ответ_без_иероглифов_не_уходит_в_озвучку(monkeypatch):
    """Найдено живой проверкой: бот прислал голосовое со словом «null».

    Проверка стоит там, где ответ модели рождается: иначе пустая фраза уедет
    и в озвучку, и в подпись, и в базу — а оттуда её потом возьмут «Текст» и
    эталон произношения.
    """

    class Модель:
        async def reply(self, _prompt, _history):
            return LlmReply(reply_zh="", correction="Опечатка в слове.")

    async def промпт(_session, _code):
        return "система {hsk_level} {topic}"

    async def событие(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dialog, "get_prompt", промпт)
    monkeypatch.setattr(dialog, "get_llm", lambda _settings: Модель())
    monkeypatch.setattr(dialog, "track", событие)

    with pytest.raises(dialog.EmptyReply):
        await dialog._ask_llm(None, _user("hsk12"), "dialog_system", [], get_settings())


@pytest.mark.asyncio
async def test_нормальный_ответ_проходит_проверку(monkeypatch):
    class Модель:
        async def reply(self, _prompt, _history):
            return LlmReply(reply_zh="你好！")

    async def промпт(_session, _code):
        return "система {hsk_level} {topic}"

    monkeypatch.setattr(dialog, "get_prompt", промпт)
    monkeypatch.setattr(dialog, "get_llm", lambda _settings: Модель())

    reply = await dialog._ask_llm(None, _user("hsk12"), "dialog_system", [], get_settings())
    assert reply.reply_zh == "你好！"
