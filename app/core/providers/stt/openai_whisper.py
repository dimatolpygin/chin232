"""STT через OpenAI Whisper."""

from __future__ import annotations

from app.config import Settings
from app.core.providers.base import ProviderError, STTProvider, Transcript, call_logged
from app.core.providers.http import get_client

API_URL = "https://api.openai.com/v1/audio/transcriptions"

# Иероглифы в подсказке обязательны — см. комментарий в transcribe.
PROMPT_HINT = "你好，我们在练习中文口语。Привет, мы практикуем разговорный китайский."


class OpenAIWhisperSTT(STTProvider):
    name = "openai_whisper"

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ProviderError(self.name, "init", "не задан OPENAI_API_KEY")
        self._key = settings.openai_api_key
        self._model = settings.whisper_model
        self._timeout = settings.provider_timeout

    def _request_data(self, language: str | None) -> dict[str, str]:
        """Тело запроса отдельным методом, чтобы тест проверял то, что реально уходит.

        Раньше подсказка жила литералом внутри вызова, а тест сверялся с
        константой рядом — константу поправили, литерал остался прежним, и
        зелёный тест прикрывал мёртвый фикс.
        """
        data = {
            "model": self._model,
            # Подсказка обязана быть ДВУЯЗЫЧНОЙ: на одном русском она уводит
            # распознавание в кириллицу, и 你好 приходит как «Ни хао».
            "prompt": PROMPT_HINT,
            "temperature": "0",
            "response_format": "verbose_json",
        }
        # Язык не фиксируем по умолчанию — в диалоге он меняется от реплики к
        # реплике. Принудительный передаётся, когда круг проверяет китайскую
        # догадку вторым проходом.
        if language:
            data["language"] = language
        return data

    async def transcribe(
        self, audio: bytes, filename: str, language: str | None = None
    ) -> Transcript:
        async with call_logged(
            self.name, "transcribe", объём_запроса_байт=len(audio), язык=language or "авто"
        ) as details:
            client = get_client()
            response = await client.post(
                API_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (filename, audio, "audio/ogg")},
                data=self._request_data(language),
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
            # Считают нам минуты звука, а не байты запроса: без этого числа
            # расход по распознаванию в админке не посчитать.
            details["секунд"] = payload.get("duration")

        text = (payload.get("text") or "").strip()
        segments = payload.get("segments") or []
        return Transcript(
            text=text,
            language=payload.get("language"),
            duration_sec=payload.get("duration"),
            no_speech_prob=max((s.get("no_speech_prob", 0.0) for s in segments), default=None),
            avg_logprob=min((s.get("avg_logprob", 0.0) for s in segments), default=None),
        )
