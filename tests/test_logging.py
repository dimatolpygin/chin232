"""Секреты не должны попадать в логи никогда."""

from __future__ import annotations

from app.logging import mask_secrets


def test_значение_по_ключу_маскируется():
    result = mask_secrets(None, "info", {"bot_token": "123:AAreal_secret_value"})
    assert result["bot_token"] == "***"


def test_ключ_в_тексте_маскируется():
    result = mask_secrets(None, "info", {"event": "ответ sk-or-v1-abcdef0123456789 от сервиса"})
    assert "abcdef0123456789" not in result["event"]
    assert "sk-or***" in result["event"]


def test_токен_бота_в_тексте_маскируется():
    result = mask_secrets(
        None, "info", {"event": "url https://api.telegram.org/bot123456789:AAabcdefghij/send"}
    )
    assert "abcdefghij" not in result["event"]


def test_обычный_текст_не_трогаем():
    result = mask_secrets(None, "info", {"event": "входящее сообщение от @user"})
    assert result["event"] == "входящее сообщение от @user"
