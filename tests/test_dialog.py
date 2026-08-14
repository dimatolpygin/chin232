"""Подстановка уровня HSK в промпт и темп озвучки."""

from __future__ import annotations

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
