"""Озвучка через OpenAI TTS.

Второй провайдер озвучки не про красоту архитектуры: за проект дважды отваливались
внешние сервисы по антифроду, и переключение должно быть сменой переменной
окружения, а не правкой кода в пятницу вечером.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.core.providers.base import ProviderError, Speech, TTSProvider, call_logged

API_URL = "https://api.openai.com/v1/audio/speech"


class OpenAITTS(TTSProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ProviderError(self.name, "init", "не задан OPENAI_API_KEY")
        self._key = settings.openai_api_key
        self._model = settings.openai_tts_model
        self._default_voice = settings.openai_tts_voice
        self._timeout = settings.provider_timeout

    async def synthesize(self, text: str, voice_id: str | None, speed: float) -> Speech:
        voice = voice_id or self._default_voice
        async with call_logged(
            self.name, "tts", знаков=len(text), голос=voice, темп=speed
        ) as details:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": self._model,
                        "input": text,
                        "voice": voice,
                        # Сервис принимает темп в диапазоне 0.25–4.0.
                        "speed": max(0.25, min(4.0, speed)),
                        "response_format": "mp3",
                    },
                )
            details["http_код"] = response.status_code
            details["объём_ответа_байт"] = len(response.content)
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "tts",
                    "сервис озвучки вернул ошибку",
                    status_code=response.status_code,
                    body=response.text[:1000],
                )
            audio = response.content

        return Speech(audio=audio, fmt="mp3")
