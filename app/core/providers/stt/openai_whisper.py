"""STT через OpenAI Whisper."""

from __future__ import annotations

import httpx

from app.config import Settings
from app.core.providers.base import ProviderError, STTProvider, Transcript, call_logged

API_URL = "https://api.openai.com/v1/audio/transcriptions"


class OpenAIWhisperSTT(STTProvider):
    name = "openai_whisper"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ProviderError(self.name, "init", "не задан OPENAI_API_KEY")
        self._key = settings.openai_api_key
        self._model = settings.whisper_model
        self._timeout = settings.provider_timeout

    async def transcribe(self, audio: bytes, filename: str) -> Transcript:
        async with call_logged(self.name, "transcribe", объём_запроса_байт=len(audio)) as details:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self._key}"},
                    files={"file": (filename, audio, "audio/ogg")},
                    data={
                        "model": self._model,
                        # Китайский и русский в одном диалоге: язык не фиксируем,
                        # пусть определяет сам. Но подсказкой сдвигаем в нужную
                        # сторону — без неё на тихих фрагментах сервис уходит
                        # в японский и выдумывает текст.
                        "prompt": "Это разговорная запись на китайском или русском языке.",
                        "temperature": "0",
                        "response_format": "verbose_json",
                    },
                )
            details["http_код"] = response.status_code
            details["объём_ответа_байт"] = len(response.content)
            if response.status_code >= 400:
                raise ProviderError(
                    self.name,
                    "transcribe",
                    "сервис распознавания вернул ошибку",
                    status_code=response.status_code,
                    body=response.text,
                )
            payload = response.json()

        text = (payload.get("text") or "").strip()
        segments = payload.get("segments") or []
        return Transcript(
            text=text,
            language=payload.get("language"),
            duration_sec=payload.get("duration"),
            no_speech_prob=max((s.get("no_speech_prob", 0.0) for s in segments), default=None),
            avg_logprob=min((s.get("avg_logprob", 0.0) for s in segments), default=None),
        )
