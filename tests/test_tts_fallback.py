"""Сбой основной озвучки не должен стоить пользователю реплики."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.providers.base import ProviderError, Speech, TTSProvider
from app.core.services import dialog as dialog_service
from app.db.models import User

BASE = {
    "bot_token": "123:AAtest",
    "database_url": "postgresql+asyncpg://u:p@localhost/db",
    "redis_url": "redis://localhost:6379/5",
}


class _Broken(TTSProvider):
    name = "fish"

    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech:
        raise ProviderError(self.name, "tts", "пусто", status_code=500, body="empty audio")


class _Working(TTSProvider):
    name = "openai"

    def __init__(self) -> None:
        self.вызван = False

    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech:
        self.вызван = True
        return Speech(audio=b"mp3-bytes", fmt="mp3")


def _user() -> User:
    user = User()
    user.hsk_level = "hsk12"
    user.speech_speed = 1.0
    user.voice_id = None
    return user


async def test_при_сбое_основной_озвучки_берётся_запасная(monkeypatch):
    working = _Working()
    monkeypatch.setattr(dialog_service, "get_tts", lambda s: _Broken())
    monkeypatch.setattr(dialog_service, "get_tts_by_name", lambda name, s: working)

    async def fake_convert(data: bytes, source_format: str = "mp3") -> bytes:
        return b"ogg-" + data

    monkeypatch.setattr("app.core.audio.to_voice_ogg", fake_convert)

    settings = Settings(tts_provider="fish", tts_fallback_provider="openai", **BASE)  # type: ignore[arg-type]
    result = await dialog_service._synthesize("你好", _user(), settings)

    assert working.вызван, "запасная озвучка не вызвана"
    assert result == b"ogg-mp3-bytes"


async def test_без_запасной_ошибка_пробрасывается(monkeypatch):
    monkeypatch.setattr(dialog_service, "get_tts", lambda s: _Broken())
    settings = Settings(tts_provider="fish", tts_fallback_provider=None, **BASE)  # type: ignore[arg-type]

    with pytest.raises(ProviderError):
        await dialog_service._synthesize("你好", _user(), settings)
